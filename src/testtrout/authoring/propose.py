"""Turning a ranked gap into a scenario specification.

The division of labour here is deliberate and narrower than it first looks.

Most of a scenario is *derivable*. An authorization test's table, policy text,
ownership column, and the two roles involved all come from the schema and the
config. A browser journey's route, sign-in step, and selector candidates all
come from the scan and the probe. None of that needs a model, and asking one to
produce it would only introduce a way for it to be wrong.

So scenarios are constructed deterministically, and the model *enriches* them:
it writes a title in the product's vocabulary, decides which of the observed
elements are meaningful anchors rather than incidental buttons, and says what
it could not determine. Enrichment is validated — a selector the model returns
must be one the prober actually saw, or it is dropped.

The result is that a scenario is useful without a model at all, and better with
one.
"""

from __future__ import annotations

from typing import Any

from testtrout.domain.config import Config
from testtrout.domain.gap import Gap, TestKind
from testtrout.domain.intent import ProductIntent, Provenance
from testtrout.domain.observation import ObservedScreen, ProbeResult, SelectorCandidate
from testtrout.domain.scenario import (
    Action,
    Assertion,
    AssertionKind,
    Fixture,
    Scenario,
    Step,
    Target,
)
from testtrout.domain.surface import Policy, ScanResult
from testtrout.llm.gateway import Gateway, GatewayError, load_prompt

# Enrichment is interactive-adjacent and runs once per gap, so it asks for low
# reasoning effort. See docs/setup.md on kimi-k3's slow default.
DEFAULT_EFFORT = "low"

# How many observed elements to offer the model. Enough to choose from, few
# enough that the choice stays considered.
MAX_SELECTOR_CHOICES = 25

ENRICHMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "given", "assertions", "open_questions"],
    "properties": {
        "title": {"type": "string", "description": "One line, in the product's vocabulary."},
        "given": {"type": "array", "items": {"type": "string"}},
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "selector_value", "expected", "reason"],
                "properties": {
                    "kind": {"type": "string", "enum": ["visible", "text", "url"]},
                    "selector_value": {
                        "type": ["string", "null"],
                        "description": "Must be one of the offered selector values, copied exactly.",
                    },
                    "expected": {"type": ["string", "null"]},
                    "reason": {
                        "type": "string",
                        "description": "The evidence for this expectation.",
                    },
                },
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}


def propose(
    gap: Gap,
    scan: ScanResult,
    config: Config,
    probe: ProbeResult | None = None,
    intent: ProductIntent | None = None,
    gateway: Gateway | None = None,
) -> tuple[Scenario, list[str]]:
    """Build a scenario specification for one gap.

    Args:
        gap: The gap this scenario closes.
        scan: The static surface map, supplying tables, policies, and routes.
        config: Repository configuration, supplying test-user roles and the
            entrypoint the scenario runs against.
        probe: Observations from a running deployment. Without these, browser
            scenarios have no selectors and can only assert that a page loads.
        intent: Stated product intent, used to give the model the consequence
            of the journey this scenario protects.
        gateway: When supplied, the deterministic skeleton is enriched. When
            omitted, a usable scenario is still produced — it simply has a
            plainer title and fewer assertions.

    Returns:
        The scenario and any warnings raised while validating enrichment.
    """
    roles = [u.role for u in config.test_users]
    entrypoint = config.entrypoint()

    if gap.kind is TestKind.AUTHORIZATION:
        scenario = _authorization(gap, scan, roles)
    elif gap.kind is TestKind.BROWSER_JOURNEY:
        scenario = _browser(gap, scan, probe, roles)
    else:
        scenario = _endpoint(gap, scan, roles)

    scenario.entrypoint = entrypoint.name if entrypoint else None
    scenario.criticality = gap.criticality
    scenario.journey_id = gap.journey_id
    scenario.estimated_seconds = gap.estimated_seconds
    scenario.substitute = sorted({e.vendor for e in scan.externals if e.side_effecting})

    warnings: list[str] = []
    if gateway is not None:
        warnings = _enrich(scenario, gap, scan, probe, intent, gateway)
    return scenario, warnings


