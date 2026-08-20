"""Gap ranking. Deterministic, so every assertion here is exact."""

from __future__ import annotations

from testtrout.domain.gap import TestKind
from testtrout.domain.intent import Journey, ProductIntent, Provenance
from testtrout.domain.observation import CallKind, NetworkCall, ObservedScreen, ProbeResult
from testtrout.domain.surface import (
    Column,
    Criticality,
    DataOperation,
    Operation,
    Policy,
    ProjectInfo,
    ScanResult,
    Screen,
    SourceLocation,
    Table,
)
from testtrout.planning import gaps as planner
from testtrout.planning.existing_tests import ExistingTests

LOCATION = SourceLocation(file="src/x.tsx", line=1)


def _scan(**overrides: object) -> ScanResult:
    base = {
        "project": ProjectInfo(root=".", framework="vite-react", backend="supabase"),
        "screens": [
            Screen(
                id="screen:/orders",
                location=LOCATION,
                path="/orders",
                component="Orders",
                reaches=["data:orders.select"],
                criticality=Criticality.MEDIUM,
            )
        ],
        "data_operations": [
            DataOperation(
                id="data:orders.select",
                location=LOCATION,
                table="orders",
                operation=Operation.SELECT,
                criticality=Criticality.MEDIUM,
            ),
            DataOperation(
                id="data:orders.delete",
                location=LOCATION,
                table="orders",
                operation=Operation.DELETE,
                criticality=Criticality.CRITICAL,
            ),
        ],
        "tables": [Table(name="orders", columns=[Column(name="user_id", type="uuid")])],
    }
    base.update(overrides)
    return ScanResult(**base)  # type: ignore[arg-type]


def _gap(result, gap_id: str):
    return next(g for g in result.gaps if g.id == gap_id)


def test_reads_reachable_from_a_screen_are_folded_into_its_journey():
    """One browser test covers the reads it triggers; a second one is noise."""
    result = planner.build(_scan())
    ids = {g.id for g in result.gaps}
    assert "gap:data:data-orders-select" not in ids
    assert "gap:screen:orders" in ids
    assert "data:orders.select" in _gap(result, "gap:screen:orders").surfaces


def test_writes_always_get_their_own_targeted_assertion():
    """A page rendering proves nothing about what a delete actually did."""
    result = planner.build(_scan())
    assert any(g.id == "gap:data:data-orders-delete" for g in result.gaps)


def test_a_write_to_a_table_without_rls_outranks_everything():
    """No row-level security plus a browser-side write means world-writable."""
    result = planner.build(_scan())
    delete = _gap(result, "gap:data:data-orders-delete")
    assert any("no row-level security" in r for r in delete.reasons)
    assert result.ranked()[0].id == delete.id


def test_rls_removes_the_unprotected_write_boost():
    scan = _scan(
        tables=[
            Table(name="orders", rls_enabled=True, columns=[Column(name="user_id", type="uuid")])
        ]
    )
    delete = _gap(planner.build(scan), "gap:data:data-orders-delete")
    assert not any("no row-level security" in r for r in delete.reasons)


def test_stated_intent_raises_a_surface_and_says_so():
    """Intent must override the scanner's prior, and be explainable."""
    intent = ProductIntent(
        journeys=[
            Journey(
                id="journey:browse",
                name="Browse orders",
                criticality=Criticality.CRITICAL,
                surfaces=["screen:/orders"],
                provenance=Provenance.STATED,
            )
        ]
    )
    plain = _gap(planner.build(_scan()), "gap:screen:orders")
    raised = _gap(planner.build(_scan(), intent=intent), "gap:screen:orders")

    assert plain.criticality is Criticality.MEDIUM
    assert raised.criticality is Criticality.CRITICAL
    assert raised.score > plain.score
    assert any("stated intent" in r for r in raised.reasons)


def test_intent_never_lowers_a_surface():
    """A developer downplaying something must not hide a critical delete."""
    intent = ProductIntent(
        journeys=[
            Journey(
                id="journey:x",
                name="X",
                criticality=Criticality.LOW,
                surfaces=["data:orders.delete"],
            )
        ]
    )
    assert (
        _gap(planner.build(_scan(), intent=intent), "gap:data:data-orders-delete").criticality
        is Criticality.CRITICAL
    )


