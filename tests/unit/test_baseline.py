"""Turning what was observed into a test that asserts it keeps happening.

The property that matters: every assertion must trace to something the probe
actually saw. A baseline that asserts anything else is guessing about a product
nobody described to it.
"""

from __future__ import annotations

from testtrout.authoring import baseline
from testtrout.domain.candidate import Candidate, CandidateKind
from testtrout.domain.config import Config, Entrypoint, TestUser
from testtrout.domain.observation import (
    AuthPosture,
    ObservedEndpoint,
    ObservedScreen,
    ProbeResult,
    SelectorCandidate,
    SelectorStrategy,
)
from testtrout.domain.provenance import Provenance
from testtrout.domain.scenario import AssertionKind, ScenarioStatus

DEPLOYED = Config(entrypoints=[Entrypoint(name="d", url="https://app.test")])


def page_probe(**kwargs) -> ProbeResult:
    return ProbeResult(
        entrypoint="d",
        base_url="https://app.test",
        screens=[
            ObservedScreen(
                path="/orders",
                url="https://app.test/orders",
                reachable=True,
                status=200,
                title=kwargs.get("title", "Orders"),
                selectors=kwargs.get("selectors", []),
            )
        ],
    )


def candidate(kind=CandidateKind.PAGE, target="/orders", **kwargs) -> Candidate:
    return Candidate(id="c", kind=kind, title="t", target=target, observed=True, **kwargs)


def test_every_assertion_traces_to_something_observed() -> None:
    """The single property this module exists to guarantee."""
    probe = page_probe(
        selectors=[
            SelectorCandidate(strategy=SelectorStrategy.TEST_ID, value="orders-table"),
            SelectorCandidate(strategy=SelectorStrategy.ROLE, value="x", role="button", name="New"),
        ]
    )
    scenario = baseline.write(candidate(), probe, DEPLOYED)

    assert scenario.then
    assert all(a.provenance is Provenance.OBSERVED for a in scenario.then)
    assert all(a.source for a in scenario.then)


def test_the_page_title_is_asserted_because_a_broken_page_loses_it_first() -> None:
    scenario = baseline.write(candidate(), page_probe(title="Orders"), DEPLOYED)

    title = next(a for a in scenario.then if a.kind is AssertionKind.TEXT)
    assert title.expected == "Orders"
    assert title.target.selector is None


def test_a_page_with_nothing_identifiable_still_asserts_it_loads_cleanly() -> None:
    """Better than no test: a blank render is the most common breakage."""
    scenario = baseline.write(candidate(), page_probe(title=None), DEPLOYED)

    assert [a.kind for a in scenario.then] == [AssertionKind.NO_CONSOLE_ERRORS]


def test_durable_selectors_win_over_copy() -> None:
    """A test pinned to wording breaks on a copy change and teaches people to
    ignore failures."""
    probe = page_probe(
        selectors=[
            SelectorCandidate(strategy=SelectorStrategy.TEXT, value="Welcome back"),
            SelectorCandidate(strategy=SelectorStrategy.TEST_ID, value="orders-table"),
        ]
    )
    scenario = baseline.write(candidate(), probe, DEPLOYED)

    visible = [a for a in scenario.then if a.kind is AssertionKind.VISIBLE]
    assert [a.target.selector.strategy for a in visible] == [SelectorStrategy.TEST_ID]


def test_a_page_behind_a_login_signs_in_first() -> None:
    config = Config(
        entrypoints=[Entrypoint(name="d", url="https://app.test")],
        test_users=[TestUser(role="owner", email="env:E", password="env:P")],
    )
    scenario = baseline.write(candidate(behind_login=True), page_probe(), config)

    assert scenario.role == "owner"
    assert scenario.when[0].action.value == "sign_in"


def test_an_endpoint_test_only_ever_sends_a_get() -> None:
    """Whatever methods it declares. A GET cannot change anything, which is
    what makes running this against production defensible."""
    probe = ProbeResult(
        entrypoint="d",
        base_url="https://app.test",
        endpoints=[
            ObservedEndpoint(
                endpoint_id="e",
                path="/jobs",
                status=201,
                posture=AuthPosture.PUBLIC,
                detail="answered 201",
            )
        ],
    )
    scenario = baseline.write(
        candidate(kind=CandidateKind.ENDPOINT, target="/jobs"), probe, DEPLOYED
    )

    assert scenario.when[0].target.method == "GET"
    assert scenario.then[0].expected == "201"


def test_a_refusal_is_recorded_as_a_refusal() -> None:
    probe = ProbeResult(
        entrypoint="d",
        base_url="https://app.test",
        endpoints=[
            ObservedEndpoint(
                endpoint_id="e",
                path="/admin",
                status=401,
                posture=AuthPosture.REQUIRES_AUTH,
                detail="refused an unauthenticated request with 401",
            )
        ],
    )
    scenario = baseline.write(
        candidate(kind=CandidateKind.ENDPOINT, target="/admin"), probe, DEPLOYED
    )

    assert "stays private" in scenario.title
    assert scenario.then[0].expected == "401"


def test_a_baseline_scenario_needs_no_approval() -> None:
    """Validation replaced approval: the test is proven by running it, and
    asking someone to approve an unrun test is asking them to guess."""
    scenario = baseline.write(candidate(), page_probe(), DEPLOYED)

    assert scenario.status is ScenarioStatus.APPROVED
    assert scenario.open_questions == []


def test_nothing_observed_produces_nothing() -> None:
    assert baseline.write(candidate(target="/never-seen"), page_probe(), DEPLOYED) is None
