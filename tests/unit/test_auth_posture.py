"""Deciding whether an endpoint needs an account by asking it.

The tool used to ask the user, once per endpoint: thirty variations of "is this
behind auth?", each of which they could only answer by going and looking — which
is what the probe already does. These tests are about reading the answer off a
status code instead, and about not reading it wrongly.
"""

from __future__ import annotations

import pytest

from testtrout.domain.config import Config, Entrypoint
from testtrout.domain.location import SourceLocation
from testtrout.domain.observation import AuthPosture, ObservedEndpoint, ProbeResult
from testtrout.domain.question import QuestionLog
from testtrout.domain.surface import Endpoint, ProjectInfo, ScanResult
from testtrout.planning import questions as question_planner
from testtrout.planning.readiness import assess


def _scan(*paths: str, api_base_var: str = "") -> ScanResult:
    return ScanResult(
        project=ProjectInfo(root="/app", framework="nextjs-app", api_base_var=api_base_var),
        endpoints=[
            Endpoint(
                id=f"endpoint:{i}",
                path=path,
                methods=["POST"],
                location=SourceLocation(file="src/lib/api.ts", line=1),
            )
            for i, path in enumerate(paths)
        ],
    )


def _probe(**statuses: int) -> ProbeResult:
    return ProbeResult(
        entrypoint="deployment",
        base_url="https://app.test",
        endpoints=[
            ObservedEndpoint(
                endpoint_id=f"endpoint:{i}",
                path=f"/{name}",
                status=status,
                posture=(
                    AuthPosture.REQUIRES_AUTH
                    if status in (401, 403)
                    else AuthPosture.PUBLIC
                    if status in (200, 405)
                    else AuthPosture.UNKNOWN
                ),
            )
            for i, (name, status) in enumerate(statuses.items())
        ],
    )


def test_a_refused_endpoint_needs_an_account() -> None:
    probe = _probe(jobs=401)

    assert probe.endpoints_needing_account
    assert probe.endpoint("endpoint:0").needs_account


def test_a_method_not_allowed_means_the_request_got_past_auth() -> None:
    """The router answered, which it can only do once auth let the request by.

    This is what makes probing a POST-only endpoint with a harmless GET work.
    """
    probe = _probe(jobs=405)

    assert probe.endpoint("endpoint:0").posture is AuthPosture.PUBLIC
    assert not probe.endpoints_needing_account


@pytest.mark.parametrize("status", [401, 403])
def test_the_credential_ask_counts_the_endpoints_behind_the_wall(status: int) -> None:
    """One ask naming a number, not one ask per endpoint."""
    plan = assess(
        Config(entrypoints=[Entrypoint(name="d", url="https://app.test")]),
        _scan("/a", "/b"),
        _probe(a=status, b=status),
    )

    asks = [m for item in plan.blocked for m in item.missing if "test account" in m]
    assert any("2 endpoint(s)" in ask for ask in asks)


def test_where_is_the_api_is_asked_once_not_once_per_endpoint() -> None:
    """A separately deployed backend 404s every path. That is one question."""
    scan = _scan("/a", "/b", "/c", api_base_var="NEXT_PUBLIC_API_URL")
    config = Config(entrypoints=[Entrypoint(name="d", url="https://app.test")])

    raised = question_planner.from_api_address(scan, _probe(a=404, b=404, c=404), config)

    assert len(raised) == 1
    assert "NEXT_PUBLIC_API_URL" in raised[0].context
    assert "3 endpoint(s)" in raised[0].unlocks


def test_an_api_on_the_same_origin_is_not_asked_about() -> None:
    """Nagging an app that serves both from one place would be noise."""
    scan = _scan("/a", "/b", api_base_var="NEXT_PUBLIC_API_URL")
    config = Config(entrypoints=[Entrypoint(name="d", url="https://app.test")])

    assert question_planner.from_api_address(scan, _probe(a=401, b=200), config) == []


def test_an_already_configured_api_url_is_not_asked_about() -> None:
    scan = _scan("/a", api_base_var="NEXT_PUBLIC_API_URL")
    config = Config(
        entrypoints=[Entrypoint(name="d", url="https://app.test", api_url="https://api.app.test")]
    )

    assert question_planner.from_api_address(scan, _probe(a=404), config) == []


def test_endpoint_requests_go_to_the_api_url_when_there_is_one() -> None:
    plain = Entrypoint(name="d", url="https://app.test/")
    split = Entrypoint(name="d", url="https://app.test", api_url="https://api.app.test/")

    assert plain.api_base == "https://app.test"
    assert split.api_base == "https://api.app.test"


def test_the_same_ask_from_two_capabilities_is_recorded_once() -> None:
    """Browser and authorization tests are blocked by one missing runner."""
    from testtrout.domain.question import Question, QuestionKind

    log = QuestionLog()
    text = "the @playwright/test test runner — install it in your project"
    log.add(Question(id="q:setup:browser_tests", kind=QuestionKind.MISSING_CREDENTIAL, text=text))
    added = log.add(
        Question(id="q:setup:authorization_tests", kind=QuestionKind.MISSING_CREDENTIAL, text=text)
    )

    assert added is False
    assert len(log.questions) == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, AuthPosture.REQUIRES_AUTH),
        (403, AuthPosture.REQUIRES_AUTH),
        (200, AuthPosture.PUBLIC),
        (405, AuthPosture.PUBLIC),
        (422, AuthPosture.PUBLIC),
        (500, AuthPosture.UNKNOWN),
    ],
)
def test_what_each_status_says_about_auth(status: int, expected: AuthPosture) -> None:
    from testtrout.deployment.prober import read_posture

    assert read_posture(status)[0] is expected


def test_a_404_means_the_api_is_elsewhere_not_that_it_is_public() -> None:
    """The bug this guards is the expensive one.

    Reading 404 as "the request reached the application" describes a
    separately deployed backend as wide open, and generates a suite of tests
    asserting that a nonexistent path keeps returning 404.
    """
    from testtrout.deployment.prober import read_posture

    posture, detail = read_posture(404)

    assert posture is AuthPosture.UNKNOWN
    assert "served somewhere else" in detail


def test_a_redirect_to_a_login_page_is_a_refusal() -> None:
    from testtrout.deployment.prober import read_posture

    assert read_posture(302, "/login?next=/jobs")[0] is AuthPosture.REQUIRES_AUTH
    assert read_posture(302, "/pricing")[0] is AuthPosture.UNKNOWN
