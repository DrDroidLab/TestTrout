"""Reading and writing repository configuration.

Shared by the CLI, the web interface, and the MCP server so that parity is
structural rather than something three call sites have to remember.

Two rules the whole module exists to enforce:

**Secret values never enter ``.trout/config.yaml``.** That file is committed.
Configuration stores ``env:NAME`` references; the values go to the repository's
gitignored ``.env``. The UI can therefore offer a password field and still be
safe, which is the only way "configure everything from the app" is a
responsible feature.

**Making a deployment writable is never incidental.** It is the one setting
that can destroy real data, so it requires an explicit acknowledgement rather
than arriving as a side effect of saving a form.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testtrout.domain.config import (
    Config,
    Entrypoint,
    EntrypointKind,
    ExternalRule,
    IsolationStrategy,
    ModelProvider,
    Permission,
    TestUser,
)
from testtrout.domain.observation import ProbeResult
from testtrout.domain.requirements import Plan
from testtrout.domain.surface import ScanResult
from testtrout.store import QaPaths, read_model, write_model

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# What each provider's own SDK looks for. Using these means a key already
# exported for another tool works without being re-entered.
DEFAULT_KEY_VAR: dict[ModelProvider, str] = {
    ModelProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
    ModelProvider.OPENAI: "OPENAI_API_KEY",
    ModelProvider.KIMI: "MOONSHOT_API_KEY",
}


class SettingsError(ValueError):
    """A configuration change was rejected, with a message meant for a person."""


@dataclass
class ConfigView:
    """Configuration plus everything needed to render and reason about it."""

    config: Config
    plan: Plan
    env_present: dict[str, bool] = field(default_factory=dict)
    """Which referenced environment variables have a value. Names only —
    values are never read out of here."""

    def as_dict(self) -> dict[str, Any]:
        """Serialise for an API or a JSON CLI response."""
        return {
            "config": self.config.model_dump(mode="json"),
            "env_present": self.env_present,
            "requirements": [r.model_dump(mode="json") for r in self.plan.requirements],
            "readiness": [
                r.model_dump(mode="json") | {"next_step": r.next_step} for r in self.plan.readiness
            ],
            "capabilities": [c.value for c in self.plan.available],
        }


def _load(paths: QaPaths) -> Config:
    return read_model(paths.config, Config) if paths.config.is_file() else Config()


def _relevant_names(config: Config, scan: ScanResult | None) -> list[str]:
    """Every variable worth showing a set/unset status for.

    Wider than what configuration currently references, on purpose. A user who
    saves a key before wiring up the reference should still see it as set —
    and the variables the *application* reads matter even when TestTrout has no
    opinion about them yet.
    """
    referenced = [
        config.supabase.anon_key,
        config.supabase.service_role_key,
        config.supabase.url,
        config.model.api_key,
        config.model.base_url,
        *[u.email for u in config.test_users],
        *[u.password for u in config.test_users],
    ]
    names = {
        value.removeprefix("env:") for value in referenced if value and value.startswith("env:")
    }

    # The provider key, whether or not it has been wired up yet.
    names.add(DEFAULT_KEY_VAR[config.model.provider])
    names.update({"SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"})

    # Per-role account variables, so the Setup panel can show their status.
    for user in config.test_users:
        upper = user.role.upper().replace("-", "_")
        names.update({f"TROUT_{upper}_EMAIL", f"TROUT_{upper}_PASSWORD"})

    # Anything the application itself reads.
    if scan is not None:
        names.update(r.name for r in scan.requirements if r.name.isupper())

    return sorted(names)


def probe_of(paths: QaPaths, config: Config) -> ProbeResult | None:
    """The most recent probe of the default deployment, if there is one.

    Readiness reads better with it: "12 endpoints refused an unauthenticated
    request" is a reason to add an account, where "this app is behind a login"
    is a guess restated.
    """
    entrypoint = config.entrypoint()
    if entrypoint is None:
        return None
    path = paths.observed / f"{entrypoint.name}.yaml"
    return read_model(path, ProbeResult) if path.is_file() else None


def view(paths: QaPaths) -> ConfigView:
    """Current configuration, what it needs, and what it can already do."""
    from testtrout.planning.readiness import assess
    from testtrout.store import load_dotenv

    load_dotenv(paths.root)
    config = _load(paths)
    scan = read_model(paths.surfaces, ScanResult) if paths.surfaces.is_file() else None
    return ConfigView(
        config=config,
        plan=assess(config, scan, probe_of(paths, config)),
        env_present={name: bool(os.environ.get(name)) for name in _relevant_names(config, scan)},
    )


def apply(paths: QaPaths, patch: dict[str, Any]) -> Config:
    """Apply a partial configuration change and save it.

    Only the sections present in ``patch`` are touched, so a form that edits
    deployments cannot silently reset the model provider.
    """
    config = _load(paths)

    if "entrypoints" in patch:
        config.entrypoints = [_entrypoint(item) for item in patch["entrypoints"] or []]
    if "test_users" in patch:
        config.test_users = [_test_user(item) for item in patch["test_users"] or []]
    if "login" in patch:
        _apply_login(config, patch["login"] or {})
    if "supabase" in patch:
        _apply_supabase(config, patch["supabase"] or {})
    if "model" in patch:
        _apply_model(config, patch["model"] or {})
    if "substitution" in patch:
        config.substitution.external = [
            ExternalRule(name=str(item.get("name", "")), match=str(item.get("match", "")))
            for item in patch["substitution"] or []
            if item.get("match")
        ]

    paths.ensure()
    write_model(paths.config, config)
    return config


def _entrypoint(item: dict[str, Any]) -> Entrypoint:
    """Build one deployment, refusing to make it writable by accident."""
    url = str(item.get("url", "")).strip()
    if not url:
        raise SettingsError("a deployment needs a URL")

    disposable = bool(item.get("disposable"))
    if disposable and not item.get("confirm_disposable"):
        raise SettingsError(
            "Marking a deployment disposable allows tests to write to it and delete its "
            "data. Confirm that explicitly before saving."
        )

    try:
        return Entrypoint(
            name=str(item.get("name") or "default").strip(),
            kind=EntrypointKind(item.get("kind", "web")),
            url=url,
            api_url=str(item.get("api_url", "")).strip(),
            disposable=disposable,
            allow=[Permission.READ, Permission.WRITE] if disposable else [Permission.READ],
            headers={str(k): str(v) for k, v in (item.get("headers") or {}).items()},
        )
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc


def _test_user(item: dict[str, Any]) -> TestUser:
    """Build one test account. Only environment variable *names* are stored."""
    role = str(item.get("role", "")).strip()
    if not role:
        raise SettingsError("a test account needs a role name")
    upper = role.upper().replace("-", "_")
    return TestUser(
        role=role,
        email=_reference(item.get("email_var") or f"TROUT_{upper}_EMAIL"),
        password=_reference(item.get("password_var") or f"TROUT_{upper}_PASSWORD"),
    )


def _apply_login(config: Config, patch: dict[str, Any]) -> None:
    """Update the sign-in form settings.

    Selectors are usually written by a probe, but a human correcting a bad
    guess is a first-class path — so they are editable here too.
    """
    for name in ("path", "email_selector", "password_selector", "submit_selector"):
        if name in patch:
            setattr(config.login, name, str(patch[name] or "") or None)
    if config.login.path is None:
        config.login.path = "/login"


def _apply_supabase(config: Config, patch: dict[str, Any]) -> None:
    if "url" in patch:
        config.supabase.url = str(patch["url"] or "") or None
    if "anon_key_var" in patch:
        config.supabase.anon_key = (
            _reference(patch["anon_key_var"]) if patch["anon_key_var"] else None
        )
    if "service_key_var" in patch:
        config.supabase.service_role_key = (
            _reference(patch["service_key_var"]) if patch["service_key_var"] else None
        )
    if patch.get("isolation"):
        try:
            config.supabase.isolation = IsolationStrategy(str(patch["isolation"]))
        except ValueError as exc:
            raise SettingsError(f"unknown isolation strategy {patch['isolation']!r}") from exc


def _apply_model(config: Config, patch: dict[str, Any]) -> None:
    if patch.get("provider"):
        try:
            config.model.provider = ModelProvider(str(patch["provider"]))
        except ValueError as exc:
            raise SettingsError(f"unknown provider {patch['provider']!r}") from exc
    if "model" in patch:
        config.model.model = str(patch["model"] or "") or None
    if "base_url" in patch:
        config.model.base_url = str(patch["base_url"] or "") or None
    if "api_key_var" in patch:
        # Default to the variable the provider's own SDK already looks for, so a
        # key the developer exported for other tools is picked up automatically.
        name = patch["api_key_var"] or DEFAULT_KEY_VAR[config.model.provider]
        config.model.api_key = _reference(name)


def _reference(name: Any) -> str:
    """Normalise into an ``env:NAME`` reference, rejecting anything else.

    This is the guard that makes a password field in the UI safe: a caller that
    tries to store a literal value here gets an error, not a committed secret.
    """
    text = str(name).strip()
    bare = text.removeprefix("env:")
    if not ENV_NAME.match(bare):
        raise SettingsError(
            f"{text!r} is not an environment variable name. Configuration stores names; "
            "values go in .env."
        )
    return f"env:{bare}"


def set_secrets(paths: QaPaths, values: dict[str, str]) -> list[str]:
    """Write secret values into the repository's gitignored ``.env``.

    Existing entries are replaced in place and unrelated lines are preserved,
    so a developer's own variables survive a save from the interface.

    Returns the names written. Never the values.
    """
    written: list[str] = []
    for name in values:
        if not ENV_NAME.match(name):
            raise SettingsError(f"{name!r} is not a valid environment variable name")

    path = paths.root / ".env"
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)
    out: list[str] = []

    for line in existing:
        stripped = line.strip().removeprefix("export ").strip()
        name = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if name in remaining:
            value = remaining.pop(name)
            if value:
                out.append(f"{name}={value}")
                written.append(name)
            # An empty value means "clear it": drop the line entirely.
        else:
            out.append(line)

    for name, value in remaining.items():
        if value:
            out.append(f"{name}={value}")
            written.append(name)

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out).rstrip("\n") + "\n")

    _ensure_gitignored(paths.root)
    for name in written:
        os.environ[name] = values[name]
    return sorted(written)


def _ensure_gitignored(root: Path) -> None:
    """Make sure ``.env`` cannot be committed.

    Writing secrets into a repository without this would be actively harmful,
    so it is done here rather than left as advice in the documentation.
    """
    path = root / ".gitignore"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if any(line.strip() in {".env", "/.env", "*.env"} for line in lines):
        return
    lines.append("")
    lines.append("# Written by TestTrout. Never commit credentials.")
    lines.append(".env")
    path.write_text("\n".join(lines).lstrip("\n") + "\n", encoding="utf-8")
