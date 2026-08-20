"""Building the ranked gap map.

Entirely deterministic: scan + probe + intent in, ranked plan out. No model.
That constraint is what makes the ranking arguable — every score is the sum of
named contributions, and a developer who disagrees can point at the rule rather
than at a vibe.

The ranking answers one question: *given no tests and limited time, what should
exist first?* The strongest signals are the ones that combine severity with
evidence — a table written from the browser with no row-level security, or a
policy the probe watched deny a read the interface clearly expects to work.
Those are not merely untested; they are probably already broken.
"""

from __future__ import annotations

from testtrout.domain.gap import Blocker, Coverage, Gap, GapMap, TestKind
from testtrout.domain.intent import Journey, ProductIntent, Provenance
from testtrout.domain.observation import CallKind, ProbeResult
from testtrout.domain.scenario import ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import (
    Criticality,
    DataOperation,
    Operation,
    Policy,
    ScanResult,
    Screen,
)
from testtrout.planning.existing_tests import ExistingTests

# Base weight by criticality. The gaps between tiers are wide on purpose: one
# critical surface should outrank several medium ones rather than losing to
# them on volume.
_BASE = {
    Criticality.CRITICAL: 100.0,
    Criticality.HIGH: 60.0,
    Criticality.MEDIUM: 30.0,
    Criticality.LOW: 10.0,
}

# Additive signals. Each is worth roughly a tier, so two weak signals do not
# outweigh one genuine severity difference.
BOOST_STATED_INTENT = 40.0
BOOST_OBSERVED_DENIAL = 55.0
BOOST_UNPROTECTED_WRITE = 65.0
BOOST_OBSERVED_FIRING = 10.0
PENALTY_POSSIBLY_COVERED = -70.0
PENALTY_UNREACHABLE = -25.0


def build(
    scan: ScanResult,
    intent: ProductIntent | None = None,
    probe: ProbeResult | None = None,
    existing: ExistingTests | None = None,
    roles: list[str] | None = None,
    scenarios: ScenarioIndex | None = None,
) -> GapMap:
    """Produce the ranked gap map.

    Args:
        scan: The static surface map. Required.
        intent: Stated product intent, which raises the surfaces a developer
            said they care about above the scanner's own prior.
        probe: Observations from a running deployment. Adds confidence, and
            supplies the strongest single signal — a policy denial actually
            witnessed.
        existing: Possible existing coverage, so the tool does not propose what
            is already there.
        roles: Configured test-user roles. Authorization tests need two, so
            this decides whether those gaps are actionable or blocked.
        scenarios: Scenarios already accepted into the suite. These are
            authoritative coverage — unlike the heuristic file matcher, we know
            exactly which surfaces they assert on — so their gaps are closed
            rather than re-proposed.
    """
    existing = existing or ExistingTests()
    context = _Context(scan, intent, probe, existing, roles or [], scenarios)

    gaps: list[Gap] = []
    gaps.extend(_policy_gaps(scan, context))
    gaps.extend(_screen_gaps(scan, context))
    gaps.extend(_operation_gaps(scan, context))
    gaps.extend(_endpoint_gaps(scan, context))

    open_gaps = [g for g in gaps if g.id not in context.closed_gaps]
    return GapMap(
        coverage=_coverage(scan, existing, context.covered_surfaces),
        gaps=sorted(open_gaps, key=lambda g: (-g.score, g.criticality.rank, g.id)),
        notes=_notes(context, closed=len(gaps) - len(open_gaps)),
    )


