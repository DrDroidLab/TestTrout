"""Work out what the tool still needs from a person.

Every fact here is something no amount of reading code or poking the
deployment can supply: a URL only the deployer knows, an account only they can
create, a real id that exists only in their database.

The bar for adding one: a competent stranger with the repository open in front
of them still could not answer it. If they could, it is the tool's job.
"""

from __future__ import annotations

import re

from testtrout.domain.candidate import Candidate
from testtrout.domain.config import Config
from testtrout.domain.fact import Fact, FactKind, FactSheet
from testtrout.domain.observation import ProbeResult
from testtrout.domain.surface import ScanResult

# A route parameter in any of the notations the adapters produce.
_PARAM = re.compile(r"[:{]([A-Za-z_][A-Za-z0-9_]*)\}?")


def build(
    scan: ScanResult,
    config: Config,
    probe: ProbeResult | None = None,
    candidates: list[Candidate] | None = None,
) -> FactSheet:
    """Everything worth asking for, with what each one turns on.

    Args:
        scan: The static surface map.
        config: What is already configured — an answered fact is still listed,
            marked known, so the form shows its own history.
        probe: What the deployment did, which is what turns "you might need an
            account" into "eleven endpoints refused one".
        candidates: Used only to count what each fact is blocking. A number
            beats an adjective for deciding what to fill in first.

    Returns:
        The sheet, ordered by the caller.
    """
    entrypoint = config.entrypoint()
    blocking = _blocking_counts(candidates or [])
    facts: list[Fact] = [
        Fact(
            id="deployment_url",
            kind=FactKind.URL,
            label="Where is this deployed?",
            why="Everything needs it. Nothing can be tested against code alone.",
            placeholder="https://your-app.vercel.app",
            known=entrypoint is not None and bool(entrypoint.url),
            blocks=blocking.get("deployment_url", 0),
        )
    ]

    if scan.endpoints:
        facts.append(_api_url(scan, config, probe, blocking))

    facts.extend(_accounts(scan, config, probe, blocking))
    facts.extend(_samples(scan, probe, blocking))
    facts.extend(_toolchain(scan))
    return FactSheet(facts=facts)


def _api_url(
    scan: ScanResult, config: Config, probe: ProbeResult | None, blocking: dict[str, int]
) -> Fact:
    """Where the HTTP API lives, when that is not where the pages live.

    Common on Vercel: the frontend deploys there and the backend elsewhere, so
    every endpoint path is meaningless against the page URL. The evidence is
    either the deployment's own 404s or the variable the client reads.
    """
    entrypoint = config.entrypoint()
    missing = [e for e in (probe.endpoints if probe else []) if e.status == 404]
    answered = [e for e in (probe.endpoints if probe else []) if e.status is not None]

    if answered and len(missing) * 2 > len(answered):
        evidence = f"{len(missing)} of {len(answered)} endpoints returned 404 against the page URL"
    elif scan.project.api_base_var:
        evidence = f"your code reads it from {scan.project.api_base_var}"
    else:
        evidence = "only needed if your backend is deployed separately"

    return Fact(
        id="api_url",
        kind=FactKind.URL,
        label="Where is the API served from?",
        why=f"Turns on {len(scan.endpoints)} API test(s). Leave blank if it is the same host.",
        placeholder="https://api.your-app.com",
        evidence=evidence,
        known=entrypoint is not None and bool(entrypoint.api_url),
        blocks=blocking.get("api_url", 0),
    )


