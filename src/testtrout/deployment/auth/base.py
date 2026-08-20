"""The authentication adapter contract.

Public API: third-party packages implement this and register under the
``testtrout.auth`` entry point group.

An adapter's job is to leave a browser context holding a valid session for one
role. How it gets there is its own business — an API call plus storage
injection, a driven login form, a cookie — but it must report honestly whether
it succeeded, because a probe that silently runs unauthenticated produces a map
of the login screen and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from testtrout.domain.config import Config, TestUser

if TYPE_CHECKING:  # pragma: no cover - import cost only matters at runtime
    from playwright.sync_api import BrowserContext, Page, StorageState

ENTRY_POINT_GROUP = "testtrout.auth"


@dataclass
class AuthOutcome:
    """Whether a role was signed in, and how.

    ``detail`` is written for a human staring at a failed probe. "No password
    grant on this project" is actionable; "auth failed" is not.
    """

    authenticated: bool
    method: str
    detail: str = ""
    storage_state: StorageState | None = field(default=None, repr=False)
    """Playwright storage state, reusable across scenarios so that a suite
    signs in once per role rather than once per test."""


@runtime_checkable
class AuthAdapter(Protocol):
    """Establishes an authenticated browser session for one role."""

    id: ClassVar[str]

    def matches(self, config: Config) -> bool:
        """Whether this adapter handles the project's auth provider."""
        ...

    def authenticate(
        self, context: BrowserContext, page: Page, config: Config, user: TestUser
    ) -> AuthOutcome:
        """Sign ``user`` in, leaving ``context`` holding the session.

        Must not raise for an ordinary failure such as bad credentials —
        return ``AuthOutcome(authenticated=False, ...)`` with a usable
        ``detail`` instead, so the caller can report it alongside everything
        else rather than aborting the run.
        """
        ...


def load_adapters() -> list[AuthAdapter]:
    """Load every registered auth adapter.

    A broken third-party adapter is skipped rather than taking down the run.
    """
    adapters: list[AuthAdapter] = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            adapters.append(entry.load()())
        except Exception:
            continue
    return adapters


def select_adapter(config: Config) -> AuthAdapter | None:
    """Pick the adapter that claims this project's auth provider."""
    return next((a for a in load_adapters() if a.matches(config)), None)
