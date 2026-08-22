"""Discovering what credentials a deployment needs, from its own source.

The scan already reads every line of the application. The environment
variables it reaches for are written down right there, so asking a developer
which keys their app needs is asking them to re-derive something the code
states plainly.

Names are classified by shape rather than by a fixed list, because every
project spells them differently: ``VITE_SUPABASE_URL``,
``NEXT_PUBLIC_SUPABASE_URL``, and ``REACT_APP_SUPABASE_URL`` are the same
requirement wearing three framework prefixes.

Nothing here reads a value. Only names, locations, and what they appear to be
for.
"""

from __future__ import annotations

import re

from testtrout.analysis.parser import SourceFile
from testtrout.domain.location import SourceLocation
from testtrout.domain.requirements import Requirement, RequirementKind
from testtrout.domain.surface import ScanResult

# `import.meta.env.X` (Vite) and `process.env.X` (Next, Node).
_ENV_ACCESS = re.compile(
    r"(?:import\s*\.\s*meta\s*\.\s*env|process\s*\.\s*env)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)"
)
# `import.meta.env['X']` and `process.env["X"]`.
_ENV_INDEX = re.compile(
    r"(?:import\s*\.\s*meta\s*\.\s*env|process\s*\.\s*env)\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
)

# Framework prefixes that only mark a variable as client-visible. Stripping
# them is what lets one rule match every spelling of the same requirement.
_PUBLIC_PREFIXES = (
    "VITE_",
    "NEXT_PUBLIC_",
    "REACT_APP_",
    "PUBLIC_",
    "NUXT_PUBLIC_",
    "EXPO_PUBLIC_",
)

# Ordered: the first match wins, so `SUPABASE_SERVICE_ROLE_KEY` is classified
# as a service key before the looser anon-key rule can claim it.
_RULES: tuple[tuple[re.Pattern[str], RequirementKind, str], ...] = (
    (
        re.compile(r"SUPABASE.*SERVICE.*KEY|SERVICE_ROLE"),
        RequirementKind.SUPABASE_SERVICE_KEY,
        "Seeds and resets test data. Never reaches a browser context.",
    ),
    (
        re.compile(r"SUPABASE.*(ANON|PUBLISHABLE).*KEY|SUPABASE_KEY$"),
        RequirementKind.SUPABASE_ANON_KEY,
        "Your deployment already has this. TestTrout does not need it — tests go "
        "through your app's interface and endpoints, not its database.",
    ),
    (
        re.compile(r"SUPABASE.*URL"),
        RequirementKind.SUPABASE_URL,
        "Your deployment already has this. Only needed here if you want TestTrout to "
        "reset the database between runs.",
    ),
    (
        re.compile(
            r"(STRIPE|RESEND|SENDGRID|TWILIO|OPENAI|ANTHROPIC|POSTHOG|SEGMENT|MAPBOX|ALGOLIA)"
        ),
        RequirementKind.THIRD_PARTY_KEY,
        "A third-party service. Intercepted during test runs, so a real key is "
        "rarely needed — but the app may refuse to start without one.",
    ),
)


def _classify(name: str) -> tuple[RequirementKind, str]:
    """Work out what a variable is for, from its name."""
    bare = name
    for prefix in _PUBLIC_PREFIXES:
        bare = bare.removeprefix(prefix)
    upper = bare.upper()

    for pattern, kind, purpose in _RULES:
        if pattern.search(upper):
            return kind, purpose
    return (
        RequirementKind.OTHER,
        "Referenced by the application. It may need a value for the app to run.",
    )


def from_source(files: dict[str, SourceFile]) -> list[Requirement]:
    """Every environment variable the application reads."""
    found: dict[str, Requirement] = {}

    for rel, file in sorted(files.items()):
        text = file.source.decode("utf-8", errors="replace")
        for pattern in (_ENV_ACCESS, _ENV_INDEX):
            for match in pattern.finditer(text):
                name = match.group(1)
                line = text.count("\n", 0, match.start()) + 1
                location = SourceLocation(file=rel, line=line)

                existing = found.get(name)
                if existing is not None:
                    if len(existing.locations) < 5:
                        existing.locations.append(location)
                    continue
                kind, purpose = _classify(name)
                found[name] = Requirement(
                    id=f"env:{name}",
                    name=name,
                    kind=kind,
                    purpose=purpose,
                    locations=[location],
                    detected_from="read by the application",
                    # A third-party key is usually only needed for the app to
                    # boot; the calls themselves get intercepted anyway.
                    # Anything the *deployment* already holds is optional here:
                    # tests reach the app over HTTP and through a browser, so
                    # TestTrout never needs the app's own service credentials.
                    optional=kind
                    in {
                        RequirementKind.THIRD_PARTY_KEY,
                        RequirementKind.SUPABASE_ANON_KEY,
                        RequirementKind.SUPABASE_URL,
                        RequirementKind.SUPABASE_SERVICE_KEY,
                    },
                )
        # Files are read once; the regex covers both patterns above.
        del text

    return sorted(found.values(), key=lambda r: (r.kind.value, r.name))


def implied(scan: ScanResult) -> list[Requirement]:
    """Requirements the tool needs that the application never mentions.

    A deployment URL and test accounts are TestTrout's own needs, not the
    app's, so they never appear in its source — but they are exactly what a
    developer has to supply, and leaving them out of the list would make it
    misleading.
    """
    out = [
        Requirement(
            id="deployment_url",
            name="deployment URL",
            kind=RequirementKind.DEPLOYMENT_URL,
            purpose="Where your app is running — a preview URL, production, or localhost.",
            detected_from="required by TestTrout",
        )
    ]

    if scan.policies or any(o.table for o in scan.data_operations):
        out.append(
            Requirement(
                id="test_users",
                name="two test accounts",
                kind=RequirementKind.TEST_USER,
                purpose=(
                    "Signing in as two accounts is how authorization is tested — what one "
                    f"can see, the other must not. This app has {len(scan.policies)} "
                    "row-level security policy(ies) riding on that."
                ),
                detected_from="row-level security policies found in migrations",
            )
        )

    out.append(
        Requirement(
            id="model_key",
            name="model provider API key",
            kind=RequirementKind.MODEL_KEY,
            purpose="Only for intent capture and scenario wording. Scanning and running never use one.",
            detected_from="required by TestTrout",
            optional=True,
        )
    )
    return out


def discover(scan: ScanResult, files: dict[str, SourceFile]) -> list[Requirement]:
    """Everything needed to test this deployment, discovered and implied."""
    return implied(scan) + from_source(files)