def _accounts(
    scan: ScanResult, config: Config, probe: ProbeResult | None, blocking: dict[str, int]
) -> list[Fact]:
    """Accounts to sign in as.

    One is asked for when something is actually behind a sign-in — either the
    deployment refused an unauthenticated request, or the code has an auth
    layer. A second is asked for only to prove one user cannot see another's
    data, which is worth saying out loud rather than demanding two accounts up
    front for no stated reason.
    """
    gated = probe.endpoints_needing_account if probe else []
    behind_login = [s for s in (probe.screens if probe else []) if not s.reachable]
    needs_auth = bool(gated) or bool(behind_login) or bool(scan.project.auth) or bool(scan.policies)
    if not needs_auth:
        return []

    if gated:
        evidence = f"{len(gated)} endpoint(s) refused a request with no credentials"
    elif behind_login:
        evidence = f"{len(behind_login)} page(s) did not load signed out"
    else:
        evidence = f"your code uses {scan.project.auth or 'an auth layer'}"

    existing = [u.role for u in config.test_users]
    facts = [
        Fact(
            id="account_primary",
            kind=FactKind.ACCOUNT,
            label="An account to sign in as",
            why="Turns on every page and endpoint behind the sign-in.",
            placeholder="email and password of a test user",
            evidence=evidence,
            env_var="TROUT_OWNER_EMAIL / TROUT_OWNER_PASSWORD",
            known=len(existing) >= 1,
            blocks=blocking.get("account_primary", 0),
        )
    ]
    facts.append(
        Fact(
            id="account_second",
            kind=FactKind.ACCOUNT,
            label="A second account, belonging to someone else",
            why="Only needed to check that one user cannot see another's data.",
            placeholder="a different user's email and password",
            env_var="TROUT_MEMBER_EMAIL / TROUT_MEMBER_PASSWORD",
            known=len(existing) >= 2,
            blocks=blocking.get("account_second", 0),
        )
    )
    return facts


def _samples(scan: ScanResult, probe: ProbeResult | None, blocking: dict[str, int]) -> list[Fact]:
    """Real values for the parameters in a URL.

    ``/jobs/:id`` cannot be loaded without an id that exists. The probe fills
    these in by itself when a list page hands it one; this asks only for the
    ones it never saw.
    """
    resolved = {
        name
        for screen in (probe.screens if probe else [])
        if screen.reachable
        for name in _PARAM.findall(screen.path)
    }
    wanted: dict[str, str] = {}
    for screen in scan.screens:
        for name in _PARAM.findall(screen.path):
            if name not in resolved:
                wanted.setdefault(name, screen.path)

    return [
        Fact(
            id=f"sample_{name}",
            kind=FactKind.SAMPLE,
            label=f"A real {name} value",
            why=f"Turns on {sum(1 for s in scan.screens if f':{name}' in s.path)} page(s).",
            placeholder=f"any {name} that exists, e.g. from your own database",
            evidence=f"{route} could not be loaded without one",
            blocks=blocking.get(f"sample_{name}", 0),
        )
        for name, route in sorted(wanted.items())
    ]


def _toolchain(scan: ScanResult) -> list[Fact]:
    """The test runners, which are a command rather than a value.

    Listed as a fact because it belongs in the same place as everything else
    standing between the user and a suite — not because it is secret.
    """
    from pathlib import Path

    from testtrout.runtime.toolchain import detect

    root = Path(scan.project.root)
    if not root.is_dir():
        return []
    chain = detect(root)
    if chain.has_playwright and chain.has_vitest:
        return []

    missing = [
        name
        for name, present in (
            ("@playwright/test", chain.has_playwright),
            ("vitest", chain.has_vitest),
        )
        if not present
    ]
    install = {"pnpm": "pnpm add -D", "yarn": "yarn add -D", "bun": "bun add -d"}.get(
        chain.package_manager, "npm install -D"
    )
    extra = " && npx playwright install chromium" if "@playwright/test" in missing else ""
    return [
        Fact(
            id="toolchain",
            kind=FactKind.COMMAND,
            label="Install the test runners",
            why="Tests can be written without them, but not run — so none can be proven.",
            placeholder=f"{install} {' '.join(missing)}{extra}",
            evidence=f"not in {root.name}/package.json",
        )
    ]


def _blocking_counts(candidates: list[Candidate]) -> dict[str, int]:
    """How many candidates each fact is holding up."""
    counts: dict[str, int] = {}
    for candidate in candidates:
        for need in candidate.needs:
            counts[need] = counts.get(need, 0) + 1
    return counts


__all__ = ["build"]
