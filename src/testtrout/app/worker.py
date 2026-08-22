"""The background worker.

Each job kind is a thin handler over the same library function the CLI calls.
Nothing here reimplements behaviour — a difference between what the app does
and what ``trout run`` does would be a bug, not a feature.

Runs in-process alongside the web server under ``trout up``, or standalone via
``trout worker``. Either way the queue is the same table, so a second worker on
the same machine is safe and needs no coordination beyond what SQLite already
provides.
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
from testtrout.domain.config import Config
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import ProbeResult
from testtrout.domain.overview import ProjectOverview
from testtrout.domain.run import RunRecord
from testtrout.domain.scenario import ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import ScanResult
from testtrout.store import QaPaths, apply_scan, load_dotenv, read_model, write_model

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

    def config(self) -> Config:
        """The repository's configuration, or defaults."""
        return read_model(self.paths.config, Config) if self.paths.config.is_file() else Config()

    def scan_result(self) -> ScanResult:
        """The last scan.

        Raises:
            RuntimeError: if the repository has never been scanned. Every other
                job depends on it, and saying so plainly beats an empty result.
        """
        if not self.paths.surfaces.is_file():
            raise RuntimeError("this repository has not been scanned yet — run a scan first")
        return read_model(self.paths.surfaces, ScanResult)

    def probe_result(self) -> ProbeResult | None:
        """The last probe for the default entrypoint, if there is one."""
        entrypoint = self.config().entrypoint()
        if entrypoint is None:
            return None
        path = self.paths.observed / f"{entrypoint.name}.yaml"
        return read_model(path, ProbeResult) if path.is_file() else None

    def intent(self) -> ProductIntent | None:
        """Captured intent, if any."""
        return read_model(self.paths.intent, ProductIntent) if self.paths.intent.is_file() else None

    def scenarios(self) -> tuple[ScenarioIndex, list[str]]:
        """The scenario index and any unreadable files."""
        from testtrout.authoring.store import load_all

        return load_all(self.paths.scenarios)


# ------------------------------------------------------------------ handlers


def handle_scan(context: JobContext) -> dict[str, Any]:
    """Analyse the repository."""
    from testtrout.analysis.scanner import scan as run_scan

    context.log(f"analysing {context.paths.root}")
    result = run_scan(context.paths.root)
    context.paths.ensure()
    write_model(context.paths.surfaces, result)
    write_model(context.paths.config, apply_scan(context.config(), result))

    context.registry.record_scan(context.repo_id, result.project.framework, result.project.backend)
    context.log(f"found {sum(result.counts.values())} surfaces")
    for warning in result.warnings:
        context.log(f"warning: {warning.message}")

    # Probing belongs to understanding the product, not to a separate chore: it
    # is what turns "the code can do this" into "the deployment actually does".
    probed = None
    if context.config().entrypoint() is not None:
        try:
            probed = handle_probe(context)
        except Exception as exc:
            context.log(f"could not probe the deployment: {exc}")

    raised = _refresh_questions(context)
    if raised:
        context.log(f"{raised} new question(s) for you — see Questions")

    changed = _record_overview(context)

    gaps = _gap_summary(context)
    context.log(f"{gaps['total']} test(s) worth writing — {gaps['ready']} can be drafted now")
    for line in gaps["headline"]:
        context.log(f"  {line}")

    return {
        "counts": result.counts,
        "warnings": len(result.warnings),
        "probed": probed,
        "gaps": gaps["total"],
        "ready": gaps["ready"],
        "delta": changed,
    }


