"""The local web application.

Small on purpose. The interface is a conversation with a sidebar of artifacts,
so the API has exactly the shape that implies: a few routes that render an
artifact, one that accepts the facts a person can supply, and the job plumbing
the conversation narrates.

Three rules constrain what it may do:

*It cannot change a deployment's safety posture.* There is no route that marks
an entrypoint disposable. One click away from pointing test writes at
production is exactly the mistake the guard exists to prevent, so that stays a
deliberate edit to a committed file.

*It does not execute work itself.* Actions enqueue a job; the worker runs it.
That keeps request handling fast and makes per-project serialisation a property
of the queue rather than something each handler has to remember.

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
from testtrout.app.session import Session
from testtrout.domain.artifact import Artifact, ArtifactKind
from testtrout.domain.run import RunRecord
from testtrout.store import QaPaths

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

    def session_for(repo_id: int) -> Session:
        return Session(paths=paths_for(repo_id))

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

    # ------------------------------------------------------- project state

    @app.get("/api/projects/{repo_id}")
    def project(repo_id: int) -> dict[str, Any]:
        """The header: what this project is and whether anything is running."""
        session = session_for(repo_id)
        record = registry.get(repo_id)
        scan = session.scan
        entrypoint = session.config.entrypoint()
        active = queue.active(repo_id)
        return {
            "id": repo_id,
            "name": record.name if record else "",
            "path": str(session.paths.root),
            "scanned": scan is not None,
            "stack": " + ".join(x for x in (scan.project.framework, scan.project.backend) if x)
            if scan
            else "",
            "deployment": entrypoint.url if entrypoint else "",
            "writable": bool(entrypoint and entrypoint.writable),
            "busy": active.kind if active else "",
        }

    # ----------------------------------------------------------- artifacts

    @app.get("/api/projects/{repo_id}/artifacts")
    def artifacts(repo_id: int) -> dict[str, Any]:
        """The sidebar: what this project has produced so far.

        A chat is a good way to drive work and a terrible way to store it.
        Everything durable is listed here instead, so nothing important is
        three screens up in the scrollback.
        """
        session = session_for(repo_id)
        scan = session.scan
        plan = session.plan
        sheet = session.facts
        index, _ = _scenarios(session)
        certified = [s for s in index.scenarios if s.status.value == "certified"]

        out = [
            Artifact(
                kind=ArtifactKind.MAP,
                ready=scan is not None,
                summary=(
                    f"{len(scan.screens)} pages · {len(scan.endpoints)} endpoints · "
                    f"{len(scan.tables)} tables"
                    if scan
                    else "not read yet"
                ),
            ),
            Artifact(
                kind=ArtifactKind.FACTS,
                ready=bool(sheet.facts),
                summary=(
                    f"{len(sheet.outstanding)} still to fill in"
                    if sheet.outstanding
                    else "everything I asked for is filled in"
                ),
                attention=len(sheet.outstanding),
            ),
            Artifact(
                kind=ArtifactKind.PLAN,
                ready=bool(plan.candidates),
                summary=(
                    f"{len(plan.ready)} ready · {len(plan.waiting)} waiting"
                    if plan.candidates
                    else "nothing worked out yet"
                ),
            ),
            Artifact(
                kind=ArtifactKind.SUITE,
                ready=bool(certified),
                summary=(f"{len(certified)} proven test(s)" if certified else "no baseline yet"),
            ),
        ]
        return {
            "artifacts": [
                a.model_dump(mode="json") | {"label": a.label, "icon": a.kind.icon} for a in out
            ]
        }

    @app.get("/api/projects/{repo_id}/map")
    def project_map(repo_id: int) -> dict[str, Any]:
        """What the project is: pages, APIs, storage, deployment."""
        session = session_for(repo_id)
        scan = session.scan
        if scan is None:
            return {"ready": False}

        overview = session.overview
        config = session.config
        entrypoint = config.entrypoint()
        return {
            "ready": True,
            "summary": overview.summary if overview else "",
            "stack": " + ".join(x for x in (scan.project.framework, scan.project.backend) if x),
            "root": scan.project.root,
            "pages": [
                {"path": s.path, "component": s.component, "params": s.params} for s in scan.screens
            ],
            "endpoints": [
                {"path": e.path, "methods": e.methods, "file": e.location.file}
                for e in scan.endpoints
            ],
            "storage": {
                "tables": [t.name for t in scan.tables],
                "policies": len(scan.policies),
                "operations": len(scan.data_operations),
                "backend": scan.project.backend or "not detected",
            },
            "deployment": {
                "url": entrypoint.url if entrypoint else "",
                "api_url": entrypoint.api_url if entrypoint else "",
                "api_base_var": scan.project.api_base_var,
                "third_parties": sorted({e.vendor for e in scan.externals}),
            },
        }

    @app.get("/api/projects/{repo_id}/facts")
    def facts(repo_id: int) -> dict[str, Any]:
        """The optional form. Every field concrete, every field skippable."""
        sheet = session_for(repo_id).facts
        return {
            "outstanding": [f.model_dump(mode="json") for f in sheet.outstanding],
            "answered": [f.model_dump(mode="json") for f in sheet.answered],
        }

    @app.post("/api/projects/{repo_id}/facts")
    def save_facts(repo_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Save whatever a person could supply, and re-derive from it.

        Partial answers are the normal case, not an error state. Whatever
        arrives is applied; everything else stays outstanding and the plan is
        recomputed so the effect of what was given is visible immediately.
        """
        from testtrout.app import facts as fact_writer

        paths = paths_for(repo_id)
        try:
            applied = fact_writer.apply(paths, payload)
        except fact_writer.FactError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Configuration changed, so what is testable changed. Re-deriving is
        # cheap and avoids showing a plan that contradicts the form above it.
        from testtrout.app import session as pipeline

        pipeline.derive(Session(paths=paths))
        return {"applied": applied}

    @app.get("/api/projects/{repo_id}/plan")
    def plan(repo_id: int) -> dict[str, Any]:
        """What can be tested now, and what each blocked item needs."""
        session = session_for(repo_id)
        current = session.plan
        sheet = session.facts
        labels = {f.id: f.label for f in sheet.facts}
        return {
            "counts": current.counts(),
            "ready": [c.model_dump(mode="json") for c in current.ready],
            "waiting": [
                c.model_dump(mode="json") | {"needs_labels": [labels.get(n, n) for n in c.needs]}
                for c in current.waiting
            ],
        }

    @app.get("/api/projects/{repo_id}/suite")
    def suite(repo_id: int) -> dict[str, Any]:
        """The baseline: every test, and what it is doing.

        A test that has been proven is protecting something. One that has not
        is not, and the difference is the only thing worth reading here.
        """
        from testtrout.planning import tests_view

        session = session_for(repo_id)
        index, _ = _scenarios(session)
        records: list[RunRecord] = []
        if session.paths.runs.is_dir():
            from testtrout.store import read_model

            for path in sorted(session.paths.runs.glob("*.yaml"), reverse=True)[:10]:
                try:
                    records.append(read_model(path, RunRecord))
                except Exception:
                    continue

        views = tests_view.build(index, tests_view.latest_results(records), [])
        return {
            "tests": [
                v.model_dump(mode="json") | {"label": v.state.label, "flagged": v.flagged}
                for v in views
            ],
            "counts": {
                state.value: sum(1 for v in views if v.state is state)
                for state in tests_view.TestState
            },
        }

    def _scenarios(session: Session):  # type: ignore[no-untyped-def]
        from testtrout.authoring.store import load_all

        return load_all(session.paths.scenarios)

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
