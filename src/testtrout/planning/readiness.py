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
from pathlib import Path

from testtrout.domain.config import Config, SecretResolutionError, resolve_secret
from testtrout.domain.observation import ProbeResult
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


def _project_toolchain(scan: ScanResult | None):  # type: ignore[no-untyped-def]
    """What the project itself has installed, if we know where it is.

    Read from the scanned application root rather than the repository root: in
    a monorepo the two differ, and looking in the wrong one reports every
    runner as missing.
    """
    if scan is None or not scan.project.root:
        return None
    from testtrout.runtime.toolchain import detect

    root = Path(scan.project.root)
    return detect(root) if root.is_dir() else None


def _install_hint(chain, package: str) -> str:  # type: ignore[no-untyped-def]
    """The one command that fixes a missing runner, for the right package manager."""
    command = {"pnpm": "pnpm add -D", "yarn": "yarn add -D", "bun": "bun add -d"}.get(
        chain.package_manager, "npm install -D"
    )
    extra = " && npx playwright install chromium" if package == "@playwright/test" else ""
    return f"the {package} test runner — install it in your project: {command} {package}{extra}"


def assess(
    config: Config, scan: ScanResult | None = None, probe: ProbeResult | None = None
) -> Plan:
    """Work out what can be done with the current configuration.

    Args:
        config: The repository's configuration.
        scan: The last scan, which supplies discovered requirements and tells
            us whether the app has an auth wall worth signing in through.
        probe: What the deployment actually did. When present it is preferred
            over inference: an endpoint that answered 401 to an unauthenticated
            request is evidence that an account is needed, and it lets the ask
            name how many endpoints are behind the wall rather than guessing
            that there is one.
    """
    entrypoint = config.entrypoint()
    users = config.test_users
    # What the *project* can run, not what TestTrout can. A repository with no
    # test runner installed drafts tests perfectly well and then cannot execute
    # one of them, which is the least useful moment to find out.
    chain = _project_toolchain(scan)
    gated = probe.endpoints_needing_account if probe else []
    needs_auth = bool(gated) or bool(scan and scan.policies) or bool(scan and scan.project.auth)
    # Tests drive the app's own interface and endpoints, so direct database
    # credentials are never required. Supabase settings only enable resetting
    # the database between runs.
    login_ready = config.login.usable

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
    if entrypoint is None:
        missing.append("a deployment URL — add one under Deployments")
    if chain is not None and not chain.has_vitest:
        missing.append(_install_hint(chain, "vitest"))
    readiness.append(
        Readiness(
            capability=Capability.API_TESTS,
            ready=not missing,
            missing=missing,
            detail="Calls your app's HTTP endpoints directly. Fast and stable; no browser.",
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
    if chain is not None and not chain.has_playwright:
        missing.append(_install_hint(chain, "@playwright/test"))
    if needs_auth and not users:
        missing.append(
            f"a test account — {len(gated)} endpoint(s) refused an unauthenticated request, "
            "so testing them needs someone to sign in as"
            if gated
            else "a test account — this app is behind a login"
        )
    if needs_auth and users and not login_ready:
        missing.append(
            "the sign-in form has not been located yet — run a probe, which finds it once"
        )
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
    if entrypoint is None:
        missing.append("a deployment URL — add one under Deployments")
    if not _playwright_installed():
        missing.append(
            "browser support — pip install 'testtrout[probe]' && playwright install chromium"
        )
    if chain is not None and not chain.has_playwright:
        missing.append(_install_hint(chain, "@playwright/test"))
    if users and not login_ready:
        missing.append(
            "the sign-in form has not been located yet — run a probe, which finds it once"
        )
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
                "Signs in as two accounts and requires that what one can see, the other "
                "cannot. Runs through the interface, so no database credentials are needed."
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
