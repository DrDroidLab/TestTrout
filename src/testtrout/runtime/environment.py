"""Building the environment a test process runs in.

Generated tests read credentials from environment variables, never from
``.trout/config.yaml``. This module is where ``env:`` references are resolved and
handed to the child process — and where the resolved values stop, because they
are never logged, never written to a run record, and never included in an
evidence bundle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from testtrout.domain.config import Config, Entrypoint, SecretResolutionError, resolve_secret
from testtrout.domain.scenario import Scenario


@dataclass
class TestEnvironment:
    """Environment variables for a test process, plus what could not be resolved."""

    variables: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether everything the tests need is present."""
        return not self.missing

    def merged(self) -> dict[str, str]:
        """The full environment for the child process."""
        return {**os.environ, **self.variables}

    def names(self) -> list[str]:
        """Variable names only. Never call anything that returns values."""
        return sorted(self.variables)


def build(
    config: Config,
    entrypoint: Entrypoint,
    scenarios: list[Scenario] | None = None,
) -> TestEnvironment:
    """Resolve every credential the generated tests expect.

    A missing variable is collected rather than raised, so the caller can
    report all of them at once. Discovering three missing secrets one run at a
    time is a miserable way to configure anything.
    """
    environment = TestEnvironment()
    environment.variables["TROUT_BASE_URL"] = entrypoint.url
    # Endpoint tests call the API, which is not always the same origin as the
    # pages. Falls back to the page URL, so an app that serves both from one
    # place needs no extra configuration.
    environment.variables["TROUT_API_URL"] = entrypoint.api_base
    # Only ever set for a deployment whose data can be destroyed. Generated
    # tests check it themselves, so the guarantee survives being run by hand.
    if entrypoint.writable:
        environment.variables["TROUT_ALLOW_WRITES"] = "1"

    def take(name: str, reference: str | None, *, required: bool) -> None:
        if reference is None:
            if required:
                environment.missing.append(f"{name} (not set in .trout/config.yaml)")
            return
        try:
            value = resolve_secret(reference)
        except SecretResolutionError as exc:
            environment.missing.append(str(exc))
            return
        if value:
            environment.variables[name] = value
        elif required:
            environment.missing.append(f"{name} resolved to an empty value")

    # Optional throughout. Tests reach the app over HTTP and through a browser,
    # so its database credentials are never needed to run them — they only
    # enable resetting data between runs.
    take("SUPABASE_URL", config.supabase.url, required=False)
    take("SUPABASE_ANON_KEY", config.supabase.anon_key, required=False)
    take("SUPABASE_SERVICE_ROLE_KEY", config.supabase.service_role_key, required=False)

    # Accounts are required only when something actually signs in. A suite of
    # public endpoint tests needs none, and demanding them would block a
    # perfectly runnable set of tests.
    needs_accounts = any(s.role or s.other_role for s in scenarios) if scenarios else True
    for user in config.test_users:
        prefix = f"TROUT_{user.role.upper().replace('-', '_')}"
        take(f"{prefix}_EMAIL", user.email, required=needs_accounts)
        take(f"{prefix}_PASSWORD", user.password, required=needs_accounts)

    # Substituted vendors are passed by name so the browser helper knows which
    # hosts to intercept. Never a credential, so safe to pass verbatim.
    if config.substitution.external:
        environment.variables["TROUT_SUBSTITUTE_HOSTS"] = ",".join(
            sorted({rule.match for rule in config.substitution.external if rule.match})
        )
    environment.variables["TROUT_ON_UNMATCHED_REQUEST"] = config.substitution.on_unmatched_request

    return environment
