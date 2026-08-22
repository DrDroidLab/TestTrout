"""The things a session produces, named so they can be come back to.

A chat is a good way to drive work and a terrible way to store it: everything
scrolls away. So each durable thing the tool identifies or creates is an
*artifact* — a project map, a form of what it still needs, a test plan, a
suite — listed beside the conversation and openable at any time.

The chat says what just happened. The sidebar holds what is true now.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ArtifactKind(StrEnum):
    """The kinds of thing worth keeping.

    Each maps to one API route that renders it, so adding a kind means adding
    a renderer rather than another tab.
    """

    MAP = "map"
    """What the project is: pages, endpoints, storage, deployment."""
    FACTS = "facts"
    """What the tool still needs from a person. An optional form."""
    PLAN = "plan"
    """What can be tested now, and what is waiting on a fact."""
    SUITE = "suite"
    """The baseline: tests that have been written and proven."""

    @property
    def label(self) -> str:
        """Display name for the sidebar."""
        return {
            "map": "Project map",
            "facts": "What I need from you",
            "plan": "Test plan",
            "suite": "Baseline suite",
        }[self.value]

    @property
    def icon(self) -> str:
        """A glyph, so the list scans at a glance."""
        return {"map": "🗺", "facts": "📝", "plan": "🎯", "suite": "🧪"}[self.value]


class Artifact(BaseModel):
    """One entry in the sidebar.

    Holds a summary only. The content is fetched when opened, because a project
    map is large and the sidebar is a list of names.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    summary: str = Field(description="One line: what it holds, in numbers.")
    ready: bool = Field(default=True, description="False while the thing does not exist yet.")
    attention: int = Field(default=0, description="How many items here are waiting on the user.")

    @property
    def label(self) -> str:
        """Display name."""
        return self.kind.label


class Message(BaseModel):
    """One turn in the conversation.

    The tool speaks; the person answers with a form or a button. There is no
    free-text channel on purpose — every input this tool needs is a concrete
    value, and a text box invites the behavioural questions the design exists
    to avoid.
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="'trout' or 'you'.")
    text: str
    at: str = Field(default="", description="ISO timestamp.")
    artifact: ArtifactKind | None = Field(
        default=None, description="Opens this artifact when the message is clicked."
    )
    action: str = Field(default="", description="A button to offer: 'scan', 'generate', or ''.")
    action_label: str = ""


class Conversation(BaseModel):
    """The whole session for one project. Stored, so a reload does not lose it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    messages: list[Message] = Field(default_factory=list)

    def say(
        self,
        text: str,
        *,
        artifact: ArtifactKind | None = None,
        action: str = "",
        action_label: str = "",
        at: str = "",
    ) -> Message:
        """Append a message from the tool."""
        message = Message(
            role="trout",
            text=text,
            at=at,
            artifact=artifact,
            action=action,
            action_label=action_label,
        )
        self.messages.append(message)
        return message

    def trim(self, keep: int = 200) -> None:
        """Bound the stored history. The artifacts hold what matters."""
        if len(self.messages) > keep:
            self.messages = self.messages[-keep:]


__all__ = ["Artifact", "ArtifactKind", "Conversation", "Message"]