def _record_overview(context: JobContext) -> dict[str, Any]:
    """Describe the product, and say what moved since the last time.

    The second scan is where this tool either earns its place or does not. A
    rescan that reprints the same forty untested things tells you nothing; one
    that says "three of those now have tests, and there is a new page" tells
    you where you are.
    """
    from testtrout.planning.overview import build as build_overview
    from testtrout.planning.overview import delta as compare
    from testtrout.planning.readiness import assess

    paths = context.paths
    previous = read_model(paths.overview, ProjectOverview) if paths.overview.is_file() else None
    index, _ = context.scenarios()
    scan = context.scan_result()
    current = build_overview(
        scan, index, assess(context.config(), scan, context.probe_result())
    )
    write_model(paths.overview, current)

    changed = compare(previous, current)
    if previous is None:
        context.log(current.summary)
    elif changed.has_changes:
        for label, items in (
            ("new since last scan", changed.new_areas),
            ("now covered", changed.newly_covered),
            ("no longer in the code", changed.gone),
        ):
            if items:
                context.log(f"{label}: {len(items)}")
                for item in items[:5]:
                    context.log(f"  {item}")
    else:
        context.log("nothing changed since the last scan")

    coverage = current.coverage
    context.log(
        f"coverage {coverage.overall_percent}% — "
        f"transactions {coverage.transactions_percent}%, "
        f"pages {coverage.pages_percent}%, apis {coverage.apis_percent}%"
    )
    return changed.model_dump(mode="json")


def _gap_summary(context: JobContext) -> dict[str, Any]:
    """What tests are missing, summarised for a job log.

    Part of scanning rather than a separate step: "what does this app do" and
    "what is untested about it" are the same question asked twice.
    """
    from testtrout.planning import gaps as planner
    from testtrout.planning.existing_tests import detect

    scan = context.scan_result()
    config = context.config()
    index, _ = context.scenarios()
    gap_map = planner.build(
        scan,
        intent=context.intent(),
        probe=context.probe_result(),
        existing=detect(context.paths.root, scan),
        roles=[u.role for u in config.test_users],
        scenarios=index,
    )
    ranked = gap_map.ranked(limit=5)
    return {
        "total": len(gap_map.gaps),
        "ready": len([g for g in gap_map.gaps if g.ready]),
        "headline": [f"[{g.criticality.value}] {g.title}" for g in ranked],
        "coverage": gap_map.coverage.percent,
    }


def handle_probe(context: JobContext) -> dict[str, Any]:
    """Explore the running deployment."""
    from testtrout.deployment.prober import probe as run_probe
    from testtrout.deployment.reconcile import persist_login, reconcile

    scan = context.scan_result()
    config = context.config()
    entrypoint = config.entrypoint(context.options.get("entrypoint"))
    if entrypoint is None:
        raise RuntimeError("no deployment is configured for this repository")

    context.log(f"probing {entrypoint.url}")
    if not entrypoint.writable:
        context.log("read-only: mutating requests will be blocked")

    observed = run_probe(scan, config, entrypoint, role=context.options.get("role"))
    observed.divergences.extend(reconcile(scan, observed))
    persist_login(context.paths, observed)
    write_model(context.paths.observed / f"{entrypoint.name}.yaml", observed)

    context.log(f"{observed.reachable_count}/{len(observed.screens)} screens reachable")
    for divergence in observed.divergences:
        context.log(f"{divergence.code}: {divergence.message}")
    return {"reachable": observed.reachable_count, "divergences": len(observed.divergences)}


def handle_intent(context: JobContext) -> dict[str, Any]:
    """Capture or draft product intent."""
    from testtrout.llm.gateway import Gateway
    from testtrout.planning import intent as planner

    scan = context.scan_result()
    gateway = Gateway(context.config().model, context.paths.cache)
    describe = context.options.get("describe")

    context.log("drafting from the codebase" if not describe else "structuring what you described")
    captured, warnings = (
        planner.structure(gateway, describe, scan, context.probe_result())
        if describe
        else planner.draft(gateway, scan, context.probe_result())
    )
    context.paths.ensure()
    write_model(context.paths.intent, captured)

    for warning in warnings:
        context.log(f"warning: {warning}")
    for question in captured.unanswered:
        context.log(f"needs an answer: {question.question}")
    return {"journeys": len(captured.journeys), "questions": len(captured.unanswered)}


