"""Repository configuration, stored as ``.trout/config.yaml``.

Design rules that the rest of the codebase depends on:

*Secrets are never stored here.* Any field that could hold a credential accepts
an ``env:VAR_NAME`` reference and is resolved at use time by
:func:`resolve_secret`. The file is meant to be committed to the repository, so
a plain secret in it is a bug, not a preference.

*Safety is explicit, not inferred.* An entrypoint is read-only unless it is
marked ``disposable``. Pointing the tool at a production URL and having it
create test data is the single worst thing this tool could do, so the default
refuses rather than warns.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_REF = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")


class SecretResolutionError(RuntimeError):
    """Raised when an ``env:`` reference names a variable that is not set."""


def resolve_secret(value: str | None) -> str | None:
    """Resolve an ``env:VAR`` reference to its value.

    Plain strings pass through unchanged, which keeps local experimentation
    easy while making the committed-secret case obvious in review.

    Raises:
        SecretResolutionError: if the referenced variable is unset. Failing
            loudly here beats a confusing authentication error three layers
            down.
    """
    if value is None:
        return None
    match = _ENV_REF.match(value)
    if not match:
        return value
    name = match.group(1)
    resolved = os.environ.get(name)
    if resolved is None:
        raise SecretResolutionError(
            f"Environment variable {name!r} is not set (referenced as {value!r} in .trout/config.yaml)"
        )
    return resolved


class EntrypointKind(StrEnum):
    """What kind of thing an entrypoint points at."""

    WEB = "web"
    """A browser-reachable application."""
    API = "api"
    """An HTTP base URL for endpoints or edge functions."""


class Permission(StrEnum):
    """Operations permitted against an entrypoint."""

    READ = "read"
    WRITE = "write"


class Entrypoint(BaseModel):
    """One deployment of the application under test.

    There is usually more than one — a local dev server, a preview deployment,
    and production — and they do not share a safety posture. Scenarios name the
    entrypoint they run against, and the runner enforces ``allow``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Referenced by 'qa run --env <name>'.")
    kind: EntrypointKind = EntrypointKind.WEB
    url: str
    disposable: bool = Field(
        default=False,
        description=(
            "True only if the data behind this deployment can be destroyed freely. "
            "Mutating scenarios are refused against non-disposable entrypoints."
        ),
    )
    allow: list[Permission] = Field(default_factory=lambda: [Permission.READ])
    headers: dict[str, str] = Field(
        default_factory=dict, description="Extra headers, e.g. a Vercel bypass token."
    )

    @field_validator("url")
    @classmethod
    def _must_be_absolute(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"entrypoint url must be absolute, got {value!r}")
        return value.rstrip("/")

    @property
    def writable(self) -> bool:
        """Whether mutating scenarios may run here.

        Requires *both* signals to line up. Two independent switches means a
        single careless edit cannot expose production.
        """
        return self.disposable and Permission.WRITE in self.allow


class IsolationStrategy(StrEnum):
    """How database state is returned to a known baseline between scenarios."""

    LOCAL_RESET = "local_reset"
    """`supabase db reset` against a local stack. Complete isolation."""
    BRANCH = "branch"
    """An ephemeral database branch per run against a hosted project."""
    SCOPED_SEED = "scoped_seed"
    """Run-scoped namespacing with cleanup. The only option against a shared
    deployment, and the reason concurrent runs must not collide."""


class SupabaseConfig(BaseModel):
    """Connection and isolation settings for the Supabase backend."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    anon_key: str | None = Field(default=None, description="Use an 'env:' reference.")
    service_role_key: str | None = Field(
        default=None,
        description=(
            "Use an 'env:' reference. Used only for seeding and reset; never "
            "exposed to a browser context."
        ),
    )
    migrations: str = "supabase/migrations"
    functions: str = "supabase/functions"
    isolation: IsolationStrategy = IsolationStrategy.SCOPED_SEED


class TestUser(BaseModel):
    """A seeded account used to exercise the application as a given role.

    At least two distinct roles are needed for authorization testing: proving
    that one user *cannot* see another's data requires a second user.
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="Logical role name, referenced by scenarios.")
    email: str = Field(description="Use an 'env:' reference.")
    password: str = Field(description="Use an 'env:' reference.")


