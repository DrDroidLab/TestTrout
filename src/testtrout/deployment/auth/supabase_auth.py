"""Supabase Auth.

Signs in through the Auth REST API and injects the resulting session into
``localStorage``, rather than driving the login form.

That choice matters. Every one of these applications has a different login
form — different labels, different component library, sometimes a magic-link
flow with no password field at all — and a tool that has to recognise the form
fails on the first app whose designer had an opinion. The REST endpoint is
identical everywhere, so this path works on an app the tool has never seen.

Form login remains available through
:mod:`testtrout.deployment.auth.form_login` for providers with no equivalent API
and for projects that disable the password grant.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from testtrout.deployment.auth.base import AuthOutcome
from testtrout.domain.config import Config, TestUser, resolve_secret

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Page

# https://<ref>.supabase.co — the ref is also the localStorage key prefix that
# supabase-js reads its session from.
_PROJECT_REF = re.compile(r"https?://([a-z0-9]+)\.supabase\.(?:co|in|net)", re.IGNORECASE)

AUTH_TIMEOUT_SECONDS = 20.0


class SupabaseAuthAdapter:
    """Password sign-in against the Supabase Auth API."""

    id: ClassVar[str] = "supabase"

    def matches(self, config: Config) -> bool:
        """Handle projects whose scan detected Supabase auth."""
        return config.project.auth == "supabase"

    def authenticate(
        self, context: BrowserContext, page: Page, config: Config, user: TestUser
    ) -> AuthOutcome:
        """Sign in and inject the session, so the next navigation is authenticated."""
        url = resolve_secret(config.supabase.url)
        anon_key = resolve_secret(config.supabase.anon_key)
        if not url or not anon_key:
            return AuthOutcome(
                False,
                self.id,
                "supabase.url and supabase.anon_key must be set in .trout/config.yaml "
                "(use env: references). Run `trout init` to configure them.",
            )

        ref = project_ref(url)
        if ref is None:
            return AuthOutcome(
                False, self.id, f"could not derive a project ref from supabase.url {url!r}"
            )

        try:
            email = resolve_secret(user.email)
            password = resolve_secret(user.password)
        except Exception as exc:
            return AuthOutcome(False, self.id, str(exc))

        session, error = _password_grant(url, anon_key, email or "", password or "")
        if session is None:
            return AuthOutcome(False, self.id, error or "sign-in failed")

        _inject_session(context, page, config, ref, session)
        return AuthOutcome(
            True,
            self.id,
            f"signed in as {email} (role {user.role})",
            storage_state=context.storage_state(),
        )


def project_ref(supabase_url: str) -> str | None:
    """Extract the project ref from a Supabase URL.

    Returns ``None`` for a self-hosted instance, where the ref convention does
    not apply and the caller must fall back to form login.
    """
    match = _PROJECT_REF.match(supabase_url)
    return match.group(1) if match else None


def _password_grant(
    supabase_url: str, anon_key: str, email: str, password: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Exchange credentials for a session. Returns ``(session, error)``."""
    try:
        response = httpx.post(
            f"{supabase_url.rstrip('/')}/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=AUTH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return None, f"could not reach the Supabase Auth API: {exc}"

    if response.status_code >= 400:
        detail = response.json().get("error_description") or response.text[:200]
        hint = ""
        if response.status_code == 400:
            hint = (
                " — check the user exists and is confirmed, and that the password "
                "grant is enabled for this project"
            )
        return None, f"sign-in rejected ({response.status_code}): {detail}{hint}"

    return response.json(), None


def _inject_session(
    context: BrowserContext, page: Page, config: Config, ref: str, session: dict[str, Any]
) -> None:
    """Write the session into localStorage the way supabase-js expects to read it.

    An init script is used rather than a one-off ``evaluate`` so that the
    session survives every subsequent navigation in this context, including
    full page reloads.
    """
    entrypoint = config.entrypoints[0] if config.entrypoints else None
    origin = entrypoint.url if entrypoint else ""
    key = f"sb-{ref}-auth-token"
    payload = json.dumps(session)

    context.add_init_script(
        f"try {{ window.localStorage.setItem({key!r}, {payload!r}); }} catch (e) {{}}"
    )
    if origin:
        # The init script only fires on future navigations, so seed the current
        # page too — otherwise the first screen probed is the only one that
        # loads signed out, which is a confusing result to debug.
        page.goto(origin, wait_until="domcontentloaded")
        page.evaluate("([k, v]) => window.localStorage.setItem(k, v)", [key, payload])
