"""Storage, the job queue, and the repository registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from testtrout.app import Database, JobQueue, RepoRegistry
from testtrout.app.models import JobState, RepoSource
from testtrout.app.queue import UnknownJobKindError
from testtrout.app.repos import RepoError


@pytest.fixture
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def registry(database: Database) -> RepoRegistry:
    return RepoRegistry(database)


@pytest.fixture
def queue(database: Database) -> JobQueue:
    return JobQueue(database)


def _repo_dir(tmp_path: Path, name: str = "app") -> Path:
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    return root


# ------------------------------------------------------------------ storage


def test_migrations_are_applied_once(tmp_path: Path):
    path = tmp_path / "m.db"
    Database(path)
    second = Database(path)  # re-opening must not re-run migrations
    version = second.connection.execute("SELECT version FROM schema_version").fetchone()
    assert version["version"] == 1


def test_wal_mode_is_on(database: Database):
    """The worker writes while requests read; WAL is the whole concurrency story."""
    mode = database.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# -------------------------------------------------------------------- repos


def test_linking_uses_the_bounded_project_root(tmp_path: Path, registry: RepoRegistry):
    """A path inside a repository links the repository, not the subdirectory."""
    root = _repo_dir(tmp_path)
    nested = root / "src" / "components"
    nested.mkdir(parents=True)

    assert registry.link_local(nested).path == str(root)


def test_linking_the_same_repo_twice_is_idempotent(tmp_path: Path, registry: RepoRegistry):
    root = _repo_dir(tmp_path)
    first = registry.link_local(root)
    second = registry.link_local(root)
    assert first.id == second.id
    assert len(registry.all()) == 1


def test_unlinking_never_deletes_a_local_directory(tmp_path: Path, registry: RepoRegistry):
    """Deleting a directory the developer already had is not this tool's call."""
    root = _repo_dir(tmp_path)
    record = registry.link_local(root)
    assert record.source is RepoSource.LOCAL

    with pytest.raises(RepoError, match="did not create"):
        registry.unlink(record.id, delete_files=True)
    assert root.is_dir()

    assert registry.unlink(record.id) is True
    assert root.is_dir()
    assert registry.all() == []


def test_a_moved_repository_reports_plainly(tmp_path: Path, registry: RepoRegistry):
    """A confusing path error three layers down helps nobody."""
    import shutil

    root = _repo_dir(tmp_path)
    record = registry.link_local(root)
    shutil.rmtree(root)

    assert record.id is not None
    with pytest.raises(RepoError, match="no longer exists"):
        registry.paths(record.id)


def test_run_history_is_recorded_and_queryable(tmp_path: Path, registry: RepoRegistry):
    from testtrout.domain.run import Classification, RunRecord, ScenarioResult

    record = registry.link_local(_repo_dir(tmp_path))
    assert record.id is not None
    run = RunRecord(
        id="20260101T000000Z",
        started_at="2026-01-01T00:00:00Z",
        entrypoint="local",
        results=[
            ScenarioResult(scenario_id="a", classification=Classification.PASSED),
            ScenarioResult(scenario_id="b", classification=Classification.ASSERTION_FAILURE),
        ],
    )
    summary = registry.record_run(record.id, run)
    assert summary.passed == 1
    assert summary.failed == 1
    assert summary.status == "fail"

    history = registry.run_history(record.id)
    assert len(history) == 1
    assert registry.get(record.id).last_run_status == "fail"


# -------------------------------------------------------------------- queue


def test_jobs_are_serialised_per_repository(
    tmp_path: Path, registry: RepoRegistry, queue: JobQueue
):
    """Two runs against one database interfere in ways neither can explain."""
    one = registry.link_local(_repo_dir(tmp_path, "one"))
    two = registry.link_local(_repo_dir(tmp_path, "two"))

    queue.enqueue(one.id, "understand")
    queue.enqueue(one.id, "run")
    queue.enqueue(two.id, "understand")

    first = queue.claim()
    assert first.repo_id == one.id

    # The next claim skips repo one entirely and takes repo two.
    second = queue.claim()
    assert second.repo_id == two.id

    # Nothing left that is not blocked.
    assert queue.claim() is None


