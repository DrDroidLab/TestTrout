"""Scenario proposal and code emission."""

from __future__ import annotations

import json
from pathlib import Path

from testtrout.authoring import propose as authoring
from testtrout.authoring.base import select_emitter
from testtrout.authoring.store import load_all, save
from testtrout.domain.config import Config, Entrypoint, ModelConfig, ModelProvider, TestUser
from testtrout.domain.gap import Gap, TestKind
from testtrout.domain.intent import Provenance
from testtrout.domain.observation import (
    ObservedScreen,
    ProbeResult,
    SelectorCandidate,
    SelectorStrategy,
)
from testtrout.domain.scenario import ScenarioStatus
from testtrout.domain.surface import (
    Column,
    Criticality,
    Policy,
    ProjectInfo,
    ScanResult,
    Screen,
    SourceLocation,
    Table,
)
from testtrout.llm.base import CompletionResponse
from testtrout.llm.gateway import Gateway

LOCATION = SourceLocation(file="src/x.tsx", line=1)

CONFIG = Config(
    entrypoints=[Entrypoint(name="local", url="http://localhost:5173", disposable=True)],
    test_users=[
        TestUser(role="owner", email="env:A", password="env:B"),
        TestUser(role="member", email="env:C", password="env:D"),
    ],
)


def _scan_with_policy(using: str, table: str = "profiles") -> ScanResult:
    return ScanResult(
        project=ProjectInfo(root=".", framework="vite-react", backend="supabase"),
        policies=[
            Policy(
                id=f"policy:{table}.own",
                location=LOCATION,
                name="own",
                table=table,
                command="SELECT",
                using=using,
            )
        ],
        tables=[Table(name=table, columns=[Column(name="id", type="uuid")])],
    )


def _authz_gap(table: str = "profiles") -> Gap:
    return Gap(
        id=f"gap:authz:{table}:own",
        kind=TestKind.AUTHORIZATION,
        title=f"A user cannot read another user's rows in {table}",
        surfaces=[f"policy:{table}.own"],
        criticality=Criticality.CRITICAL,
    )


def test_owner_column_comes_from_the_policy_not_column_names():
    """`profiles` is scoped by `id`, which no name-based heuristic would find.

    The policy states it outright, so deriving it is strictly better than
    guessing — and it is the difference between a real test and an open
    question.
    """
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)

    assert scenario.fixtures[0].columns["__owner_column__"] == "id"
    assert scenario.open_questions == []


def test_a_policy_that_joins_is_admitted_as_unknown_rather_than_guessed():
    """A policy reaching through a join is not single-column ownership."""
    scan = _scan_with_policy(
        "exists (select 1 from orders o where o.id = payments.order_id and o.user_id = auth.uid())",
        table="payments",
    )
    scenario, _ = authoring.propose(_authz_gap("payments"), scan, CONFIG)

    assert scenario.fixtures == []
    assert scenario.open_questions
    assert "column" in scenario.open_questions[0]


def test_authorization_assertions_cite_the_policy_that_justifies_them():
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)

    assertion = scenario.then[0]
    assert assertion.provenance is Provenance.DERIVED
    assert "auth.uid() = id" in assertion.source
    assert assertion.is_evidence


def test_a_browser_scenario_without_probe_data_says_so():
    """Without observed selectors there is nothing honest to assert."""
    scan = ScanResult(
        project=ProjectInfo(root=".", framework="vite-react"),
        screens=[Screen(id="screen:orders", location=LOCATION, path="/orders", component="O")],
    )
    gap = Gap(
        id="gap:screen:orders",
        kind=TestKind.BROWSER_JOURNEY,
        title="/orders loads",
        surfaces=["screen:orders"],
    )
    scenario, _ = authoring.propose(gap, scan, CONFIG)

    assert scenario.open_questions
    assert "probe" in scenario.open_questions[0]
    assert scenario.then == []


def test_a_browser_scenario_uses_observed_selectors():
    scan = ScanResult(
        project=ProjectInfo(root=".", framework="vite-react"),
        screens=[Screen(id="screen:orders", location=LOCATION, path="/orders", component="O")],
    )
    probe = ProbeResult(
        entrypoint="local",
        base_url="http://localhost:5173",
        screens=[
            ObservedScreen(
                path="/orders",
                url="http://localhost:5173/orders",
                reachable=True,
                selectors=[
                    SelectorCandidate(
                        strategy=SelectorStrategy.TEST_ID,
                        value="page-heading",
                        role="heading",
                        description="Orders",
                    ),
                    SelectorCandidate(
                        strategy=SelectorStrategy.TEST_ID,
                        value="order-row",
                        description="A row",
                    ),
                ],
            )
        ],
    )
    gap = Gap(
        id="gap:screen:orders",
        kind=TestKind.BROWSER_JOURNEY,
        title="/orders loads",
        surfaces=["screen:orders"],
    )
    scenario, _ = authoring.propose(gap, scan, CONFIG, probe=probe)

    assert scenario.then
    assert all(a.provenance is Provenance.OBSERVED for a in scenario.then)
    assert {a.target.selector.value for a in scenario.then if a.target.selector} <= {
        "page-heading",
        "order-row",
    }


