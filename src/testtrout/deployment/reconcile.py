"""Comparing what the code says against what the deployment does.

This is the payoff of probing. Each divergence is a concrete question a
developer can answer in seconds and that no amount of static analysis could
have raised on its own:

* a route the code declares that nobody can actually reach;
* a table being queried at runtime that no call site explains;
* a call site in the code that never fired — a blind spot for any suite built
  from observation alone;
* a row-level security policy denying a read the interface plainly expects to
  succeed;
* a third-party host contacted that the substitution boundary does not cover.

Divergences are observations, never verdicts. Several have innocent
explanations, and the message says so where that is true. Reporting a real
finding as a certainty and being wrong costs more trust than it buys.
"""

from __future__ import annotations

from testtrout.deployment.network import is_noise
from testtrout.deployment.selectors import best_strategy
from testtrout.domain.observation import CallKind, Divergence, ProbeResult, SelectorStrategy
from testtrout.domain.surface import Operation, ScanResult

# Console noise that says nothing about application correctness.
_IGNORED_CONSOLE = ("favicon", "sourcemap", "source map", "devtools", "react devtools")


def reconcile(scan: ScanResult, probe: ProbeResult) -> list[Divergence]:
    """Produce every divergence between the static scan and one probe run."""
    findings: list[Divergence] = []
    findings.extend(_unreachable_screens(scan, probe))
    findings.extend(_auth_walls(probe))
    findings.extend(_policy_denials(probe))
    findings.extend(_undeclared_tables(scan, probe))
    findings.extend(_unexercised_operations(scan, probe))
    findings.extend(_undeclared_externals(scan, probe))
    findings.extend(_blocked_writes(probe))
    findings.extend(_console_errors(probe))
    findings.extend(_weak_selectors(probe))
    return findings


def _unreachable_screens(scan: ScanResult, probe: ProbeResult) -> list[Divergence]:
    """Routes declared in code that did not load."""
    return [
        Divergence(
            code="unreachable_screen",
            message=f"{screen.path} is declared in code but could not be loaded",
            surface_id=_screen_id(scan, screen.path),
            detail=screen.note,
        )
        for screen in probe.screens
        if not screen.reachable
    ]


def _auth_walls(probe: ProbeResult) -> list[Divergence]:
    """Routes that redirected to a sign-in screen.

    Informational when signed out — that is the guard working. A finding when
    signed in, because it means either the session did not take or the role
    lacks access, and both change what a generated test should expect.
    """
    findings: list[Divergence] = []
    for screen in probe.screens:
        if not screen.requires_auth:
            continue
        if probe.authenticated:
            findings.append(
                Divergence(
                    code="auth_wall_while_signed_in",
                    message=(
                        f"{screen.path} redirected to sign-in even though role "
                        f"{probe.role!r} was authenticated"
                    ),
                    detail=(
                        "Either the session was not accepted by the app, or this role is "
                        "genuinely not permitted here. Both are worth knowing before "
                        "generating tests for this screen."
                    ),
                )
            )
        else:
            findings.append(
                Divergence(
                    code="protected_route",
                    message=f"{screen.path} requires authentication",
                    detail="Confirmed by probing signed out.",
                )
            )
    return findings


def _policy_denials(probe: ProbeResult) -> list[Divergence]:
    """Backend refusals, which usually mean row-level security rejected a request."""
    findings: list[Divergence] = []
    for screen in probe.screens:
        for call in screen.calls:
            if not call.denied or call.kind is not CallKind.SUPABASE_REST:
                continue
            findings.append(
                Divergence(
                    code="policy_denial",
                    message=(
                        f"{screen.path}: {call.method} on {call.table or 'unknown table'} "
                        f"was denied ({call.status})"
                    ),
                    detail=(
                        "Row-level security refused this request. If the screen is supposed "
                        "to show this data, the policy and the interface disagree — which is "
                        "exactly the kind of bug an authorization test would pin down."
                    ),
                )
            )
    return findings


