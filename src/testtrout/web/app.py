"""The local web application.

Every endpoint calls the same library function the CLI calls, so behaviour that
differs between the two interfaces is a bug rather than a feature.

Two rules constrain what the API is allowed to do:

*It cannot change a deployment's safety posture.* There is no endpoint that
marks an entrypoint disposable. Doing that from a web page — one click away
from pointing test writes at production — is exactly the mistake the guard
exists to prevent, so it stays a deliberate edit to a committed file.

*It binds to loopback.* This is a developer's own tool reading their
credentials; it is not something to expose on a network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from testtrout import __version__
from testtrout.domain.config import Config
from testtrout.domain.gap import GapMap
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import ProbeResult
from testtrout.domain.run import RunRecord
from testtrout.domain.scenario import ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import ScanResult
from testtrout.store import QaPaths, apply_scan, load_dotenv, read_model, write_model
from testtrout.web.jobs import Job, JobRunner

STATIC = Path(__file__).parent / "static"


def create_app(root: Path) -> FastAPI:
    """Build the application, bound to one project."""
    paths = QaPaths(root=root.resolve())
    load_dotenv(paths.root)
    jobs = JobRunner()

    app = FastAPI(title="TestTrout", version=__version__, docs_url=None, redoc_url=None)

    # ------------------------------------------------------------- helpers

    def config() -> Config:
        return read_model(paths.config, Config) if paths.config.is_file() else Config()

    def scan_result() -> ScanResult | None:
        return read_model(paths.surfaces, ScanResult) if paths.surfaces.is_file() else None

    def probe_result() -> ProbeResult | None:
        entrypoint = config().entrypoint()
        if entrypoint is None:
            return None
        path = paths.observed / f"{entrypoint.name}.yaml"
        return read_model(path, ProbeResult) if path.is_file() else None

    def scenario_index() -> tuple[ScenarioIndex, list[str]]:
        from testtrout.authoring.store import load_all

        return load_all(paths.scenarios)

    def gap_map() -> GapMap | None:
        from testtrout.planning import gaps as planner
        from testtrout.planning.existing_tests import detect

        result = scan_result()
        if result is None:
            return None
        settings = config()
        index, _ = scenario_index()
        return planner.build(
            result,
            intent=read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None,
            probe=probe_result(),
            existing=detect(paths.root, result),
            roles=[u.role for u in settings.test_users],
            scenarios=index,
        )

    def latest_run() -> RunRecord | None:
        records = sorted(paths.runs.glob("*.yaml")) if paths.runs.is_dir() else []
        return read_model(records[-1], RunRecord) if records else None

    def start(name: str, work: Any) -> dict[str, Any]:
        try:
            job = jobs.start(name, work)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.as_dict()

    # ---------------------------------------------------------------- read

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        """Everything the dashboard needs in one request."""
        result = scan_result()
        settings = config()
        index, problems = scenario_index()
        gaps = gap_map()
        run = latest_run()
        observed = probe_result()

        return {
            "version": __version__,
            "root": str(paths.root),
            "configured": paths.config.is_file(),
            "scanned": result is not None,
            "project": result.project.model_dump(mode="json") if result else None,
            "counts": result.counts if result else {},
            "coverage": gaps.coverage.model_dump(mode="json")
            | {
                "percent": gaps.coverage.percent,
                "critical_percent": gaps.coverage.critical_percent,
            }
            if gaps
            else None,
            "gap_total": len(gaps.gaps) if gaps else 0,
            "gap_ready": len([g for g in gaps.gaps if g.ready]) if gaps else 0,
            "notes": gaps.notes if gaps else [],
            "scenarios": index.counts,
            "scenario_problems": problems,
            "entrypoints": [
                {
                    "name": e.name,
                    "url": e.url,
                    "disposable": e.disposable,
                    "writable": e.writable,
                }
                for e in settings.entrypoints
            ],
            "test_users": [u.role for u in settings.test_users],
            "model": {
                "provider": settings.model.provider.value,
                "model": settings.model.model or "provider default",
            },
            "substitution": [
                {"name": r.name, "match": r.match} for r in settings.substitution.external
            ],
            "isolation": settings.supabase.isolation.value,
            "probe": {
                "entrypoint": observed.entrypoint,
                "reachable": observed.reachable_count,
                "total": len(observed.screens),
                "divergences": len(observed.divergences),
            }
            if observed
            else None,
            "last_run": {
                "id": run.id,
                "status": run.status.value,
                "counts": run.counts,
                "duration_seconds": run.duration_seconds,
            }
            if run
            else None,
            "authorization_possible": len(settings.test_users) >= 2,
        }

    @app.get("/api/surfaces")
    def surfaces() -> dict[str, Any]:
        """The full surface map."""
        result = scan_result()
        if result is None:
            return {"surfaces": [], "warnings": []}
        return {
            "surfaces": [
                s.model_dump(mode="json", exclude_none=True) for s in result.all_surfaces()
            ],
            "warnings": [w.model_dump(mode="json") for w in result.warnings],
            "tables": [t.model_dump(mode="json") for t in result.tables],
        }

    @app.get("/api/gaps")
    def gaps_endpoint() -> dict[str, Any]:
        """The ranked gap map."""
        gaps = gap_map()
        if gaps is None:
            return {"gaps": [], "notes": ["no scan yet"], "coverage": None}
        return gaps.model_dump(mode="json", exclude_none=True)

    @app.get("/api/scenarios")
    def scenarios_endpoint() -> dict[str, Any]:
        """Every scenario specification."""
        index, problems = scenario_index()
        return {
            "scenarios": [s.model_dump(mode="json", exclude_none=True) for s in index.scenarios],
            "problems": problems,
        }

    @app.get("/api/intent")
    def intent_endpoint() -> dict[str, Any]:
        """Captured product intent."""
        if not paths.intent.is_file():
            return {}
        return read_model(paths.intent, ProductIntent).model_dump(mode="json", exclude_none=True)

    @app.get("/api/runs")
    def runs_endpoint() -> dict[str, Any]:
        """Run history, newest first."""
        records = sorted(paths.runs.glob("*.yaml"), reverse=True) if paths.runs.is_dir() else []
        out: list[dict[str, Any]] = []
        for path in records[:25]:
            try:
                record = read_model(path, RunRecord)
            except Exception:
                continue
            out.append(
                {
                    "id": record.id,
                    "status": record.status.value,
                    "counts": record.counts,
                    "duration_seconds": record.duration_seconds,
                    "entrypoint": record.entrypoint,
                    "isolation": record.isolation,
                    "started_at": record.started_at,
                    "notes": record.notes,
                    "results": [
                        r.model_dump(mode="json", exclude_none=True) for r in record.results
                    ],
                }
            )
        return {"runs": out}

    @app.get("/api/job")
    def job_endpoint() -> dict[str, Any]:
        """The current or most recent background job."""
        current = jobs.current
        return current.as_dict() if current else {"state": "idle", "events": []}

    @app.get("/api/job/stream")
    def job_stream() -> StreamingResponse:
        """Server-sent events for the active job.

        Polls the in-memory log rather than pushing, because the job runs on a
        worker thread and a queue between them would add machinery without
        changing what the page sees.
        """
        import time

        def generate() -> Iterator[str]:
            sent = 0
            idle_ticks = 0
            while idle_ticks < 600:
                current = jobs.current
                if current is not None:
                    events = list(current.events)
                    while sent < len(events):
                        yield f"data: {json.dumps(events[sent].as_dict())}\n\n"
                        sent += 1
                    if current.state != "running":
                        yield f"data: {json.dumps({'level': 'done', 'message': current.state})}\n\n"
                        return
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                time.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream")

    # --------------------------------------------------------------- write

    @app.post("/api/scenarios/{scenario_id}/status")
    def set_status(scenario_id: str, payload: dict[str, str]) -> dict[str, Any]:
        """Approve or reject one scenario.

        A scenario with unanswered questions is refused, exactly as on the CLI —
        approving it would produce a test that passes vacuously.
        """
        from testtrout.authoring.store import save

        index, _ = scenario_index()
        scenario = index.get(scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail=f"no scenario {scenario_id!r}")

        target = payload.get("status", "")
        if target not in {"approved", "rejected", "draft"}:
            raise HTTPException(status_code=400, detail=f"unsupported status {target!r}")
        if target == "approved" and not scenario.ready_to_approve:
            raise HTTPException(
                status_code=409,
                detail="This scenario has unanswered questions. Answering them first is the "
                "point: approving it now would produce a test that passes vacuously.",
            )

        scenario.status = ScenarioStatus(target)
        save(paths.scenarios, scenario)
        return scenario.model_dump(mode="json", exclude_none=True)

    # -------------------------------------------------------------- actions

    @app.post("/api/actions/scan")
    def action_scan() -> dict[str, Any]:
        """Re-analyse the repository."""

        def work(job: Job) -> dict[str, Any]:
            from testtrout.analysis.scanner import scan as run_scan

            job.log("analysing the repository")
            result = run_scan(paths.root)
            paths.ensure()
            write_model(paths.surfaces, result)
            write_model(paths.config, apply_scan(config(), result))
            job.log(
                f"found {sum(result.counts.values())} surfaces across "
                f"{len(result.counts)} categories"
            )
            for warning in result.warnings:
                job.log(warning.message, level="warn")
            return {"counts": result.counts}

        return start("scan", work)

    @app.post("/api/actions/probe")
    def action_probe(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Explore the running deployment."""
        options = payload or {}

        def work(job: Job) -> dict[str, Any]:
            from testtrout.deployment.prober import probe as run_probe
            from testtrout.deployment.reconcile import reconcile

            result = scan_result()
            settings = config()
            entrypoint = settings.entrypoint(options.get("entrypoint"))
            if result is None or entrypoint is None:
                raise RuntimeError("scan and configure an entrypoint first")

            job.log(f"probing {entrypoint.url}")
            if not entrypoint.writable:
                job.log("read-only: mutating requests will be blocked", level="warn")
            observed = run_probe(result, settings, entrypoint, role=options.get("role"))
            observed.divergences.extend(reconcile(result, observed))
            write_model(paths.observed / f"{entrypoint.name}.yaml", observed)
            job.log(f"{observed.reachable_count}/{len(observed.screens)} screens reachable")
            for divergence in observed.divergences:
                job.log(f"{divergence.code}: {divergence.message}", level="warn")
            return {"reachable": observed.reachable_count}

        return start("probe", work)

    @app.post("/api/actions/propose")
    def action_propose(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Draft scenarios for the top-ranked gaps."""
        options = payload or {}

        def work(job: Job) -> dict[str, Any]:
            from testtrout.authoring import propose as authoring
            from testtrout.authoring.store import save
            from testtrout.llm.gateway import Gateway

            result = scan_result()
            gaps = gap_map()
            if result is None or gaps is None:
                raise RuntimeError("scan first")

            settings = config()
            intent = read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None
            gateway = None if options.get("no_model") else Gateway(settings.model, paths.cache)
            candidates = [
                g
                for g in gaps.ranked(ready_only=True)
                if not options.get("kind") or g.kind.value == options["kind"]
            ][: int(options.get("limit", 5))]

            paths.ensure()
            drafted = 0
            for candidate in candidates:
                job.log(f"drafting {candidate.title}")
                scenario, warnings = authoring.propose(
                    candidate,
                    result,
                    settings,
                    probe=probe_result(),
                    intent=intent,
                    gateway=gateway,
                )
                save(paths.scenarios, scenario)
                drafted += 1
                for warning in warnings:
                    job.log(warning, level="warn")
                for question in scenario.open_questions:
                    job.log(f"needs an answer: {question}", level="warn")
            return {"drafted": drafted}

        return start("propose", work)

    @app.post("/api/actions/generate")
    def action_generate() -> dict[str, Any]:
        """Compile approved scenarios into test files."""

        def work(job: Job) -> dict[str, Any]:
            from testtrout.authoring.base import select_emitter
            from testtrout.authoring.store import save

            settings = config()
            index, _ = scenario_index()
            approved = index.by_status(ScenarioStatus.APPROVED)
            if not approved:
                raise RuntimeError("no approved scenarios — approve some first")

            shared: dict[str, str] = {}
            written = 0
            for scenario in approved:
                emitter = select_emitter(scenario)
                if emitter is None:
                    job.log(f"no emitter for {scenario.kind.value}", level="warn")
                    continue
                emitted = emitter.emit(scenario, settings)
                destination = paths.root / emitted.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(emitted.content, encoding="utf-8")
                shared.update(emitted.shared)
                scenario.emitted_to = emitted.path
                save(paths.scenarios, scenario)
                written += 1
                job.log(f"wrote {emitted.path}")
                for note in emitted.notes:
                    job.log(note, level="warn")

            for rel, content in shared.items():
                destination = paths.root / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                job.log(f"wrote {rel}")
            return {"written": written}

        return start("generate", work)

    @app.post("/api/actions/run")
    def action_run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute the suite."""
        options = payload or {}

        def work(job: Job) -> dict[str, Any]:
            from testtrout.runtime.runner import run as execute

            settings = config()
            entrypoint = settings.entrypoint(options.get("entrypoint"))
            if entrypoint is None:
                raise RuntimeError("configure an entrypoint first")
            index, _ = scenario_index()

            job.log(f"running against {entrypoint.url}")
            paths.ensure()
            record = execute(
                settings,
                entrypoint,
                index,
                paths.root,
                report_dir=paths.runs / "latest",
                scenario_id=options.get("scenario"),
            )
            write_model(paths.runs / f"{record.id}.yaml", record, header=False)

            for note in record.notes:
                job.log(note, level="warn")
            for result in record.results:
                level = (
                    "error"
                    if result.classification.is_product_signal
                    else ("info" if result.passed else "warn")
                )
                job.log(
                    f"{result.classification.value}: "
                    f"{result.scenario_id.replace('scenario:', '')}"
                    + (f" — {result.message}" if result.message else ""),
                    level=level,
                )
            job.log(f"run finished: {record.status.value.upper()}")
            return {"status": record.status.value, "id": record.id}

        return start("run", work)

    @app.post("/api/actions/certify")
    def action_certify(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Prove scenarios are deterministic."""
        options = payload or {}

        def work(job: Job) -> dict[str, Any]:
            from testtrout.authoring.store import save
            from testtrout.runtime.runner import apply_verdicts
            from testtrout.runtime.runner import certify as run_certification

            settings = config()
            entrypoint = settings.entrypoint(options.get("entrypoint"))
            if entrypoint is None:
                raise RuntimeError("configure an entrypoint first")
            index, _ = scenario_index()

            attempts = int(options.get("runs") or settings.run.certification_runs)
            job.log(f"running the suite {attempts} times to check for flakiness")
            verdicts, _ = run_certification(
                settings, entrypoint, index, paths.root, paths.runs, runs=attempts
            )
            changes = apply_verdicts(index, verdicts)
            for scenario in index.scenarios:
                if scenario.id in changes:
                    save(paths.scenarios, scenario)
                    job.log(f"{scenario.id.replace('scenario:', '')} → {scenario.status.value}")
            return {"changes": {k: v.value for k, v in changes.items()}}

        return start("certify", work)

    # ------------------------------------------------------------------ ui

    @app.get("/")
    def index() -> FileResponse:
        """Serve the single-page application."""
        return FileResponse(STATIC / "index.html")

    return app
