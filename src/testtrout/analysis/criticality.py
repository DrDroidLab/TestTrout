"""Criticality scoring — deterministic, explainable, and conservative.

Ranking decides which tests get proposed first, so it has to be defensible.
Every score carries the reasons that produced it, and every reason is a fact
about the code rather than a model's opinion. A developer who disagrees can
override it in the intent document; nothing here silently overrules them.

The priors are tuned for this stack specifically: in an application where the
database is reached directly from the browser, the operations that write data
and the policies that guard it are where damage happens.
"""

from __future__ import annotations

from testtrout.domain.surface import (
    Criticality,
    DataOperation,
    Endpoint,
    ExternalDependency,
    Operation,
    Policy,
    ScanResult,
    Screen,
    ServerAction,
)

# Table name fragments that imply money, identity, or irreversible consequence.
_SENSITIVE = (
    "payment",
    "invoice",
    "subscription",
    "charge",
    "billing",
    "order",
    "credit",
    "wallet",
    "transaction",
    "user",
    "profile",
    "account",
    "member",
    "permission",
    "role",
    "api_key",
    "secret",
    "token",
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _sensitive(name: str | None) -> bool:
    """Whether a table or route name suggests sensitive data."""
    return bool(name) and any(fragment in (name or "").lower() for fragment in _SENSITIVE)


def score_data_operation(operation: DataOperation) -> tuple[Criticality, list[str]]:
    """Score a Supabase call site."""
    reasons: list[str] = []
    level = Criticality.LOW

    if operation.operation is Operation.SELECT:
        level = Criticality.MEDIUM
        reasons.append("reads data")
    elif operation.operation is Operation.DELETE:
        level = Criticality.CRITICAL
        reasons.append("deletes data — failure is not recoverable by the user")
    elif operation.operation is Operation.AUTH:
        level = Criticality.CRITICAL
        reasons.append("authentication — a break locks every user out")
    elif operation.operation.writes:
        level = Criticality.HIGH
        reasons.append(f"writes data ({operation.operation.value})")

    if _sensitive(operation.table):
        reasons.append(f"table {operation.table!r} holds sensitive or financial data")
        if level.rank > Criticality.HIGH.rank:
            level = Criticality.HIGH

    # An unscoped UPDATE or DELETE rewrites or removes every row the caller can
    # see. Only these two take filters — an INSERT or RPC legitimately has none,
    # so the rule must not fire for them.
    if operation.operation in {Operation.UPDATE, Operation.DELETE} and not operation.filters:
        reasons.append("no filter applied — this rewrites or removes every visible row")
        level = Criticality.CRITICAL

    return level, reasons


def score_policy(policy: Policy) -> tuple[Criticality, list[str]]:
    """Score a row-level security policy.

    Policies are never below HIGH: an authorization failure exposes one user's
    data to another, which is the worst common failure in this stack.
    """
    reasons = [f"row-level security on {policy.table!r} for {policy.command}"]
    level = Criticality.HIGH
    if policy.command.upper() == "ALL":
        reasons.append("governs every operation on the table")
        level = Criticality.CRITICAL
    if _sensitive(policy.table):
        reasons.append("guards sensitive data")
        level = Criticality.CRITICAL
    if "anon" in policy.roles:
        reasons.append("grants access to anonymous users")
        level = Criticality.CRITICAL
    return level, reasons


def score_endpoint(endpoint: Endpoint) -> tuple[Criticality, list[str]]:
    """Score an HTTP endpoint by the methods it exposes."""
    mutating = sorted(set(endpoint.methods) & _MUTATING_METHODS)
    if mutating:
        return Criticality.HIGH, [f"accepts {', '.join(mutating)}"]
    return Criticality.MEDIUM, ["read-only endpoint"]


def score_server_action(action: ServerAction) -> tuple[Criticality, list[str]]:
    """Score a Server Action.

    Uniformly HIGH: it is a callable server endpoint whose authorization is the
    developer's responsibility and is frequently forgotten.
    """
    return Criticality.HIGH, ["server action — a callable endpoint with no framework-level auth"]


def score_external(external: ExternalDependency) -> tuple[Criticality, list[str]]:
    """Score a third-party dependency by whether real calls have consequences."""
    if external.side_effecting:
        return Criticality.HIGH, [f"{external.vendor} calls cost money or reach real users"]
    return Criticality.LOW, [f"{external.vendor} is observability or analytics"]


def score_screen(screen: Screen, result: ScanResult) -> tuple[Criticality, list[str]]:
    """Score a screen from the operations reachable through it.

    A screen inherits the risk of what it can trigger. A page that can delete an
    order matters more than a settings page that only reads, regardless of how
    either looks.
    """
    operations = [op for op in result.data_operations if op.id in screen.reaches]
    endpoints = [e for e in result.endpoints if e.id in screen.reaches]
    if not operations and not endpoints:
        return Criticality.LOW, ["no backend calls resolved from this screen"]

    levels = [x.criticality for x in (*operations, *endpoints)]
    highest = min(levels, key=lambda c: c.rank)

    reasons: list[str] = []
    if operations:
        reasons.append(f"reaches {len(operations)} data operation(s)")
        writes = [op for op in operations if op.operation.writes]
        if writes:
            reasons.append(f"including {len(writes)} that write data")
    if endpoints:
        reasons.append(f"calls {len(endpoints)} endpoint(s)")
        mutating = [e for e in endpoints if set(e.methods) & {"POST", "PUT", "PATCH", "DELETE"}]
        if mutating:
            reasons.append(f"including {len(mutating)} that change data")
    return highest, reasons


def apply(result: ScanResult) -> ScanResult:
    """Score every surface in a scan result, in dependency order.

    Screens are scored last because their score is derived from the data
    operations they reach, which must be scored first.
    """
    for operation in result.data_operations:
        operation.criticality, operation.criticality_reasons = score_data_operation(operation)
    for policy in result.policies:
        policy.criticality, policy.criticality_reasons = score_policy(policy)
    for endpoint in result.endpoints:
        endpoint.criticality, endpoint.criticality_reasons = score_endpoint(endpoint)
    for action in result.server_actions:
        action.criticality, action.criticality_reasons = score_server_action(action)
    for external in result.externals:
        external.criticality, external.criticality_reasons = score_external(external)
    for screen in result.screens:
        screen.criticality, screen.criticality_reasons = score_screen(screen, result)
    return result