class _Context:
    """Precomputed lookups shared by every gap builder."""

    def __init__(
        self,
        scan: ScanResult,
        intent: ProductIntent | None,
        probe: ProbeResult | None,
        existing: ExistingTests,
        roles: list[str],
        scenarios: ScenarioIndex | None = None,
    ) -> None:
        self.scan = scan
        self.intent = intent
        self.probe = probe
        self.existing = existing
        self.roles = sorted(set(roles))

        # A scenario that a human accepted closes its gap outright. This is
        # stronger than the heuristic file matcher: we know which surfaces it
        # asserts on, rather than guessing from a table name appearing in a file.
        accepted = [
            s
            for s in (scenarios.scenarios if scenarios else [])
            if s.status in {ScenarioStatus.APPROVED, ScenarioStatus.CERTIFIED}
        ]
        self.closed_gaps = {s.gap_id for s in accepted if s.gap_id}
        self.covered_surfaces = {sid for s in accepted for sid in s.surfaces}
        self.rls_tables = {t.name for t in scan.tables if t.rls_enabled}
        self.known_tables = {t.name for t in scan.tables}

        self.unreachable: set[str] = set()
        self.denied_tables: set[str] = set()
        self.fired_tables: set[str] = set()
        if probe is not None:
            by_path = {s.path: s for s in probe.screens}
            self.unreachable = {
                screen.id
                for screen in scan.screens
                if (observed := by_path.get(screen.path)) is not None and not observed.reachable
            }
            for call in probe.all_calls():
                if call.kind is not CallKind.SUPABASE_REST or not call.table:
                    continue
                self.fired_tables.add(call.table)
                if call.denied:
                    self.denied_tables.add(call.table)

        # Reads reachable from a screen are covered by that screen's journey
        # test; writes always earn a targeted assertion of their own, because a
        # page rendering proves nothing about what the write did.
        self.reads_in_journeys = {
            operation_id
            for screen in scan.screens
            for operation_id in screen.reaches
            if _is_read(scan, operation_id)
        }

    def ambiguous(self, operation: DataOperation) -> bool:
        """Whether another call site performs the same operation on the same table."""
        return (
            sum(
                1
                for other in self.scan.data_operations
                if other.table == operation.table and other.operation is operation.operation
            )
            > 1
        )

    def criticality(self, surface_id: str, default: Criticality) -> tuple[Criticality, list[str]]:
        """Apply stated intent on top of the scanner's deterministic prior."""
        if self.intent is None:
            return default, []
        stated = self.intent.criticality_for(surface_id)
        if stated is None or stated.rank >= default.rank:
            return default, []
        journey = next((j for j in self.intent.journeys if surface_id in j.surfaces), None)
        label = f" ({journey.name})" if journey else ""
        return stated, [f"raised to {stated.value} by stated intent{label}"]


def _is_read(scan: ScanResult, operation_id: str) -> bool:
    """Whether a data operation id refers to a read."""
    operation = next((o for o in scan.data_operations if o.id == operation_id), None)
    return operation is not None and operation.operation is Operation.SELECT


def _score(base: Criticality, contributions: list[tuple[float, str]]) -> tuple[float, list[str]]:
    """Sum the base weight and every named contribution."""
    total = _BASE[base]
    reasons = [f"{base.value} surface"]
    for amount, reason in contributions:
        total += amount
        reasons.append(reason)
    return max(total, 0.0), reasons


def _policy_gaps(scan: ScanResult, context: _Context) -> list[Gap]:
    """One authorization test per row-level security policy.

    The cheapest high-value tests available. The policy already states the
    expectation — "a user may read rows where auth.uid() = user_id" — so the
    test writes itself, and the failure it catches (one tenant reading
    another's data) is the worst thing these applications do.
    """
    gaps: list[Gap] = []
    for policy in scan.policies:
        criticality, intent_reasons = context.criticality(policy.id, policy.criticality)
        contributions: list[tuple[float, str]] = [(BOOST_STATED_INTENT, r) for r in intent_reasons]
        if policy.table in context.denied_tables:
            contributions.append(
                (
                    BOOST_OBSERVED_DENIAL,
                    f"probe saw {policy.table} deny a request the interface expected to succeed",
                )
            )
        if context.existing.covers(policy.id):
            contributions.append((PENALTY_POSSIBLY_COVERED, "a test file mentions this policy"))

        blockers: list[Blocker] = []
        if len(context.roles) < 2:
            blockers.append(
                Blocker(
                    code="needs_two_roles",
                    message=(
                        "Two test-user roles are required: proving one user cannot see "
                        "another's data needs a second user. Add one with `trout init`."
                    ),
                )
            )

        score, reasons = _score(criticality, contributions)
        gaps.append(
            Gap(
                id=f"gap:authz:{policy.table}:{_slug(policy.name)}",
                kind=TestKind.AUTHORIZATION,
                title=_policy_title(policy),
                surfaces=[policy.id],
                criticality=criticality,
                score=score,
                reasons=[*reasons, f"policy: {policy.using or policy.name}"],
                provenance=Provenance.DERIVED,
                blockers=blockers,
                estimated_seconds=TestKind.AUTHORIZATION.typical_runtime_seconds,
            )
        )
    return gaps


def _policy_title(policy: Policy) -> str:
    """State the authorization expectation as a readable sentence.

    ``FOR ALL`` is a SQL keyword, not a verb, so it needs its own phrasing —
    "cannot all rows in orders" reads as a bug in the tool.
    """
    verbs = {
        "SELECT": "read",
        "INSERT": "create",
        "UPDATE": "modify",
        "DELETE": "delete",
    }
    if policy.command.upper() == "ALL":
        return f"A user without access cannot read or modify another user's rows in {policy.table}"
    verb = verbs.get(policy.command.upper(), policy.command.lower())
    return f"A user without access cannot {verb} another user's rows in {policy.table}"


