"""Loading a deployment in a real browser and recording what happens.

The probe is what grounds every later stage in reality instead of inference.
Static analysis says the code *can* query ``orders``; the probe says that
visiting ``/orders`` actually issues that query, returns 200, and renders a
table with these three stable selectors on it.

Safety is enforced here rather than assumed. Every request passes through
:func:`testtrout.deployment.network.should_block` before it leaves the browser,
and a mutating request against a deployment that is not marked ``disposable``
is refused at the network layer. Navigation alone is not safe — plenty of
applications write on mount — so the guarantee has to sit below navigation.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from testtrout.deployment import selectors
from testtrout.deployment.auth.base import AuthOutcome, select_adapter
from testtrout.deployment.network import classify, is_noise, should_block
from testtrout.domain.config import Config, Entrypoint, resolve_secret
from testtrout.domain.observation import (
    CallKind,
    Divergence,
    NetworkCall,
    ObservedScreen,
    ProbeResult,
)
from testtrout.domain.surface import ScanResult, Screen

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page, Request, Response, Route, ViewportSize

NAVIGATION_TIMEOUT_MS = 20_000
SETTLE_TIMEOUT_MS = 6_000
VIEWPORT: ViewportSize = {"width": 1280, "height": 900}

_PARAM = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)\*?\??")

# Route patterns that are never worth visiting.
_SKIP_PATHS = frozenset({"*", "/*"})


class ProbeUnavailableError(RuntimeError):
    """Playwright or its browser is not installed."""


@dataclass
class _Recorder:
    """Per-screen collector for requests, responses, and console output."""

    app_origin: str
    supabase_url: str | None
    writable: bool
    calls: list[NetworkCall] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    """Sample row ids harvested from responses, used to visit parameterised
    routes. Read from the response body and immediately discarded — the body
    itself is never stored."""

    def reset(self) -> None:
        """Clear per-screen state, keeping harvested identifiers."""
        self.calls = []
        self.console_errors = []

    def handle_route(self, route: Route, request: Request) -> None:
        """Classify a request, then either block it or let it through."""
        call = classify(request.url, request.method, self.app_origin, self.supabase_url)
        if should_block(call, self.writable):
            call.blocked = True
            self.calls.append(call)
            route.abort("blockedbyclient")
            return
        self.calls.append(call)
        route.continue_()

    def handle_response(self, response: Response) -> None:
        """Record the status, and harvest a sample id if this was a table read."""
        for call in reversed(self.calls):
            if call.url == response.url and call.status is None:
                call.status = response.status
                break

        if response.status != 200 or not self.supabase_url:
            return
        if "/rest/v1/" not in response.url:
            return
        try:
            body: Any = response.json()
        except Exception:
            return
        rows = body if isinstance(body, list) else [body]
        for row in rows[:1]:
            if isinstance(row, dict) and isinstance(row.get("id"), str | int):
                self.identifiers.setdefault("id", str(row["id"]))

    def handle_console(self, message: Any) -> None:
        """Collect console errors, which often explain a blank screen."""
        if message.type == "error":
            self.console_errors.append(str(message.text)[:300])


def probe(
    scan: ScanResult,
    config: Config,
    entrypoint: Entrypoint,
    role: str | None = None,
    headless: bool = True,
    max_screens: int | None = None,
) -> ProbeResult:
    """Load every known screen and record what the deployment does.

    Args:
        scan: The static surface map, which supplies the routes to visit.
        config: Repository configuration.
        entrypoint: Which deployment to probe.
        role: Test-user role to sign in as. ``None`` probes signed out, which
            is itself a useful check of what an anonymous visitor can reach.
        headless: Run without a visible browser window.
        max_screens: Cap for a quick look at a large application.

    Raises:
        ProbeUnavailableError: if Playwright or Chromium is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ProbeUnavailableError(
            "Playwright is not installed. Run:\n"
            "  pip install 'testtrout[probe]' && playwright install chromium"
        ) from exc

    supabase_url = resolve_secret(config.supabase.url)
    result = ProbeResult(entrypoint=entrypoint.name, base_url=entrypoint.url, role=role)
    recorder = _Recorder(
        app_origin=entrypoint.url, supabase_url=supabase_url, writable=entrypoint.writable
    )

    if not entrypoint.writable:
        result.divergences.append(
            Divergence(
                code="read_only_probe",
                message=(
                    f"{entrypoint.name!r} is not marked disposable, so mutating requests were "
                    "blocked at the network layer. Screens that write on load may render "
                    "incompletely — that is the guard working, not a bug."
                ),
            )
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport=VIEWPORT,
                ignore_https_errors=False,
                extra_http_headers=entrypoint.headers or None,
            )
            context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
            context.route("**/*", recorder.handle_route)
            page = context.new_page()
            page.on("response", recorder.handle_response)
            page.on("console", recorder.handle_console)

            if role is not None:
                outcome = _authenticate(context, page, config, role)
                result.authenticated = outcome.authenticated
                result.divergences.append(
                    Divergence(
                        code="auth_ok" if outcome.authenticated else "auth_failed",
                        message=f"[{outcome.method}] {outcome.detail}",
                    )
                )

            for screen in _visit_order(scan.screens, max_screens):
                recorder.reset()
                result.screens.append(_visit(page, screen, entrypoint, recorder))
        finally:
            browser.close()

    result.external_hosts = sorted(
        {
            call.host
            for call in result.all_calls()
            if call.kind is CallKind.EXTERNAL and not is_noise(call.host)
        }
    )
    return result


