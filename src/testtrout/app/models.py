"""Typed rows for the application database."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    """Current UTC timestamp, ISO-8601. One definition so ordering is total."""
    return datetime.now(UTC).isoformat()


class RepoSource(StrEnum):
    """How a repository got onto this machine."""

    LOCAL = "local"
    """A directory the developer already has. Never modified by the registry."""
    GITHUB = "github"
    """Cloned by TestTrout from GitHub using a personal access token."""


class RepoRecord(BaseModel):
    """One linked repository.

    The registry stores where a repository *is*, never what is in it. The
    scan, scenarios, and generated tests all live in the repository's own
    ``.trout/`` directory, so unlinking loses nothing but the run history.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    name: str
    path: str
    source: RepoSource = RepoSource.LOCAL
    remote: str | None = Field(default=None, description="GitHub owner/name, when cloned.")
    default_branch: str | None = None
    framework: str | None = None
    backend: str | None = None
    created_at: str = Field(default_factory=now)
    last_scanned_at: str | None = None
    last_run_at: str | None = None
    last_run_status: str | None = None

    @property
    def root(self) -> Path:
        """Filesystem root of the repository."""
        return Path(self.path)

    @property
    def exists(self) -> bool:
        """Whether the directory is still there.

        A linked repository can be moved or deleted behind the app's back, and
        reporting that plainly beats failing with a confusing path error later.
        """
        return self.root.is_dir()


class JobState(StrEnum):
    """Lifecycle of a queued job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Whether the job has finished, one way or another."""
        return self in {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


class JobRecord(BaseModel):
    """One unit of background work.

    Jobs are per-repository and serialised per repository: two runs against one
    database interfere in ways neither can account for. Different repositories
    proceed in parallel.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    repo_id: int
    kind: str = Field(description="scan | probe | intent | propose | generate | run | certify")
    state: JobState = JobState.QUEUED
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    log: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration, or ``None`` if it has not finished."""
        if not self.started_at or not self.finished_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.finished_at)
        return round((end - start).total_seconds(), 1)


class RunSummary(BaseModel):
    """A run's headline result, kept for history.

    Deliberately a summary. The full record with evidence stays in the
    repository's ``.trout/runs/``; what belongs here is the small, queryable
    part that answers questions across time.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    repo_id: int
    run_id: str
    status: str
    entrypoint: str = ""
    passed: int = 0
    failed: int = 0
    inconclusive: int = 0
    duration_seconds: float = 0.0
    started_at: str = Field(default_factory=now)
