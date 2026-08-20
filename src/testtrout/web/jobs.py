"""Running long operations in the background, with a live log.

Scanning is fast; probing a deployment and running a suite are not. Both need
to report progress while they work, so each action runs on a worker thread and
appends to an in-memory event log that the page tails over server-sent events.

State is intentionally in-memory and single-job. This is a local tool for one
developer, and a job queue would be machinery without a purpose. Refusing to
start a second job while one is running is also a safety property: two
concurrent runs against the same database would interfere in ways neither
could explain.
"""

from __future__ import annotations

import threading
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_EVENTS = 500


@dataclass
class Event:
    """One line of job output."""

    at: str
    level: str
    message: str

    def as_dict(self) -> dict[str, str]:
        """Serialise for the event stream."""
        return {"at": self.at, "level": self.level, "message": self.message}


@dataclass
class Job:
    """A background operation and everything the page needs to render it."""

    name: str
    state: str = "running"
    """running | done | failed"""
    events: deque[Event] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None

    def log(self, message: str, level: str = "info") -> None:
        """Append a line the page will pick up on its next poll."""
        self.events.append(Event(at=datetime.now(UTC).isoformat(), level=level, message=message))

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the API."""
        return {
            "name": self.name,
            "state": self.state,
            "events": [e.as_dict() for e in self.events],
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRunner:
    """Owns the single active job.

    One at a time, deliberately: concurrent runs against one database produce
    failures that neither run can account for.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Job | None = None

    @property
    def current(self) -> Job | None:
        """The most recent job, running or finished."""
        return self._current

    @property
    def busy(self) -> bool:
        """Whether a job is in flight."""
        return self._current is not None and self._current.state == "running"

    def start(self, name: str, work: Callable[[Job], dict[str, Any] | None]) -> Job:
        """Begin a job on a worker thread.

        Raises:
            RuntimeError: if a job is already running. The caller turns this
                into a 409 rather than silently queueing, so the page can say
                what is already happening.
        """
        with self._lock:
            if self.busy:
                raise RuntimeError(
                    f"{self._current.name if self._current else 'a job'} is already running"
                )
            job = Job(name=name)
            self._current = job

        def target() -> None:
            try:
                job.result = work(job)
                job.state = "done"
            except Exception as exc:
                job.state = "failed"
                job.error = str(exc)
                job.log(str(exc), level="error")
                for line in traceback.format_exc().splitlines()[-6:]:
                    job.log(line, level="error")
            finally:
                job.finished_at = datetime.now(UTC).isoformat()

        threading.Thread(target=target, daemon=True, name=f"qa-{name}").start()
        return job