def test_enrichment_drops_selectors_the_prober_never_saw(tmp_path: Path):
    """A model that invents a selector would produce a test that cannot pass.

    Worse, it would look plausible in review.
    """
    scan = ScanResult(
        project=ProjectInfo(root=".", framework="vite-react"),
        screens=[Screen(id="screen:orders", location=LOCATION, path="/orders", component="O")],
    )
    probe = ProbeResult(
        entrypoint="local",
        base_url="http://x",
        screens=[
            ObservedScreen(
                path="/orders",
                url="http://x/orders",
                reachable=True,
                selectors=[SelectorCandidate(strategy=SelectorStrategy.TEST_ID, value="real-one")],
            )
        ],
    )
    gap = Gap(
        id="gap:screen:orders",
        kind=TestKind.BROWSER_JOURNEY,
        title="/orders loads",
        surfaces=["screen:orders"],
    )

    gateway = Gateway(ModelConfig(provider=ModelProvider.KIMI, model="m"), tmp_path)
    scenario_skeleton, _ = authoring.propose(gap, scan, CONFIG, probe=probe)
    request_context = authoring._enrichment_context(
        scenario_skeleton, gap, scan, None, probe.screens[0].selectors
    )
    from testtrout.llm.base import CompletionRequest

    request = CompletionRequest(
        system=authoring.load_prompt("propose_scenario"),
        user=request_context,
        schema=authoring.ENRICHMENT_SCHEMA,
        max_tokens=8192,
        effort=authoring.DEFAULT_EFFORT,
    )
    gateway.store.save(
        gateway.store.key("kimi", "m", request),
        "kimi",
        "m",
        request,
        CompletionResponse(
            text=json.dumps(
                {
                    "title": "Orders are listed",
                    "given": [],
                    "assertions": [
                        {
                            "kind": "visible",
                            "selector_value": "real-one",
                            "expected": None,
                            "reason": "observed",
                        },
                        {
                            "kind": "visible",
                            "selector_value": "invented",
                            "expected": None,
                            "reason": "made up",
                        },
                    ],
                    "open_questions": [],
                }
            ),
            model="m",
            provider="kimi",
        ),
    )

    scenario, warnings = authoring.propose(gap, scan, CONFIG, probe=probe, gateway=gateway)
    values = {a.target.selector.value for a in scenario.then if a.target.selector}
    assert values == {"real-one"}
    assert any("never observed" in w for w in warnings)


def test_rls_emitter_produces_a_test_that_cites_its_policy():
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)
    emitter = select_emitter(scenario)
    assert emitter is not None

    emitted = emitter.emit(scenario, CONFIG)
    assert "Do not edit this file" in emitted.content
    assert ".trout/scenarios/scenario_authz_profiles_own.yaml" in emitted.content
    assert "auth.uid() = id" in emitted.content
    assert "row['id'] !== me" in emitted.content
    assert "tests/trout/_supabase.ts" in emitted.shared


def test_emitters_are_deterministic():
    """Regenerating an unchanged suite must produce an empty diff."""
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)
    emitter = select_emitter(scenario)
    assert emitter is not None
    assert emitter.emit(scenario, CONFIG).content == emitter.emit(scenario, CONFIG).content


def test_a_scenario_with_open_questions_cannot_be_approved():
    """It would pass vacuously, which is worse than having no test."""
    scan = _scan_with_policy(
        "exists (select 1 from orders o where o.user_id = auth.uid())", table="payments"
    )
    scenario, _ = authoring.propose(_authz_gap("payments"), scan, CONFIG)
    assert not scenario.ready_to_approve


def test_blocking_requires_certification_and_real_evidence():
    """A certified test built only on guesses proves nothing worth blocking on."""
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)

    scenario.status = ScenarioStatus.APPROVED
    assert not scenario.blocking_eligible

    scenario.status = ScenarioStatus.CERTIFIED
    assert scenario.blocking_eligible

    for assertion in scenario.then:
        assertion.provenance = Provenance.INFERRED
    assert not scenario.blocking_eligible


def test_scenarios_round_trip_through_disk(tmp_path: Path):
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)
    save(tmp_path, scenario)

    index, problems = load_all(tmp_path)
    assert problems == []
    assert index.get(scenario.id) is not None
    assert index.get(scenario.id).title == scenario.title


def test_a_malformed_scenario_file_does_not_hide_the_good_ones(tmp_path: Path):
    scan = _scan_with_policy("auth.uid() = id")
    scenario, _ = authoring.propose(_authz_gap(), scan, CONFIG)
    save(tmp_path, scenario)
    (tmp_path / "broken.yaml").write_text("id: 1\nnot_a_field: true\n", encoding="utf-8")

    index, problems = load_all(tmp_path)
    assert len(index.scenarios) == 1
    assert len(problems) == 1
