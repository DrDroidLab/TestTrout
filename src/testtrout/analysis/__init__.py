"""Deterministic static analysis of a repository.

Nothing in this package may call a model provider. That constraint is what
makes ``qa scan`` fast, free, offline, and testable with golden files — see
``docs/adr/0003-deterministic-core.md``. If you find yourself wanting a model
here, the answer is almost always to emit a :class:`~testtrout.domain.surface.ScanWarning`
and let a later, model-aware layer resolve the ambiguity by asking the user.
"""
