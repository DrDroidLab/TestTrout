"""Turn observed behaviour into tests that assert it keeps happening.

There is no proposal step here and no model involved. A candidate is ready
precisely because the deployment already answered for it, so the scenario
writes itself: record the status, the title, the elements that were actually
on the page, and assert those again next time.

That is the whole idea of a baseline. It does not know whether the current
behaviour is *correct* — nobody has told it, and it will not guess. It knows
what the behaviour is, and it will notice the day it changes, which is the job.
"""

from __future__ import annotations

from testtrout.domain.candidate import Candidate, CandidateKind
from testtrout.domain.config import Config
from testtrout.domain.observation import ObservedScreen, ProbeResult, SelectorStrategy
from testtrout.domain.provenance import Provenance
from testtrout.domain.scenario import (
    Action,
    Assertion,
    AssertionKind,
    Scenario,
    ScenarioStatus,
    Step,
    Target,
    TestKind,
)

# How many elements to pin on a page. Enough that a blank render fails the
# test; few enough that an unrelated copy change does not.
_MAX_ANCHORS = 3


def write(candidate: Candidate, probe: ProbeResult | None, config: Config) -> Scenario | None:
    """Write the scenario for one ready candidate.

    Returns ``None`` when there is nothing observed to assert, which should not
    happen for a ready candidate but is cheaper to handle than to forbid.
    """
    if candidate.kind is CandidateKind.PAGE:
        return _page(candidate, probe, config)
    return _endpoint(candidate, probe, config)


def _page(candidate: Candidate, probe: ProbeResult | None, config: Config) -> Scenario | None:
    """A browser test that reproduces a load that already worked."""
    observed = next(
        (s for s in (probe.screens if probe else []) if s.path == candidate.target), None
    )
    if observed is None or not observed.reachable:
        return None

    role = config.test_users[0].role if config.test_users and candidate.behind_login else None
    steps: list[Step] = []
    if role:
        steps.append(Step(action=Action.SIGN_IN, value=role, description=f"Sign in as {role}"))
    steps.append(
        Step(
            action=Action.NAVIGATE,
            target=Target(url=candidate.target),
            description=f"Open {candidate.target}",
        )
    )

    assertions: list[Assertion] = []
    if observed.title:
        assertions.append(
            Assertion(
                kind=AssertionKind.TEXT,
                target=Target(),
                expected=observed.title,
                provenance=Provenance.OBSERVED,
                source=f"the page title when {candidate.target} was loaded",
                description="the page still has the title it had at baseline",
            )
        )
    assertions.extend(_anchors(observed, candidate.target))

    if not assertions:
        # A page that renders nothing identifiable can still be asserted to
        # load without errors, which catches the most common breakage of all.
        assertions.append(
            Assertion(
                kind=AssertionKind.NO_CONSOLE_ERRORS,
                target=Target(),
                provenance=Provenance.OBSERVED,
                source="no console errors were seen at baseline",
                description="the page loads without errors",
            )
        )

    return Scenario(
        id=f"scenario:page:{_slug(candidate.target)}",
        title=f"{candidate.target} still loads",
        kind=TestKind.BROWSER_JOURNEY,
        provenance=Provenance.OBSERVED,
        surfaces=list(candidate.surfaces),
        status=ScenarioStatus.APPROVED,
        role=role,
        given=[f"Signed in as {role}."] if role else [],
        when=steps,
        then=assertions,
    )


def _anchors(observed: ObservedScreen, path: str) -> list[Assertion]:
    """Assert on the most durable elements the probe actually saw.

    Ordered by selector strategy, so a `data-testid` is preferred to a role,
    and visible text is used only when nothing better exists. A test pinned to
    copy breaks on a wording change and teaches people to ignore failures.
    """
    durable = sorted(observed.selectors, key=lambda a: a.strategy.rank)[:_MAX_ANCHORS]
    return [
        Assertion(
            kind=AssertionKind.VISIBLE,
            target=Target(selector=anchor),
            provenance=Provenance.OBSERVED,
            source=f"seen on {path} at baseline",
            description=f"{anchor.value} is still on the page",
        )
        for anchor in durable
        if anchor.strategy is not SelectorStrategy.TEXT or len(durable) == 1
    ]


def _endpoint(candidate: Candidate, probe: ProbeResult | None, config: Config) -> Scenario | None:
    """An API test that reproduces the answer already given.

    Always a GET, because that is the request the probe made, and a GET cannot
    change anything on the deployment being tested. Asserting the status it
    actually returned covers both the interesting cases: a public endpoint that
    starts failing, and a private one that stops being private.
    """
    observed = next(
        (e for e in (probe.endpoints if probe else []) if e.path == candidate.target), None
    )
    if observed is None or observed.status is None:
        return None

    private = observed.needs_account
    return Scenario(
        id=f"scenario:api:{_slug(candidate.target)}",
        title=(
            f"{candidate.target} stays private" if private else f"{candidate.target} still answers"
        ),
        kind=TestKind.ENDPOINT,
        provenance=Provenance.OBSERVED,
        surfaces=list(candidate.surfaces),
        status=ScenarioStatus.APPROVED,
        when=[
            Step(
                action=Action.REQUEST,
                target=Target(url=candidate.target, method="GET"),
                description=f"GET {candidate.target} with no credentials",
            )
        ],
        then=[
            Assertion(
                kind=AssertionKind.STATUS,
                target=Target(url=candidate.target, method="GET"),
                expected=str(observed.status),
                provenance=Provenance.OBSERVED,
                source=observed.detail,
                description=(
                    "an unauthenticated caller is still refused"
                    if private
                    else f"still answers {observed.status}"
                ),
            )
        ],
    )


def _slug(path: str) -> str:
    """A stable, filesystem-safe id from a route."""
    cleaned = "".join(c if c.isalnum() else "-" for c in path.strip("/"))
    return "-".join(part for part in cleaned.split("-") if part) or "root"


__all__ = ["write"]