def _authorization(gap: Gap, scan: ScanResult, roles: list[str]) -> Scenario:
    """Build an authorization scenario. Entirely derived — no inference.

    The policy states the expectation, the schema supplies the ownership
    column, and the config supplies the roles. There is nothing here a model
    could improve by guessing at.
    """
    policy = next((p for p in scan.policies if p.id in gap.surfaces), None)
    table = policy.table if policy else "unknown"
    # The policy states the ownership column outright when it is a simple
    # comparison, which beats guessing from column names — `profiles` is scoped
    # by `id`, which no name-based heuristic would find.
    owner_column = policy.owner_column if policy else None
    if owner_column is None:
        owner_column = next((t.owner_column for t in scan.tables if t.name == table), None)

    fixtures: list[Fixture] = []
    if owner_column:
        # Threaded to the emitter through the fixture rather than as a bespoke
        # field, so the scenario schema stays generic across test kinds.
        fixtures.append(
            Fixture(
                table=table,
                description=f"At least one row in {table} owned by another user.",
                columns={"__owner_column__": owner_column},
            )
        )

    assertion = Assertion(
        kind=AssertionKind.ROW_COUNT,
        target=Target(table=table, description=f"rows in {table} owned by someone else"),
        expected="0",
        provenance=Provenance.DERIVED,
        source=_policy_source(policy),
        description="No row readable by this user belongs to anyone else.",
    )

    return Scenario(
        id=gap.id.replace("gap:", "scenario:"),
        title=gap.title,
        kind=TestKind.AUTHORIZATION,
        provenance=Provenance.DERIVED,
        gap_id=gap.id,
        surfaces=list(gap.surfaces),
        role=roles[0] if roles else None,
        other_role=roles[1] if len(roles) > 1 else None,
        given=[
            f"A seeded test user with the {roles[0] if roles else 'first'} role.",
            f"At least one row in {table} owned by a different user.",
        ],
        when=[
            Step(
                action=Action.QUERY,
                target=Target(table=table),
                description=f"Read everything the policy permits from {table}.",
            )
        ],
        then=[assertion],
        fixtures=fixtures,
        open_questions=(
            []
            if owner_column
            else [
                f"Which column on {table} scopes a row to a user? Without it the generated "
                "test can only check that the read succeeds, which proves very little."
            ]
        ),
    )


def _browser(gap: Gap, scan: ScanResult, probe: ProbeResult | None, roles: list[str]) -> Scenario:
    """Build a browser journey from the route and what the prober observed."""
    screen = next((s for s in scan.screens if s.id in gap.surfaces), None)
    path = screen.path if screen else "/"
    observed = _observed_for(probe, path)
    role = roles[0] if roles else None

    steps: list[Step] = []
    if role:
        steps.append(Step(action=Action.SIGN_IN, value=role, description=f"Sign in as {role}."))
    steps.append(
        Step(
            action=Action.NAVIGATE,
            target=Target(url=_concrete(path, observed)),
            description=f"Visit {path}.",
        )
    )

    anchors = _default_anchors(observed)
    assertions = [
        Assertion(
            kind=AssertionKind.VISIBLE,
            target=Target(selector=anchor, description=anchor.description),
            provenance=Provenance.OBSERVED,
            source=f"observed on {path} during `trout probe`",
            description=f"{anchor.description or anchor.value} is present",
        )
        for anchor in anchors
    ]

    questions: list[str] = []
    if observed is None:
        questions.append(
            f"{path} has not been probed, so no selectors are known. Run `trout probe` first — "
            "otherwise this test can only assert that the page loads."
        )
    elif not anchors:
        questions.append(
            f"{path} exposed no stable elements when probed. What should this test check?"
        )

    return Scenario(
        id=gap.id.replace("gap:", "scenario:"),
        title=gap.title,
        kind=TestKind.BROWSER_JOURNEY,
        provenance=Provenance.OBSERVED if observed else Provenance.DERIVED,
        gap_id=gap.id,
        surfaces=list(gap.surfaces),
        role=role,
        given=[f"Signed in as {role}."] if role else [],
        when=steps,
        then=assertions,
        open_questions=questions,
    )


