"""The background worker.

Two job kinds, because the flow has two moments a person waits for:
**understand** (read the code, ask the deployment, work out what is testable)
and **build** (write the tests, run them, keep the ones that pass).

Both are thin handlers over :mod:`testtrout.app.session`, which is the same
code path the CLI takes. A difference between what the app does and what
``trout`` does would be a bug, not a feature.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from testtrout.app.db import Database
from testtrout.app.models import JobRecord
from testtrout.app.queue import JobQueue
from testtrout.app.repos import RepoRegistry
from testtrout.app.session import Session
from testtrout.store import QaPaths, load_dotenv

POLL_SECONDS = 0.5

Handler = Callable[["JobContext"], dict[str, Any]]


class JobContext:
    """Everything a handler needs, plus a way to report progress."""

    def __init__(
        self, job: JobRecord, paths: QaPaths, queue: JobQueue, registry: RepoRegistry
    ) -> None:
        self.job = job
        self.paths = paths
        self.queue = queue
        self.registry = registry
        # Credentials live in the repository's own .env, so load the one that
        # belongs to *this* job rather than whatever the server started with.
        load_dotenv(paths.root)

    @property
    def options(self) -> dict[str, Any]:
        """The job's payload."""
        return self.job.payload

    @property
    def repo_id(self) -> int:
        """Which repository this job belongs to."""
        return self.job.repo_id

    def log(self, message: str) -> None:
        """Append a progress line the UI will pick up."""
        if self.job.id is not None:
            self.queue.log(self.job.id, message)

    def session(self) -> Session:
        """The project, wired so its progress reaches the job log."""
        return Session(paths=self.paths, log=self.log)


def handle_understand(context: JobContext) -> dict[str, Any]:
    """Read the code, ask the deployment, and work out what is testable.

    One job rather than three, because a scan whose consequences are not worked
    out is a file on disk that nobody asked for. Pressing one button and
    getting one answer is the whole point of the redesign.
    """
    from testtrout.app import session as pipeline

    current = context.session()
    sheet, plan = pipeline.refresh(current, rescan=bool(context.options.get("rescan", True)))

    scan = current.scan
    context.registry.record_scan(
        context.repo_id,
        scan.project.framework if scan else "",
        scan.project.backend if scan else None,
    )
    counts = plan.counts()
    return {
        "ready": counts["ready"],
        "waiting": counts["waiting"],
        "pages": counts["pages"],
        "endpoints": counts["endpoints"],
        "asking": len(sheet.outstanding),
    }


def handle_build(context: JobContext) -> dict[str, Any]:
    """Write a test for everything ready, prove it, and keep what passes."""
    from testtrout.app import session as pipeline

    current = context.session()
    outcome = pipeline.build(current, limit=int(context.options.get("limit", 20)))
    run_id = outcome.get("run_id")
    if run_id:
        from testtrout.domain.run import RunRecord
        from testtrout.store import read_model

        record = read_model(context.paths.runs / f"{run_id}.yaml", RunRecord)
        context.registry.record_run(context.repo_id, record)
    return outcome


def handle_run(context: JobContext) -> dict[str, Any]:
    """Re-run the baseline and report what changed.

    This is the regression check: every assertion in the suite came from
    observing the deployment once, so a failure here means the deployment stopped
    doing something it used to do.
    """
    from testtrout.domain.run import RunRecord
    from testtrout.runtime.runner import run as execute
    from testtrout.store import write_model

    current = context.session()
    config = current.config
    entrypoint = config.entrypoint(context.options.get("entrypoint"))
    if entrypoint is None:
        raise RuntimeError("no deployment is configured for this project")

    from testtrout.authoring.store import load_all

    index, _ = load_all(context.paths.scenarios)
    context.log(f"Running the baseline against {entrypoint.url}")
    record: RunRecord = execute(
        config,
        entrypoint,
        index,
        context.paths.root,
        report_dir=context.paths.runs / "latest",
    )
    write_model(context.paths.runs / f"{record.id}.yaml", record, header=False)
    context.registry.record_run(context.repo_id, record)

    for note in record.notes:
        context.log(note)
    changed = [r for r in record.results if r.classification.is_product_signal]
    for result in changed:
        context.log(f"changed: {result.title or result.scenario_id} — {result.message}")
    context.log(
        f"{len(changed)} change(s) from baseline out of {len(record.results)} test(s)."
        if record.results
        else "Nothing ran — the baseline is empty."
    )
    return {"status": record.status.value, "run_id": record.id, "changed": len(changed)}


HANDLERS: dict[str, Handler] = {
    "understand": handle_understand,
    "build": handle_build,
    "run": handle_run,
}


# -------------------------------------------------------------------- loop


class Worker:
    """Polls the queue and executes jobs until stopped."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.queue = JobQueue(database)
        self.registry = RepoRegistry(database)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Run the loop on a background thread."""
        if self._thread is not None:
            return
        reaped = self.queue.reap_stale()
        if reaped:
            # Not retried: a half-finished run may have left state behind, and
            # silently repeating it risks two concurrent writes to one database.
            pass
        self._thread = threading.Thread(target=self.loop, daemon=True, name="trout-worker")
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to finish after the current job."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def loop(self) -> None:
        """Claim and execute until stopped."""
        while not self._stop.is_set():
            job = self.queue.claim()
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            self.execute(job)

    def execute(self, job: JobRecord) -> None:
        """Run one job, recording whatever happens.

        Never raises. A handler that fails takes its job down with a readable
        error; it must not take the worker down with it, because the next job
        may be for an entirely unrelated repository.
        """
        assert job.id is not None
        handler = HANDLERS.get(job.kind)
        if handler is None:
            self.queue.finish(job.id, error=f"no handler for job kind {job.kind!r}")
            return

        try:
            paths = self.registry.paths(job.repo_id)
            result = handler(JobContext(job, paths, self.queue, self.registry))
        except Exception as exc:
            self.queue.log(job.id, f"failed: {exc}")
            for line in traceback.format_exc().splitlines()[-4:]:
                self.queue.log(job.id, line)
            self.queue.finish(job.id, error=str(exc))
            return
        self.queue.finish(job.id, result=result)
