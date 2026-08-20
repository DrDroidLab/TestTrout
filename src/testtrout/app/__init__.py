"""The TestTrout application: storage, a job queue, and a repository registry.

Everything below the CLI and the web interface. The design follows one split
that keeps the useful properties of the file-based approach while adding what
files genuinely cannot do:

*Scenario specs and generated tests stay in the linked repository.* They are
the test suite. They belong next to the code, reviewable in a pull request, and
they move with a branch.

*Runs, history, and the job queue live in SQLite.* Coverage over time,
flakiness trends, "which test has caught the most regressions" — no arrangement
of files in one repo can answer those.

SQLite and an in-process worker are deliberate. A single developer on their own
machine should be able to run ``trout up`` and have it work, with no daemon to
start and no container to pull. The storage layer is written so that swapping
in Postgres for a hosted deployment is contained rather than a rewrite.
"""

from testtrout.app.db import Database, default_database_path
from testtrout.app.models import JobRecord, JobState, RepoRecord
from testtrout.app.queue import JobQueue
from testtrout.app.repos import RepoRegistry

__all__ = [
    "Database",
    "JobQueue",
    "JobRecord",
    "JobState",
    "RepoRecord",
    "RepoRegistry",
    "default_database_path",
]