def _endpoint(gap: Gap, scan: ScanResult, roles: list[str]) -> Scenario:
    """Build an endpoint or data-operation scenario."""
    endpoint = next((e for e in scan.endpoints if e.id in gap.surfaces), None)
    action = next((a for a in scan.server_actions if a.id in gap.surfaces), None)
    operation = next((o for o in scan.data_operations if o.id in gap.surfaces), None)

    url = endpoint.path if endpoint else "/"
    method = endpoint.methods[0] if endpoint and endpoint.methods else "GET"

    questions: list[str] = []
    assertions: list[Assertion] = []

    if endpoint or action:
        name = endpoint.path if endpoint else (action.name if action else "the endpoint")
        assertions.append(
            Assertion(
                kind=AssertionKind.STATUS,
                target=Target(url=url, method=method),
                expected="401",
                provenance=Provenance.INFERRED,
                source="unauthenticated callers should be refused",
                description=f"{name} refuses an unauthenticated caller",
            )
        )
        questions.append(
            f"Should {name} refuse unauthenticated callers with 401, or does it serve public "
            "data? The generated test assumes it refuses."
        )
    elif operation:
        questions.append(
            f"What is the correct outcome of {operation.operation.value} on "
            f"{operation.table or 'this table'}? The tool can see that it happens but not "
            "what it should produce."
        )

    return Scenario(
        id=gap.id.replace("gap:", "scenario:"),
        title=gap.title,
        kind=TestKind.ENDPOINT if (endpoint or action) else TestKind.DATA_OPERATION,
        provenance=Provenance.DERIVED,
        gap_id=gap.id,
        surfaces=list(gap.surfaces),
        role=roles[0] if roles else None,
        when=[
            Step(
                action=Action.REQUEST,
                target=Target(url=url, method=method),
                description=f"{method} {url}",
            )
        ],
        then=assertions,
        open_questions=questions,
    )


def _policy_source(policy: Policy | None) -> str:
    """Quote the policy that justifies an authorization assertion."""
    if policy is None:
        return "row-level security policy"
    clause = policy.using or policy.with_check or ""
    return f"policy {policy.name!r} on {policy.table}: {clause}".strip().rstrip(":")


def _observed_for(probe: ProbeResult | None, path: str) -> ObservedScreen | None:
    """The probe result for a route, if it was reached."""
    if probe is None:
        return None
    screen = next((s for s in probe.screens if s.path == path), None)
    return screen if screen and screen.reachable else None


def _concrete(path: str, observed: ObservedScreen | None) -> str:
    """Prefer the URL actually visited, so route parameters are already resolved."""
    if observed is None:
        return path
    from urllib.parse import urlparse

    return urlparse(observed.url).path or path


def _default_anchors(observed: ObservedScreen | None) -> list[SelectorCandidate]:
    """Pick a couple of durable anchors without a model.

    Deliberately conservative: a heading and one interactive element are enough
    to prove the screen rendered, and asserting on everything visible produces
    a test that fails for cosmetic reasons.
    """
    if observed is None:
        return []
    best = [c for c in observed.selectors if c.strategy.rank <= 1]
    heading = next((c for c in best if c.role == "heading" or "head" in c.value.lower()), None)
    other = next((c for c in best if c is not heading), None)
    return [c for c in (heading, other) if c is not None][:2]


