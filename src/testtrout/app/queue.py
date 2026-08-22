"""A job queue backed by the same SQLite database.

No Redis, no broker. Claiming a job is one ``UPDATE ... WHERE state='queued'``
inside an immediate transaction, which is atomic and therefore safe for as many
workers as a laptop will ever run.

One rule shapes the claim query: **jobs are serialised per repository.** Two
runs against the same database interfere in ways neither can explain, and the
same is true of a scan racing a generate over one working tree. Different
repositories proceed in parallel, which is the concurrency that actually
matters when you have several linked.
"""

from __future__ import annotations

from typing import Any

from testtrout.app.db import Database, dumps, loads
from testtrout.app.models import JobRecord, JobState, now

# Kinds the worker knows how to execute. Anything else is rejected at enqueue
# rather than failing later on a worker thread with no useful context.
KINDS: frozenset[str] = frozenset({"understand", "build", "run"})

# Jobs that survived a crash. On startup they are failed rather than retried:
# a half-finished run may have left state behind, and silently repeating it is
# how you get two concurrent writes to one database.
STALE_MESSAGE = "interrupted — the worker stopped before this finished"


class UnknownJobKindError(ValueError):
    """Raised when a job kind has no worker handler."""


class JobQueue:
    """Enqueue, claim, and complete background work."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------ producing

    def enqueue(self, repo_id: int, kind: str, payload: dict[str, Any] | None = None) -> JobRecord:
        """Add a job.

        Raises:
            UnknownJobKindError: if no handler exists. Failing here means the
                caller finds out at the API boundary with a usable message,
                rather than discovering it in a worker log.
        """
        if kind not in KINDS:
            raise UnknownJobKindError(
                f"unknown job kind {kind!r}; expected one of {', '.join(sorted(KINDS))}"
            )
        with self.database.write() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs (repo_id, kind, state, payload, log, created_at) "
                "VALUES (?, ?, ?, ?, '[]', ?)",
                (repo_id, kind, JobState.QUEUED.value, dumps(payload or {}), now()),
            )
            job_id = int(cursor.lastrowid or 0)
        found = self.get(job_id)
        assert found is not None
        return found

    # ------------------------------------------------------------ consuming

    def claim(self) -> JobRecord | None:
        """Take the oldest queued job whose repository is idle.

        Returns ``None`` when there is nothing to do, or when every queued job
        belongs to a repository that already has one running.
        """
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT id FROM jobs
                 WHERE state = 'queued'
                   AND repo_id NOT IN (SELECT repo_id FROM jobs WHERE state = 'running')
                 ORDER BY id
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE jobs SET state = ?, started_at = ? WHERE id = ?",
                (JobState.RUNNING.value, now(), row["id"]),
            )
            claimed = int(row["id"])
        return self.get(claimed)

    def log(self, job_id: int, message: str) -> None:
        """Append a line to a running job's log."""
        with self.database.write() as connection:
            row = connection.execute("SELECT log FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            lines = loads(row["log"], [])
            lines.append(message)
            connection.execute(
                "UPDATE jobs SET log = ? WHERE id = ?", (dumps(lines[-500:]), job_id)
            )

    def finish(
        self, job_id: int, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        """Mark a job done or failed."""
        state = JobState.FAILED if error else JobState.DONE
        with self.database.write() as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
                (state.value, dumps(result) if result else None, error, now(), job_id),
            )

    def cancel(self, job_id: int) -> bool:
        """Cancel a job that has not started.

        A running job is left alone: it is executing a subprocess against a real
        deployment, and killing it mid-way could leave the database in a state
        nobody can reason about.
        """
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET state = ?, finished_at = ? WHERE id = ? AND state = 'queued'",
                (JobState.CANCELLED.value, now(), job_id),
            )
            return cursor.rowcount > 0

    def reap_stale(self) -> int:
        """Fail jobs left running by a worker that died.

        Called at startup. Not retried, deliberately — see :data:`STALE_MESSAGE`.
        """
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET state = ?, error = ?, finished_at = ? WHERE state = 'running'",
                (JobState.FAILED.value, STALE_MESSAGE, now()),
            )
            return int(cursor.rowcount)

    # ------------------------------------------------------------- querying

    def get(self, job_id: int) -> JobRecord | None:
        """One job by id."""
        row = self.database.connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _to_job(row) if row else None

    def recent(self, repo_id: int | None = None, limit: int = 25) -> list[JobRecord]:
        """Most recent jobs, newest first."""
        if repo_id is None:
            rows = self.database.connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.database.connection.execute(
                "SELECT * FROM jobs WHERE repo_id = ? ORDER BY id DESC LIMIT ?",
                (repo_id, limit),
            ).fetchall()
        return [_to_job(row) for row in rows]

    def active(self, repo_id: int) -> JobRecord | None:
        """The job currently running for a repository, if any."""
        row = self.database.connection.execute(
            "SELECT * FROM jobs WHERE repo_id = ? AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1",
            (repo_id,),
        ).fetchone()
        return _to_job(row) if row else None


def _to_job(row: Any) -> JobRecord:
    """Build a :class:`JobRecord` from a database row."""
    return JobRecord(
        id=row["id"],
        repo_id=row["repo_id"],
        kind=row["kind"],
        state=JobState(row["state"]),
        payload=loads(row["payload"], {}),
        result=loads(row["result"], None),
        error=row["error"],
        log=loads(row["log"], []),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
