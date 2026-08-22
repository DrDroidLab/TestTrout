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
from testtrout.deployment.login import LoginForm
from testtrout.deployment.network import classify, is_noise, should_block
from testtrout.domain.config import Config, Entrypoint, TestUser, resolve_secret
from testtrout.domain.observation import (
    AuthPosture,
    CallKind,
    Divergence,
    NetworkCall,
    ObservedEndpoint,
    ObservedLogin,
    ObservedScreen,
    ProbeResult,
)
from testtrout.domain.surface import Endpoint, ScanResult, Screen

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        Request,
        Response,
        Route,
        ViewportSize,
    )

NAVIGATION_TIMEOUT_MS = 20_000
SETTLE_TIMEOUT_MS = 6_000
ENDPOINT_TIMEOUT_MS = 10_000
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

            # Discover the sign-in form once, before authenticating. Everything
            # downstream replays what is found here rather than re-guessing.
            discovered = _discover_login(page, config, entrypoint, result)

            if role is not None:
                outcome = _authenticate(context, page, config, role, discovered)
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

            # Ask each endpoint what it does with no credentials, rather than
            # asking the user once per endpoint. Done in a fresh context so the
            # session established above cannot answer the question for it.
            result.endpoints = _probe_auth(browser, scan, entrypoint)
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


# Statuses that mean the auth layer answered before anything else ran.
_REFUSED = frozenset({401, 403})

# Statuses that mean the request got *past* auth. 405 is the useful one: the
# router rejected the method, which it can only do once the request reached it.
_REACHED_THE_HANDLER = frozenset({400, 405, 409, 415, 422, 501})

# Not in that set on purpose. A 404 is far more often "this API is not served
# from this origin" than "the router answered" — reading it as public would
# describe a separately deployed backend as a wide-open one.
_NOT_HERE = 404


def _probe_auth(
    browser: Browser, scan: ScanResult, entrypoint: Entrypoint
) -> list[ObservedEndpoint]:
    """Find out which endpoints need an account, by asking them.

    Every endpoint is probed with a **GET**, whatever methods it declares. A
    GET cannot create, update, or delete anything, so this is safe against a
    production deployment — and the distinction that matters survives it:

    * ``401``/``403`` — the auth layer answered first. Needs an account.
    * ``405 Method Not Allowed`` — the router answered, so the request got past
      auth. Public, even though the endpoint is POST-only.
    * ``2xx`` on a readable endpoint — public, plainly.

    Anything else is left unknown rather than guessed at. A wrong answer here
    would bake a false assertion into every generated test.
    """
    if not scan.endpoints:
        return []

    context = browser.new_context(extra_http_headers=entrypoint.headers or None)
    observed: list[ObservedEndpoint] = []
    try:
        for endpoint in scan.endpoints:
            url = _concrete_url(entrypoint.api_base, endpoint.path)
            if url is None:
                observed.append(
                    ObservedEndpoint(
                        endpoint_id=endpoint.id,
                        path=endpoint.path,
                        detail="path has parameters with no known sample value",
                    )
                )
                continue
            observed.append(_ask(context, endpoint, url))
    finally:
        context.close()
    return observed


def _ask(context: BrowserContext, endpoint: Endpoint, url: str) -> ObservedEndpoint:
    """One unauthenticated GET, read for what it says about auth."""
    try:
        response = context.request.get(url, timeout=ENDPOINT_TIMEOUT_MS, max_redirects=0)
        status = response.status
        location = response.headers.get("location", "")
    except Exception as exc:
        return ObservedEndpoint(
            endpoint_id=endpoint.id,
            path=endpoint.path,
            detail=f"no answer: {type(exc).__name__}",
        )

    posture, detail = read_posture(status, location)
    return ObservedEndpoint(
        endpoint_id=endpoint.id,
        path=endpoint.path,
        posture=posture,
        status=status,
        detail=detail,
    )