def test_authorization_gaps_are_blocked_without_two_roles():
    """One account cannot express "A cannot see B's data"."""
    scan = _scan(
        policies=[
            Policy(
                id="policy:orders.own",
                location=LOCATION,
                name="own",
                table="orders",
                command="ALL",
                using="auth.uid() = user_id",
            )
        ]
    )
    one_role = planner.build(scan, roles=["owner"])
    two_roles = planner.build(scan, roles=["owner", "member"])

    blocked = next(g for g in one_role.gaps if g.kind is TestKind.AUTHORIZATION)
    ready = next(g for g in two_roles.gaps if g.kind is TestKind.AUTHORIZATION)
    assert not blocked.ready
    assert blocked.blockers[0].code == "needs_two_roles"
    assert ready.ready


def test_policy_titles_read_as_english_not_sql():
    """`FOR ALL` is a keyword, not a verb.

    Regression test: the phrasing helper was once written but never wired to
    the call site, and nothing caught it because an unused function is not a
    lint error.
    """
    scan = _scan(
        policies=[
            Policy(
                id="policy:orders.own",
                location=LOCATION,
                name="own",
                table="orders",
                command="ALL",
            )
        ]
    )
    title = next(g for g in planner.build(scan).gaps if g.kind is TestKind.AUTHORIZATION).title
    assert "cannot all" not in title
    assert title == "A user without access cannot read or modify another user's rows in orders"


def test_an_observed_policy_denial_is_the_strongest_signal():
    scan = _scan(
        policies=[
            Policy(
                id="policy:orders.own",
                location=LOCATION,
                name="own",
                table="orders",
                command="SELECT",
            )
        ]
    )
    probe = ProbeResult(
        entrypoint="e",
        base_url="http://x",
        screens=[
            ObservedScreen(
                path="/orders",
                url="http://x/orders",
                reachable=True,
                calls=[
                    NetworkCall(
                        method="GET",
                        url="http://x/rest/v1/orders",
                        host="x",
                        kind=CallKind.SUPABASE_REST,
                        table="orders",
                        status=403,
                    )
                ],
            )
        ],
    )
    with_probe = planner.build(scan, probe=probe)
    without = planner.build(scan)
    a = next(g for g in with_probe.gaps if g.kind is TestKind.AUTHORIZATION)
    b = next(g for g in without.gaps if g.kind is TestKind.AUTHORIZATION)
    assert a.score > b.score
    assert any("deny" in r for r in a.reasons)


def test_an_unreachable_screen_is_blocked_rather_than_proposed():
    """A test for a route that will not load fails for unrelated reasons."""
    probe = ProbeResult(
        entrypoint="e",
        base_url="http://x",
        screens=[
            ObservedScreen(path="/orders", url="http://x/orders", reachable=False, note="404")
        ],
    )
    gap = _gap(planner.build(_scan(), probe=probe), "gap:screen:orders")
    assert not gap.ready
    assert gap.blockers[0].code == "unreachable"


def test_possible_existing_coverage_demotes_but_never_hides():
    """A heuristic match must not silently mark a critical surface protected."""
    existing = ExistingTests(
        files=["tests/orders.spec.ts"], by_surface={"data:orders.delete": ["tests/orders.spec.ts"]}
    )
    demoted = _gap(planner.build(_scan(), existing=existing), "gap:data:data-orders-delete")
    plain = _gap(planner.build(_scan()), "gap:data:data-orders-delete")
    assert demoted.score < plain.score
    assert demoted.id in {g.id for g in planner.build(_scan(), existing=existing).gaps}


def test_every_gap_explains_its_own_rank():
    """A ranking that cannot be interrogated is just an assertion of taste."""
    for gap in planner.build(_scan()).gaps:
        assert gap.reasons, f"{gap.id} has no reasons"


def test_notes_flag_missing_evidence():
    notes = " ".join(planner.build(_scan()).notes)
    assert "intent" in notes
    assert "probe" in notes


def test_budget_returns_the_most_valuable_suite_that_fits():
    result = planner.build(_scan(), roles=["owner", "member"])
    chosen = result.budget(seconds=10)
    assert sum(g.estimated_seconds for g in chosen) <= 10
    assert all(g.ready for g in chosen)
