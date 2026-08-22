"""MCP server implementation, scoped to a single project.

The server is bound to one project root at startup rather than taking a path on
every call. That removes a whole class of agent mistake — operating on the
wrong repository — and lets resources have stable, memorable URIs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from testtrout import __version__
from testtrout.domain.config import Config
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import ProbeResult
from testtrout.domain.scenario import ScenarioStatus
from testtrout.domain.surface import ScanResult
from testtrout.store import QaPaths, apply_scan, load_dotenv, read_model, write_model

INSTRUCTIONS = """\
Build and run a baseline regression test suite for a React/Next.js + Supabase
application.

Suggested order: `scan` (free, offline, safe on any repo) then `gaps` to see
what is missing and why. `probe` and `intent` make the ranking substantially
better. `propose` drafts scenarios; a human approves them; `generate` compiles them;
`run` executes them.

Rules that matter:
- Never mark a deployment disposable on the user's behalf. Writes against a
  non-disposable deployment are blocked at the network layer, and that guard is
  why this is safe to point at production.
- Approval belongs to the user. Draft and explain; let them decide.
- Quote the `reasons` on a gap rather than re-deriving your own ranking. They
  are deterministic and auditable.
- On a run, read `status` before `results`. An `inconclusive` run says nothing
  about the product. Only `assertion_failure` is a product signal.

