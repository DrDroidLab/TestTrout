"""Decide what can be tested, from the code and what the deployment did.

The rule throughout: a candidate is *ready* when the deployment has already
answered for it. That is not a shortcut, it is the definition of a baseline —
the assertion is "keep doing what you were doing", and there is nothing to
assert until it has done something once.

Anything not ready names the concrete fact that would change that, and nothing
else. There is no criticality score here, because importance is product
knowledge the tool does not have, and ranking without it is a guess dressed up
as advice.
"""

from __future__ import annotations

from testtrout.domain.candidate import Candidate, CandidateKind, TestPlan
from testtrout.domain.config import Config
from testtrout.domain.observation import ObservedScreen, ProbeResult
from testtrout.domain.surface import Endpoint, ScanResult, Screen

_PARAMETERISED = (":", "{")


def build(scan: ScanResult, config: Config, probe: ProbeResult | None = None) -> TestPlan:
    """Everything worth testing, and whether it can be tested yet."""
    entrypoint = config.entrypoint()
    if entrypoint is None or not entrypoint.url:
        # Without a deployment nothing has been observed, so nothing is ready.
        # Saying so once is kinder than repeating it on every row.
        return TestPlan(
            candidates=[_page(screen, None, ["deployment_url"]) for screen in scan.screens]
            + [_endpoint(endpoint, None, ["deployment_url"]) for endpoint in scan.endpoints]
        )

    seen = {screen.path: screen for screen in (probe.screens if probe else [])}
    signed_in = bool(config.test_users)

    candidates = [_page(screen, seen.get(screen.path), []) for screen in scan.screens]
    candidates.extend(
        _endpoint(endpoint, probe.endpoint(endpoint.id) if probe else None, [])
        for endpoint in scan.endpoints
    )

    # An account unlocks two different things, and the difference matters: a
    # page that redirected to a login needs one to be reached at all, while an
    # endpoint that answered 401 is already testable — the 401 *is* the
    # baseline — and only needs one to see past it.
    for candidate in candidates:
        already_answered = candidate.observed
        if (
            candidate.behind_login
            and not signed_in
            and not already_answered
            and "account_primary" not in candidate.needs
        ):
            candidate.needs.append("account_primary")

    return TestPlan(candidates=candidates)


def _page(screen: Screen, observed: ObservedScreen | None, needs: list[str]) -> Candidate:
    """One route, and whether a browser has managed to load it."""
    needs = list(needs)
    behind_login = bool(observed and observed.requires_auth)

    if observed is None:
        detail = "not visited yet" if not needs else "no deployment configured"
    elif observed.reachable:
        detail = f"loaded, {observed.status or 200}" + (
            f" — {observed.title}" if observed.title else ""
        )
    elif observed.requires_auth:
        detail = "redirected to a sign-in"
    else:
        detail = observed.redirected_to or "did not load"
        for name in screen.params:
            needs.append(f"sample_{name}")

    return Candidate(
        id=f"page:{screen.path}",
        kind=CandidateKind.PAGE,
        title=f"{screen.path} loads",
        target=screen.path,
        observed=bool(observed and observed.reachable),
        detail=detail,
        needs=needs,
        surfaces=[screen.id],
        behind_login=behind_login,
    )


def _endpoint(endpoint: Endpoint, observed, needs: list[str]) -> Candidate:  # type: ignore[no-untyped-def]
    """One endpoint, and what it said when asked without credentials.

    A 401 makes a perfectly good baseline: "this stays private" is a real
    regression test, and one of the more valuable ones. It is marked as behind
    a login too, so that supplying an account later unlocks the *other* test —
    what it returns to someone allowed to see it.
    """
    needs = list(needs)
    method = "/".join(endpoint.methods) if endpoint.methods else "GET"

    if observed is None:
        detail = "not asked yet" if not needs else "no deployment configured"
    elif observed.status is None:
        detail = observed.detail or "no answer"
    elif observed.status == 404:
        detail = "not found at this address"
        needs.append("api_url")
    else:
        detail = observed.detail

    if any(marker in endpoint.path for marker in _PARAMETERISED) and observed is None:
        detail = "path needs a real value before it can be called"

    return Candidate(
        id=f"endpoint:{endpoint.path}",
        kind=CandidateKind.ENDPOINT,
        title=f"{method} {endpoint.path}",
        target=endpoint.path,
        observed=bool(observed and observed.status is not None and observed.status != 404),
        detail=detail,
        needs=needs,
        surfaces=[endpoint.id],
        behind_login=bool(observed and getattr(observed, "needs_account", False)),
    )


__all__ = ["build"]