def _screen_gaps(scan: ScanResult, context: _Context) -> list[Gap]:
    """One browser journey per screen, covering the reads it triggers."""
    gaps: list[Gap] = []
    for screen in scan.screens:
        criticality, intent_reasons = context.criticality(screen.id, screen.criticality)
        contributions: list[tuple[float, str]] = [(BOOST_STATED_INTENT, r) for r in intent_reasons]
        blockers: list[Blocker] = []

        if screen.id in context.unreachable:
            contributions.append((PENALTY_UNREACHABLE, "the probe could not load this screen"))
            blockers.append(
                Blocker(
                    code="unreachable",
                    message=(
                        "The probe could not load this route. A test for it would fail for "
                        "reasons unrelated to the product. Resolve that first."
                    ),
                )
            )
        if context.existing.covers(screen.id):
            contributions.append((PENALTY_POSSIBLY_COVERED, "a test file mentions this route"))

        reads = [op for op in screen.reaches if _is_read(scan, op)]
        if reads:
            contributions.append((0.0, f"covers {len(reads)} read(s) reachable from this screen"))

        journey = _matching_journey(context, screen)
        score, reasons = _score(criticality, contributions)
        gaps.append(
            Gap(
                id=f"gap:screen:{_slug(screen.path)}",
                kind=TestKind.BROWSER_JOURNEY,
                title=f"{screen.path} loads and shows its data",
                surfaces=[screen.id, *reads],
                criticality=criticality,
                score=score,
                reasons=reasons,
                provenance=Provenance.STATED if journey else Provenance.DERIVED,
                journey_id=journey.id if journey else None,
                blockers=blockers,
                estimated_seconds=TestKind.BROWSER_JOURNEY.typical_runtime_seconds,
            )
        )
    return gaps


def _operation_gaps(scan: ScanResult, context: _Context) -> list[Gap]:
    """A targeted assertion for every write, and for reads no screen reaches."""
    gaps: list[Gap] = []
    for operation in scan.data_operations:
        if operation.id in context.reads_in_journeys:
            continue  # already covered by the screen's browser journey

        criticality, intent_reasons = context.criticality(operation.id, operation.criticality)
        contributions: list[tuple[float, str]] = [(BOOST_STATED_INTENT, r) for r in intent_reasons]

        table = operation.table
        if (
            operation.operation.writes
            and table
            and table in context.known_tables
            and table not in context.rls_tables
        ):
            contributions.append(
                (
                    BOOST_UNPROTECTED_WRITE,
                    f"{table} is written from the browser with no row-level security — "
                    "anyone with the anon key can write to it",
                )
            )
        if table and table in context.fired_tables:
            contributions.append((BOOST_OBSERVED_FIRING, "confirmed firing during the probe"))
        if context.existing.covers(operation.id):
            contributions.append((PENALTY_POSSIBLY_COVERED, "a test file mentions this table"))

        blockers: list[Blocker] = []
        if table is None and operation.operation not in {Operation.AUTH, Operation.REALTIME}:
            blockers.append(
                Blocker(
                    code="unresolved_table",
                    message=(
                        "The table name is computed, so the scan could not resolve it. "
                        "Tell the tool which table this touches."
                    ),
                )
            )

        score, reasons = _score(criticality, contributions)
        gaps.append(
            Gap(
                id=f"gap:data:{_slug(operation.id)}",
                kind=TestKind.DATA_OPERATION,
                title=_operation_title(operation, context),
                surfaces=[operation.id],
                criticality=criticality,
                score=score,
                reasons=reasons,
                provenance=Provenance.DERIVED,
                blockers=blockers,
                estimated_seconds=TestKind.DATA_OPERATION.typical_runtime_seconds,
            )
        )
    return gaps


