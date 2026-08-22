"""What can be tested, and the rule that decides it.

A candidate is ready when the deployment has already answered for it. That is
not a shortcut — it is the definition of a baseline. The assertion is "keep
doing what you were doing", and there is nothing to assert until it has done
something once.
"""

from __future__ import annotations

from testtrout.domain.config import Config, Entrypoint, TestUser
from testtrout.domain.location import SourceLocation
from testtrout.domain.observation import (
    AuthPosture,
    ObservedEndpoint,
    ObservedScreen,
    ProbeResult,
)
from testtrout.domain.surface import Endpoint, ProjectInfo, ScanResult, Screen
from testtrout.planning import candidates as planner

LOCATION = SourceLocation(file="src/x.ts", line=1)
DEPLOYED = Config(entrypoints=[Entrypoint(name="d", url="https://app.test")])


def scan(paths: tuple[str, ...] = (), endpoints: tuple[str, ...] = ()) -> ScanResult:
    return ScanResult(
        project=ProjectInfo(root="/app", framework="nextjs-app"),
        screens=[Screen(id=f"screen:{p}", path=p, component="C", location=LOCATION) for p in paths],
        endpoints=[
            Endpoint(id=f"endpoint:{p}", path=p, methods=["POST"], location=LOCATION)
            for p in endpoints
        ],
    )


def seen(
    screens: dict[str, tuple[bool, bool]] | None = None,
    endpoints: dict[str, int] | None = None,
) -> ProbeResult:
    """screens maps path -> (reachable, requires_auth)."""
    return ProbeResult(
        entrypoint="d",
        base_url="https://app.test",
        screens=[
            ObservedScreen(
                path=path,
                url=f"https://app.test{path}",
                reachable=reachable,
                requires_auth=needs_auth,
                status=200 if reachable else 302,
                title="Page" if reachable else None,
            )
            for path, (reachable, needs_auth) in (screens or {}).items()
        ],
        endpoints=[
            ObservedEndpoint(
                endpoint_id=f"endpoint:{path}",
                path=path,
                status=status,
                posture=(AuthPosture.REQUIRES_AUTH if status in (401, 403) else AuthPosture.PUBLIC),
            )
            for path, status in (endpoints or {}).items()
        ],
    )


def test_nothing_is_ready_without_a_deployment() -> None:
    """There is nothing to record, so there is nothing to assert."""
    plan = planner.build(scan(paths=("/", "/about")), Config())

    assert plan.ready == []
    assert all("deployment_url" in c.needs for c in plan.waiting)


def test_a_page_that_loaded_is_ready() -> None:
    plan = planner.build(scan(paths=("/",)), DEPLOYED, seen(screens={"/": (True, False)}))

    (candidate,) = plan.ready
    assert candidate.target == "/"
    assert "200" in candidate.detail


def test_a_page_behind_a_sign_in_waits_for_an_account() -> None:
    plan = planner.build(scan(paths=("/app",)), DEPLOYED, seen(screens={"/app": (False, True)}))

    (candidate,) = plan.waiting
    assert candidate.needs == ["account_primary"]
    assert "sign-in" in candidate.detail


def test_an_account_on_file_unblocks_it() -> None:
    config = Config(
        entrypoints=[Entrypoint(name="d", url="https://app.test")],
        test_users=[TestUser(role="owner", email="env:E", password="env:P")],
    )
    plan = planner.build(scan(paths=("/app",)), config, seen(screens={"/app": (False, True)}))

    assert plan.waiting[0].needs == []


def test_an_endpoint_that_refused_is_ready_because_the_refusal_is_the_baseline() -> None:
    """ "This stays private" is a real regression test, and a valuable one."""
    plan = planner.build(scan(endpoints=("/admin",)), DEPLOYED, seen(endpoints={"/admin": 401}))

    (candidate,) = plan.ready
    assert candidate.behind_login


def test_an_endpoint_that_404s_waits_for_the_api_address() -> None:
    """A separately deployed backend, not a wide-open one."""
    plan = planner.build(scan(endpoints=("/jobs",)), DEPLOYED, seen(endpoints={"/jobs": 404}))

    (candidate,) = plan.waiting
    assert candidate.needs == ["api_url"]


def test_nothing_is_ranked_by_importance() -> None:
    """Importance is product knowledge. Ranking without it is a guess dressed
    up as advice, and the previous design's biggest source of noise."""
    plan = planner.build(
        scan(paths=("/", "/settings"), endpoints=("/a",)),
        DEPLOYED,
        seen(screens={"/": (True, False), "/settings": (True, False)}, endpoints={"/a": 200}),
    )

    assert len(plan.ready) == 3
    dumped = plan.model_dump(mode="json")
    assert "criticality" not in str(dumped)
