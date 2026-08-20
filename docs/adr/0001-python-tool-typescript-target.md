# 1. Python tool, TypeScript target

**Status:** accepted

## Context

The tool analyses TypeScript/React codebases but is itself a separate program.
Those two languages need not match, and there is a real cost either way.

## Decision

Implement the tool in Python. Target TypeScript codebases.

## Consequences

Good: tree-sitter bindings are mature, the model-provider ecosystem is
Python-first, and `uv tool install` / `pipx` make distribution a single command
even for a team that has never run Python.

Bad: we do not dogfood. A Node developer installs a Python CLI, which is
friction we accept for now and can paper over later with a thin npm wrapper.

Because we cannot dogfood, the fixture applications in `examples/` carry more
weight than they normally would — they are the only realistic input the test
suite ever sees. Keep them realistic, and add one whenever a new stack shape
shows up in the wild.