def _undeclared_tables(scan: ScanResult, probe: ProbeResult) -> list[Divergence]:
    """Tables queried at runtime that no static call site accounts for."""
    declared = {op.table for op in scan.data_operations if op.table}
    observed = {
        call.table
        for call in probe.all_calls()
        if call.kind is CallKind.SUPABASE_REST and call.table
    }
    return [
        Divergence(
            code="undeclared_table",
            message=f"{table!r} is queried at runtime but no call site in the code resolves to it",
            detail=(
                "Usually a computed table name the scan could not resolve, or a query made "
                "from a dependency. Either way it is untracked, so nothing will generate a "
                "test for it."
            ),
        )
        for table in sorted(observed - declared)
    ]


def _unexercised_operations(scan: ScanResult, probe: ProbeResult) -> list[Divergence]:
    """Read operations in the code that never fired during the probe.

    Restricted to reads on purpose: writes are not expected to fire during a
    read-only probe, so reporting them would bury the signal in noise.
    """
    fired = {
        (call.table, call.operation)
        for call in probe.all_calls()
        if call.kind is CallKind.SUPABASE_REST
    }
    findings: list[Divergence] = []
    for operation in scan.data_operations:
        if operation.operation is not Operation.SELECT or not operation.table:
            continue
        if (operation.table, "select") in fired:
            continue
        findings.append(
            Divergence(
                code="unexercised_read",
                message=f"{operation.id} never fired while probing",
                surface_id=operation.id,
                detail=(
                    "It may sit behind an interaction the probe does not perform, since the "
                    "probe only navigates and never clicks. Worth confirming it is reachable "
                    "before relying on a test for it."
                ),
            )
        )
    return findings


def _undeclared_externals(scan: ScanResult, probe: ProbeResult) -> list[Divergence]:
    """Third-party hosts contacted that the substitution boundary does not cover."""
    known = {host for external in scan.externals for host in external.hosts}
    return [
        Divergence(
            code="undeclared_external",
            message=f"{host} was contacted but is not a recognised dependency",
            detail=(
                "It will not be substituted during a test run, so tests will reach it for "
                "real. Add it to `substitution.external` in .trout/config.yaml if that matters."
            ),
        )
        for host in probe.external_hosts
        if host not in known and not is_noise(host)
    ]


def _blocked_writes(probe: ProbeResult) -> list[Divergence]:
    """Writes the safety guard refused to let through."""
    blocked = [call for call in probe.all_calls() if call.blocked]
    if not blocked:
        return []
    summary = sorted({f"{c.method} {c.table or c.host}" for c in blocked})
    return [
        Divergence(
            code="write_blocked",
            message=f"{len(blocked)} mutating request(s) were blocked: {', '.join(summary[:6])}",
            detail=(
                "This deployment is not marked disposable, so writes were refused at the "
                "network layer. These screens write on load — worth knowing, because they "
                "cannot be tested against a shared or production deployment."
            ),
        )
    ]


def _console_errors(probe: ProbeResult) -> list[Divergence]:
    """Runtime errors logged by the application itself."""
    findings: list[Divergence] = []
    for screen in probe.screens:
        real = [
            error
            for error in screen.console_errors
            if not any(token in error.lower() for token in _IGNORED_CONSOLE)
        ]
        if real:
            findings.append(
                Divergence(
                    code="console_error",
                    message=f"{screen.path} logged {len(real)} console error(s)",
                    detail=real[0],
                )
            )
    return findings


def _weak_selectors(probe: ProbeResult) -> list[Divergence]:
    """Screens with nothing durable to write a test against."""
    findings: list[Divergence] = []
    for screen in probe.screens:
        if not screen.reachable:
            continue
        best = best_strategy(screen.selectors)
        if best is None:
            findings.append(
                Divergence(
                    code="no_selectors",
                    message=f"{screen.path} exposes no addressable elements",
                    detail="The screen may have rendered empty. Check the console errors.",
                )
            )
        elif best.rank >= SelectorStrategy.TEXT.rank:
            findings.append(
                Divergence(
                    code="weak_selectors",
                    message=f"{screen.path} has nothing more stable than visible text to target",
                    detail=(
                        "Tests generated for this screen would break on a copy change. "
                        "Adding data-testid attributes to its key elements is the cheapest fix."
                    ),
                )
            )
    return findings


def _screen_id(scan: ScanResult, path: str) -> str | None:
    """Look up the stable surface id for a route path."""
    return next((s.id for s in scan.screens if s.path == path), None)
