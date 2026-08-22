"""TestTrout: automated regression testing for AI-built web applications.

The package is layered so that the expensive, non-deterministic parts are
isolated and optional:

``testtrout.domain``
    Pure data. No I/O, no model calls, no framework knowledge. Every other
    layer speaks in these types.
``testtrout.analysis``
    Deterministic static analysis of a repository. Never calls a model, so it
    is fast, free, offline, and verifiable with golden files.
``testtrout.store``
    Reads and writes the ``.trout/`` directory, which is the single source of
    truth shared by the CLI, the web interface, and any agent driving them.
``testtrout.cli``
    A thin presentation layer. It must contain no business logic.

See ``docs/adr/`` for why the boundaries sit where they do.
"""

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]
