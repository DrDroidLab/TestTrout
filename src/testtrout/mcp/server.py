"""MCP server, scoped to a single project.

Bound to one project root at startup rather than taking a path on every call.
That removes a whole class of agent mistake — operating on the wrong repository
— and lets resources have stable, memorable URIs.

The tools mirror the CLI exactly, because an agent and a person doing the same
work should be doing the same thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from testtrout import __version__
from testtrout.app.session import Session
from testtrout.store import QaPaths, load_dotenv

INSTRUCTIONS = """\
Build and run a baseline regression suite for a web application.

A baseline is what the deployment does today. This tool records that and
asserts it keeps happening. It does not know whether current behaviour is
correct, and it will not ask — so do not offer it product knowledge, and do not
invent expectations on the user's behalf.

Order: `look` (read the code, ask the deployment, work out what is testable),
then `facts` to see what concrete values are still missing, then `build` to
write and prove the baseline, then `run` to check for changes.

Rules that matter:
- Never mark a deployment disposable on the user's behalf. Writes against a
  non-disposable deployment are blocked, and that guard is why this is safe to
  point at production.
- Everything `facts` asks for is a concrete value the user holds: a URL, an
  account, a real id. If you can work an answer out from the repository, it is
  a bug that it was asked — say so rather than guessing.
- On a run, read `status` before `results`. An `inconclusive` run says nothing
  about the product. Only `assertion_failure` means behaviour changed.

Full state is available as resources: trout://map, trout://facts, trout://plan,
trout://config, trout://scenarios.
"""


def build_server(root: Path) -> MCPServer:
    """Create an MCP server bound to one project."""
    paths = QaPaths(root=root.resolve())
    load_dotenv(paths.root)

    server = MCPServer(
        name="testtrout",
        title="TestTrout",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def session() -> Session:
        return Session(paths=paths)

    @server.tool()
    def look(rescan: bool = True) -> dict[str, Any]:
        """Read the code, ask the deployment, and work out what can be tested.

        Free and offline for the code half; the deployment half sends only GET
        requests, so this is safe against production.

        Returns counts, plus what is still needed from the user.
        """
        from testtrout.app import session as pipeline

        sheet, plan = pipeline.refresh(session(), rescan=rescan)
        return {
            "counts": plan.counts(),
            "asking": [f.model_dump(mode="json") for f in sheet.outstanding],
        }

    @server.tool()
    def facts() -> dict[str, Any]:
        """What concrete values are still needed, and what each unlocks.

        Every item is something only the user can supply. Ask them; do not
        invent a value, and do not skip an item because it looks guessable.
        """
        sheet = session().facts
        return {
            "outstanding": [f.model_dump(mode="json") for f in sheet.outstanding],
            "answered": [f.id for f in sheet.answered],
        }

    @server.tool()
    def set_facts(values: dict[str, Any]) -> dict[str, Any]:
        """Save values the user supplied, then re-derive the plan.

        Partial input is expected. Keys are fact ids; an account takes
        ``{"email": ..., "password": ...}``. Secrets go to a gitignored .env
        and never reach committed configuration.
        """
        from testtrout.app import facts as fact_writer
        from testtrout.app import session as pipeline

        try:
            applied = fact_writer.apply(paths, values)
        except fact_writer.FactError as exc:
            return {"error": str(exc)}
        pipeline.derive(session())
        return {"applied": applied, "counts": session().plan.counts()}

    @server.tool()
    def plan() -> dict[str, Any]:
        """What can be tested now, and what each blocked item is waiting for."""
        current = session().plan
        sheet = session().facts
        labels = {f.id: f.label for f in sheet.facts}
        return {
            "counts": current.counts(),
            "ready": [c.model_dump(mode="json") for c in current.ready],
            "waiting": [
                c.model_dump(mode="json") | {"needs": [labels.get(n, n) for n in c.needs]}
                for c in current.waiting
            ],
        }

    @server.tool()
    def build(limit: int = 20) -> dict[str, Any]:
        """Write a test for everything ready, prove it, and keep what passes.

        Writes files into the user's repository under tests/trout/ and runs
        them against the configured deployment. Only tests that pass are kept.
        """
        from testtrout.app import session as pipeline

        try:
            return pipeline.build(session(), limit=limit)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @server.tool()
    def run(env: str | None = None) -> dict[str, Any]:
        """Re-run the baseline and report what changed.

        Read ``status`` first. Only results classified ``assertion_failure``
        mean the deployment stopped doing something it used to do.
        """
        from testtrout.authoring.store import load_all
        from testtrout.runtime.runner import run as execute
        from testtrout.store import write_model

        current = session()
        config = current.config
        entrypoint = config.entrypoint(env)
        if entrypoint is None:
            return {"error": "no deployment configured", "fix": "set the deployment_url fact"}

        index, _ = load_all(paths.scenarios)
        paths.ensure()
        record = execute(config, entrypoint, index, paths.root, report_dir=paths.runs / "latest")
        write_model(paths.runs / f"{record.id}.yaml", record, header=False)
        return {
            "status": record.status.value,
            "changed": [
                r.model_dump(mode="json")
                for r in record.results
                if r.classification.is_product_signal
            ],
            "counts": record.counts,
            "notes": record.notes,
        }

    @server.tool()
    def suite() -> dict[str, Any]:
        """Every test in the baseline and what it is currently doing."""
        from testtrout.authoring.store import load_all
        from testtrout.domain.run import RunRecord
        from testtrout.planning import tests_view
        from testtrout.store import read_model

        index, _ = load_all(paths.scenarios)
        records = []
        if paths.runs.is_dir():
            for path in sorted(paths.runs.glob("*.yaml"), reverse=True)[:10]:
                try:
                    records.append(read_model(path, RunRecord))
                except Exception:
                    continue
        views = tests_view.build(index, tests_view.latest_results(records))
        return {"tests": [v.model_dump(mode="json") for v in views]}

    # ----------------------------------------------------------- resources

    @server.resource("trout://map", mime_type="application/yaml")
    def surfaces_resource() -> str:
        """The last scan."""
        return paths.surfaces.read_text(encoding="utf-8") if paths.surfaces.is_file() else ""

    @server.resource("trout://facts", mime_type="application/yaml")
    def facts_resource() -> str:
        """What the tool still needs."""
        return paths.facts.read_text(encoding="utf-8") if paths.facts.is_file() else ""

    @server.resource("trout://plan", mime_type="application/yaml")
    def plan_resource() -> str:
        """What can be tested."""
        return paths.plan.read_text(encoding="utf-8") if paths.plan.is_file() else ""

    @server.resource("trout://config", mime_type="application/yaml")
    def config_resource() -> str:
        """Repository configuration."""
        return paths.config.read_text(encoding="utf-8") if paths.config.is_file() else ""

    @server.resource("trout://scenarios", mime_type="application/json")
    def scenarios_resource() -> str:
        """Every scenario specification."""
        from testtrout.authoring.store import load_all

        index, _ = load_all(paths.scenarios)
        return json.dumps(index.model_dump(mode="json"), indent=2)

    return server


def run(root: Path) -> None:
    """Run the server over stdio."""
    build_server(root).run()
