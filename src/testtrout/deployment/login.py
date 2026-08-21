"""Finding and driving an application's own login form.

This is the only sign-in approach that needs nothing a developer would refuse
to hand over: a URL and a test password. No database credentials, no API keys.

The obvious objection is that every application's form is different. The answer
is to discover it once rather than guess on every run — ``trout probe`` locates
the fields, records how to reach them, and everything afterwards just replays
that. When discovery is wrong, the locators are ordinary configuration a
developer can correct by hand.

Fields are located by accessible role and label first, falling back to input
types, because those survive a restyle where a generated class name does not.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Locator, Page

# Where a login form is likely to live, tried in order.
CANDIDATE_PATHS = ("/login", "/signin", "/sign-in", "/auth/login", "/auth/signin", "/")

NAVIGATION_TIMEOUT_MS = 15_000
SETTLE_TIMEOUT_MS = 8_000


@dataclass
class LoginForm:
    """A discovered sign-in form, described in terms a test can replay."""

    path: str
    email_selector: str
    password_selector: str
    submit_selector: str | None = None
    note: str = ""

    @property
    def complete(self) -> bool:
        """Whether both credential fields were found."""
        return bool(self.email_selector and self.password_selector)


# Ordered by durability. A locator built from an accessible label survives a
# restyle; one built from a CSS type selector mostly does.
_EMAIL_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("getByLabel('Email', { exact: false })", "label"),
    ("getByRole('textbox', { name: /email/i })", "role"),
    ("locator('input[type=\"email\"]')", "type"),
    ("locator('input[name=\"email\"]')", "name"),
    ("locator('input[name=\"identifier\"]')", "name"),
    ("locator('input[name=\"username\"]')", "name"),
)
_PASSWORD_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("getByLabel('Password', { exact: false })", "label"),
    ("locator('input[type=\"password\"]')", "type"),
    ("locator('input[name=\"password\"]')", "name"),
)
_SUBMIT_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("getByRole('button', { name: /sign in|log in|continue|submit/i })", "role"),
    ("locator('button[type=\"submit\"]')", "type"),
    ("locator('form button')", "structure"),
)


def _resolve(page: Page, source: str) -> Locator | None:
    """Turn a locator source string into a locator that matches exactly one visible element.

    Requiring a unique match avoids the classic failure where a page shows both
    a sign-in and a sign-up form and half of each gets filled.
    """
    try:
        locator = _apply(page, source)
        if locator.count() != 1:
            return None
        first = locator.first
        return first if first.is_visible() else None
    except Exception:
        return None


def _apply(page: Page, source: str) -> Locator:
    """Evaluate a locator source against a page.

    A tiny dispatcher rather than ``eval``: the strings are ours, but a test
    tool that evaluates arbitrary source against a live page is not something
    to ship.
    """
    import re

    if source.startswith("getByLabel("):
        text = re.search(r"getByLabel\('([^']*)'", source)
        return page.get_by_label(text.group(1) if text else "", exact=False)
    if source.startswith("getByRole("):
        role_match = re.search(r"getByRole\('([^']*)'", source)
        role = role_match.group(1) if role_match else "button"
        name = re.search(r"name:\s*/([^/]*)/i", source)
        if name:
            return page.get_by_role(role, name=re.compile(name.group(1), re.I))  # type: ignore[arg-type]
        return page.get_by_role(role)  # type: ignore[arg-type]
    if source.startswith("getByTestId("):
        value = re.search(r"getByTestId\('([^']*)'", source)
        return page.get_by_test_id(value.group(1) if value else "")
    inner = re.search(r"locator\('(.*)'\)$", source)
    return page.locator(inner.group(1) if inner else source)


def discover(page: Page, base_url: str, hint: str | None = None) -> LoginForm | None:
    """Find the sign-in form, returning locators a generated test can replay.

    Args:
        page: A browser page to drive.
        base_url: The deployment's origin.
        hint: A path to try first, from configuration or from a route the scan
            found. Saves walking the candidate list on an app that names its
            login screen something unusual.
    """
    paths = ([hint] if hint else []) + [p for p in CANDIDATE_PATHS if p != hint]

    for path in paths:
        try:
            page.goto(
                f"{base_url}{path}", wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
        except Exception:
            continue
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)

        email = next((source for source, _ in _EMAIL_STRATEGIES if _resolve(page, source)), None)
        password = next(
            (source for source, _ in _PASSWORD_STRATEGIES if _resolve(page, source)), None
        )
        if not (email and password):
            continue

        submit = next((source for source, _ in _SUBMIT_STRATEGIES if _resolve(page, source)), None)
        return LoginForm(
            path=path,
            email_selector=email,
            password_selector=password,
            submit_selector=submit,
            note=(
                ""
                if submit
                else "No submit button was found; the generated test presses Enter instead."
            ),
        )
    return None


def sign_in(page: Page, base_url: str, form: LoginForm, email: str, password: str) -> bool:
    """Drive a discovered form. Returns whether sign-in appears to have worked."""
    page.goto(
        f"{base_url}{form.path}", wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
    )

    email_field = _resolve(page, form.email_selector)
    password_field = _resolve(page, form.password_selector)
    if email_field is None or password_field is None:
        return False

    email_field.fill(email)
    password_field.fill(password)
    before = page.url

    submit = _resolve(page, form.submit_selector) if form.submit_selector else None
    if submit is not None:
        submit.click()
    else:
        password_field.press("Enter")

    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)

    # Leaving the login screen is the only signal available without knowing the
    # app. It is weak, and the caller reports it as inferred rather than proven.
    return page.url != before or _resolve(page, form.password_selector) is None