class ExternalRule(BaseModel):
    """A third-party host to intercept during test runs.

    Populated by ``qa scan`` from the SDKs it finds, so a test run cannot reach
    a real payment processor without someone having explicitly removed the
    entry.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Vendor name, e.g. 'stripe'.")
    match: str = Field(description="Hostname to intercept, e.g. 'api.stripe.com'.")


class SubstitutionConfig(BaseModel):
    """The third-party substitution boundary.

    ``on_unmatched_request: fail`` is the important default. A mock that
    silently matches nothing is how a functional suite starts reporting green
    while testing nothing, so an unmatched outbound request is a scenario
    failure rather than a pass.
    """

    model_config = ConfigDict(extra="forbid")

    network: str = Field(default="deny", description="'deny' or 'allowlist'.")
    allowlist: list[str] = Field(default_factory=list)
    contracts: str = ".trout/contracts"
    on_unmatched_request: str = Field(
        default="fail", description="'fail', 'passthrough', 'record'."
    )
    external: list[ExternalRule] = Field(
        default_factory=list,
        description="Third-party hosts to intercept. Populated by `qa scan`.",
    )


class ModelProvider(StrEnum):
    """Supported model providers.

    ``kimi`` speaks the OpenAI wire format against a configurable base URL,
    which also covers self-hosted and foundry-style deployments.
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    KIMI = "kimi"


class ModelConfig(BaseModel):
    """Which model to use, and where to reach it."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider: ModelProvider = ModelProvider.ANTHROPIC
    model: str | None = Field(
        default=None, description="Defaults to the provider's recommended model when unset."
    )
    base_url: str | None = Field(default=None, description="Required for self-hosted endpoints.")
    api_key: str | None = Field(default=None, description="Use an 'env:' reference.")
    max_tokens: int = 8192
    effort: str | None = Field(
        default=None,
        description=(
            "Reasoning effort, for providers that support it: low | high | max. "
            "Left unset uses the provider's default, which on kimi-k3 is 'max' — "
            "accurate but slow enough to be painful for interactive commands."
        ),
    )
    temperature: float | None = None
    """Left unset by default, meaning "whatever the provider does".

    Deliberately not zero. Reasoning models reject a caller-chosen temperature:
    current Claude models return 400 for the parameter at all, and Moonshot's
    kimi-k3 accepts only ``1``. Sending a sensible-looking ``0.0`` therefore
    breaks the two providers most likely to be configured. Set this only for a
    model you know accepts it."""


class RunConfig(BaseModel):
    """Execution defaults."""

    model_config = ConfigDict(extra="forbid")

    certification_runs: int = Field(
        default=3, ge=1, description="Consecutive passes required before a scenario is certified."
    )
    parallel: int = Field(default=4, ge=1)
    retries: int = Field(
        default=0, ge=0, description="Kept at zero: a retry hides the flake it should surface."
    )
    timeout_seconds: int = Field(default=60, ge=1)


class ProjectConfig(BaseModel):
    """What kind of application this is. Populated by ``qa scan``."""

    model_config = ConfigDict(extra="forbid")

    framework: str | None = None
    backend: str | None = None
    auth: str | None = None


class Config(BaseModel):
    """The complete contents of ``.trout/config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    entrypoints: list[Entrypoint] = Field(default_factory=list)
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)
    test_users: list[TestUser] = Field(default_factory=list)
    substitution: SubstitutionConfig = Field(default_factory=SubstitutionConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    run: RunConfig = Field(default_factory=RunConfig)

    def entrypoint(self, name: str | None = None) -> Entrypoint | None:
        """Get an entrypoint by name, or the first configured one."""
        if not self.entrypoints:
            return None
        if name is None:
            return self.entrypoints[0]
        return next((e for e in self.entrypoints if e.name == name), None)

    def user(self, role: str) -> TestUser | None:
        """Get a seeded test user by role."""
        return next((u for u in self.test_users if u.role == role), None)