def _enrich(
    scenario: Scenario,
    gap: Gap,
    scan: ScanResult,
    probe: ProbeResult | None,
    intent: ProductIntent | None,
    gateway: Gateway,
) -> list[str]:
    """Improve a scenario with a model, validating everything it returns."""
    observed = _observed_for(probe, _path_of(scenario, scan))
    choices = observed.selectors[:MAX_SELECTOR_CHOICES] if observed else []
    allowed = {c.value: c for c in choices}

    try:
        response = gateway.complete(
            system=load_prompt("propose_scenario"),
            user=_enrichment_context(scenario, gap, scan, intent, choices),
            schema=ENRICHMENT_SCHEMA,
            effort=DEFAULT_EFFORT,
        )
        payload = response.json()
    except (GatewayError, ValueError) as exc:
        return [f"enrichment skipped: {exc}"]

    if not isinstance(payload, dict):
        return ["enrichment skipped: the model returned something that was not an object"]

    warnings: list[str] = []
    if title := str(payload.get("title") or "").strip():
        scenario.title = title
    if given := [str(g) for g in payload.get("given") or []]:
        scenario.given = given

    added: list[Assertion] = []
    for raw in payload.get("assertions") or []:
        if not isinstance(raw, dict):
            continue
        value = raw.get("selector_value")
        selector = allowed.get(str(value)) if value else None
        if value and selector is None:
            warnings.append(
                f"{scenario.id}: the model referenced a selector "
                f"{str(value)[:40]!r} that was never observed — assertion dropped"
            )
            continue
        try:
            kind = AssertionKind(str(raw.get("kind")))
        except ValueError:
            continue
        added.append(
            Assertion(
                kind=kind,
                target=Target(selector=selector),
                expected=str(raw["expected"]) if raw.get("expected") else None,
                # The model chose it, but the element itself was observed. That
                # is a real distinction: the anchor exists, the judgement about
                # what it means does not carry the same weight.
                provenance=Provenance.OBSERVED if selector else Provenance.INFERRED,
                source=str(raw.get("reason") or ""),
            )
        )

    if added:
        scenario.then = added
    scenario.open_questions.extend(str(q) for q in payload.get("open_questions") or [])
    return warnings


def _path_of(scenario: Scenario, scan: ScanResult) -> str:
    """The route *pattern* a browser scenario covers.

    Not the URL in the navigate step: that has already had its parameters
    substituted, so `/orders/ord_1` would never match the probe's record of
    `/orders/:id` and the scenario would look unprobed when it was not.
    """
    for surface_id in scenario.surfaces:
        screen = next((s for s in scan.screens if s.id == surface_id), None)
        if screen is not None:
            return screen.path
    return next((s.target.url or "/" for s in scenario.when if s.action is Action.NAVIGATE), "/")


def _enrichment_context(
    scenario: Scenario,
    gap: Gap,
    scan: ScanResult,
    intent: ProductIntent | None,
    choices: list[SelectorCandidate],
) -> str:
    """Everything the model is allowed to reason from."""
    lines = [
        f"## Scenario\n\nkind: {scenario.kind.value}\ntitle: {scenario.title}",
        f"criticality: {scenario.criticality.value}",
        "\n## Why this test was proposed\n",
        *[f"- {reason}" for reason in gap.reasons],
    ]

    if scenario.surfaces:
        lines.append("\n## Surfaces\n")
        for surface_id in scenario.surfaces:
            surface = scan.by_id(surface_id)
            if surface is not None:
                lines.append(f"- `{surface_id}` at {surface.location}")

    if choices:
        lines.append("\n## Elements observed on this screen\n")
        lines += [
            f"- `{c.value}` ({c.strategy.value}"
            + (f", {c.role}" if c.role else "")
            + f") — {c.description or c.name or 'no text'}"
            for c in choices
        ]
    else:
        lines.append("\n## Elements observed on this screen\n\nNone. The screen was not probed.")

    if intent is not None and scenario.journey_id:
        journey = intent.journey(scenario.journey_id)
        if journey is not None:
            lines.append(
                f"\n## Stated intent\n\n{journey.name}: {journey.description}\n"
                f"If it breaks: {journey.consequence}"
            )

    return "\n".join(lines)
