"""What is possible right now, given whatever is configured.

The point is graceful degradation. A partial set of credentials should produce
a partial suite, not a refusal — given only a URL you can still test public
pages; add an anon key and API tests work; add a second account and
authorization tests become possible.

Every blocked capability names exactly one concrete missing thing, so the next
step is never "configure it properly".

Deterministic, and it reads names rather than values: whether a variable is
*set* is all that matters, and its contents are none of this module's business.
"""

from __future__ import annotations

import os

from testtrout.domain.config import Config, SecretResolutionError, resolve_secret
from testtrout.domain.requirements import Capability, Plan, Readiness
from testtrout.domain.surface import ScanResult


def _has(reference: str | None) -> bool:
    """Whether a config field resolves to a usable value."""
    if not reference:
        return False
    try:
        return bool(resolve_secret(reference))
    except SecretResolutionError:
        return False


def _model_key_available(config: Config) -> bool:
    """Whether any credential the configured provider accepts is present."""
    if _has(config.model.api_key):
        return True
    fallbacks = {
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        "openai": ("OPENAI_API_KEY",),
        "kimi": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    }[config.model.provider.value]
    return any(os.environ.get(name) for name in fallbacks)


def _playwright_installed() -> bool:
    """Whether the browser automation extra is importable."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def assess(config: Config, scan: ScanResult | None = None) -> Plan:
    """Work out what can be done with the current configuration.

    Args:
        config: The repository's configuration.
        scan: The last scan, which supplies discovered requirements and tells
            us whether the app has an auth wall worth signing in through.
    """
    entrypoint = config.entrypoint()
    users = config.test_users
    supabase_ready = _has(config.supabase.url) and _has(config.supabase.anon_key)
    needs_auth = bool(scan and scan.policies) or bool(scan and scan.project.auth)

    readiness: list[Readiness] = []

    # ---------------------------------------------------------------- probe
    missing = []
    if entrypoint is None:
        missing.append("a deployment URL — add one under Deployments")
    if not _playwright_installed():
        missing.append(
            "browser support — pip install 'testtrout[probe]' && playwright install chromium"
        )
    readiness.append(
        Readiness(
            capability=Capability.PROBE,
            ready=not missing,
            missing=missing,
            detail="Loads your app in a browser and records what it actually does.",
        )
    )

    # ------------------------------------------------------------ api tests
    missing = []
    if entrypoint is None and not supabase_ready:
        missing.append("a deployment URL, or a Supabase URL and anon key")
    readiness.append(
        Readiness(
            capability=Capability.API_TESTS,
            ready=not missing,
            missing=missing,
            detail="Calls endpoints and the database directly. Fast and stable; no browser.",
        )
    )

    # -------------------------------------------------------- browser tests
    missing = []
    if entrypoint is None:
        missing.append("a deployment URL — add one under Deployments")
    if not _playwright_installed():
        missing.append(
            "browser support — pip install 'testtrout[probe]' && playwright install chromium"
        )
    if needs_auth and not users:
        missing.append("at least one test account — this app is behind a login")
    if needs_auth and users and not supabase_ready:
        missing.append("a Supabase URL and anon key, to sign the test account in")
    readiness.append(
        Readiness(
            capability=Capability.BROWSER_TESTS,
            ready=not missing,
            missing=missing,
            detail=(
                "Drives the interface. Public pages work with just a URL; anything behind a "
                "login needs an account."
            ),
        )
    )

    # ------------------------------------------------ authorization tests
    missing = []
    if not supabase_ready:
        missing.append("a Supabase URL and anon key")
    if len(users) < 2:
        missing.append(
            f"a second test account — you have {len(users)}; proving user A cannot see "
            "user B's data needs a B"
        )
    readiness.append(
        Readiness(
            capability=Capability.AUTHORIZATION_TESTS,
            ready=not missing,
            missing=missing,
            detail=(
                f"Proves your {len(scan.policies)} row-level security policy(ies) actually "
                "hold. The cheapest high-value tests available."
                if scan
                else "Proves your row-level security policies hold. Scan first to see how many."
            ),
        )
    )

    # ------------------------------------------------------ model features
    missing = (
        []
        if _model_key_available(config)
        else [f"an API key for {config.model.provider.value} — set it under Model provider"]
    )
    readiness.append(
        Readiness(
            capability=Capability.MODEL_FEATURES,
            ready=not missing,
            missing=missing,
            detail=(
                "Intent capture and better scenario wording. Everything else — scanning, "
                "ranking, generating, running — works without it."
            ),
        )
    )

    return Plan(requirements=list(scan.requirements) if scan else [], readiness=readiness)


def kinds_possible(plan: Plan) -> set[str]:
    """Which test kinds can actually be produced and run.

    Used to filter proposals: drafting a scenario that cannot run yet wastes a
    model call and leaves something unapprovable in the list.
    """
    possible: set[str] = set()
    if plan.can(Capability.BROWSER_TESTS):
        possible.add("browser_journey")
    if plan.can(Capability.API_TESTS):
        possible.update({"endpoint", "data_operation"})
    if plan.can(Capability.AUTHORIZATION_TESTS):
        possible.add("authorization")
    return possible
