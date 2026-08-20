from testtrout.analysis.criticality import score_data_operation, score_policy
from testtrout.domain.surface import Criticality, DataOperation, Operation, Policy, SourceLocation

LOCATION = SourceLocation(file="src/x.tsx", line=1)


def _op(operation: Operation, table: str | None = "widgets", filters: list[str] | None = None):
    return DataOperation(
        id="x", location=LOCATION, table=table, operation=operation, filters=filters or []
    )


def test_delete_is_critical():
    level, reasons = score_data_operation(_op(Operation.DELETE, filters=["eq(id)"]))
    assert level is Criticality.CRITICAL
    assert any("not recoverable" in r for r in reasons)


def test_unfiltered_update_is_critical():
    level, _ = score_data_operation(_op(Operation.UPDATE, filters=[]))
    assert level is Criticality.CRITICAL


def test_insert_without_filters_is_not_treated_as_unscoped():
    """Inserts never take filters, so the unscoped-write rule must not fire."""
    level, reasons = score_data_operation(_op(Operation.INSERT, table="widgets"))
    assert level is Criticality.HIGH
    assert not any("every visible row" in r for r in reasons)


def test_every_score_carries_its_reasons():
    """A score with no explanation is not usable in a report."""
    for operation in (Operation.SELECT, Operation.INSERT, Operation.DELETE, Operation.AUTH):
        _, reasons = score_data_operation(_op(operation))
        assert reasons


def test_policy_granting_anonymous_access_is_critical():
    policy = Policy(
        id="p",
        location=LOCATION,
        name="public read",
        table="widgets",
        command="SELECT",
        roles=["anon"],
    )
    level, reasons = score_policy(policy)
    assert level is Criticality.CRITICAL
    assert any("anonymous" in r for r in reasons)
