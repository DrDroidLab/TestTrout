"""The product surface: everything in an application that can be tested.

A *surface* is one addressable behavior of the application. The scanner's job
is to enumerate surfaces; the gap analyser's job is to decide which ones are
unprotected; the proposer's job is to suggest scenarios for them.

Why this shape
--------------
A generic "HTTP route to handler" model finds almost nothing in the target
stack, because a React application built with Lovable or v0 typically talks to
Supabase directly from component code with no API layer in between. So the
surface taxonomy here is deliberately stack-aware: a ``DataOperation`` (one
Supabase call site) is a first-class surface, and so is a row-level security
``Policy``, because authorization is the failure class that hurts these
applications most.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.location import SourceLocation as SourceLocation
from testtrout.domain.requirements import Requirement


class Criticality(StrEnum):
    """How much it matters that a surface keeps working.

    Assigned by :mod:`testtrout.analysis.criticality` from deterministic signals
    (does it write data, is it behind auth, does it touch payments), then
    optionally raised by the developer's stated intent. It is never lowered by
    a model.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """Sort key, highest criticality first."""
        return {"critical": 0, "high": 1, "medium": 2, "low": 3}[self.value]


class Operation(StrEnum):
    """A Supabase/PostgREST operation observed at a call site."""

    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    UPSERT = "upsert"
    DELETE = "delete"
    RPC = "rpc"
    # Auth and storage calls are surfaces too — they are common sources of
    # user-visible breakage and are frequently untested.
    AUTH = "auth"
    STORAGE = "storage"
    REALTIME = "realtime"

    @property
    def writes(self) -> bool:
        """Whether the operation can modify persisted state."""
        return self in {
            Operation.INSERT,
            Operation.UPDATE,
            Operation.UPSERT,
            Operation.DELETE,
            Operation.RPC,
            Operation.STORAGE,
        }


class SurfaceKind(StrEnum):
    """Discriminator for the :data:`Surface` union."""

    SCREEN = "screen"
    DATA_OPERATION = "data_operation"
    ENDPOINT = "endpoint"
    SERVER_ACTION = "server_action"
    EDGE_FUNCTION = "edge_function"
    POLICY = "policy"
    EXTERNAL = "external"


