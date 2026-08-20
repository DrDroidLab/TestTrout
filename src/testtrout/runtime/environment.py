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


def build(config: Config, entrypoint: Entrypoint) -> TestEnvironment:
    """Resolve every credential the generated tests expect.

    A missing variable is collected rather than raised, so the caller can
    report all of them at once. Discovering three missing secrets one run at a
    time is a miserable way to configure anything.
    """
    environment = TestEnvironment()
    environment.variables["TROUT_BASE_URL"] = entrypoint.url

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

    take("SUPABASE_URL", config.supabase.url, required=True)
    take("SUPABASE_ANON_KEY", config.supabase.anon_key, required=True)
    take("SUPABASE_SERVICE_ROLE_KEY", config.supabase.service_role_key, required=False)

    for user in config.test_users:
        prefix = f"TROUT_{user.role.upper().replace('-', '_')}"
        take(f"{prefix}_EMAIL", user.email, required=True)
        take(f"{prefix}_PASSWORD", user.password, required=True)

    # Substituted vendors are passed by name so the browser helper knows which
    # hosts to intercept. Never a credential, so safe to pass verbatim.
    if config.substitution.external:
        environment.variables["TROUT_SUBSTITUTE_HOSTS"] = ",".join(
            sorted({rule.match for rule in config.substitution.external if rule.match})
        )
    environment.variables["TROUT_ON_UNMATCHED_REQUEST"] = config.substitution.on_unmatched_request

    return environment