def read_posture(status: int, location: str = "") -> tuple[AuthPosture, str]:
    """What one status code says about whether an account is needed.

    Separated from the request so the rules can be argued with directly. The
    only interesting case is 404: it is far more often "this API is not served
    from this origin" than "the router answered", and reading it as public
    would describe a separately deployed backend as a wide-open one.
    """
    if status in _REFUSED:
        return (
            AuthPosture.REQUIRES_AUTH,
            f"refused an unauthenticated request with {status}",
        )
    if status == _NOT_HERE:
        return (
            AuthPosture.UNKNOWN,
            "not found at this address — the API may be served somewhere else",
        )
    if status in _REACHED_THE_HANDLER or 200 <= status < 300:
        return (
            AuthPosture.PUBLIC,
            f"answered {status} without credentials — the request reached the application, "
            "so no sign-in is enforced here",
        )
    # A redirect to a login page is a refusal by another name.
    if 300 <= status < 400 and any(
        word in location.lower() for word in ("login", "signin", "auth")
    ):
        return AuthPosture.REQUIRES_AUTH, f"redirected an unauthenticated request to {location}"
    return AuthPosture.UNKNOWN, f"answered {status}"


def _concrete_url(base: str, path: str) -> str | None:
    """A requestable URL, or ``None`` if the path has unfilled parameters.

    Guessing a value for ``:id`` would produce a 404 that says nothing about
    auth, so an unfillable path is skipped rather than answered wrongly.
    """
    if ":" in path or "{" in path or "*" in path:
        return None
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _discover_login(
    page: Page, config: Config, entrypoint: Entrypoint, result: ProbeResult
) -> LoginForm | None:
    """Locate the sign-in form and report how to reach it.

    Paid once, at probe time, so that generated tests replay a known form
    instead of re-guessing on every run — which is what makes driving the
    app's own login viable at all.
    """
    from testtrout.deployment import login as login_module

    form = login_module.discover(page, entrypoint.url, hint=config.login.path or None)
    if form is None:
        result.divergences.append(
            Divergence(
                code="no_login_form",
                message="no sign-in form was found",
                detail=(
                    "Looked at: "
                    + ", ".join(login_module.CANDIDATE_PATHS)
                    + ". If this app signs in elsewhere, set the login path in Setup and "
                    "probe again."
                ),
            )
        )
        return None

    result.login = ObservedLogin(
        path=form.path,
        email_selector=form.email_selector,
        password_selector=form.password_selector,
        submit_selector=form.submit_selector,
        note=form.note,
    )
    result.divergences.append(
        Divergence(
            code="login_form_found",
            message=f"sign-in form found at {form.path}",
            detail=f"{form.email_selector} / {form.password_selector}"
            + (f" — {form.note}" if form.note else ""),
        )
    )
    return form


def _authenticate(
    context: Any, page: Page, config: Config, role: str, form: LoginForm | None = None
) -> AuthOutcome:
    """Sign in as the given role, reporting rather than raising on failure."""
    user = config.user(role)
    if user is None:
        return AuthOutcome(
            False,
            "none",
            f"no test user with role {role!r} in .trout/config.yaml — run `trout facts`",
        )
    # Prefer the app's own login form. It needs nothing but a URL and a
    # password, where every other adapter needs credentials a developer may
    # reasonably refuse to share.
    if form is not None:
        return _sign_in_with_form(context, page, config, user, form)

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


def _sign_in_with_form(
    context: Any, page: Page, config: Config, user: TestUser, form: LoginForm
) -> AuthOutcome:
    """Sign in by driving the discovered form."""
    from testtrout.deployment import login as login_module
    from testtrout.domain.config import resolve_secret

    entrypoint = config.entrypoints[0] if config.entrypoints else None
    if entrypoint is None:
        return AuthOutcome(False, "form", "no deployment configured")

    try:
        email = resolve_secret(user.email) or ""
        password = resolve_secret(user.password) or ""
    except Exception as exc:
        return AuthOutcome(False, "form", str(exc))

    try:
        worked = login_module.sign_in(page, entrypoint.url, form, email, password)
    except Exception as exc:
        return AuthOutcome(False, "form", f"driving the sign-in form failed: {exc}")

    return AuthOutcome(
        worked,
        "form",
        f"signed in as {email} through the app's own login form"
        if worked
        else "filled the form but the login screen did not go away — the credentials "
        "were probably rejected",
        storage_state=context.storage_state() if worked else None,
    )


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