class _BaseSurface(BaseModel):
    """Fields common to every surface kind.

    ``id`` is stable across scans so that approvals, coverage records, and
    quarantine decisions survive a re-scan. Stability comes from deriving it
    from durable properties (route path, table plus operation, policy name)
    rather than from file position.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, human-readable identifier, e.g. 'data:orders.insert'.")
    location: SourceLocation
    criticality: Criticality = Criticality.MEDIUM
    criticality_reasons: list[str] = Field(
        default_factory=list,
        description="Why this criticality was assigned. Shown to the user; never invented.",
    )
    requires_auth: bool | None = Field(
        default=None, description="None when the scanner could not determine it."
    )


class Screen(_BaseSurface):
    """A user-reachable route and the component tree behind it."""

    kind: Literal[SurfaceKind.SCREEN] = SurfaceKind.SCREEN
    path: str = Field(description="Route pattern, e.g. '/orders/:id'.")
    component: str = Field(description="Top-level component rendered at this route.")
    params: list[str] = Field(default_factory=list)
    layout: str | None = None
    reaches: list[str] = Field(
        default_factory=list,
        description="Ids of data operations and endpoints reachable from this screen.",
    )


class DataOperation(_BaseSurface):
    """A single Supabase client call site.

    This is the characteristic surface of the target stack. ``table``,
    ``operation`` and ``columns`` are resolved statically from the method
    chain; anything that cannot be resolved is left ``None`` rather than
    guessed, and is reported as a scan warning instead.
    """

    kind: Literal[SurfaceKind.DATA_OPERATION] = SurfaceKind.DATA_OPERATION
    table: str | None = Field(default=None, description="None for rpc/auth/storage calls.")
    operation: Operation
    columns: list[str] = Field(default_factory=list)
    filters: list[str] = Field(
        default_factory=list,
        description="Filter methods applied, e.g. ['eq(user_id)', 'limit'].",
    )
    function: str | None = Field(default=None, description="Function name for rpc calls.")
    bucket: str | None = Field(default=None, description="Bucket name for storage calls.")
    in_component: str | None = None


class Endpoint(_BaseSurface):
    """A Next.js Route Handler or any first-party HTTP endpoint."""

    kind: Literal[SurfaceKind.ENDPOINT] = SurfaceKind.ENDPOINT
    path: str
    methods: list[str] = Field(default_factory=list)
    runtime: str | None = Field(default=None, description="'edge' or 'nodejs' when declared.")


class ServerAction(_BaseSurface):
    """A Next.js Server Action.

    Easy to miss and rarely tested: it is an RPC endpoint that happens to look
    like a function call, so it carries the risk profile of an endpoint with
    the visibility of a helper.
    """

    kind: Literal[SurfaceKind.SERVER_ACTION] = SurfaceKind.SERVER_ACTION
    name: str
    module: str


class EdgeFunction(_BaseSurface):
    """A Supabase Edge Function."""

    kind: Literal[SurfaceKind.EDGE_FUNCTION] = SurfaceKind.EDGE_FUNCTION
    name: str
    verify_jwt: bool | None = None


class Policy(_BaseSurface):
    """A row-level security policy parsed from a SQL migration.

    Policies are surfaces because they are behavior: "a member cannot read
    another tenant's orders" is an assertion a test can make. They are also
    the cheapest high-value tests to generate, since the policy text states
    the expectation directly.
    """

    kind: Literal[SurfaceKind.POLICY] = SurfaceKind.POLICY
    name: str
    table: str
    command: str = Field(description="SELECT, INSERT, UPDATE, DELETE, or ALL.")
    roles: list[str] = Field(default_factory=list)
    permissive: bool = True
    using: str | None = Field(default=None, description="USING expression, verbatim.")
    with_check: str | None = Field(default=None, description="WITH CHECK expression, verbatim.")

    @property
    def owner_column(self) -> str | None:
        """The column this policy compares against the current user, if it is simple.

        A policy like ``auth.uid() = user_id`` states the ownership column
        outright, which is strictly better than guessing from column names —
        ``profiles`` is scoped by ``id``, which no name-based heuristic would
        find.

        Returns ``None`` for anything more complex than a direct comparison. A
        policy that reaches through a join genuinely cannot be reduced to one
        column, and pretending otherwise would generate a test asserting the
        wrong thing.
        """
        import re

        clause = self.using or self.with_check
        if not clause:
            return None
        # Reject subqueries and joins outright: those are not single-column
        # ownership, and a partial match would be worse than no answer.
        if re.search(r"\b(select|exists|join)\b", clause, re.IGNORECASE):
            return None

        patterns = (
            r"auth\.uid\(\)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)",
            r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*auth\.uid\(\)",
        )
        for pattern in patterns:
            match = re.search(pattern, clause)
            if match:
                return match.group(1).split(".")[-1]
        return None


class ExternalDependency(_BaseSurface):
    """A third-party SDK call site — the substitution boundary."""

    kind: Literal[SurfaceKind.EXTERNAL] = SurfaceKind.EXTERNAL
    vendor: str = Field(description="Normalised vendor name, e.g. 'stripe'.")
    package: str
    hosts: list[str] = Field(
        default_factory=list, description="Hostnames to intercept when substituting."
    )
    side_effecting: bool = Field(
        default=True, description="Whether calling it for real has consequences (charges, emails)."
    )


Surface = Annotated[
    Screen | DataOperation | Endpoint | ServerAction | EdgeFunction | Policy | ExternalDependency,
    Field(discriminator="kind"),
]
"""Discriminated union of every surface kind. Serialises to clean YAML."""


class Column(BaseModel):
    """One column of a database table."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    primary_key: bool = False
    references: str | None = Field(default=None, description="'table.column' for a foreign key.")


