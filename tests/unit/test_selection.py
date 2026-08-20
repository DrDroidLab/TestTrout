"""Change-based selection and the base differential."""

from __future__ import annotations

from pathlib import Path

import pytest

from testtrout.domain.config import Config, Entrypoint
from testtrout.domain.gap import TestKind
from testtrout.domain.run import Classification, RunRecord, ScenarioResult
from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import (
    Criticality,
    DataOperation,
    Operation,
    ProjectInfo,
    ScanResult,
    Screen,
    SourceLocation,
)
from testtrout.planning import selection as selector
from testtrout.runtime import differential

ORDERS = SourceLocation(file="src/Orders.tsx", line=1)
BILLING = SourceLocation(file="src/Billing.tsx", line=1)

SCAN = ScanResult(
    project=ProjectInfo(root=".", framework="vite-react"),
    screens=[
        Screen(
            id="screen:orders",
            location=ORDERS,
            path="/orders",
            component="Orders",
            reaches=["data:orders.select"],
        )
    ],
    data_operations=[
        DataOperation(
            id="data:orders.select", location=ORDERS, table="orders", operation=Operation.SELECT
        ),
        DataOperation(
            id="data:billing.insert", location=BILLING, table="billing", operation=Operation.INSERT
        ),
    ],
)


def _scenario(sid: str, surfaces: list[str], criticality=Criticality.MEDIUM) -> Scenario:
    return Scenario(
        id=sid,
        title=sid,
        kind=TestKind.BROWSER_JOURNEY,
        status=ScenarioStatus.APPROVED,
        emitted_to=f"tests/trout/{sid}.spec.ts",
        surfaces=surfaces,
        criticality=criticality,
    )


INDEX = ScenarioIndex(
    scenarios=[
        _scenario("scenario:orders", ["screen:orders", "data:orders.select"]),
        _scenario("scenario:billing", ["data:billing.insert"]),
    ]
)


def test_a_change_selects_only_the_scenarios_it_can_affect():
    chosen = selector.select(SCAN, INDEX, ["src/Billing.tsx"], include_critical=False)
    assert chosen.scenarios == ["scenario:billing"]
    assert "data:billing.insert" in chosen.changed_surfaces


def test_every_selection_explains_itself():
    chosen = selector.select(SCAN, INDEX, ["src/Billing.tsx"])
    assert chosen.reasons["scenario:billing"]
    assert "changed surface" in chosen.reasons["scenario:billing"][0]


def test_critical_scenarios_run_regardless_of_the_diff():
    """Cheap insurance against the declared-coverage blind spot."""
    index = ScenarioIndex(
        scenarios=[_scenario("scenario:critical", ["screen:elsewhere"], Criticality.CRITICAL)]
    )
    chosen = selector.select(SCAN, index, ["src/Unrelated.tsx"])
    assert chosen.scenarios == ["scenario:critical"]
    assert "always run" in chosen.reasons["scenario:critical"][0]


def test_a_change_to_a_reached_operation_selects_the_screen_that_reaches_it():
    """The interesting breakage is usually a level below the route file."""
    chosen = selector.select(SCAN, INDEX, ["src/Orders.tsx"], include_critical=False)
    assert "scenario:orders" in chosen.scenarios


def test_uncovered_changed_surfaces_are_reported():
    """The most valuable output: this change walked into untested ground."""
    index = ScenarioIndex(scenarios=[_scenario("scenario:orders", ["screen:orders"])])
    chosen = selector.select(SCAN, index, ["src/Billing.tsx"], include_critical=False)
    assert chosen.uncovered_surfaces == ["data:billing.insert"]
    assert any("no scenario protecting them" in n for n in chosen.notes)


def test_selection_admits_its_own_blind_spot():
    """Declared coverage is not observed coverage, and the note must say so."""
    chosen = selector.select(SCAN, INDEX, ["src/Orders.tsx"])
    assert any("declared surfaces" in n for n in chosen.notes)


