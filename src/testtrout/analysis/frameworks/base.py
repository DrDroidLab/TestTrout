"""The framework adapter contract.

This protocol is public API. Third-party packages implement it and register
under the ``testtrout.frameworks`` entry point group, so changing a signature
here breaks downstream adapters.

An adapter's whole job is to answer "what can a user navigate to, and what
server code backs it?" for one framework. It must not call a model, and it must
not fail on a malformed project — return what you found and let the scanner
report the gap.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from testtrout.analysis.detect import ProjectContext
from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import SourceFile
from testtrout.domain.surface import Endpoint, ScanWarning, Screen, ServerAction


@runtime_checkable
class FrameworkAdapter(Protocol):
    """Extracts navigable and server-side surfaces for one framework."""

    id: ClassVar[str]
    """Stable adapter identifier, matching the entry point name."""

    def matches(self, context: ProjectContext) -> bool:
        """Whether this adapter should handle the project."""
        ...

    def screens(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> tuple[list[Screen], list[ScanWarning]]:
        """User-reachable routes and the component behind each.

        ``Screen.reaches`` may be left empty; the scanner fills it in from the
        module graph, since reachability is framework-independent.
        """
        ...

    def endpoints(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> list[Endpoint]:
        """First-party HTTP endpoints. Empty for frameworks that have none."""
        ...

    def server_actions(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> list[ServerAction]:
        """Server-side functions callable from the client. Empty when unsupported."""
        ...
