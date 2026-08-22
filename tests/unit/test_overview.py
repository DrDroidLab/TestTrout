"""The product account, and what a rescan reports.

The overview is the first thing a person reads, so the tests here are about
whether it says something true and useful rather than whether it has the right
number of fields. Two properties matter most: coverage counts what is actually
asserted on, and a second scan reports movement rather than reprinting the
list.
"""

from __future__ import annotations

import pytest

from testtrout.domain.config import Config
from testtrout.domain.gap import TestKind
from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus
from testtrout.planning.overview import build, delta


def _certifying(surface_ids: list[str], status: ScenarioStatus) -> ScenarioIndex:
    return ScenarioIndex(
        scenarios=[
            Scenario(
                id="s1",
                title="a test",
                kind=TestKind.BROWSER_JOURNEY,
                surfaces=surface_ids,
                status=status,
            )
        ]
    )


@pytest.fixture
def overview(scanned):
    return build(scanned, ScenarioIndex())


def test_describes_the_product_in_product_language(overview) -> None:
    """Not a count of call sites."""
    assert overview.summary
    assert overview.pages
    assert overview.total_surfaces == len(overview.pages) + len(overview.apis) + len(
        overview.transactions
    )


def test_nothing_is_covered_without_tests(overview) -> None:
    assert overview.coverage.overall_percent == 0
    assert not any(page.covered for page in overview.pages)


def test_one_transaction_test_covers_what_it_touches(scanned, overview) -> None:
    """A transaction test exercises the page and the endpoints beneath it.

    This is why transactions are suggested first: the coverage it buys is
    wider than the one row it appears on.
    """
    transaction = overview.transactions[0]
    after = build(scanned, _certifying(transaction.surface_ids, ScenarioStatus.CERTIFIED))

    assert after.coverage.transactions_covered >= 1
    assert after.coverage.overall_percent > 0


def test_a_draft_test_does_not_count_as_coverage(scanned, overview) -> None:
    """A test held back for a question is not protecting anything yet."""
    transaction = overview.transactions[0]
    after = build(scanned, _certifying(transaction.surface_ids, ScenarioStatus.DRAFT))

    assert after.coverage.overall_percent == 0


def test_a_first_scan_reports_no_changes(overview) -> None:
    """Nothing is "new" relative to nothing.

    Listing the whole product as a change is true only vacuously, and it would
    bury the first real change under a wall of noise next time.
    """
    first = delta(None, overview)

    assert not first.has_changes
    assert first.new_areas == []
    assert first.still_missing


def test_rescan_with_no_change_reports_no_change(overview) -> None:
    assert not delta(overview, overview).has_changes


def test_rescan_reports_only_what_newly_became_covered(scanned, overview) -> None:
    """Something already tested last time is not news."""
    transaction = overview.transactions[0]
    covered = build(scanned, _certifying(transaction.surface_ids, ScenarioStatus.CERTIFIED))

    first = delta(overview, covered)
    assert f"transaction {transaction.name}" in first.newly_covered

    again = delta(covered, covered)
    assert not again.newly_covered


def test_a_removed_page_is_reported_as_gone(scanned, overview) -> None:
    shrunk = overview.model_copy(update={"pages": overview.pages[1:]})
    changed = delta(overview, shrunk)

    assert f"page {overview.pages[0].path}" in changed.gone
    assert not changed.new_areas


def test_what_is_left_leads_with_transactions(overview) -> None:
    """Working down the list in order should do the most per test."""
    missing = delta(None, overview).still_missing

    assert missing
    assert missing[0].startswith("transaction")
    assert len(missing) == overview.total_surfaces


def test_covered_things_drop_off_the_list(scanned, overview) -> None:
    transaction = overview.transactions[0]
    after = build(scanned, _certifying(transaction.surface_ids, ScenarioStatus.CERTIFIED))

    assert f"transaction {transaction.name}" not in delta(None, after).still_missing


def test_needs_from_you_comes_from_what_is_blocked(scanned) -> None:
    """Whatever readiness says is missing, said once, in one line."""
    from testtrout.planning.readiness import assess

    plan = assess(Config(), scanned)
    overview = build(scanned, ScenarioIndex(), plan)

    assert len(overview.needs_from_you) == len([b for b in plan.blocked if b.missing])