def handle_propose(context: JobContext) -> dict[str, Any]:
    """Draft scenarios for the top-ranked gaps."""
    from testtrout.authoring import propose as authoring
    from testtrout.authoring.store import save
    from testtrout.llm.gateway import Gateway
    from testtrout.planning import gaps as planner
    from testtrout.planning.existing_tests import detect

    scan = context.scan_result()
    config = context.config()
    index, _ = context.scenarios()
    gap_map = planner.build(
        scan,
        intent=context.intent(),
        probe=context.probe_result(),
        existing=detect(context.paths.root, scan),
        roles=[u.role for u in config.test_users],
        scenarios=index,
    )

    kind = context.options.get("kind")
    limit = int(context.options.get("limit", 5))
    gateway = (
        None if context.options.get("no_model") else Gateway(config.model, context.paths.cache)
    )
    candidates = [g for g in gap_map.ranked(ready_only=True) if not kind or g.kind.value == kind][
        :limit
    ]

    if not candidates:
        # Silence here is the worst outcome: the user pressed a button and got
        # nothing back. Say which filter emptied the list.
        total = len(gap_map.gaps)
        ready = len([g for g in gap_map.gaps if g.ready])
        blockers = sorted({b.message for g in gap_map.gaps for b in g.blockers})
        context.log(
            f"nothing to draft — {total} gap(s) known, {ready} ready to write"
            + (f", none of kind '{kind}'" if kind else "")
        )
        for message in blockers[:3]:
            context.log(f"blocked: {message}")
        return {"drafted": 0, "gaps": total, "ready": ready, "blockers": blockers[:3]}

    context.paths.ensure()
    drafted = 0
    for candidate in candidates:
        context.log(f"drafting: {candidate.title}")
        scenario, warnings = authoring.propose(
            candidate,
            scan,
            config,
            probe=context.probe_result(),
            intent=context.intent(),
            gateway=gateway,
        )
        save(context.paths.scenarios, scenario)
        drafted += 1
        for warning in warnings:
            context.log(f"warning: {warning}")
    return {"drafted": drafted}