def _authenticate(context: Any, page: Page, config: Config, role: str) -> AuthOutcome:
    """Sign in as the given role, reporting rather than raising on failure."""
    user = config.user(role)
    if user is None:
        return AuthOutcome(
            False,
            "none",
            f"no test user with role {role!r} in .trout/config.yaml — run `trout init`",
        )
    adapter = select_adapter(config)
    if adapter is None:
        return AuthOutcome(
            False,
            "none",
            f"no auth adapter for provider {config.project.auth!r}",
        )
    try:
        return adapter.authenticate(context, page, config, user)
    except Exception as exc:
        return AuthOutcome(False, adapter.id, f"adapter raised: {exc}")


def _visit_order(screens: list[Screen], max_screens: int | None) -> list[Screen]:
    """Order routes so that parameterless ones are visited first.

    Visiting ``/orders`` before ``/orders/:id`` means the list request has
    already yielded a real row id by the time the detail route needs one. The
    alternative is visiting detail routes with a placeholder and recording a
    404 that says nothing about the application.
    """
    ordered = sorted(
        (s for s in screens if s.path not in _SKIP_PATHS),
        key=lambda s: (bool(s.params), s.criticality.rank, s.path),
    )
    return ordered[:max_screens] if max_screens else ordered


def _resolve_path(path: str, identifiers: dict[str, str]) -> tuple[str, str | None]:
    """Substitute route parameters with harvested values.

    Returns ``(url_path, note)`` where ``note`` explains any substitution that
    could not be made honestly.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = identifiers.get(name) or identifiers.get("id")
        if value is None:
            missing.append(name)
            return match.group(0)
        return value

    resolved = _PARAM.sub(replace, path)
    if missing:
        return resolved, (
            f"could not resolve route parameter(s) {', '.join(missing)} — no sample row was "
            "observed. Visit a list screen that returns rows first, or add a sample value."
        )
    return resolved, None


def _visit(
    page: Page, screen: Screen, entrypoint: Entrypoint, recorder: _Recorder
) -> ObservedScreen:
    """Load one route and record everything observable about it."""
    resolved, note = _resolve_path(screen.path, recorder.identifiers)
    if note is not None:
        return ObservedScreen(
            path=screen.path, url=f"{entrypoint.url}{resolved}", reachable=False, note=note
        )

    url = f"{entrypoint.url}{resolved}"
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except Exception as exc:
        return ObservedScreen(
            path=screen.path, url=url, reachable=False, note=f"navigation failed: {exc}"
        )

    # A busy or polling app may never reach networkidle; proceed with
    # whatever has loaded rather than failing the step.
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)

    landed = page.url
    redirected = landed.rstrip("/") != url.rstrip("/")

    return ObservedScreen(
        path=screen.path,
        url=url,
        reachable=True,
        status=response.status if response else None,
        title=(page.title() or None),
        redirected_to=landed if redirected else None,
        requires_auth=redirected and _looks_like_login(landed),
        console_errors=list(recorder.console_errors),
        selectors=selectors.extract(page),
        calls=list(recorder.calls),
    )


def _looks_like_login(url: str) -> bool:
    """Whether a redirect target looks like a sign-in screen."""
    lowered = url.lower()
    return any(token in lowered for token in ("login", "signin", "sign-in", "auth"))
