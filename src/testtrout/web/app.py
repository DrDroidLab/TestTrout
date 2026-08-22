"""The local web application.

Multi-repository and database-backed. Every endpoint calls the same library
function the CLI calls, so behaviour that differs between the two interfaces is
a bug rather than a feature.

Three rules constrain what the API may do:

*It cannot change a deployment's safety posture.* There is no endpoint that
marks an entrypoint disposable. One click away from pointing test writes at
production is exactly the mistake the guard exists to prevent, so that stays a
deliberate edit to a committed file.

*It does not execute work itself.* Actions enqueue a job; the worker runs it.
That keeps request handling fast and makes per-repository serialisation a
property of the queue rather than something each handler has to remember.

*It binds to loopback.* This reads a developer's credentials and can drive
their deployments; it is not something to expose on a network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from testtrout import __version__
from testtrout.app import Database, JobQueue, RepoRegistry
from testtrout.app.queue import UnknownJobKindError
from testtrout.app.repos import RepoError
from testtrout.domain.config import Config
from testtrout.domain.gap import GapMap
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import ProbeResult
from testtrout.domain.question import QuestionLog
from testtrout.domain.run import RunRecord
from testtrout.domain.scenario import ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import ScanResult
from testtrout.store import QaPaths, read_model, write_model

STATIC = Path(__file__).parent / "static"


def create_app(database: Database | None = None) -> FastAPI:
    """Build the application over a storage database."""
    db = database or Database()
    registry = RepoRegistry(db)
    queue = JobQueue(db)

    app = FastAPI(title="TestTrout", version=__version__, docs_url=None, redoc_url=None)
    app.state.database = db

    # ------------------------------------------------------------- helpers

    def paths_for(repo_id: int) -> QaPaths:
        try:
            return registry.paths(repo_id)
        except RepoError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def config_of(paths: QaPaths) -> Config:
        return read_model(paths.config, Config) if paths.config.is_file() else Config()

    def scan_of(paths: QaPaths) -> ScanResult | None:
        return read_model(paths.surfaces, ScanResult) if paths.surfaces.is_file() else None

    def probe_of(paths: QaPaths) -> ProbeResult | None:
        entrypoint = config_of(paths).entrypoint()
        if entrypoint is None:
            return None
        path = paths.observed / f"{entrypoint.name}.yaml"
        return read_model(path, ProbeResult) if path.is_file() else None

    def scenarios_of(paths: QaPaths) -> tuple[ScenarioIndex, list[str]]:
        from testtrout.authoring.store import load_all

        return load_all(paths.scenarios)

    def gaps_of(paths: QaPaths) -> GapMap | None:
        from testtrout.planning import gaps as planner
        from testtrout.planning.existing_tests import detect

        scan = scan_of(paths)
        if scan is None:
            return None
        config = config_of(paths)
        index, _ = scenarios_of(paths)
        return planner.build(
            scan,
            intent=read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None,
            probe=probe_of(paths),
            existing=detect(paths.root, scan),
            roles=[u.role for u in config.test_users],
            scenarios=index,
        )

    # --------------------------------------------------------------- repos

    @app.get("/api/repos")
    def list_repos() -> dict[str, Any]:
        """Every linked repository, with just enough to render the picker."""
        out = []
        for record in registry.all():
            active = queue.active(record.id) if record.id else None
            out.append(
                record.model_dump(mode="json")
                | {
                    "exists": record.exists,
                    "busy": active is not None,
                    "busy_with": active.kind if active else None,
                }
            )
        return {"repos": out, "version": __version__, "database": str(db.path)}

    @app.post("/api/repos")
    def link_repo(payload: dict[str, Any]) -> dict[str, Any]:
        """Add a project: a repository, and the URL it is deployed at.

        The deployment is recorded *before* the initial scan is queued. A scan
        that runs without one can only read code; with one it can also probe
        the running system, and probing is what turns a guess into evidence.
        """
        source = payload.get("source", "local")
        try:
            if source == "github":
                from testtrout.app import github as gh

                token = gh.read_token()
                if not token:
                    raise HTTPException(
                        status_code=400,
                        detail="No GitHub token available. Set GITHUB_TOKEN, run `gh auth "
                        "login`, or store one with `trout github-login`.",
                    )
                record = registry.link_github(str(payload.get("slug", "")), token)
            else:
                target = str(payload.get("path", "")).strip()
                if not target:
                    raise HTTPException(status_code=400, detail="a path is required")
                record = registry.link_local(Path(target), name=payload.get("name"))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        deployment_url = str(payload.get("deployment_url", "")).strip()
        if deployment_url:
            from testtrout.app import settings

            try:
                settings.apply(
                    QaPaths(root=Path(record.path)),
                    {"entrypoints": [{"name": "deployment", "url": deployment_url}]},
                )
            except settings.SettingsError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        # A freshly linked repository is useless until it is scanned.
        registry.queue_initial_scan(record)
        return record.model_dump(mode="json")

    @app.delete("/api/repos/{repo_id}")
    def unlink_repo(repo_id: int) -> dict[str, Any]:
        """Forget a repository. Files are never deleted from here."""
        return {"unlinked": registry.unlink(repo_id, delete_files=False)}

    @app.get("/api/github/repos")
    def github_repos() -> dict[str, Any]:
        """Repositories the configured token can see."""
        from testtrout.app import github as gh

        token = gh.read_token()
        if not token:
            return {"authenticated": False, "repos": []}
        try:
            remotes = gh.list_repos(token)
            return {
                "authenticated": True,
                "account": gh.whoami(token),
                "repos": [
                    {
                        "full_name": r.full_name,
                        "private": r.private,
                        "description": r.description,
                        "pushed_at": r.pushed_at,
                    }
                    for r in remotes
                ],
            }
        except gh.GitHubError as exc:
            return {"authenticated": False, "error": str(exc), "repos": []}

    # ------------------------------------------------------- per-repo read

    @app.get("/api/repos/{repo_id}/overview")
    def overview(repo_id: int) -> dict[str, Any]:
        """Everything the dashboard needs for one repository."""
        paths = paths_for(repo_id)
        record = registry.get(repo_id)
        scan = scan_of(paths)
        config = config_of(paths)
        index, problems = scenarios_of(paths)
        gaps = gaps_of(paths)
        observed = probe_of(paths)
        history = registry.run_history(repo_id, limit=20)

        return {
            "repo": record.model_dump(mode="json") if record else None,
            "scanned": scan is not None,
            "scan_stale": (scan is not None and scan.tool_version != __version__),
            "scan_empty": scan is not None and sum(scan.counts.values()) == 0,
            "project": scan.project.model_dump(mode="json") if scan else None,
            "counts": scan.counts if scan else {},
            "coverage": (
                gaps.coverage.model_dump(mode="json")
                | {
                    "percent": gaps.coverage.percent,
                    "critical_percent": gaps.coverage.critical_percent,
                }
                if gaps
                else None
            ),
            "gap_total": len(gaps.gaps) if gaps else 0,
            "gap_ready": len([g for g in gaps.gaps if g.ready]) if gaps else 0,
            "notes": gaps.notes if gaps else [],
            "scenarios": index.counts,
            "scenario_problems": problems,
            "entrypoints": [
                {"name": e.name, "url": e.url, "disposable": e.disposable, "writable": e.writable}
                for e in config.entrypoints
            ],
            "test_users": [u.role for u in config.test_users],
            "model": {
                "provider": config.model.provider.value,
                "model": config.model.model or "provider default",
            },
            "substitution": [
                {"name": r.name, "match": r.match} for r in config.substitution.external
            ],
            "isolation": config.supabase.isolation.value,
            "probe": (
                {
                    "entrypoint": observed.entrypoint,
                    "reachable": observed.reachable_count,
                    "total": len(observed.screens),
                    "divergences": len(observed.divergences),
                }
                if observed
                else None
            ),
            "history": [h.model_dump(mode="json") for h in history],
            "authorization_possible": len(config.test_users) >= 2,
            "open_questions": len(_questions(paths).open_questions()),
        }

    @app.get("/api/repos/{repo_id}/surfaces")
    def surfaces(repo_id: int) -> dict[str, Any]:
        """The full surface map."""
        scan = scan_of(paths_for(repo_id))
        if scan is None:
            return {"surfaces": [], "warnings": [], "tables": []}
        return {
            "surfaces": [s.model_dump(mode="json", exclude_none=True) for s in scan.all_surfaces()],
            "warnings": [w.model_dump(mode="json") for w in scan.warnings],
            "tables": [t.model_dump(mode="json") for t in scan.tables],
        }

    @app.get("/api/repos/{repo_id}/gaps")
    def gaps(repo_id: int) -> dict[str, Any]:
        """The ranked gap map."""
        result = gaps_of(paths_for(repo_id))
        return (
            result.model_dump(mode="json", exclude_none=True)
            if result
            else {"gaps": [], "notes": ["this repository has not been scanned"], "coverage": None}
        )

    @app.get("/api/repos/{repo_id}/scenarios")
    def scenarios(repo_id: int) -> dict[str, Any]:
        """Every scenario specification."""
        index, problems = scenarios_of(paths_for(repo_id))
        return {
            "scenarios": [s.model_dump(mode="json", exclude_none=True) for s in index.scenarios],
            "problems": problems,
        }

    @app.get("/api/repos/{repo_id}/tests")
    def tests(repo_id: int) -> dict[str, Any]:
        """Every test, with its last result and anything flagged against it.

        Joined here rather than in the browser: a scenario file, a run record,
        and the question log each hold one third of the answer, and a list that
        does not join them shows tests with no indication of which are actually
        protecting anything.
        """
        from testtrout.planning import tests_view

        paths = paths_for(repo_id)
        index, _ = scenarios_of(paths)
        records = []
        if paths.runs.is_dir():
            for path in sorted(paths.runs.glob("*.yaml"), reverse=True)[:10]:
                try:
                    records.append(read_model(path, RunRecord))
                except Exception:
                    continue

        views = tests_view.build(index, tests_view.latest_results(records), _questions(paths).questions)
        return {
            "tests": [
                view.model_dump(mode="json") | {"label": view.state.label, "flagged": view.flagged}
                for view in views
            ],
            "counts": {
                state.value: sum(1 for v in views if v.state is state)
                for state in tests_view.TestState
            },
        }

    @app.get("/api/repos/{repo_id}/intent")
    def intent(repo_id: int) -> dict[str, Any]:
        """Captured product intent."""
        paths = paths_for(repo_id)
        if not paths.intent.is_file():
            return {}
        return read_model(paths.intent, ProductIntent).model_dump(mode="json", exclude_none=True)

    @app.get("/api/repos/{repo_id}/runs")
    def runs(repo_id: int) -> dict[str, Any]:
        """Full run records from the repository, newest first."""
        paths = paths_for(repo_id)
        files = sorted(paths.runs.glob("*.yaml"), reverse=True) if paths.runs.is_dir() else []
        out: list[dict[str, Any]] = []
        for path in files[:25]:
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

    # ------------------------------------------------------ per-repo write

    @app.post("/api/repos/{repo_id}/scenarios/{scenario_id}/status")
    def set_status(repo_id: int, scenario_id: str, payload: dict[str, str]) -> dict[str, Any]:
        """Approve or reject one scenario.

        A scenario with unanswered questions is refused, exactly as on the CLI —
        approving it would produce a test that passes vacuously.
        """
        from testtrout.authoring.store import save

        paths = paths_for(repo_id)
        index, _ = scenarios_of(paths)
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

    @app.get("/api/repos/{repo_id}/project")
    def project(repo_id: int) -> dict[str, Any]:
        """What this project is, in product language, and how much is tested."""
        from testtrout.planning.overview import build as build_overview
        from testtrout.planning.readiness import assess

        paths = paths_for(repo_id)
        scan = scan_of(paths)
        if scan is None:
            return {"scanned": False}

        index, _ = scenarios_of(paths)
        overview = build_overview(scan, index, assess(config_of(paths), scan))
        coverage = overview.coverage

        # Coverage moves every time a test is certified, so it is recomputed
        # here rather than read back; the stored overview is the snapshot from
        # the last scan and serves only as the "before" side. Practically that
        # means ``delta.newly_covered`` answers "what has the suite gained
        # since I last scanned", and ``still_missing`` answers "what is left" —
        # which is the question a rescan is actually asking.
        from testtrout.domain.overview import ProjectOverview
        from testtrout.planning.overview import delta as compare

        previous = read_model(paths.overview, ProjectOverview) if paths.overview.is_file() else None
        changed = compare(previous, overview)

        return {
            "scanned": True,
            "delta": changed.model_dump(mode="json") | {"has_changes": changed.has_changes},
            "summary": overview.summary,
            "stack": overview.stack,
            "pages": [p.model_dump(mode="json") for p in overview.pages],
            "apis": [a.model_dump(mode="json") for a in overview.apis],
            "transactions": [t.model_dump(mode="json") for t in overview.transactions],
            "needs_from_you": overview.needs_from_you,
            "coverage": coverage.model_dump(mode="json")
            | {
                "pages_percent": coverage.pages_percent,
                "apis_percent": coverage.apis_percent,
                "transactions_percent": coverage.transactions_percent,
                "overall_percent": coverage.overall_percent,
            },
        }

    # ----------------------------------------------------------- questions

    def _questions(paths: QaPaths) -> QuestionLog:
        return (
            read_model(paths.questions, QuestionLog) if paths.questions.is_file() else QuestionLog()
        )

    @app.get("/api/repos/{repo_id}/questions")
    def questions(repo_id: int) -> dict[str, Any]:
        """What the tool needs answered, most consequential first."""
        log = _questions(paths_for(repo_id))
        return {
            "counts": log.counts,
            "open": [
                q.model_dump(mode="json") | {"label": q.kind.label, "blocks": q.kind.blocks_work}
                for q in log.open_questions()
            ],
            "answered": [q.model_dump(mode="json") for q in log.questions if not q.open],
        }

    @app.post("/api/repos/{repo_id}/questions/{question_id}")
    def answer_question(repo_id: int, question_id: str, payload: dict[str, str]) -> dict[str, Any]:
        """Record an answer, or dismiss a question as not worth answering."""
        paths = paths_for(repo_id)
        log = _questions(paths)
        question = log.get(question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=f"no question {question_id!r}")

        if payload.get("dismiss"):
            question.dismiss()
        else:
            answer = (payload.get("answer") or "").strip()
            if not answer:
                raise HTTPException(status_code=400, detail="an answer is required")
            question.resolve(answer)

        paths.ensure()
        write_model(paths.questions, log)
        return question.model_dump(mode="json")

    # ------------------------------------------------------------ settings

    @app.get("/api/repos/{repo_id}/config")
    def get_config(repo_id: int) -> dict[str, Any]:
        """Configuration, what it still needs, and what it can already do."""
        from testtrout.app import settings

        return settings.view(paths_for(repo_id)).as_dict()

    @app.put("/api/repos/{repo_id}/config")
    def put_config(repo_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial configuration change.

        Only the sections present are touched, so editing deployments cannot
        silently reset the model provider.
        """
        from testtrout.app import settings

        paths = paths_for(repo_id)
        try:
            settings.apply(paths, patch)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return settings.view(paths).as_dict()

    @app.post("/api/repos/{repo_id}/secrets")
    def put_secrets(repo_id: int, payload: dict[str, str]) -> dict[str, Any]:
        """Write credential values into the repository's gitignored .env.

        Values are never stored in configuration and never read back out; the
        response reports names only.
        """
        from testtrout.app import settings

        paths = paths_for(repo_id)
        try:
            written = settings.set_secrets(paths, payload)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"written": written} | settings.view(paths).as_dict()

    # ---------------------------------------------------------------- jobs

    @app.post("/api/repos/{repo_id}/jobs")
    def enqueue(repo_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue background work for a repository."""
        paths_for(repo_id)  # 404 early if the repository has gone
        try:
            job = queue.enqueue(repo_id, str(payload.get("kind", "")), payload.get("options"))
        except UnknownJobKindError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.model_dump(mode="json")

    @app.get("/api/jobs")
    def list_jobs(repo_id: int | None = None, limit: int = 25) -> dict[str, Any]:
        """Recent jobs, newest first."""
        return {
            "jobs": [
                j.model_dump(mode="json") | {"duration_seconds": j.duration_seconds}
                for j in queue.recent(repo_id=repo_id, limit=limit)
            ]
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        """One job, including its log."""
        job = queue.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        return job.model_dump(mode="json") | {"duration_seconds": job.duration_seconds}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int) -> dict[str, Any]:
        """Cancel a job that has not started yet."""
        return {"cancelled": queue.cancel(job_id)}

    @app.get("/api/jobs/{job_id}/stream")
    def stream_job(job_id: int) -> StreamingResponse:
        """Server-sent events for one job's log."""
        import time

        def generate() -> Iterator[str]:
            sent = 0
            for _ in range(4800):  # ~20 minutes at 0.25s
                job = queue.get(job_id)
                if job is None:
                    return
                while sent < len(job.log):
                    yield f"data: {json.dumps({'message': job.log[sent]})}\n\n"
                    sent += 1
                if job.state.terminal:
                    payload = {"done": True, "state": job.state.value, "error": job.error}
                    yield f"data: {json.dumps(payload)}\n\n"
                    return
                time.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream")

    # ------------------------------------------------------------------ ui

    @app.get("/")
    def index() -> FileResponse:
        """Serve the single-page application."""
        return FileResponse(STATIC / "index.html")

    return app