def test_finishing_a_job_frees_its_repository(
    tmp_path: Path, registry: RepoRegistry, queue: JobQueue
):
    record = registry.link_local(_repo_dir(tmp_path))
    queue.enqueue(record.id, "understand")
    queue.enqueue(record.id, "run")

    first = queue.claim()
    assert queue.claim() is None
    queue.finish(first.id, result={"ok": True})

    second = queue.claim()
    assert second.kind == "run"


def test_an_unknown_job_kind_is_rejected_at_enqueue(
    tmp_path: Path, registry: RepoRegistry, queue: JobQueue
):
    """Failing here beats discovering it in a worker log with no context."""
    record = registry.link_local(_repo_dir(tmp_path))
    with pytest.raises(UnknownJobKindError, match="unknown job kind"):
        queue.enqueue(record.id, "definitely-not-a-kind")


def test_a_queued_job_can_be_cancelled_but_a_running_one_cannot(
    tmp_path: Path, registry: RepoRegistry, queue: JobQueue
):
    """A running job is driving a subprocess against a real deployment."""
    record = registry.link_local(_repo_dir(tmp_path))
    queued = queue.enqueue(record.id, "understand")
    assert queue.cancel(queued.id) is True
    assert queue.get(queued.id).state is JobState.CANCELLED

    running = queue.enqueue(record.id, "run")
    queue.claim()
    assert queue.cancel(running.id) is False


def test_stale_jobs_are_failed_not_retried(tmp_path: Path, registry: RepoRegistry, queue: JobQueue):
    """A half-finished run may have left state behind; repeating it is worse."""
    record = registry.link_local(_repo_dir(tmp_path))
    job = queue.enqueue(record.id, "run")
    queue.claim()

    assert queue.reap_stale() == 1
    reaped = queue.get(job.id)
    assert reaped.state is JobState.FAILED
    assert "interrupted" in reaped.error


def test_logs_accumulate_on_a_job(tmp_path: Path, registry: RepoRegistry, queue: JobQueue):
    record = registry.link_local(_repo_dir(tmp_path))
    job = queue.enqueue(record.id, "understand")
    queue.log(job.id, "first")
    queue.log(job.id, "second")
    assert queue.get(job.id).log == ["first", "second"]


# ------------------------------------------------------------------- worker


def test_a_failing_handler_fails_its_job_not_the_worker(
    tmp_path: Path, registry: RepoRegistry, database: Database
):
    """The next job may be for an entirely unrelated repository."""
    from testtrout.app import worker as worker_module
    from testtrout.app.worker import Worker

    record = registry.link_local(_repo_dir(tmp_path))
    instance = Worker(database)
    job = instance.queue.enqueue(record.id, "understand")
    instance.queue.claim()

    def explode(context):
        raise RuntimeError("boom")

    original = worker_module.HANDLERS["understand"]
    worker_module.HANDLERS["understand"] = explode
    try:
        instance.execute(instance.queue.get(job.id))
    finally:
        worker_module.HANDLERS["understand"] = original

    finished = instance.queue.get(job.id)
    assert finished.state is JobState.FAILED
    assert "boom" in finished.error


def test_the_worker_runs_a_real_scan(tmp_path: Path, registry: RepoRegistry, database: Database):
    """End to end: enqueue, claim, execute, record."""
    import shutil

    from testtrout.app.worker import Worker

    fixture = Path(__file__).resolve().parents[2] / "examples" / "lovable-shop"
    root = tmp_path / "shop"
    shutil.copytree(fixture, root)
    (root / ".git").mkdir()

    record = registry.link_local(root)
    instance = Worker(database)
    job = instance.queue.enqueue(record.id, "understand")
    instance.execute(instance.queue.claim())

    finished = instance.queue.get(job.id)
    assert finished.state is JobState.DONE
    # The understand job reports what it worked out, not raw surface counts.
    assert finished.result["pages"] > 0
    assert registry.get(record.id).framework == "vite-react"
