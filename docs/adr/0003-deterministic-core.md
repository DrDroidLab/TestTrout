# 3. Deterministic core, model at the edges

**Status:** accepted

## Context

The obvious way to build this is to hand the repository to a model and ask what
it finds. That is fast to write and wrong to ship.

## Decision

`testtrout.analysis` never calls a model. All static analysis — surfaces, call
chains, schema, policies, criticality — is deterministic. The model is used for
exactly four things: interpreting stated intent, proposing scenarios, authoring
test code, and explaining a failure that has already been classified.

## Consequences

`qa scan` is free, offline, instant, and safe to run on a repository you do not
trust. It is also byte-for-byte reproducible, which is what makes the
golden-file tests in `tests/golden/` meaningful.

The project's own test suite runs with no API key. For an open-source tool this
is the difference between a contributor sending a patch and a contributor
closing the tab.

Criticality scores are explainable. Every score carries the facts that produced
it, so a user who disagrees can argue with a rule rather than with a vibe.

The cost is that some things a model would find easily — the *purpose* of a
screen, whether two operations are really the same workflow — we cannot infer
statically. We emit a `ScanWarning` and ask the user instead. That is the right
trade: an honest gap beats a confident guess.
