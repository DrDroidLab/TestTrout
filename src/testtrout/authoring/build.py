"""Draft tests and prove them against the deployment before keeping any.

This is where validation replaces approval as the gate. Asking someone to
approve a test nobody has run is asking them to guess; a test that demonstrably
passes against a working deployment has earned its place. What is left for a
person is the part only they can do — answering what the tool could not
determine.

One candidate at a time, all the way through: draft it, write it, run it,
decide. Building the whole batch before running any of it is cheaper per test,
but it leaves the list frozen for minutes and then changes every row at once.
Here each test settles into its final state as it is made, and anything that
goes wrong shows up next to the test that caused it.

Shared by the app's Build button and ``trout build`` so the two cannot drift.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from testtrout.domain.config import Config, Entrypoint
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import ProbeResult
from testtrout.domain.scenario import Scenario, ScenarioStatus
from testtrout.domain.surface import ScanResult
from testtrout.llm.gateway import Gateway
from testtrout.store import QaPaths, write_model


@dataclass
class BuildOutcome:
    """What a build produced, by the only distinction that matters.

    A test is either protecting something or waiting on a person. There is no
    third state worth reporting, so there are only three lists here.
    """

    kept: list[str] = field(default_factory=list)
    """Proven against the deployment. These are the suite."""
    held: list[str] = field(default_factory=list)
    """Written and run, but did not pass. Each carries the failure as a question."""
    unclear: list[str] = field(default_factory=list)
    """Never run — something about them was ambiguous from the start."""
    run_id: str = ""
    """The record holding what happened, if anything ran."""

    @property
    def drafted(self) -> int:
        """Everything attempted."""
        return len(self.kept) + len(self.held) + len(self.unclear)

    @property
    def needs_you(self) -> int:
        """How many are waiting on a person."""
        return len(self.held) + len(self.unclear)

    def as_dict(self) -> dict[str, object]:
        """Summary for a job result or a ``--json`` response."""
        return {
            "drafted": self.drafted,
            "kept": len(self.kept),
            "held": self.needs_you,
            "run_id": self.run_id,
        }


def build_suite(
    paths: QaPaths,
    config: Config,
    scan: ScanResult,
    entrypoint: Entrypoint,
    *,
    intent: ProductIntent | None = None,
    probe: ProbeResult | None = None,
    gateway: Gateway | None = None,
    limit: int = 5,
    log: Callable[[str], None] = lambda _: None,
) -> BuildOutcome:
    """Write tests for the highest-ranked gaps, keeping only what passes.

    Args:
        paths: Where the suite lives.
        config: Repository configuration.
        scan: The most recent scan.
        entrypoint: The deployment to prove tests against.
        intent: Captured product intent, if any. Improves what gets drafted.
        probe: Probe observations, if any.
        gateway: Model gateway. ``None`` builds only what can be derived
            without a model, which is less but is never a guess.
        limit: How many gaps to attempt.
        log: Called with each line of progress, as it happens.

    Returns:
        What was kept, held back, and left unclear.
    """
    from testtrout.authoring import propose as authoring
    from testtrout.authoring.base import select_emitter
    from testtrout.authoring.store import load_all, save
    from testtrout.domain.run import RunRecord
    from testtrout.planning import gaps as planner
    from testtrout.planning.existing_tests import detect
    from testtrout.runtime.runner import run as execute

    index, _ = load_all(paths.scenarios)
    gap_map = planner.build(
        scan,
        intent=intent,
        probe=probe,
        existing=detect(paths.root, scan),
        roles=[user.role for user in config.test_users],
        scenarios=index,
    )
    candidates = gap_map.ranked(ready_only=True)[:limit]
    outcome = BuildOutcome()
    if not candidates:
        log("nothing new to build — every ready area already has a test")
        return outcome

    paths.ensure()
    # Every test run here goes into one record. Without it a test that was just
    # proven has no evidence on disk, and the tests list has to report it as
    # never having run — which is exactly the visibility this is meant to give.
    combined: RunRecord | None = None

    for position, candidate in enumerate(candidates, start=1):
        log(f"drafting {position}/{len(candidates)}: {candidate.title}")
        scenario, warnings = authoring.propose(
            candidate, scan, config, probe=probe, intent=intent, gateway=gateway
        )
        for warning in warnings:
            log(f"  {warning}")

        if scenario.open_questions:
            # Held back deliberately: a test built on a guess is worse than no
            # test, and the guess is exactly what a person can settle quickly.
            _hold(paths, scenario, outcome.unclear)
            log("  needs an answer before this can be trusted")
            continue

        emitter = select_emitter(scenario)
        if emitter is None:
            scenario.open_questions.append(
                f"TestTrout has no way to write a {scenario.kind.value} test yet."
            )
            _hold(paths, scenario, outcome.unclear)
            log("  no emitter for this kind of test")
            continue

        output = emitter.emit(scenario, config)
        for relative, content in {output.path: output.content, **output.shared}.items():
            destination = paths.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        scenario.emitted_to = output.path
        scenario.status = ScenarioStatus.APPROVED
        save(paths.scenarios, scenario)

        log(f"  trying it against {entrypoint.url}")
        current, _ = load_all(paths.scenarios)
        record = execute(
            config,
            entrypoint,
            current,
            paths.root,
            report_dir=paths.runs / "build",
            only=[scenario.id],
        )
        for note in record.notes:
            log(f"  {note}")

        if combined is None:
            combined = record.model_copy(deep=True)
        else:
            combined.results.extend(record.results)
            combined.notes.extend(n for n in record.notes if n not in combined.notes)
            combined.finished_at = record.finished_at

        result = next((r for r in record.results if r.scenario_id == scenario.id), None)
        if result is not None and result.passed:
            scenario.status = ScenarioStatus.CERTIFIED
            outcome.kept.append(scenario.id)
            save(paths.scenarios, scenario)
            log(f"  kept: {scenario.title}")
        else:
            detail = result.message if result else "the test did not run"
            # Only a real disagreement with the product is worth asking a
            # person about. A missing toolchain or an unreachable deployment is
            # a setup problem, and dressing it up as "is this a regression?"
            # trains people to ignore the question queue.
            scenario.open_questions.append(
                f"This test did not pass against {entrypoint.name}: {detail}. "
                "Is the expectation wrong, or is this a real problem?"
                if result is not None and result.classification.is_product_signal
                else f"This test could not be proven against {entrypoint.name}: {detail}"
            )
            _hold(paths, scenario, outcome.held)
            log(f"  held back: {scenario.title} — {detail[:60]}")

    if combined is not None:
        write_model(paths.runs / f"{combined.id}.yaml", combined, header=False)
        outcome.run_id = combined.id

    log(
        f"{len(outcome.kept)} test(s) proven against your deployment, "
        f"{outcome.needs_you} need your input"
    )
    return outcome


def _hold(paths: QaPaths, scenario: Scenario, bucket: list[str]) -> None:
    """Park a scenario as a draft.

    A draft protects nothing and counts towards nothing, which is the point:
    an unproven test that looked like coverage would be worse than none.
    """
    from testtrout.authoring.store import save

    scenario.status = ScenarioStatus.DRAFT
    save(paths.scenarios, scenario)
    bucket.append(scenario.id)


__all__ = ["BuildOutcome", "build_suite"]