def handle_generate(context: JobContext) -> dict[str, Any]:
    """Compile approved scenarios into test files."""
    from testtrout.authoring.base import select_emitter
    from testtrout.authoring.store import save
    from testtrout.runtime.toolchain import app_root

    config = context.config()
    index, _ = context.scenarios()
    approved = index.by_status(ScenarioStatus.APPROVED)
    if not approved:
        raise RuntimeError("no approved scenarios — approve some first")

    shared: dict[str, str] = {}
    written = 0
    for scenario in approved:
        emitter = select_emitter(scenario)
        if emitter is None:
            context.log(f"no emitter for {scenario.kind.value}")
            continue
        emitted = emitter.emit(scenario, config)
        destination = app_root(context.paths.root) / emitted.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(emitted.content, encoding="utf-8")
        shared.update(emitted.shared)
        scenario.emitted_to = emitted.path
        save(context.paths.scenarios, scenario)
        written += 1
        context.log(f"wrote {emitted.path}")
        for note in emitted.notes:
            context.log(note)

    for rel, content in shared.items():
        path = app_root(context.paths.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        context.log(f"wrote {rel}")
    return {"written": written}


def handle_run(context: JobContext) -> dict[str, Any]:
    """Execute the suite."""
    from testtrout.runtime.runner import run as execute

    config = context.config()
    entrypoint = config.entrypoint(context.options.get("entrypoint"))
    if entrypoint is None:
        raise RuntimeError("no deployment is configured for this repository")
    # Generate anything approved that has no code yet. Making someone press a
    # second button to compile what they already approved is ceremony, not
    # safety — the approval was the decision.
    index, _ = context.scenarios()
    pending = [
        item
        for item in index.by_status(ScenarioStatus.APPROVED)
        if not item.emitted_to or not (context.paths.root / item.emitted_to).is_file()
    ]
    if pending:
        context.log(f"generating {len(pending)} approved scenario(s) with no code yet")
        handle_generate(context)
        index, _ = context.scenarios()

    context.log(f"running against {entrypoint.url}")
    context.paths.ensure()
    record = execute(
        config,
        entrypoint,
        index,
        context.paths.root,
        report_dir=context.paths.runs / "latest",
        scenario_id=context.options.get("scenario"),
    )
    write_model(context.paths.runs / f"{record.id}.yaml", record, header=False)
    context.registry.record_run(context.repo_id, record)

    for note in record.notes:
        context.log(note)
    for result in record.results:
        context.log(
            f"{result.classification.value}: "
            f"{result.scenario_id.replace('scenario:', '')}"
            + (f" — {result.message}" if result.message else "")
        )
    context.log(f"run finished: {record.status.value.upper()}")
    return {"status": record.status.value, "run_id": record.id}


def handle_build(context: JobContext) -> dict[str, Any]:
    """Draft tests and prove them against the deployment before keeping any.

    The work itself lives in :mod:`testtrout.authoring.build`, shared with
    ``trout build`` so the button and the command cannot drift apart.
    """
    from testtrout.authoring.build import build_suite
    from testtrout.llm.gateway import Gateway

    config = context.config()
    entrypoint = config.entrypoint(context.options.get("entrypoint"))
    if entrypoint is None:
        raise RuntimeError("no deployment is configured — add one under Setup")

    outcome = build_suite(
        context.paths,
        config,
        context.scan_result(),
        entrypoint,
        intent=context.intent(),
        probe=context.probe_result(),
        gateway=(
            None if context.options.get("no_model") else Gateway(config.model, context.paths.cache)
        ),
        limit=int(context.options.get("limit", 5)),
        log=context.log,
    )
    if outcome.run_id:
        record = read_model(context.paths.runs / f"{outcome.run_id}.yaml", RunRecord)
        context.registry.record_run(context.repo_id, record)
    return outcome.as_dict() | {"questions": _refresh_questions(context)}


def _refresh_questions(context: JobContext) -> int:
    """Gather everything the tool could not determine into the question queue."""
    from testtrout.domain.question import QuestionLog
    from testtrout.planning import gaps as planner
    from testtrout.planning import questions as question_planner
    from testtrout.planning.existing_tests import detect
    from testtrout.planning.readiness import assess

    paths = context.paths
    log = read_model(paths.questions, QuestionLog) if paths.questions.is_file() else QuestionLog()
    scan = read_model(paths.surfaces, ScanResult) if paths.surfaces.is_file() else None
    config = context.config()
    index, _ = context.scenarios()
    gaps = (
        planner.build(
            scan,
            intent=context.intent(),
            probe=context.probe_result(),
            existing=detect(paths.root, scan),
            roles=[u.role for u in config.test_users],
            scenarios=index,
        )
        if scan
        else None
    )

    raised = question_planner.collect(
        log,
        scan=scan,
        plan=assess(config, scan, context.probe_result()),
        probe=context.probe_result(),
        gaps=gaps,
        scenarios=index,
        config=config,
    )
    paths.ensure()
    write_model(paths.questions, log)
    return raised


def handle_certify(context: JobContext) -> dict[str, Any]:
    """Prove scenarios are deterministic."""
    from testtrout.authoring.store import save
    from testtrout.runtime.runner import apply_verdicts
    from testtrout.runtime.runner import certify as run_certification

    config = context.config()
    entrypoint = config.entrypoint(context.options.get("entrypoint"))
    if entrypoint is None:
        raise RuntimeError("no deployment is configured for this repository")
    index, _ = context.scenarios()

    attempts = int(context.options.get("runs") or config.run.certification_runs)
    context.log(f"running the suite {attempts} times to check for flakiness")
    verdicts, _ = run_certification(
        config, entrypoint, index, context.paths.root, context.paths.runs, runs=attempts
    )
    changes = apply_verdicts(index, verdicts)
    for scenario in index.scenarios:
        if scenario.id in changes:
            save(context.paths.scenarios, scenario)
            context.log(f"{scenario.id.replace('scenario:', '')} → {scenario.status.value}")
    return {"changes": {k: v.value for k, v in changes.items()}}


HANDLERS: dict[str, Handler] = {
    "scan": handle_scan,
    "build": handle_build,
    "probe": handle_probe,
    "intent": handle_intent,
    "propose": handle_propose,
    "generate": handle_generate,
    "run": handle_run,
    "certify": handle_certify,
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