def test_only_generated_and_accepted_scenarios_are_eligible():
    index = ScenarioIndex(
        scenarios=[
            Scenario(
                id="scenario:draft",
                title="d",
                kind=TestKind.BROWSER_JOURNEY,
                surfaces=["data:billing.insert"],
                emitted_to=None,
            ),
        ]
    )
    chosen = selector.select(SCAN, index, ["src/Billing.tsx"])
    assert chosen.empty
    assert any("Approve and generate" in n for n in chosen.notes)


def test_a_missing_git_ref_fails_loudly(tmp_path: Path):
    """Guessing at a diff would be worse than saying so."""
    with pytest.raises(selector.GitUnavailableError):
        selector.changed_files(tmp_path, "no-such-ref")


# ------------------------------------------------------------- differential


def _result(classification: Classification) -> ScenarioResult:
    return ScenarioResult(scenario_id="scenario:x", classification=classification)


def _record(*results: ScenarioResult) -> RunRecord:
    return RunRecord(id="r", started_at="now", results=list(results))


BASELINE = Entrypoint(name="production", url="https://app.example.com")


def test_fails_here_passes_on_baseline_is_a_regression():
    verdict = differential._verdict(
        _result(Classification.ASSERTION_FAILURE),
        _record(_result(Classification.PASSED)),
        BASELINE,
    )
    assert verdict.is_regression
    assert verdict.blocking_eligible
    assert "this change broke it" in verdict.summary


def test_failing_on_both_is_pre_existing_not_a_regression():
    """Failing someone's PR for a break they did not cause is how a check gets bypassed."""
    verdict = differential._verdict(
        _result(Classification.ASSERTION_FAILURE),
        _record(_result(Classification.ASSERTION_FAILURE)),
        BASELINE,
    )
    assert not verdict.is_regression
    assert not verdict.blocking_eligible
    assert "not caused by this change" in verdict.summary


def test_an_unreachable_baseline_is_never_called_a_regression():
    verdict = differential._verdict(
        _result(Classification.ASSERTION_FAILURE),
        _record(_result(Classification.ENVIRONMENT_FAILURE)),
        BASELINE,
    )
    assert not verdict.is_regression
    assert "inconclusive" in verdict.summary


def test_a_scenario_missing_from_the_baseline_run_is_unconfirmed():
    verdict = differential._verdict(_result(Classification.ASSERTION_FAILURE), _record(), BASELINE)
    assert not verdict.is_regression
    assert verdict.on_baseline is None


def test_applying_verdicts_downgrades_a_pre_existing_failure():
    """The run's status must reflect what this change actually did."""
    record = _record(_result(Classification.ASSERTION_FAILURE))
    verdicts = [
        differential.Verdict(
            scenario_id="scenario:x",
            on_head=Classification.ASSERTION_FAILURE,
            on_baseline=Classification.ASSERTION_FAILURE,
            is_regression=False,
            summary="already failing on production",
        )
    ]
    updated = differential.apply(record, verdicts)
    assert updated.results[0].classification is Classification.INCONCLUSIVE
    assert not updated.regressions
    assert any("0 confirmed regression" in n for n in updated.notes)


def test_applying_verdicts_keeps_a_confirmed_regression():
    record = _record(_result(Classification.ASSERTION_FAILURE))
    verdicts = [
        differential.Verdict(
            scenario_id="scenario:x",
            on_head=Classification.ASSERTION_FAILURE,
            on_baseline=Classification.PASSED,
            is_regression=True,
            summary="passes on production, fails here",
        )
    ]
    updated = differential.apply(record, verdicts)
    assert updated.results[0].classification is Classification.ASSERTION_FAILURE
    assert len(updated.regressions) == 1


def test_only_product_signals_are_compared():
    """Re-running an auth failure elsewhere just produces a second auth failure."""
    record = _record(_result(Classification.AUTH_FAILURE))
    assert (
        differential.compare(record, Config(), BASELINE, ScenarioIndex(), Path("."), Path("."))
        == []
    )
