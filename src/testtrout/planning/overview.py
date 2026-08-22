"""Turning a scan into an account of the product a person recognises.

Deterministic. The structure is read out of the code — which pages exist, what
they call, which of those calls change state — and only the wording could ever
benefit from a model.

The interesting derivation is transactions. A page that only reads is a page; a
page that can change something is a feature with a name, and grouping it with
the calls it triggers is what turns "PUT /jobs/:id/assignment" back into
"attach an assignment to a job".
"""

from __future__ import annotations

import re

from testtrout.domain.overview import (
    ApiSummary,
    CoverageEstimate,
    PageSummary,
    ProjectOverview,
    ScanDelta,
    TransactionSummary,
)
from testtrout.domain.scenario import ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import Operation, ScanResult

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Verbs read off an HTTP method, so a transaction gets a name rather than a
# method and a path.
_VERBS = {"POST": "create", "PUT": "update", "PATCH": "change", "DELETE": "remove"}


def _humanise(path: str) -> str:
    """A readable noun from a route or endpoint path."""
    parts = [p for p in path.strip("/").split("/") if p and not p.startswith(":")]
    if not parts:
        return "home"
    return re.sub(r"[-_]+", " ", parts[-1])


def _covered(surface_ids: list[str], index: ScenarioIndex) -> bool:
    """Whether any accepted test asserts on one of these surfaces."""
    accepted = {
        sid
        for scenario in index.scenarios
        if scenario.status in {ScenarioStatus.APPROVED, ScenarioStatus.CERTIFIED}
        for sid in scenario.surfaces
    }
    return any(sid in accepted for sid in surface_ids)


