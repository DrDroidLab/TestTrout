"""Capturing product intent, with a model doing the one job it is best at.

The model is used to turn prose into structure and to draft a first guess from
the surface map. It is not used to decide what matters — that judgement is the
developer's, and the whole point of this step is to get it out of their head.

Three guardrails make the output trustworthy:

*Surface ids are validated, never trusted.* A model that invents an id would
silently attach a journey to nothing. Unknown ids are dropped and reported.

*Everything drafted is marked ``inferred``* until a human confirms it, and
inferred intent can never justify a blocking assertion on its own.

*Uncertainty becomes an open question.* The model is instructed to ask rather
than guess, because a developer answers "what is the audit_log table for?" in
five seconds and a wrong guess propagates into every test built afterwards.
"""

from __future__ import annotations

import re
from typing import Any

from testtrout.domain.intent import Journey, OpenQuestion, ProductIntent, Provenance
from testtrout.domain.observation import ProbeResult
from testtrout.domain.surface import Criticality, ScanResult
from testtrout.llm.gateway import Gateway, load_prompt

# Intent capture is an interactive command, and a developer will not wait
# minutes for a first draft they are about to correct anyway. Providers that do
# not understand the parameter ignore it.
DEFAULT_EFFORT = "low"

# Kept flat and fully-required: strict structured-output modes reject optional
# properties, and a shallow schema is markedly more reliable across providers.
INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "audience", "journeys", "never_break", "open_questions"],
    "properties": {
        "summary": {"type": "string", "description": "What this product is, in 1-2 sentences."},
        "audience": {"type": "string", "description": "Who uses it."},
        "journeys": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "description",
                    "steps",
                    "criticality",
                    "roles",
                    "surface_ids",
                    "consequence",
                ],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "criticality": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "roles": {"type": "array", "items": {"type": "string"}},
                    "surface_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only ids from the provided surface list.",
                    },
                    "consequence": {
                        "type": "string",
                        "description": "What it costs the business if this silently breaks.",
                    },
                },
            },
        },
        "never_break": {"type": "array", "items": {"type": "string"}},
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "context", "surface_id"],
                "properties": {
                    "question": {"type": "string"},
                    "context": {"type": "string"},
                    "surface_id": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def draft(
    gateway: Gateway,
    scan: ScanResult,
    probe: ProbeResult | None = None,
) -> tuple[ProductIntent, list[str]]:
    """Draft product intent from the surface map alone.

    Starting from a draft rather than a blank prompt is a deliberate choice.
    "What does your product do and what must never break?" is a genuinely hard
    question to answer cold; "here is what I think this app does — correct me"
    is easy, and it grounds the conversation in evidence the tool can actually
    verify.

    Returns:
        The drafted intent, marked ``inferred`` throughout, and any warnings
        raised while validating it.
    """
    response = gateway.complete(
        system=load_prompt("draft_intent"),
        user=_context_block(scan, probe),
        schema=INTENT_SCHEMA,
        effort=DEFAULT_EFFORT,
    )
    return _build(response.json(), scan, Provenance.INFERRED)


def structure(
    gateway: Gateway,
    description: str,
    scan: ScanResult,
    probe: ProbeResult | None = None,
) -> tuple[ProductIntent, list[str]]:
    """Turn a developer's own description into structured intent.

    Everything produced here is marked ``stated``: it came from the person who
    knows, so it outranks anything the tool derived on its own.
    """
    response = gateway.complete(
        system=load_prompt("structure_intent"),
        user=f"{_context_block(scan, probe)}\n\n## What the developer said\n\n{description}",
        schema=INTENT_SCHEMA,
        effort=DEFAULT_EFFORT,
    )
    return _build(response.json(), scan, Provenance.STATED)


def _context_block(scan: ScanResult, probe: ProbeResult | None) -> str:
    """Render the evidence the model is allowed to reason from.

    Deliberately compact. Sending the whole scan buries the signal, and the
    model only needs enough to recognise the shape of the product and to
    reference surfaces by id.
    """
    lines: list[str] = [f"## Project\n\n{scan.project.framework}"]
    if scan.project.backend:
        lines[0] += f" + {scan.project.backend}"

    if scan.screens:
        lines.append("## Screens\n")
        lines += [
            f"- `{s.id}` — {s.path} ({len(s.reaches)} data operations reachable)"
            for s in scan.screens
        ]

    if scan.data_operations:
        lines.append("\n## Data operations\n")
        lines += [
            f"- `{o.id}` — {o.operation.value} {o.table or o.function or ''}".rstrip()
            for o in scan.data_operations
        ]

    if scan.tables:
        lines.append("\n## Tables\n")
        lines += [
            f"- {t.name} ({', '.join(c.name for c in t.columns[:8])})"
            + ("" if t.rls_enabled else "  [no row-level security]")
            for t in scan.tables
        ]

    if scan.policies:
        lines.append("\n## Row-level security policies\n")
        lines += [
            f"- `{p.id}` — {p.command} on {p.table}: {p.using or p.name}" for p in scan.policies
        ]

    if probe is not None:
        reachable = [s.path for s in probe.screens if s.reachable]
        lines.append(
            f"\n## Observed against {probe.base_url}\n\n"
            f"Reachable screens: {', '.join(reachable) or 'none'}"
        )
        denials = [d.message for d in probe.divergences if d.code == "policy_denial"]
        if denials:
            lines.append("Policy denials observed:\n" + "\n".join(f"- {d}" for d in denials))

    return "\n".join(lines)


def _build(
    payload: Any, scan: ScanResult, provenance: Provenance
) -> tuple[ProductIntent, list[str]]:
    """Validate a model response into a :class:`ProductIntent`."""
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return ProductIntent(), ["the model returned something that was not an object"]

    known_ids = {s.id for s in scan.all_surfaces()}
    journeys: list[Journey] = []

    for index, raw in enumerate(payload.get("journeys") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or f"journey {index + 1}")
        claimed = [str(sid) for sid in raw.get("surface_ids") or []]
        valid = [sid for sid in claimed if sid in known_ids]
        if len(valid) != len(claimed):
            invented = sorted(set(claimed) - set(valid))
            warnings.append(
                f"journey {name!r} referenced {len(invented)} surface id(s) that do not exist "
                f"({', '.join(invented[:3])}) — dropped"
            )
        journeys.append(
            Journey(
                id=f"journey:{_slug(name)}",
                name=name,
                description=str(raw.get("description") or ""),
                steps=[str(s) for s in raw.get("steps") or []],
                criticality=_criticality(raw.get("criticality")),
                roles=[str(r) for r in raw.get("roles") or []],
                surfaces=valid,
                provenance=provenance,
                consequence=str(raw.get("consequence") or ""),
            )
        )

    questions: list[OpenQuestion] = []
    for index, raw in enumerate(payload.get("open_questions") or []):
        if not isinstance(raw, dict) or not raw.get("question"):
            continue
        surface_id = raw.get("surface_id")
        questions.append(
            OpenQuestion(
                id=f"q{index + 1}",
                question=str(raw["question"]),
                context=str(raw.get("context") or ""),
                surface_id=str(surface_id) if surface_id in known_ids else None,
            )
        )

    intent = ProductIntent(
        summary=str(payload.get("summary") or ""),
        audience=str(payload.get("audience") or ""),
        journeys=journeys,
        never_break=[str(n) for n in payload.get("never_break") or []],
        open_questions=questions,
    )
    if not journeys:
        warnings.append("the model produced no journeys — the surface map may be too sparse")
    return intent, warnings


def _criticality(value: Any) -> Criticality:
    """Coerce a model-supplied level, defaulting to HIGH rather than guessing low."""
    try:
        return Criticality(str(value).lower())
    except ValueError:
        return Criticality.HIGH


def _slug(value: str) -> str:
    """Normalise a name into a stable journey id."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "journey"
