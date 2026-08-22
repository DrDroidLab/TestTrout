"""What the tool asks for, and — more importantly — what it never asks.

The rule these tests exist to defend: **never ask what the product should do.**
Every item on the sheet must be a concrete value a person can produce in
seconds. If a question could be answered by reading the repository, asking it
is a bug.
"""

from __future__ import annotations

from testtrout.domain.config import Config, Entrypoint, TestUser
from testtrout.domain.fact import FactKind
from testtrout.domain.location import SourceLocation
from testtrout.domain.observation import (
    AuthPosture,
    ObservedEndpoint,
    ObservedScreen,
    ProbeResult,
)
from testtrout.domain.surface import Endpoint, ProjectInfo, ScanResult, Screen
from testtrout.planning import facts as planner

LOCATION = SourceLocation(file="src/x.ts", line=1)


def scan(
    *, endpoints: int = 0, screens: tuple[str, ...] = (), auth: str | None = None
) -> ScanResult:
    return ScanResult(
        project=ProjectInfo(root="/app", framework="nextjs-app", auth=auth),
        screens=[
            Screen(
                id=f"screen:{path}",
                path=path,
                component="C",
                params=[p.lstrip(":") for p in path.split("/") if p.startswith(":")],
                location=LOCATION,
            )
            for path in screens
        ],
        endpoints=[
            Endpoint(id=f"endpoint:{i}", path=f"/e{i}", methods=["GET"], location=LOCATION)
            for i in range(endpoints)
        ],
    )


def probe(*, endpoints: dict[str, int] | None = None, screens: dict[str, bool] | None = None):
    return ProbeResult(
        entrypoint="deployment",
        base_url="https://app.test",
        endpoints=[
            ObservedEndpoint(
                endpoint_id=name,
                path=f"/{name}",
                status=status,
                posture=AuthPosture.REQUIRES_AUTH if status in (401, 403) else AuthPosture.PUBLIC,
            )
            for name, status in (endpoints or {}).items()
        ],
        screens=[
            ObservedScreen(path=path, url=f"https://app.test{path}", reachable=ok)
            for path, ok in (screens or {}).items()
        ],
    )


def test_the_only_thing_asked_for_up_front_is_where_it_is_deployed() -> None:
    sheet = planner.build(scan(screens=("/",)), Config())

    assert [f.id for f in sheet.outstanding] == ["deployment_url"]
    assert sheet.outstanding[0].kind is FactKind.URL


def test_nothing_on_the_sheet_asks_about_behaviour() -> None:
    """The regression guard for the whole redesign.

    The predecessor asked "what is the correct outcome of insert on payments?"
    once per data operation. Nothing here may ever ask a question whose answer
    lives in someone's head rather than in their deployment.
    """
    sheet = planner.build(
        scan(endpoints=3, screens=("/", "/jobs/:id"), auth="supabase"),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(endpoints={"a": 401}, screens={"/": True}),
    )

    banned = ("should", "correct", "expected", "supposed", "meant to", "outcome")
    for fact in sheet.facts:
        text = f"{fact.label} {fact.why}".lower()
        assert not any(word in text for word in banned), fact.label
        assert fact.kind in set(FactKind)


def test_an_account_is_asked_for_only_with_evidence_that_one_is_needed() -> None:
    """A 401 is evidence. A hunch is not."""
    open_app = planner.build(
        scan(endpoints=1),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(endpoints={"a": 200}),
    )
    assert not [f for f in open_app.facts if f.id.startswith("account")]

    closed = planner.build(
        scan(endpoints=1),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(endpoints={"a": 401}),
    )
    account = closed.get("account_primary")
    assert account is not None
    assert "1 endpoint(s) refused" in account.evidence


def test_a_supplied_account_stops_being_asked_for() -> None:
    config = Config(
        entrypoints=[Entrypoint(name="d", url="https://app.test")],
        test_users=[TestUser(role="owner", email="env:E", password="env:P")],
    )
    sheet = planner.build(scan(endpoints=1), config, probe(endpoints={"a": 401}))

    assert sheet.get("account_primary").known
    assert "account_primary" not in [f.id for f in sheet.outstanding]


def test_a_sample_value_is_asked_for_only_when_the_probe_could_not_find_one() -> None:
    """The probe fills these in from list pages. Asking anyway would be noise."""
    unreached = planner.build(
        scan(screens=("/jobs/:id",)),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(screens={"/jobs/:id": False}),
    )
    assert "sample_id" in [f.id for f in unreached.outstanding]

    reached = planner.build(
        scan(screens=("/jobs/:id",)),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(screens={"/jobs/:id": True}),
    )
    assert "sample_id" not in [f.id for f in reached.outstanding]


def test_what_blocks_the_most_is_offered_first() -> None:
    """A number beats an adjective for deciding what to fill in."""
    from testtrout.domain.candidate import Candidate, CandidateKind

    candidates = [
        Candidate(
            id=f"c{i}", kind=CandidateKind.PAGE, title="t", target="/", needs=["account_primary"]
        )
        for i in range(4)
    ] + [Candidate(id="c9", kind=CandidateKind.PAGE, title="t", target="/", needs=["sample_id"])]
    sheet = planner.build(
        scan(screens=("/jobs/:id",), auth="supabase"),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(screens={"/jobs/:id": False}),
        candidates,
    )

    outstanding = [f.id for f in sheet.outstanding]
    assert outstanding.index("account_primary") < outstanding.index("sample_id")


def test_every_fact_says_what_it_unlocks() -> None:
    """A form of unexplained boxes gets abandoned."""
    sheet = planner.build(
        scan(endpoints=2, screens=("/",), auth="supabase"),
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        probe(endpoints={"a": 401}),
    )

    assert all(fact.why for fact in sheet.facts)
