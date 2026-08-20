# Contributing

## Setup

```bash
uv sync --all-extras --dev
uv run trout scan examples/lovable-shop
uv run pytest
```

No API key is needed for development. The full suite runs offline against
recorded cassettes, and CI enforces that.

## The one rule

**`testtrout.analysis` must never call a model.** Static analysis is
deterministic, and that is what makes it fast, free, auditable, and testable
with golden files. If a model seems necessary there, emit a `ScanWarning`
instead and let a later layer resolve the ambiguity by asking the user.

## Where things go

| Package | Rule |
|---|---|
| `domain/` | Pure data. No I/O, no model calls, no framework knowledge. Public API. |
| `analysis/` | Deterministic static analysis. No network. |
| `llm/` | The only place allowed to reach a model provider. |
| `store/` | Reads and writes `.trout/`. |
| `cli/` | Presentation only — no business logic. |

## Adding an adapter

Three extension points, each a protocol plus an entry point. You do not need to
modify this repository to add one; ship it as your own package.

- **Framework** — `analysis/frameworks/base.py`, group `testtrout.frameworks`
- **Auth provider** — group `testtrout.auth`
- **Test emitter** — group `testtrout.emitters`

See [docs/adapters.md](docs/adapters.md).

## Golden tests

`tests/golden/` holds committed scan output for the fixture apps in `examples/`.
A change in extraction shows up as a diff in review, which is the point.

```bash
uv run pytest tests/golden          # verify
QA_UPDATE_GOLDEN=1 uv run pytest tests/golden   # accept intentional changes
```

Never update goldens without reading the diff.

## Style

`ruff` and `mypy --strict` both run in CI.

Docstrings are not decoration here. This is a tool other people extend, so a
docstring should explain *why* a boundary exists, not restate the signature.
When a decision was non-obvious, write an ADR in `docs/adr/`.
