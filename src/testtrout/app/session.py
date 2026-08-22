"""The whole pipeline for one project, in the order it happens.

Four steps, each of which can be run on its own and each of which leaves an
artifact behind:

1. **scan** — read the code. Endpoints, pages, storage, deployment components.
2. **probe** — ask the deployment what it actually does, if one is configured.
3. **derive** — from those two, work out what can be tested and what facts are
   still missing. Deterministic; no model, no guessing.
4. **build** — turn every ready candidate into a test, run it, and keep the
   ones that pass. That set is the baseline.

Steps 1 to 3 always run together, because a scan whose consequences are not worked
out is just a file on disk. Step 4 is separate because it writes code into the
user's repository and takes minutes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from testtrout.domain.candidate import TestPlan
from testtrout.domain.config import Config
from testtrout.domain.fact import FactSheet
from testtrout.domain.observation import ProbeResult
from testtrout.domain.overview import ProjectOverview
from testtrout.domain.surface import ScanResult
from testtrout.store import QaPaths, apply_scan, read_model, write_model

Log = Callable[[str], None]


@dataclass
class Session:
    """Everything known about one project, loaded once and passed around."""

    paths: QaPaths
    log: Log = field(default=lambda _: None)

    @property
    def config(self) -> Config:
        """Current configuration."""
        return read_model(self.paths.config, Config) if self.paths.config.is_file() else Config()

    @property
    def scan(self) -> ScanResult | None:
        """The last scan, if there has been one."""
        return (
            read_model(self.paths.surfaces, ScanResult) if self.paths.surfaces.is_file() else None
        )

    @property
    def probe(self) -> ProbeResult | None:
        """The last probe of the default deployment."""
        entrypoint = self.config.entrypoint()
        if entrypoint is None:
            return None
        path = self.paths.observed / f"{entrypoint.name}.yaml"
        return read_model(path, ProbeResult) if path.is_file() else None

    @property
    def facts(self) -> FactSheet:
        """What the tool still needs."""
        return (
            read_model(self.paths.facts, FactSheet) if self.paths.facts.is_file() else FactSheet()
        )

    @property
    def plan(self) -> TestPlan:
        """What can be tested."""
        return read_model(self.paths.plan, TestPlan) if self.paths.plan.is_file() else TestPlan()

    @property
    def overview(self) -> ProjectOverview | None:
        """The project map."""
        return (
            read_model(self.paths.overview, ProjectOverview)
            if self.paths.overview.is_file()
            else None
        )


def scan(session: Session) -> ScanResult:
    """Read the code. No network, no API key, safe on anything."""
    from testtrout.analysis.scanner import scan as run_scan

    session.log(f"Reading {session.paths.root}")
    result = run_scan(session.paths.root)
    session.paths.ensure()
    write_model(session.paths.surfaces, result)
    write_model(session.paths.config, apply_scan(session.config, result))

    counts = result.counts
    session.log(
        f"Found {counts.get('screens', 0)} page(s), {counts.get('endpoints', 0)} endpoint(s), "
        f"{counts.get('tables', 0)} table(s)."
    )
    return result


def probe(session: Session) -> ProbeResult | None:
    """Ask the deployment what it does. Skipped when there is nowhere to ask."""
    from testtrout.deployment.prober import ProbeUnavailableError
    from testtrout.deployment.prober import probe as run_probe

    scan_result = session.scan
    config = session.config
    entrypoint = config.entrypoint()
    if scan_result is None or entrypoint is None or not entrypoint.url:
        return None

    role = config.test_users[0].role if config.test_users else None
    session.log(
        f"Checking {entrypoint.url}" + (f", signed in as {role}" if role else ", signed out")
    )
    try:
        result = run_probe(scan_result, config, entrypoint, role=role)
    except ProbeUnavailableError as exc:
        session.log(str(exc))
        return None
    except Exception as exc:
        session.log(f"Could not reach the deployment: {exc}")
        return None

    write_model(session.paths.observed / f"{entrypoint.name}.yaml", result)
    reachable = result.reachable_count
    answered = sum(1 for e in result.endpoints if e.status is not None)
    session.log(f"{reachable} page(s) loaded, {answered} endpoint(s) answered.")
    return result


def derive(session: Session) -> tuple[FactSheet, TestPlan]:
    """Work out what can be tested, and what is missing.

    Deterministic on purpose. Everything here follows from the scan and the
    probe, so two runs against unchanged inputs produce the same answer — which
    is what lets a person trust the list rather than re-reading it each time.
    """
    from testtrout.planning import candidates as candidate_planner
    from testtrout.planning import facts as fact_planner
    from testtrout.planning.overview import build as build_overview

    scan_result = session.scan
    if scan_result is None:
        return FactSheet(), TestPlan()

    config = session.config
    probe_result = session.probe

    plan = candidate_planner.build(scan_result, config, probe_result)
    sheet = fact_planner.build(scan_result, config, probe_result, plan.candidates)

    from testtrout.authoring.store import load_all

    index, _ = load_all(session.paths.scenarios)
    overview = build_overview(scan_result, index)

    write_model(session.paths.plan, plan)
    write_model(session.paths.facts, sheet)
    write_model(session.paths.overview, overview)

    counts = plan.counts()
    session.log(
        f"{counts['ready']} thing(s) I can test right now; {counts['waiting']} waiting on "
        f"something from you."
    )
    return sheet, plan


def refresh(session: Session, *, rescan: bool = True) -> tuple[FactSheet, TestPlan]:
    """Scan, probe, and derive — the whole understanding step.

    Args:
        session: The project.
        rescan: False re-uses the last scan, for when only configuration
            changed and re-parsing the tree would be wasted work.
    """
    if rescan or session.scan is None:
        scan(session)
    probe(session)
    return derive(session)


def build(session: Session, limit: int = 20) -> dict[str, object]:
    """Turn every ready candidate into a proven test.

    A test is kept only if it passes against the deployment. Since every
    assertion here came from observing that same deployment, a failure means
    something genuinely inconsistent — a flaky page, a value that changes on
    every load — and holding it back is right.
    """
    from testtrout.authoring import baseline
    from testtrout.authoring.base import select_emitter
    from testtrout.authoring.store import load_all, save
    from testtrout.domain.run import RunRecord
    from testtrout.runtime.runner import run as execute
    from testtrout.runtime.toolchain import app_root

    scan_result = session.scan
    config = session.config
    entrypoint = config.entrypoint()
    if scan_result is None or entrypoint is None:
        raise RuntimeError("nothing to build against — add a deployment URL first")

    ready = session.plan.ready[:limit]
    if not ready:
        session.log("Nothing is ready to test yet. Fill in what I asked for and scan again.")
        return {"kept": 0, "held": 0, "run_id": ""}

    probe_result = session.probe
    session.paths.ensure()
    kept: list[str] = []
    held: list[str] = []
    combined: RunRecord | None = None

    for position, candidate in enumerate(ready, start=1):
        session.log(f"Writing {position}/{len(ready)}: {candidate.title}")
        scenario = baseline.write(candidate, probe_result, config)
        if scenario is None:
            continue
        emitter = select_emitter(scenario)
        if emitter is None:
            continue

        output = emitter.emit(scenario, config)
        for relative, content in {output.path: output.content, **output.shared}.items():
            destination = app_root(session.paths.root) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        scenario.emitted_to = output.path
        save(session.paths.scenarios, scenario)

        index, _ = load_all(session.paths.scenarios)
        record = execute(
            config,
            entrypoint,
            index,
            session.paths.root,
            report_dir=session.paths.runs / "baseline",
            only=[scenario.id],
        )
        if combined is None:
            combined = record.model_copy(deep=True)
        else:
            combined.results.extend(record.results)
            combined.finished_at = record.finished_at

        result = next((r for r in record.results if r.scenario_id == scenario.id), None)
        if result is not None and result.passed:
            from testtrout.domain.scenario import ScenarioStatus

            scenario.status = ScenarioStatus.CERTIFIED
            kept.append(scenario.id)
            session.log(f"  kept — {candidate.title}")
        else:
            held.append(scenario.id)
            detail = result.message if result else "it did not run"
            session.log(f"  held back — {detail[:70]}")
        save(session.paths.scenarios, scenario)

    run_id = ""
    if combined is not None:
        write_model(session.paths.runs / f"{combined.id}.yaml", combined, header=False)
        run_id = combined.id

    session.log(f"Baseline now holds {len(kept)} proven test(s).")
    return {"kept": len(kept), "held": len(held), "run_id": run_id}


def project_root(path: Path) -> QaPaths:
    """Resolve a project directory to its ``.trout/`` layout."""
    return QaPaths(root=path.resolve())


__all__ = ["Session", "build", "derive", "probe", "project_root", "refresh", "scan"]
