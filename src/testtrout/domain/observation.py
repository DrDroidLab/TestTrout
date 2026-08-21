"""What was observed against a running deployment.

Static analysis says what the code *can* do. Probing says what the deployment
*actually* does when a real browser loads it as a real user. The gap between
those two is where the interesting questions live: a screen the code declares
but nobody can reach, a query firing that no call site explains, a policy
denying a read the UI clearly expects to work.

Everything here is an observation, not a conclusion. Observations are evidence
for later stages; they are never treated as assertions on their own.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CallKind(StrEnum):
    """What a request seen in the browser was talking to."""

    SUPABASE_REST = "supabase_rest"
    """PostgREST: the table reads and writes that are this stack's backend."""
    SUPABASE_AUTH = "supabase_auth"
    SUPABASE_STORAGE = "supabase_storage"
    SUPABASE_REALTIME = "supabase_realtime"
    SUPABASE_FUNCTION = "supabase_function"
    FIRST_PARTY = "first_party"
    """The application's own origin — Route Handlers, Server Actions, assets."""
    EXTERNAL = "external"
    """A third-party host. These become the substitution boundary."""


class NetworkCall(BaseModel):
    """One request the page made, as the browser saw it.

    The response *body* is deliberately not stored. It is the fastest way to
    leak customer data into a committed artifact, and nothing downstream needs
    it — shape and status are enough to reconcile against the static scan.
    """

    model_config = ConfigDict(extra="forbid")

    method: str
    url: str
    host: str
    kind: CallKind
    status: int | None = None
    table: str | None = Field(default=None, description="Parsed from a PostgREST path.")
    operation: str | None = Field(default=None, description="select/insert/update/delete.")
    blocked: bool = Field(
        default=False,
        description="True when the prober refused to let a write reach a non-disposable deployment.",
    )
    failed: bool = False

    @property
    def denied(self) -> bool:
        """Whether the backend refused this request.

        401 and 403 against PostgREST usually mean a row-level security policy
        rejected it, which is a finding rather than a failure of the probe.
        """
        return self.status in {401, 403}


class SelectorStrategy(StrEnum):
    """How a generated test would locate an element, best first.

    Ordering is the point. A test that finds a button by its accessible role
    and name survives a restyle; one that finds it by a generated class name
    does not. Phase 4 always prefers the highest-ranked available strategy.
    """

    TEST_ID = "test_id"
    ROLE = "role"
    LABEL = "label"
    TEXT = "text"
    CSS = "css"

    @property
    def rank(self) -> int:
        """Sort key, most stable first."""
        return ["test_id", "role", "label", "text", "css"].index(self.value)


class SelectorCandidate(BaseModel):
    """A way to address one element on a screen."""

    model_config = ConfigDict(extra="forbid")

    strategy: SelectorStrategy
    value: str
    role: str | None = None
    name: str | None = None
    description: str = Field(default="", description="What the element appears to do.")

    def playwright(self) -> str:
        """Render as Playwright locator source.

        Kept next to the data rather than in the emitter so that the mapping
        from observation to locator is verifiable in one place.
        """
        if self.strategy is SelectorStrategy.TEST_ID:
            return f"getByTestId({self.value!r})"
        if self.strategy is SelectorStrategy.ROLE:
            name = f", name={self.name!r}" if self.name else ""
            return f"getByRole({self.role!r}{name})"
        if self.strategy is SelectorStrategy.LABEL:
            return f"getByLabel({self.value!r})"
        if self.strategy is SelectorStrategy.TEXT:
            return f"getByText({self.value!r})"
        return f"locator({self.value!r})"


class ObservedScreen(BaseModel):
    """The result of loading one route in a real browser."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Route pattern from the static scan.")
    url: str = Field(description="The concrete URL actually visited.")
    reachable: bool
    status: int | None = None
    title: str | None = None
    redirected_to: str | None = None
    requires_auth: bool = Field(
        default=False, description="Set when the route redirects to a login screen."
    )
    console_errors: list[str] = Field(default_factory=list)
    selectors: list[SelectorCandidate] = Field(default_factory=list)
    calls: list[NetworkCall] = Field(default_factory=list)
    note: str | None = Field(
        default=None, description="Why it could not be probed, if it could not."
    )


class Divergence(BaseModel):
    """One difference between what the code says and what the deployment does.

    These are the headline output of a probe. Each is a question worth a
    developer's attention, phrased as an observation rather than a verdict.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    surface_id: str | None = None
    detail: str | None = None


class ObservedLogin(BaseModel):
    """A sign-in form the probe located.

    Recorded so that generated tests replay a known form rather than guessing
    at one on every run — which is what makes driving the app's own login a
    viable alternative to holding its database credentials.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    email_selector: str
    password_selector: str
    submit_selector: str | None = None
    note: str = ""


class ProbeResult(BaseModel):
    """Everything one probe run observed. Written to ``.trout/observed/``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    entrypoint: str
    base_url: str
    role: str | None = Field(default=None, description="Which test user was signed in.")
    authenticated: bool = False
    login: ObservedLogin | None = Field(
        default=None, description="The sign-in form, if one was found."
    )
    screens: list[ObservedScreen] = Field(default_factory=list)
    divergences: list[Divergence] = Field(default_factory=list)
    external_hosts: list[str] = Field(
        default_factory=list,
        description="Third-party hosts contacted, for the substitution boundary.",
    )

    @property
    def reachable_count(self) -> int:
        """How many routes actually loaded."""
        return sum(1 for s in self.screens if s.reachable)

    def all_calls(self) -> list[NetworkCall]:
        """Every request observed across every screen."""
        return [call for screen in self.screens for call in screen.calls]
