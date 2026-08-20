"""Deciding whether a failure is actually a regression.

A test that fails tells you something is wrong. It does not tell you that
*this change* broke it — the test may have been failing all week, or the
deployment may be having a bad afternoon. Reporting either as a regression is
how a check loses its audience.

So every failure is re-executed against a deployment believed to be good,
usually production or the base branch's preview. The comparison is what turns
"a test failed" into a claim worth blocking on:

===========  ===========  =====================================================
This build   Baseline     Conclusion
===========  ===========  =====================================================
fail         pass         Regression. This change broke it.
fail         fail         Pre-existing. Real, but not caused by this change.
fail         error        Inconclusive. The baseline could not be reached.
===========  ===========  =====================================================

This is the strongest signal available, it is entirely deterministic, and it
runs before any model is asked to explain anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from testtrout.domain.config import Config, Entrypoint
from testtrout.domain.run import Classification, RunRecord, ScenarioResult
from testtrout.domain.scenario import ScenarioIndex


@dataclass
class Verdict:
    """The comparison outcome for one failing scenario."""

    scenario_id: str
    on_head: Classification
    on_baseline: Classification | None
    is_regression: bool
    summary: str

    @property
    def blocking_eligible(self) -> bool:
        """Whether this may fail a check.

        Only a confirmed regression qualifies. A pre-existing failure is worth
        reporting and worth fixing, but failing someone's pull request for a
        break they did not cause is how a team learns to bypass the check.
        """
        return self.is_regression


def compare(
    record: RunRecord,
    config: Config,
    baseline: Entrypoint,
    scenarios: ScenarioIndex,
    root: Path,
    report_dir: Path,
) -> list[Verdict]:
    """Re-run this run's failures against a baseline deployment.

    Only genuine product signals are re-run. Re-running an auth failure against
    a second deployment just produces a second auth failure, and wastes a
    minute doing it.
    """
    from testtrout.runtime.runner import run as execute

    candidates = [r for r in record.results if r.classification.is_product_signal]
    verdicts: list[Verdict] = []

    for index, failure in enumerate(candidates):
        baseline_run = execute(
            config,
            baseline,
            scenarios,
            root,
            report_dir=report_dir / f"baseline-{index}",
            scenario_id=failure.scenario_id,
            # The baseline is a reference point, not a workspace. Resetting the
            # database there could destroy data on a deployment the developer
            # never asked us to touch.
            reset=False,
        )
        verdicts.append(_verdict(failure, baseline_run, baseline))
    return verdicts


def _verdict(failure: ScenarioResult, baseline_run: RunRecord, baseline: Entrypoint) -> Verdict:
    """Interpret one head/baseline pair."""
    match = next((r for r in baseline_run.results if r.scenario_id == failure.scenario_id), None)

    if match is None:
        return Verdict(
            scenario_id=failure.scenario_id,
            on_head=failure.classification,
            on_baseline=None,
            is_regression=False,
            summary=(
                f"could not run this scenario against {baseline.name} — treating the "
                "failure as unconfirmed rather than calling it a regression"
            ),
        )

    if not match.classification.is_product_signal and not match.passed:
        return Verdict(
            scenario_id=failure.scenario_id,
            on_head=failure.classification,
            on_baseline=match.classification,
            is_regression=False,
            summary=(
                f"{baseline.name} returned {match.classification.value}, so the comparison "
                "is inconclusive — the baseline could not be reached reliably"
            ),
        )

    if match.passed:
        return Verdict(
            scenario_id=failure.scenario_id,
            on_head=failure.classification,
            on_baseline=match.classification,
            is_regression=True,
            summary=f"passes on {baseline.name}, fails here — this change broke it",
        )

    return Verdict(
        scenario_id=failure.scenario_id,
        on_head=failure.classification,
        on_baseline=match.classification,
        is_regression=False,
        summary=(f"already failing on {baseline.name} — real, but not caused by this change"),
    )


def apply(record: RunRecord, verdicts: list[Verdict]) -> RunRecord:
    """Fold verdicts back into a run record.

    A pre-existing failure is downgraded out of the product-signal class, so
    the run's overall status reflects what this change actually did.
    """
    by_id = {v.scenario_id: v for v in verdicts}
    for result in record.results:
        verdict = by_id.get(result.scenario_id)
        if verdict is None:
            continue
        result.message = f"{result.message} — {verdict.summary}".strip(" —")
        if not verdict.is_regression:
            result.classification = Classification.INCONCLUSIVE
    if verdicts:
        confirmed = sum(1 for v in verdicts if v.is_regression)
        record.notes.append(
            f"compared {len(verdicts)} failure(s) against a baseline deployment: "
            f"{confirmed} confirmed regression(s)"
        )
    return record