def build(scan: ScanResult, index: ScenarioIndex | None = None) -> ProjectOverview:
    """Describe the project, and how much of it is currently tested."""
    index = index or ScenarioIndex()

    pages: list[PageSummary] = []
    for screen in scan.screens:
        reached_ops = [o for o in scan.data_operations if o.id in screen.reaches]
        reached_apis = [e for e in scan.endpoints if e.id in screen.reaches]
        writes = sum(1 for o in reached_ops if o.operation.writes) + sum(
            1 for e in reached_apis if set(e.methods) & MUTATING
        )
        pages.append(
            PageSummary(
                path=screen.path,
                name=screen.component,
                criticality=screen.criticality,
                reads=len(reached_ops) + len(reached_apis) - writes,
                writes=writes,
                behind_login=bool(screen.requires_auth),
                surface_ids=[screen.id, *screen.reaches],
                how_to_test=(
                    "Sign in, open the page, and check its content appears."
                    if screen.requires_auth
                    else "Open the page and check its content appears."
                ),
            )
        )

    apis: list[ApiSummary] = []
    for endpoint in scan.endpoints:
        changes = bool(set(endpoint.methods) & MUTATING)
        apis.append(
            ApiSummary(
                path=endpoint.path,
                methods=endpoint.methods,
                criticality=endpoint.criticality,
                used_by=[s.path for s in scan.screens if endpoint.id in s.reaches],
                changes_data=changes,
                surface_ids=[endpoint.id],
                how_to_test=(
                    "Call it without signing in and check it is refused, then call it "
                    "properly and check the result."
                    if changes
                    else "Call it and check the response shape and status."
                ),
            )
        )

    transactions: list[TransactionSummary] = []
    # An endpoint reachable from nearly every page — a login call, a shared
    # fetcher — is an artifact of following imports, not something that page
    # does. Naming a transaction after one produces "Create login" nine times.
    ubiquitous = {
        e.id
        for e in scan.endpoints
        if len([s for s in scan.screens if e.id in s.reaches]) > max(2, len(scan.screens) // 2)
    }

    for screen in scan.screens:
        mutating_apis = [
            e
            for e in scan.endpoints
            if e.id in screen.reaches and set(e.methods) & MUTATING and e.id not in ubiquitous
        ]
        mutating_ops = [
            o for o in scan.data_operations if o.id in screen.reaches and o.operation.writes
        ]
        if not mutating_apis and not mutating_ops:
            continue

        steps = [f"Open {screen.path}"]
        steps += [
            f"{_VERBS.get(e.methods[0], 'call')} {_humanise(e.path)}" for e in mutating_apis[:4]
        ]
        steps += [
            f"{o.operation.value} {o.table or 'data'}"
            for o in mutating_ops[:4]
            if o.operation is not Operation.SELECT
        ]
        transactions.append(
            TransactionSummary(
                name=_name_transaction(screen.path, mutating_apis, mutating_ops),
                page=screen.path,
                steps=steps,
                criticality=screen.criticality,
                surface_ids=[
                    screen.id,
                    *[e.id for e in mutating_apis],
                    *[o.id for o in mutating_ops],
                ],
                how_to_test=(
                    "Drive it in a browser end to end, then confirm the change actually "
                    "took effect rather than only that the page did not error."
                ),
            )
        )

    # Three pages may genuinely create jobs. Naming them all "Create jobs" is
    # accurate and useless, so a collision takes the page as a qualifier.
    from collections import Counter

    duplicates = {
        name for name, count in Counter(t.name for t in transactions).items() if count > 1
    }
    for transaction in transactions:
        if transaction.name in duplicates:
            transaction.name = f"{transaction.name} from {transaction.page}"

    testable: list[PageSummary | ApiSummary | TransactionSummary] = [*pages, *apis, *transactions]
    for item in testable:
        item.covered = _covered(item.surface_ids, index)

    coverage = CoverageEstimate(
        pages_total=len(pages),
        pages_covered=sum(1 for p in pages if p.covered),
        apis_total=len(apis),
        apis_covered=sum(1 for a in apis if a.covered),
        transactions_total=len(transactions),
        transactions_covered=sum(1 for t in transactions if t.covered),
    )

    return ProjectOverview(
        summary=_describe(scan, pages, apis, transactions),
        stack=" + ".join(x for x in (scan.project.framework, scan.project.backend) if x),
        pages=sorted(pages, key=lambda p: (p.criticality.rank, p.path)),
        apis=sorted(apis, key=lambda a: (a.criticality.rank, a.path)),
        transactions=sorted(transactions, key=lambda t: (t.criticality.rank, t.name)),
        coverage=coverage,
    )


# Path segments that describe transport rather than a resource.
_GENERIC_SEGMENTS = frozenset({"api", "public", "rest", "v1", "v2", "internal"})


def _resource(path: str) -> str:
    """The thing an endpoint acts on.

    The *first* meaningful segment, not the last: `/assignments/ai/:param` is
    about assignments, and `/public/session/begin` is about a session. Reading
    the last segment gives "ai" and "begin", which name nothing.
    """
    parts = [
        part
        for part in path.strip("/").split("/")
        if part and not part.startswith(":") and part.lower() not in _GENERIC_SEGMENTS
    ]
    return re.sub(r"[-_]+", " ", parts[0]) if parts else "data"


def _name_transaction(page: str, apis: list, operations: list) -> str:  # type: ignore[type-arg]
    """Name a transaction after what it changes, not where it starts.

    The page path is a poor source: `/a/:slug` yields "a", and `/jobs/:id`
    yields "jobs" for something that actually assigns work. The endpoint being
    called says what is happening.
    """
    if apis:
        endpoint = apis[0]
        verb = _VERBS.get(endpoint.methods[0], "change")
        return f"{verb.capitalize()} {_resource(endpoint.path)}"

    if operations:
        operation = operations[0]
        return f"{operation.operation.value.capitalize()} {operation.table or 'data'}"

    return f"Change {_humanise(page)}"


def _describe(
    scan: ScanResult,
    pages: list[PageSummary],
    apis: list[ApiSummary],
    transactions: list[TransactionSummary],
) -> str:
    """One sentence about the product, from what the code shows."""
    behind_login = sum(1 for p in pages if p.behind_login)
    parts = [f"{len(pages)} page(s)"]
    if apis:
        parts.append(f"{len(apis)} endpoint(s)")
    if scan.policies:
        parts.append(f"{len(scan.policies)} access policy(ies)")

    sentence = f"A {scan.project.framework} application with " + ", ".join(parts) + "."
    if transactions:
        sentence += (
            f" {len(transactions)} of its pages can change data — those are where a "
            "regression costs the most."
        )
    if behind_login:
        sentence += f" {behind_login} page(s) sit behind a sign-in."
    return sentence


def delta(previous: ProjectOverview | None, current: ProjectOverview) -> ScanDelta:
    """What changed since the last scan, measured against the existing suite.

    The answer to "I scanned again — now what?". Without it a rescan produces
    the same forty items and no sense of whether anything moved. Two questions
    get answered here: what the code grew or lost, and what the suite now
    reaches that it did not before.

    ``still_missing`` is ordered by criticality, because a rescan that reports
    thirty untested things is only useful if the first one is the one to do
    next.
    """
    now = _named(current)
    before = _named(previous) if previous else {}

    covered_now = {name for name, item in now.items() if item.covered}
    covered_before = {name for name, item in before.items() if item.covered}

    # A first scan has nothing to compare against, so nothing is "new". Listing
    # the whole product as a change would be true only in a vacuous sense, and
    # it would bury the first real change under a wall of noise next time.
    return ScanDelta(
        new_areas=sorted(now.keys() - before.keys()) if previous else [],
        gone=sorted(before.keys() - now.keys()),
        # Newly covered, not merely covered: something that was already tested
        # last time is not news, and reporting it as progress is misleading.
        newly_covered=sorted(covered_now - covered_before) if previous else [],
        still_missing=[
            name
            for name, _ in sorted(
                ((n, i) for n, i in now.items() if not i.covered),
                key=lambda pair: (pair[1].criticality.rank, _kind_rank(pair[0]), pair[0]),
            )
        ],
    )


def _kind_rank(name: str) -> int:
    """Which kind to suggest first when criticality ties.

    Transactions before endpoints before pages. A transaction is the unit a
    person recognises as a feature, and testing one usually exercises the
    endpoints underneath it — so working down this list in order does the most
    per test rather than covering the same ground three times.
    """
    return {"transaction": 0, "api": 1}.get(name.split(" ", 1)[0], 2)


def _named(
    overview: ProjectOverview,
) -> dict[str, PageSummary | ApiSummary | TransactionSummary]:
    """Every testable thing, keyed by a name stable across scans.

    Stability is the whole point: the key is what the delta compares, so it is
    built from the path or transaction name rather than a surface id, which
    moves when a file does.
    """
    named: dict[str, PageSummary | ApiSummary | TransactionSummary] = {}
    for page in overview.pages:
        named[f"page {page.path}"] = page
    for api in overview.apis:
        named[f"api {'/'.join(api.methods)} {api.path}"] = api
    for transaction in overview.transactions:
        named[f"transaction {transaction.name}"] = transaction
    return named