def _endpoint_gaps(scan: ScanResult, context: _Context) -> list[Gap]:
    """One test per first-party endpoint and server action."""
    gaps: list[Gap] = []
    for endpoint in scan.endpoints:
        criticality, intent_reasons = context.criticality(endpoint.id, endpoint.criticality)
        score, reasons = _score(criticality, [(BOOST_STATED_INTENT, r) for r in intent_reasons])
        gaps.append(
            Gap(
                id=f"gap:endpoint:{_slug(endpoint.path)}",
                kind=TestKind.ENDPOINT,
                title=f"{'/'.join(endpoint.methods)} {endpoint.path} behaves correctly",
                surfaces=[endpoint.id],
                criticality=criticality,
                score=score,
                reasons=reasons,
                estimated_seconds=TestKind.ENDPOINT.typical_runtime_seconds,
            )
        )
    for action in scan.server_actions:
        criticality, intent_reasons = context.criticality(action.id, action.criticality)
        score, reasons = _score(
            criticality,
            [
                *[(BOOST_STATED_INTENT, r) for r in intent_reasons],
                (0.0, "server actions are callable endpoints with no framework-level auth"),
            ],
        )
        gaps.append(
            Gap(
                id=f"gap:action:{_slug(action.name)}",
                kind=TestKind.ENDPOINT,
                title=f"{action.name}() rejects unauthorised callers and validates its input",
                surfaces=[action.id],
                criticality=criticality,
                score=score,
                reasons=reasons,
                estimated_seconds=TestKind.ENDPOINT.typical_runtime_seconds,
            )
        )
    return gaps


def _operation_title(operation: DataOperation, context: _Context) -> str:
    """A one-line statement of what the test would assert.

    Two call sites doing the same thing to the same table are two genuinely
    different tests — a refund and a checkout both update `orders`, but not to
    the same value. Identical titles make that look like a duplication bug, so
    the component is named whenever more than one call site collides.
    """
    if operation.operation is Operation.AUTH:
        return f"Authentication ({operation.function or 'sign-in'}) works"
    if operation.operation is Operation.RPC:
        return f"{operation.function or 'rpc'}() produces the expected result"
    if operation.operation is Operation.STORAGE:
        return f"Uploads to {operation.bucket or 'storage'} succeed and are readable"
    table = operation.table or "an unresolved table"
    verbs = {
        Operation.SELECT: "returns the expected rows from",
        Operation.INSERT: "creates a valid row in",
        Operation.UPDATE: "updates only the intended row in",
        Operation.UPSERT: "upserts correctly into",
        Operation.DELETE: "removes only the intended row from",
    }
    title = f"The app {verbs.get(operation.operation, 'operates on')} {table}"
    if context.ambiguous(operation) and operation.in_component:
        title += f" (from {operation.in_component})"
    return title


def _matching_journey(context: _Context, screen: Screen) -> Journey | None:
    """The stated journey that includes this screen, if any."""
    if context.intent is None:
        return None
    return next((j for j in context.intent.journeys if screen.id in j.surfaces), None)


def _coverage(scan: ScanResult, existing: ExistingTests, by_scenario: set[str]) -> Coverage:
    """Count what is currently protected.

    An accepted scenario counts, and so does a heuristic file match — but the
    two are not equivalent, and only the first is something the tool actually
    knows.
    """

    def covered(surface_id: str) -> bool:
        return surface_id in by_scenario or existing.covers(surface_id)

    surfaces = scan.all_surfaces()
    critical = [s for s in surfaces if s.criticality is Criticality.CRITICAL]
    policies: list[Policy] = scan.policies
    return Coverage(
        total_surfaces=len(surfaces),
        covered_surfaces=sum(1 for s in surfaces if covered(s.id)),
        critical_total=len(critical),
        critical_covered=sum(1 for s in critical if covered(s.id)),
        policies_total=len(policies),
        policies_covered=sum(1 for p in policies if covered(p.id)),
    )


def _notes(context: _Context, closed: int = 0) -> list[str]:
    """Caveats about the analysis, so the ranking is read with the right weight."""
    notes: list[str] = []
    if closed:
        notes.append(
            f"{closed} gap(s) are already covered by an accepted scenario and are not "
            "listed. See `trout scenarios`."
        )
    if context.intent is None:
        notes.append(
            "No product intent captured. Ranking uses code signals only — run `trout intent` "
            "so the things you actually care about outrank the things that merely look risky."
        )
    if context.probe is None:
        notes.append(
            "No probe data. Nothing here is confirmed against a running deployment, so some "
            "of these surfaces may not be reachable at all. Run `trout probe`."
        )
    if len(context.roles) < 2:
        notes.append(
            "Fewer than two test-user roles are configured, so authorization tests cannot be "
            "written yet — they need a second user to be possible at all."
        )
    if context.existing.found_any:
        notes.append(
            f"{len(context.existing.files)} existing test file(s) found. Matches are heuristic "
            "and reported as *possible* coverage; verify before trusting them."
        )
    return notes


def _slug(value: str) -> str:
    """Normalise a fragment for use in a gap id."""
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"
