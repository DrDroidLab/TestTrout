"""Turning approved scenario specifications into runnable test code.

Emitted code is a build artifact. It carries a header saying so, and editing it
is not a supported workflow — the next generation overwrites it. Change the
specification in ``.trout/scenarios/`` and regenerate.

That rule is what makes it safe to improve selector strategy or test structure
centrally: a fix lands in every generated test at once rather than requiring
someone to sweep hundreds of files by hand.
"""

from testtrout.authoring.base import EmittedFile, TestEmitter, load_emitters, select_emitter

__all__ = ["EmittedFile", "TestEmitter", "load_emitters", "select_emitter"]
