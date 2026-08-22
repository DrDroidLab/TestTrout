"""Apply the facts a person supplied.

One rule about where things land, and it is not negotiable: a secret goes to
the gitignored ``.env`` and only its *name* is recorded in committed
configuration. Everything else — a URL, a sample id — is committed, because the
next person to read the suite benefits from seeing what it was pointed at.

Partial input is the normal case. Someone who knows the deployment URL but has
not made a test account yet should be able to save the URL and get the pages
that do not need signing in. So every field is applied independently, and a
missing one is not an error.
"""

from __future__ import annotations

from typing import Any

from testtrout.app import settings
from testtrout.domain.config import Config, Entrypoint, EntrypointKind, Permission, TestUser
from testtrout.store import QaPaths, read_model, write_model


class FactError(ValueError):
    """A supplied value could not be used, with the reason."""


# Which role each account fact fills. Two is the useful maximum: one to sign in
# as, and one to prove the first cannot see the second's data.
_ROLES = {"account_primary": "owner", "account_second": "member"}


def apply(paths: QaPaths, payload: dict[str, Any]) -> list[str]:
    """Save whatever was supplied. Returns the ids that were applied.

    Args:
        paths: The project.
        payload: Fact id to value. An account's value is a mapping with
            ``email`` and ``password``; everything else is a string.

    Raises:
        FactError: only for a value that is present but unusable — a URL that
            is not a URL. A missing value is never an error.
    """
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    applied: list[str] = []
    secrets: dict[str, str] = {}

    deployment = str(payload.get("deployment_url", "")).strip()
    api = str(payload.get("api_url", "")).strip()
    if deployment or api:
        _set_deployment(config, deployment, api)
        applied.extend(k for k in ("deployment_url", "api_url") if payload.get(k))

    for fact_id, role in _ROLES.items():
        account = payload.get(fact_id)
        if not isinstance(account, dict):
            continue
        email = str(account.get("email", "")).strip()
        password = str(account.get("password", "")).strip()
        if not email or not password:
            continue
        prefix = f"TROUT_{role.upper()}"
        _set_user(config, role, prefix)
        secrets[f"{prefix}_EMAIL"] = email
        secrets[f"{prefix}_PASSWORD"] = password
        applied.append(fact_id)

    samples = {
        key.removeprefix("sample_"): str(value).strip()
        for key, value in payload.items()
        if key.startswith("sample_") and str(value).strip()
    }
    if samples:
        config.run.samples.update(samples)
        applied.extend(f"sample_{name}" for name in samples)

    header = str(payload.get("header", "")).strip()
    if header:
        _set_header(config, header)
        applied.append("header")

    paths.ensure()
    write_model(paths.config, config)
    if secrets:
        settings.set_secrets(paths, secrets)
    return applied


def _set_deployment(config: Config, url: str, api_url: str) -> None:
    """Point the default deployment at a URL, without touching its safety.

    A deployment stays read-only however it was created. Making it writable is
    a deliberate edit to a committed file, never a side effect of filling in a
    form.
    """
    existing = config.entrypoint()
    if existing is None:
        if not url:
            raise FactError("a deployment URL is needed before an API URL means anything")
        config.entrypoints.append(
            Entrypoint(
                name="deployment",
                kind=EntrypointKind.WEB,
                url=_valid(url),
                api_url=_valid(api_url) if api_url else "",
                allow=[Permission.READ],
            )
        )
        return
    if url:
        existing.url = _valid(url)
    if api_url:
        existing.api_url = _valid(api_url)


def _set_user(config: Config, role: str, prefix: str) -> None:
    """Record that a role exists, and where to find its credentials."""
    for user in config.test_users:
        if user.role == role:
            user.email = f"env:{prefix}_EMAIL"
            user.password = f"env:{prefix}_PASSWORD"
            return
    config.test_users.append(
        TestUser(
            role=role,
            email=f"env:{prefix}_EMAIL",
            password=f"env:{prefix}_PASSWORD",
        )
    )


def _set_header(config: Config, value: str) -> None:
    """Store a bypass header by reference, never by value."""
    entrypoint = config.entrypoint()
    if entrypoint is None:
        raise FactError("add a deployment URL before a header for it")
    name, _, _ = value.partition(":")
    entrypoint.headers[name.strip() or "x-vercel-protection-bypass"] = "env:TROUT_BYPASS_TOKEN"


def _valid(url: str) -> str:
    """Reject something that is plainly not a URL, before it reaches a probe."""
    if not url.startswith(("http://", "https://")):
        raise FactError(f"{url!r} does not look like a URL — it should start with https://")
    return url.rstrip("/")


__all__ = ["FactError", "apply"]
