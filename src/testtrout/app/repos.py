"""The repository registry.

Records where linked repositories are. Never what is inside them: the scan,
scenarios, and generated tests all live in each repository's own ``.trout/``
directory, so unlinking costs nothing but run history, and a repository stays
fully usable through the CLI whether or not the app knows about it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from testtrout.app.db import Database
from testtrout.app.models import RepoRecord, RepoSource, RunSummary, now
from testtrout.domain.run import RunRecord
from testtrout.store import QaPaths


class RepoError(RuntimeError):
    """A repository could not be linked, with a message meant for a person."""


class RepoRegistry:
    """Linked repositories and their run history."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------- linking

    def link_local(self, path: Path, name: str | None = None) -> RepoRecord:
        """Link a directory already on this machine.

        The directory is never modified by linking — resolving the project root
        uses the same bounded walk the CLI does, so a path inside a repository
        links the repository rather than the subdirectory.
        """
        resolved = QaPaths.find(path.expanduser().resolve()).root
        if not resolved.is_dir():
            raise RepoError(f"{resolved} is not a directory")

        existing = self.by_path(resolved)
        if existing is not None:
            return existing

        return self._insert(
            RepoRecord(
                name=name or resolved.name,
                path=str(resolved),
                source=RepoSource.LOCAL,
                default_branch=_current_branch(resolved),
            )
        )

    def link_github(self, slug: str, token: str, into: Path | None = None) -> RepoRecord:
        """Clone a GitHub repository and link it."""
        from testtrout.app import github

        remote = github.get_repo(token, slug)
        destination = (into or default_clone_root()) / remote.full_name.replace("/", "__")

        existing = self.by_path(destination)
        if existing is not None:
            return existing

        github.clone(remote, token, destination)
        return self._insert(
            RepoRecord(
                name=remote.full_name.split("/")[-1],
                path=str(destination),
                source=RepoSource.GITHUB,
                remote=remote.full_name,
                default_branch=remote.default_branch,
            )
        )

    def unlink(self, repo_id: int, delete_files: bool = False) -> bool:
        """Forget a repository.

        Files are left alone unless explicitly asked for, and even then only
        for repositories TestTrout cloned itself. Deleting a directory the
        developer already had is not a decision this tool gets to make.
        """
        record = self.get(repo_id)
        if record is None:
            return False

        if delete_files:
            if record.source is not RepoSource.GITHUB:
                raise RepoError(
                    "refusing to delete a directory TestTrout did not create — "
                    "unlink without deleting, then remove it yourself"
                )
            import shutil

            shutil.rmtree(record.root, ignore_errors=True)

        with self.database.write() as connection:
            connection.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        return True

    def queue_initial_scan(self, record: RepoRecord) -> bool:
        """Scan a freshly linked repository.

        Lives here rather than in each caller so that linking from the CLI and
        linking from the interface leave identical state. They drifted once, and
        the symptom was a settings page confidently reporting "0 policies" for a
        repository nobody had scanned.
        """
        from testtrout.app.queue import JobQueue

        if record.id is None or not record.exists:
            return False
        # Always scan, even when a scan file already exists. Skipping to save
        # work meant a result written by an older build — one that could not yet
        # read this repository's layout — silently shadowed a fixed scanner, and
        # the interface faithfully displayed its zeros.
        JobQueue(self.database).enqueue(record.id, "understand")
        return True

    # ------------------------------------------------------------ querying

    def all(self) -> list[RepoRecord]:
        """Every linked repository, most recently active first."""
        rows = self.database.connection.execute(
            "SELECT * FROM repos ORDER BY COALESCE(last_run_at, last_scanned_at, created_at) DESC"
        ).fetchall()
        return [_to_repo(row) for row in rows]

    def get(self, repo_id: int) -> RepoRecord | None:
        """One repository by id."""
        row = self.database.connection.execute(
            "SELECT * FROM repos WHERE id = ?", (repo_id,)
        ).fetchone()
        return _to_repo(row) if row else None

    def by_path(self, path: Path) -> RepoRecord | None:
        """One repository by filesystem path."""
        row = self.database.connection.execute(
            "SELECT * FROM repos WHERE path = ?", (str(path),)
        ).fetchone()
        return _to_repo(row) if row else None

    def paths(self, repo_id: int) -> QaPaths:
        """The ``.trout/`` layout for a repository.

        Raises:
            RepoError: if the repository is unknown, or its directory has been
                moved or removed since it was linked.
        """
        record = self.get(repo_id)
        if record is None:
            raise RepoError(f"no linked repository with id {repo_id}")
        if not record.exists:
            raise RepoError(
                f"{record.name} was linked at {record.path}, which no longer exists. "
                "Unlink it, or move it back."
            )
        return QaPaths(root=record.root)

    # ------------------------------------------------------------ updating

    def record_scan(self, repo_id: int, framework: str | None, backend: str | None) -> None:
        """Note what a scan discovered, for the repository list."""
        with self.database.write() as connection:
            connection.execute(
                "UPDATE repos SET framework = ?, backend = ?, last_scanned_at = ? WHERE id = ?",
                (framework, backend, now(), repo_id),
            )

    def record_run(self, repo_id: int, record: RunRecord) -> RunSummary:
        """Store a run's headline result.

        A summary only. The full record with evidence stays in the repository,
        where it is large and where it belongs; what is kept here is the small
        queryable part that answers questions across time.
        """
        counts = record.counts
        summary = RunSummary(
            repo_id=repo_id,
            run_id=record.id,
            status=record.status.value,
            entrypoint=record.entrypoint,
            passed=counts.get("passed", 0),
            failed=counts.get("assertion_failure", 0),
            inconclusive=sum(
                value
                for key, value in counts.items()
                if key
                in {"inconclusive", "auth_failure", "environment_failure", "dependency_failure"}
            ),
            duration_seconds=record.duration_seconds,
            started_at=record.started_at,
        )
        with self.database.write() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs (repo_id, run_id, status, entrypoint, passed, "
                "failed, inconclusive, duration_seconds, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    repo_id,
                    summary.run_id,
                    summary.status,
                    summary.entrypoint,
                    summary.passed,
                    summary.failed,
                    summary.inconclusive,
                    summary.duration_seconds,
                    summary.started_at,
                ),
            )
            connection.execute(
                "UPDATE repos SET last_run_at = ?, last_run_status = ? WHERE id = ?",
                (summary.started_at, summary.status, repo_id),
            )
        return summary

    def run_history(self, repo_id: int, limit: int = 50) -> list[RunSummary]:
        """Recent runs for a repository, newest first."""
        rows = self.database.connection.execute(
            "SELECT * FROM runs WHERE repo_id = ? ORDER BY id DESC LIMIT ?", (repo_id, limit)
        ).fetchall()
        return [
            RunSummary(
                id=row["id"],
                repo_id=row["repo_id"],
                run_id=row["run_id"],
                status=row["status"],
                entrypoint=row["entrypoint"],
                passed=row["passed"],
                failed=row["failed"],
                inconclusive=row["inconclusive"],
                duration_seconds=row["duration_seconds"],
                started_at=row["started_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------- private

    def _insert(self, record: RepoRecord) -> RepoRecord:
        with self.database.write() as connection:
            cursor = connection.execute(
                "INSERT INTO repos (name, path, source, remote, default_branch, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    record.name,
                    record.path,
                    record.source.value,
                    record.remote,
                    record.default_branch,
                    record.created_at,
                ),
            )
            repo_id = int(cursor.lastrowid or 0)
        found = self.get(repo_id)
        assert found is not None
        return found


def default_clone_root() -> Path:
    """Where GitHub repositories are cloned to."""
    from testtrout.app.db import default_database_path

    return default_database_path().parent / "repos"


def _current_branch(root: Path) -> str | None:
    """The checked-out branch, if this is a git repository."""
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _to_repo(row: Any) -> RepoRecord:
    """Build a :class:`RepoRecord` from a database row."""
    return RepoRecord(
        id=row["id"],
        name=row["name"],
        path=row["path"],
        source=RepoSource(row["source"]),
        remote=row["remote"],
        default_branch=row["default_branch"],
        framework=row["framework"],
        backend=row["backend"],
        created_at=row["created_at"],
        last_scanned_at=row["last_scanned_at"],
        last_run_at=row["last_run_at"],
        last_run_status=row["last_run_status"],
    )