Full state is available as resources: trout://surfaces, trout://gaps, trout://intent,
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

    def _config() -> Config:
        return read_model(paths.config, Config) if paths.config.is_file() else Config()

    def _scan_result() -> ScanResult | None:
        return read_model(paths.surfaces, ScanResult) if paths.surfaces.is_file() else None

    def _probe_result() -> ProbeResult | None:
        entrypoint = _config().entrypoint()
        if entrypoint is None:
            return None
        path = paths.observed / f"{entrypoint.name}.yaml"
        return read_model(path, ProbeResult) if path.is_file() else None

    def _needs_scan() -> dict[str, Any]:
        return {"error": "no scan found", "fix": "call scan first"}

    # ---------------------------------------------------------------- tools

    @server.tool()
    def scan(depth: int = 3) -> dict[str, Any]:
        """Analyse the repository. No API key, no network, safe on any repo.

        Returns counts and warnings. Read trout://surfaces for the full map.
        """
        from testtrout.analysis.scanner import scan as run_scan

        result = run_scan(paths.root, max_depth=depth)
        paths.ensure()
        write_model(paths.surfaces, result)

        write_model(paths.config, apply_scan(_config(), result))

        return {
            "framework": result.project.framework,
            "backend": result.project.backend,
            "auth": result.project.auth,
            "detected_from": result.project.detected_from,
            "counts": result.counts,
            "warnings": [
                {"code": w.code, "message": w.message, "location": str(w.location or "")}
                for w in result.warnings
            ],
        }

    @server.tool()
    def surfaces(kind: str | None = None, min_criticality: str | None = None) -> dict[str, Any]:
        """List what the scan found, filtered. Ordered by criticality."""
        result = _scan_result()
        if result is None:
            return _needs_scan()

        items = result.all_surfaces()
        if kind:
            items = [s for s in items if s.kind == kind]
        if min_criticality:
            from testtrout.domain.surface import Criticality

            floor = Criticality(min_criticality)
            items = [s for s in items if s.criticality.rank <= floor.rank]

        return {
            "count": len(items),
            "surfaces": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "criticality": s.criticality.value,
                    "reasons": s.criticality_reasons,
                    "location": str(s.location),
                }
                for s in items
            ],
        }

    @server.tool()
    def gaps(kind: str | None = None, ready_only: bool = False, limit: int = 20) -> dict[str, Any]:
        """Rank the tests this application is missing, and say why.

        Deterministic — no model is used. Every gap carries `reasons`: the named
        contributions that produced its score. Quote those rather than
        re-deriving a ranking of your own.
        """
        from testtrout.authoring.store import load_all
        from testtrout.planning import gaps as planner
        from testtrout.planning.existing_tests import detect

        result = _scan_result()
        if result is None:
            return _needs_scan()
        config = _config()
        captured = read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None
        index, _ = load_all(paths.scenarios)

        gap_map = planner.build(
            result,
            intent=captured,
            probe=_probe_result(),
            existing=detect(paths.root, result),
            roles=[u.role for u in config.test_users],
            scenarios=index,
        )
        if kind:
            gap_map.gaps = [g for g in gap_map.gaps if g.kind.value == kind]

        return {
            "coverage": {
                "percent": gap_map.coverage.percent,
                "critical_percent": gap_map.coverage.critical_percent,
                "policies": f"{gap_map.coverage.policies_covered}/{gap_map.coverage.policies_total}",
            },
            "notes": gap_map.notes,
            "gaps": [
                {
                    "id": g.id,
                    "kind": g.kind.value,
                    "title": g.title,
                    "criticality": g.criticality.value,
                    "score": round(g.score, 1),
                    "reasons": g.reasons,
                    "ready": g.ready,
                    "blockers": [{"code": b.code, "message": b.message} for b in g.blockers],
                }
                for g in gap_map.ranked(limit=limit, ready_only=ready_only)
            ],
        }

    @server.tool()
    def intent(describe: str | None = None) -> dict[str, Any]:
        """Capture what the product does and what must never break.

        With no argument, drafts from the codebase — everything is marked
        `inferred` and should be shown to the user for correction. Pass
        `describe` with the user's own words to record it as `stated`.

        Requires a configured model provider.
        """
        from testtrout.llm.gateway import Gateway, GatewayError
        from testtrout.planning import intent as planner

        result = _scan_result()
        if result is None:
            return _needs_scan()
        gateway = Gateway(_config().model, paths.cache)
        probe = _probe_result()

        try:
            captured, warnings = (
                planner.structure(gateway, describe, result, probe)
                if describe
                else planner.draft(gateway, result, probe)
            )
        except GatewayError as exc:
            return {"error": str(exc)}

        paths.ensure()
        write_model(paths.intent, captured)
        return {
            "summary": captured.summary,
            "audience": captured.audience,
            "provenance": "stated" if describe else "inferred",
            "journeys": [
                {
                    "name": j.name,
                    "criticality": j.criticality.value,
                    "consequence": j.consequence,
                    "surfaces": len(j.surfaces),
                }
                for j in captured.journeys
            ],
            "never_break": captured.never_break,
            "open_questions": [q.question for q in captured.unanswered],
            "warnings": warnings,
        }

    @server.tool()
    def probe(entrypoint: str | None = None, role: str | None = None) -> dict[str, Any]:
        """Load the deployment in a browser and record what it does.

        Navigates only — never clicks or submits. Mutating requests are blocked
        at the network layer unless the entrypoint is marked disposable, so this
        is safe against a shared or production URL. Never change that setting on
        the user's behalf.
        """
        from testtrout.deployment.prober import ProbeUnavailableError
        from testtrout.deployment.prober import probe as run_probe
        from testtrout.deployment.reconcile import reconcile

        result = _scan_result()
        if result is None:
            return _needs_scan()
        config = _config()
        target = config.entrypoint(entrypoint)
        if target is None:
            return {"error": "no entrypoint configured", "fix": "run `trout init`"}

        try:
            observed = run_probe(result, config, target, role=role)
        except ProbeUnavailableError as exc:
            return {"error": str(exc)}

        observed.divergences.extend(reconcile(result, observed))
        write_model(paths.observed / f"{target.name}.yaml", observed)

        return {
            "entrypoint": target.name,
            "writable": target.writable,
            "authenticated": observed.authenticated,
            "screens_reachable": observed.reachable_count,
            "screens_total": len(observed.screens),
            "external_hosts": observed.external_hosts,
            "divergences": [
                {"code": d.code, "message": d.message, "detail": d.detail}
                for d in observed.divergences
            ],
        }

    @server.tool()
    def propose(limit: int = 5, kind: str | None = None, use_model: bool = True) -> dict[str, Any]:
        """Draft scenario specifications for the highest-ranked gaps.

        Everything lands as a draft. Do not approve on the user's behalf —
        present the drafts and let them decide.
        """
        from testtrout.authoring import propose as authoring
        from testtrout.authoring.store import load_all, save
        from testtrout.llm.gateway import Gateway
        from testtrout.planning import gaps as planner
        from testtrout.planning.existing_tests import detect

        result = _scan_result()
        if result is None:
            return _needs_scan()
        config = _config()
        captured = read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None
        index, _ = load_all(paths.scenarios)
        gap_map = planner.build(
            result,
            intent=captured,
            probe=_probe_result(),
            existing=detect(paths.root, result),
            roles=[u.role for u in config.test_users],
            scenarios=index,
        )
        candidates = [
            g for g in gap_map.ranked(ready_only=True) if not kind or g.kind.value == kind
        ][:limit]

        gateway = Gateway(config.model, paths.cache) if use_model else None
        paths.ensure()

        drafted: list[dict[str, Any]] = []
        warnings: list[str] = []
        for candidate in candidates:
            scenario, issues = authoring.propose(
                candidate,
                result,
                config,
                probe=_probe_result(),
                intent=captured,
                gateway=gateway,
            )
            save(paths.scenarios, scenario)
            warnings.extend(issues)
            drafted.append(
                {
                    "id": scenario.id,
                    "title": scenario.title,
                    "kind": scenario.kind.value,
                    "ready_to_approve": scenario.ready_to_approve,
                    "open_questions": scenario.open_questions,
                    "assertions": [
                        {"kind": a.kind.value, "provenance": a.provenance.value, "source": a.source}
                        for a in scenario.then
                    ],
                }
            )
        return {"drafted": drafted, "warnings": warnings}

    @server.tool()
    def scenarios(status: str | None = None) -> dict[str, Any]:
        """List scenario specifications and their status."""
        from testtrout.authoring.store import load_all

        index, problems = load_all(paths.scenarios)
        items = [s for s in index.scenarios if not status or s.status.value == status]
        return {
            "counts": index.counts,
            "problems": problems,
            "scenarios": [
                {
                    "id": s.id,
                    "title": s.title,
                    "kind": s.kind.value,
                    "status": s.status.value,
                    "criticality": s.criticality.value,
                    "ready_to_approve": s.ready_to_approve,
                    "open_questions": s.open_questions,
                    "emitted_to": s.emitted_to,
                }
                for s in items
            ],
        }

    @server.tool()
    def approve(scenario_ids: list[str], reject: bool = False) -> dict[str, Any]:
        """Accept (or reject) scenarios into the suite.

        This is the user's decision. Only call it when they have said which
        scenarios they want. A scenario with unanswered questions is refused —
        approving it would produce a test that passes vacuously.
        """
        from testtrout.authoring.store import load_all, save

        index, _ = load_all(paths.scenarios)
        wanted = {i if i.startswith("scenario:") else f"scenario:{i}" for i in scenario_ids}
        target = ScenarioStatus.REJECTED if reject else ScenarioStatus.APPROVED

        changed: list[str] = []
        refused: list[dict[str, Any]] = []
        for scenario in index.scenarios:
            if scenario.id not in wanted:
                continue
            if not reject and not scenario.ready_to_approve:
                refused.append({"id": scenario.id, "open_questions": scenario.open_questions})
                continue
            scenario.status = target
            save(paths.scenarios, scenario)
            changed.append(scenario.id)

        return {
            "changed": changed,
            "refused": refused,
            "not_found": sorted(wanted - {s.id for s in index.scenarios}),
        }

    @server.tool()
    def generate() -> dict[str, Any]:
        """Compile approved scenarios into runnable test files.

        Generated code is a build artifact and is overwritten on every run.
        Never edit it — edit the scenario .yaml and regenerate.
        """
        from testtrout.authoring.base import select_emitter
        from testtrout.authoring.store import load_all, save

        config = _config()
        index, _ = load_all(paths.scenarios)
        approved = index.by_status(ScenarioStatus.APPROVED)
        if not approved:
            return {"error": "no approved scenarios", "fix": "call propose, then approve"}

        from testtrout.runtime.toolchain import app_root

        files: list[dict[str, Any]] = []
        shared: dict[str, str] = {}
        for scenario in approved:
            emitter = select_emitter(scenario)
            if emitter is None:
                files.append({"path": None, "error": f"no emitter for {scenario.kind.value}"})
                continue
            emitted = emitter.emit(scenario, config)
            destination = app_root(paths.root) / emitted.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(emitted.content, encoding="utf-8")
            shared.update(emitted.shared)
            scenario.emitted_to = emitted.path
            save(paths.scenarios, scenario)
            files.append({"path": emitted.path, "notes": emitted.notes})

        for rel, content in shared.items():
            destination = app_root(paths.root) / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            files.append({"path": rel, "notes": []})

        return {"files": files}

    @server.tool()
    def doctor() -> dict[str, Any]:
        """Report what is configured and what is missing.

        Call this first whenever something fails, before guessing.
        """
        config = _config()
        return {
            "root": str(paths.root),
            "scan": paths.surfaces.is_file(),
            "config": paths.config.is_file(),
            "intent": paths.intent.is_file(),
            "entrypoints": [
                {"name": e.name, "url": e.url, "disposable": e.disposable, "writable": e.writable}
                for e in config.entrypoints
            ],
            "test_users": [u.role for u in config.test_users],
            "model_provider": config.model.provider.value,
            "authorization_tests_possible": len(config.test_users) >= 2,
        }

    @server.tool()
    def run(entrypoint: str | None = None, scenario: str | None = None) -> dict[str, Any]:
        """Execute the generated suite and classify every result.

        An inconclusive run is never reported as a pass. Read `status` before
        `results`: if it is `inconclusive`, something prevented a reliable
        decision and the individual results say nothing about the product.

        Only `assertion_failure` is a product signal. `auth_failure`,
        `environment_failure`, and `contract_mismatch` are about the harness.
        """
        from testtrout.authoring.store import load_all
        from testtrout.runtime.runner import run as execute

        config = _config()
        target = config.entrypoint(entrypoint)
        if target is None:
            return {"error": "no entrypoint configured", "fix": "run `trout init`"}

        index, _ = load_all(paths.scenarios)
        paths.ensure()
        record = execute(
            config,
            target,
            index,
            paths.root,
            report_dir=paths.runs / "latest",
            scenario_id=scenario,
        )
        write_model(paths.runs / f"{record.id}.yaml", record, header=False)

        return {
            "status": record.status.value,
            "counts": record.counts,
            "duration_seconds": record.duration_seconds,
            "isolation": record.isolation,
            "notes": record.notes,
            "results": [
                {
                    "scenario_id": r.scenario_id,
                    "classification": r.classification.value,
                    "is_product_signal": r.classification.is_product_signal,
                    "message": r.message,
                    "reproduce": r.evidence.reproduce,
                    "trace": r.evidence.trace,
                }
                for r in record.results
            ],
        }

    @server.tool()
    def certify(entrypoint: str | None = None, runs: int | None = None) -> dict[str, Any]:
        """Run the suite repeatedly to prove scenarios are deterministic.

        Only a clean sweep certifies. An inconsistent scenario is quarantined,
        not admitted — an intermittently-failing test in a blocking suite is how
        a team learns to ignore the suite.
        """
        from testtrout.authoring.store import load_all, save
        from testtrout.runtime.runner import apply_verdicts
        from testtrout.runtime.runner import certify as run_certification

        config = _config()
        target = config.entrypoint(entrypoint)
        if target is None:
            return {"error": "no entrypoint configured", "fix": "run `trout init`"}

        index, _ = load_all(paths.scenarios)
        paths.ensure()
        verdicts, records = run_certification(
            config, target, index, paths.root, paths.runs, runs=runs
        )
        changes = apply_verdicts(index, verdicts)
        for scenario in index.scenarios:
            if scenario.id in changes:
                save(paths.scenarios, scenario)

        return {
            "runs": len(records),
            "verdicts": {k: v.value for k, v in verdicts.items()},
            "changes": {k: v.value for k, v in changes.items()},
            "notes": records[-1].notes if records else [],
        }

    @server.tool()
    def report() -> dict[str, Any]:
        """Results and evidence from the most recent run."""
        from testtrout.domain.run import RunRecord

        records = sorted(paths.runs.glob("*.yaml")) if paths.runs.is_dir() else []
        if not records:
            return {"error": "no runs yet", "fix": "call run"}
        record = read_model(records[-1], RunRecord)
        return {
            "id": record.id,
            "status": record.status.value,
            "counts": record.counts,
            "regressions": [
                {
                    "scenario_id": r.scenario_id,
                    "message": r.message,
                    "reproduce": r.evidence.reproduce,
                }
                for r in record.regressions
            ],
            "notes": record.notes,
        }

    # ------------------------------------------------------------ resources

    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    @server.resource("trout://surfaces", mime_type="application/yaml")
    def surfaces_resource() -> str:
        """The full surface map from the last scan."""
        return _read(paths.surfaces)

    @server.resource("trout://config", mime_type="application/yaml")
    def config_resource() -> str:
        """Repository configuration. Holds env: references, never secrets."""
        return _read(paths.config)

    @server.resource("trout://intent", mime_type="application/yaml")
    def intent_resource() -> str:
        """Captured product intent."""
        return _read(paths.intent)

    @server.resource("trout://scenarios", mime_type="application/json")
    def scenarios_resource() -> str:
        """Every scenario specification."""
        from testtrout.authoring.store import load_all

        index, _ = load_all(paths.scenarios)
        return json.dumps(
            [s.model_dump(mode="json", exclude_none=True) for s in index.scenarios], indent=2
        )

    return server


def run(root: Path) -> None:
    """Run the server over stdio."""
    build_server(root).run()