class Table(BaseModel):
    """A database table parsed from migrations.

    Not a surface in itself — you do not test a table, you test the operations
    against it — but scenario generation needs the schema to build valid
    fixtures, and policy tests need to know which column carries ownership.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    columns: list[Column] = Field(default_factory=list)
    rls_enabled: bool = False
    location: SourceLocation | None = None

    @property
    def owner_column(self) -> str | None:
        """Best guess at the column that scopes a row to a user or tenant.

        Used to generate authorization tests. Returns ``None`` rather than
        guessing when nothing matches, so callers ask the developer instead.
        """
        for candidate in ("user_id", "owner_id", "profile_id", "account_id", "tenant_id", "org_id"):
            if any(c.name == candidate for c in self.columns):
                return candidate
        return None


class ScanWarning(BaseModel):
    """Something the scanner could not resolve.

    Surfaced to the user rather than silently dropped: an unresolved table name
    is a blind spot in the resulting test suite, and the developer is the one
    who can fix it.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    location: SourceLocation | None = None


class ProjectInfo(BaseModel):
    """What the scanner determined about the project as a whole."""

    model_config = ConfigDict(extra="forbid")

    root: str
    framework: str = Field(description="Adapter id, e.g. 'vite-react' or 'nextjs-app'.")
    backend: str | None = None
    auth: str | None = None
    package_manager: str | None = None
    typescript: bool = True
    api_base_var: str = Field(
        default="",
        description=(
            "Environment variable holding the API's base URL, when the app calls a "
            "separate backend. The *name* only — a URL in the source is a development "
            "default and would be wrong about production."
        ),
    )
    detected_from: list[str] = Field(
        default_factory=list, description="Evidence for the detection, for auditability."
    )


class ScanResult(BaseModel):
    """The complete output of ``trout look``, serialised to ``.trout/surfaces.yaml``.

    Deterministic: the same repository at the same commit always produces a
    byte-identical result. That property is what makes the golden-file tests in
    ``tests/golden/`` meaningful, and it is worth preserving.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    tool_version: str = Field(
        default="",
        description=(
            "The build that produced this scan. A result from an older build can be "
            "wrong in ways nothing else reveals — it simply reports less."
        ),
    )
    project: ProjectInfo
    screens: list[Screen] = Field(default_factory=list)
    data_operations: list[DataOperation] = Field(default_factory=list)
    endpoints: list[Endpoint] = Field(default_factory=list)
    server_actions: list[ServerAction] = Field(default_factory=list)
    edge_functions: list[EdgeFunction] = Field(default_factory=list)
    policies: list[Policy] = Field(default_factory=list)
    externals: list[ExternalDependency] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    requirements: list[Requirement] = Field(
        default_factory=list,
        description="Credentials this deployment needs, discovered from its own source.",
    )
    warnings: list[ScanWarning] = Field(default_factory=list)

    def all_surfaces(self) -> list[Surface]:
        """Every surface, flattened, ordered by criticality then id."""
        items: list[Surface] = [
            *self.screens,
            *self.data_operations,
            *self.endpoints,
            *self.server_actions,
            *self.edge_functions,
            *self.policies,
            *self.externals,
        ]
        return sorted(items, key=lambda s: (s.criticality.rank, s.id))

    def by_id(self, surface_id: str) -> Surface | None:
        """Look up a single surface by its stable id."""
        return next((s for s in self.all_surfaces() if s.id == surface_id), None)

    @property
    def counts(self) -> dict[str, int]:
        """Surface counts by kind, for summary output."""
        return {
            "screens": len(self.screens),
            "data_operations": len(self.data_operations),
            "endpoints": len(self.endpoints),
            "server_actions": len(self.server_actions),
            "edge_functions": len(self.edge_functions),
            "policies": len(self.policies),
            "externals": len(self.externals),
            "tables": len(self.tables),
        }
