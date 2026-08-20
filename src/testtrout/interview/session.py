"""An interactive, resumable configuration interview.

Every prompt has a default and every answer is written straight to
``.trout/config.yaml``, so quitting halfway loses nothing. The same questions are
skipped entirely when the corresponding value arrived as a command-line flag,
which is what lets ``trout init`` serve both a person at a terminal and an agent
in a script from one implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

import typer
from rich.console import Console

from testtrout.domain.config import (
    Config,
    Entrypoint,
    EntrypointKind,
    ModelProvider,
    Permission,
    TestUser,
)

console = Console()


@dataclass
class Interview:
    """Fills in a :class:`Config` by asking only for what is missing."""

    config: Config
    interactive: bool = True

    def ask(self, question: str, default: str | None = None, *, secret_name: bool = False) -> str:
        """Ask one question, returning the default when non-interactive.

        Args:
            question: Prompt text.
            default: Value used when the user presses enter, or when running
                non-interactively.
            secret_name: Marks a question that expects an environment variable
                *name*. Purely for the reminder shown to the user; the rule is
                enforced by never offering to store a value.
        """
        if not self.interactive:
            return default or ""
        suffix = " [dim](env var name, not the value)[/dim]" if secret_name else ""
        answer = typer.prompt(
            _plain(f"{question}{suffix}"), default=default or "", show_default=bool(default)
        )
        return str(answer).strip()

    def confirm(self, question: str, default: bool = False) -> bool:
        """Ask a yes/no question."""
        if not self.interactive:
            return default
        return typer.confirm(_plain(question), default=default)

    def add_entrypoint(
        self,
        name: str | None = None,
        url: str | None = None,
        disposable: bool | None = None,
    ) -> Entrypoint:
        """Configure one deployment.

        ``disposable`` is asked as a question about the data, not about
        permissions — "can this data be destroyed" is something a developer can
        answer without thinking, where "should writes be allowed" invites a
        careless yes.
        """
        name = name or self.ask("Name for this deployment", "local")
        url = url or self.ask("URL", "http://localhost:5173")

        if disposable is None:
            disposable = self.confirm(
                f"Can the data behind {name} be destroyed freely? "
                "(yes for a local or throwaway database, no for anything shared)",
                default=False,
            )

        entrypoint = Entrypoint(
            name=name,
            kind=EntrypointKind.WEB,
            url=url,
            disposable=disposable,
            allow=[Permission.READ, Permission.WRITE] if disposable else [Permission.READ],
        )
        if not disposable:
            console.print(
                f"  [yellow]·[/yellow] {name} is read-only. Mutating requests will be blocked."
            )
        self.config.entrypoints = [
            e for e in self.config.entrypoints if e.name != entrypoint.name
        ] + [entrypoint]
        return entrypoint

    def add_supabase(self) -> None:
        """Configure the Supabase connection, by environment variable name."""
        supabase = self.config.supabase
        supabase.url = self.ask(
            "Supabase project URL", supabase.url or "https://YOUR-REF.supabase.co"
        )
        supabase.anon_key = _env_ref(
            self.ask(
                "Env var holding the anon key",
                _env_name(supabase.anon_key) or "SUPABASE_ANON_KEY",
                secret_name=True,
            )
        )
        if self.confirm(
            "Configure a service-role key? (needed only to seed and reset test data)",
            default=False,
        ):
            supabase.service_role_key = _env_ref(
                self.ask(
                    "Env var holding the service-role key",
                    _env_name(supabase.service_role_key) or "SUPABASE_SERVICE_ROLE_KEY",
                    secret_name=True,
                )
            )

    def add_test_user(self, role: str | None = None) -> TestUser:
        """Configure one seeded test account."""
        role = role or self.ask("Role name", "owner")
        upper = role.upper().replace("-", "_")
        user = TestUser(
            role=role,
            email=_env_ref(
                self.ask(
                    f"Env var holding the {role} email", f"TROUT_{upper}_EMAIL", secret_name=True
                )
            ),
            password=_env_ref(
                self.ask(
                    f"Env var holding the {role} password",
                    f"TROUT_{upper}_PASSWORD",
                    secret_name=True,
                )
            ),
        )
        self.config.test_users = [u for u in self.config.test_users if u.role != user.role] + [user]
        return user

    def set_model(self, provider: str | None = None, model: str | None = None) -> None:
        """Choose a model provider.

        Only scenario proposal, intent capture, and failure explanation use it,
        so leaving it unconfigured is a valid outcome rather than an error.
        """
        chosen = provider or self.ask(
            "Model provider (anthropic / openai / kimi)", self.config.model.provider.value
        )
        try:
            self.config.model.provider = ModelProvider(chosen.strip().lower())
        except ValueError:
            console.print(f"  [yellow]·[/yellow] unknown provider {chosen!r}, keeping the default")
            return

        if model:
            self.config.model.model = model

        default_key_var = {
            ModelProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
            ModelProvider.OPENAI: "OPENAI_API_KEY",
            ModelProvider.KIMI: "MOONSHOT_API_KEY",
        }[self.config.model.provider]
        self.config.model.api_key = _env_ref(
            self.ask(
                "Env var holding the API key",
                _env_name(self.config.model.api_key) or default_key_var,
                secret_name=True,
            )
        )


def _env_ref(name: str) -> str:
    """Normalise an answer into an ``env:NAME`` reference.

    Accepts a bare name or an already-prefixed reference, so a user who types
    what they saw in the docs gets the same result either way.
    """
    cleaned = name.strip()
    return cleaned if cleaned.startswith("env:") else f"env:{cleaned}"


def _env_name(reference: str | None) -> str | None:
    """Strip the ``env:`` prefix for display as a default."""
    return reference.removeprefix("env:") if reference else None


def _plain(text: str) -> str:
    """Strip rich markup, since typer.prompt does not render it."""
    import re

    return re.sub(r"\[/?[a-z ]+\]", "", text)
