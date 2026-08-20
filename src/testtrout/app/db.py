"""SQLite storage for the application.

Chosen so that ``trout up`` works with nothing installed: no daemon, no
container, no port to claim. For one developer and a handful of repositories
that is not a compromise, it is the correct amount of machinery.

Two settings do the heavy lifting. **WAL mode** lets the worker write while the
web request handling reads, which is the entire concurrency requirement here.
**A busy timeout** turns the one remaining contention case into a short wait
rather than an immediate ``database is locked``.

Schema changes are applied as an ordered list of migrations rather than a
`CREATE TABLE IF NOT EXISTS` soup, so an existing database on a developer's
machine upgrades predictably instead of silently diverging.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Applied in order, each exactly once. Never edit a migration that has shipped;
# append a new one. An edited migration silently skips on machines that already
# ran it, which is the worst kind of drift to debug.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE repos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        path            TEXT    NOT NULL UNIQUE,
        source          TEXT    NOT NULL DEFAULT 'local',
        remote          TEXT,
        default_branch  TEXT,
        framework       TEXT,
        backend         TEXT,
        created_at      TEXT    NOT NULL,
        last_scanned_at TEXT,
        last_run_at     TEXT,
        last_run_status TEXT
    );

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
        kind        TEXT    NOT NULL,
        state       TEXT    NOT NULL DEFAULT 'queued',
        payload     TEXT    NOT NULL DEFAULT '{}',
        result      TEXT,
        error       TEXT,
        log         TEXT    NOT NULL DEFAULT '[]',
        created_at  TEXT    NOT NULL,
        started_at  TEXT,
        finished_at TEXT
    );
    -- The worker claims by (state, id); the UI lists by repo and recency.
    CREATE INDEX idx_jobs_claim ON jobs(state, id);
    CREATE INDEX idx_jobs_repo  ON jobs(repo_id, id DESC);

    CREATE TABLE runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id          INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
        run_id           TEXT    NOT NULL,
        status           TEXT    NOT NULL,
        entrypoint       TEXT    NOT NULL DEFAULT '',
        passed           INTEGER NOT NULL DEFAULT 0,
        failed           INTEGER NOT NULL DEFAULT 0,
        inconclusive     INTEGER NOT NULL DEFAULT 0,
        duration_seconds REAL    NOT NULL DEFAULT 0,
        started_at       TEXT    NOT NULL,
        UNIQUE(repo_id, run_id)
    );
    CREATE INDEX idx_runs_repo ON runs(repo_id, id DESC);
    """,
)


def default_database_path() -> Path:
    """Where the database lives.

    User-level rather than per-repository, because the whole point is to span
    repositories. Honours ``XDG_DATA_HOME`` where it is set.
    """
    import os

    base = os.environ.get("TROUT_HOME")
    if base:
        return Path(base).expanduser() / "testtrout.db"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "testtrout" / "testtrout.db"
    return Path.home() / ".testtrout" / "testtrout.db"


class Database:
    """A SQLite database with a connection per thread.

    SQLite connections are not safe to share across threads, and the worker
    runs on its own. Rather than serialise everything through one connection,
    each thread gets its own — which is also what makes WAL mode worth having.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_database_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        existing: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if existing is not None:
            return existing

        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=NORMAL")
        self._local.connection = connection
        return connection

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A transaction that takes the write lock immediately.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: claiming a
        job is read-then-write, and a deferred transaction would let two
        workers both read the same queued row before either writes.
        """
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def migrate(self) -> None:
        """Apply any migrations this database has not seen."""
        connection = self.connection
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = connection.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            connection.execute("INSERT INTO schema_version (version) VALUES (0)")
            applied = 0
        else:
            applied = int(row["version"])

        for index, statement in enumerate(MIGRATIONS[applied:], start=applied + 1):
            connection.executescript(statement)
            connection.execute("UPDATE schema_version SET version = ?", (index,))

    def close(self) -> None:
        """Close this thread's connection."""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None


def dumps(value: Any) -> str:
    """Serialise a JSON column."""
    return json.dumps(value, default=str)


def loads(raw: str | None, fallback: Any) -> Any:
    """Deserialise a JSON column, tolerating a corrupt value."""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback
