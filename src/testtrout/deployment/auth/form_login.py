"""Generic form-based sign-in, for providers with no usable auth API.

Used for Clerk and NextAuth, and as the fallback when a Supabase project has
the password grant disabled or is self-hosted.

This is genuinely harder than the API path and it is honest about that. Fields
are located by accessible role and label first, falling back to input types.
When the form cannot be recognised the adapter says so and names what it looked
for, so the user can supply explicit selectors in ``.trout/config.yaml`` instead of
guessing at why nothing happened.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar

from testtrout.deployment.auth.base import AuthOutcome
from testtrout.domain.config import Config, TestUser, resolve_secret

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Locator, Page

# Route names a login screen is likely to live at, tried in order.
LOGIN_PATHS = ("/login", "/signin", "/sign-in", "/auth/login", "/auth/signin", "/")

NAVIGATION_TIMEOUT_MS = 15_000
SETTLE_TIMEOUT_MS = 8_000


class FormLoginAdapter:
    """Signs in by filling and submitting a login form."""

    id: ClassVar[str] = "form"

    def matches(self, config: Config) -> bool:
        """Handle Clerk and NextAuth, which have no drop-in password API here."""
        return config.project.auth in {"clerk", "nextauth"}

    def authenticate(
        self, context: BrowserContext, page: Page, config: Config, user: TestUser
    ) -> AuthOutcome:
        """Find a login form, fill it, and confirm we ended up somewhere else."""
        entrypoint = config.entrypoints[0] if config.entrypoints else None
        if entrypoint is None:
            return AuthOutcome(False, self.id, "no entrypoint configured — run `trout facts`")

        try:
            email = resolve_secret(user.email) or ""
            password = resolve_secret(user.password) or ""
        except Exception as exc:
            return AuthOutcome(False, self.id, str(exc))

        for path in LOGIN_PATHS:
            page.goto(
                f"{entrypoint.url}{path}",
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            email_field = _first_visible(page, _email_locators(page))
            password_field = _first_visible(page, _password_locators(page))
            if email_field is None or password_field is None:
                continue

            email_field.fill(email)
            password_field.fill(password)
            before = page.url

            submit = _first_visible(page, _submit_locators(page))
            if submit is None:
                password_field.press("Enter")
            else:
                submit.click()

            # A busy or polling app may never reach networkidle; proceed with
            # whatever has loaded rather than failing the step.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)

            # Leaving the login screen is the only signal available without
            # knowing the app. It is weak, so it is reported as such rather
            # than asserted.
            if page.url != before or _first_visible(page, _password_locators(page)) is None:
                return AuthOutcome(
                    True,
                    self.id,
                    f"submitted the form at {path} as {email}; the login screen was left "
                    "(this is inferred, not confirmed by the app)",
                    storage_state=context.storage_state(),
                )
            return AuthOutcome(
                False,
                self.id,
                f"filled the form at {path} but the password field is still present — "
                "the credentials were probably rejected",
            )

        return AuthOutcome(
            False,
            self.id,
            "no login form found. Looked for an email/username field and a password field at: "
            + ", ".join(LOGIN_PATHS),
        )


def _email_locators(page: Page) -> list[Locator]:
    """Ways to find the identifier field, most reliable first."""
    return [
        page.get_by_label("email", exact=False),
        page.get_by_role("textbox", name="email"),
        page.locator("input[type='email']"),
        page.locator("input[name='email']"),
        page.locator("input[name='identifier']"),
        page.locator("input[name='username']"),
    ]


def _password_locators(page: Page) -> list[Locator]:
    """Ways to find the password field."""
    return [
        page.get_by_label("password", exact=False),
        page.locator("input[type='password']"),
        page.locator("input[name='password']"),
    ]


def _submit_locators(page: Page) -> list[Locator]:
    """Ways to find the submit control."""
    return [
        page.get_by_role("button", name="sign in"),
        page.get_by_role("button", name="log in"),
        page.get_by_role("button", name="continue"),
        page.locator("button[type='submit']"),
    ]


def _first_visible(page: Page, locators: list[Locator]) -> Locator | None:
    """First locator that resolves to exactly one visible element.

    Requiring a unique match avoids the classic failure where a page has both a
    sign-in and a sign-up form and the adapter fills half of each.
    """
    for locator in locators:
        try:
            if locator.count() != 1:
                continue
            candidate = locator.first
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None
