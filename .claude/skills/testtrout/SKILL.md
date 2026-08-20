---
name: testtrout
description: Drive TestTrout (the `trout` CLI or its MCP server) to analyse a web app repository, connect to a deployment, and build or run a baseline regression test suite. Use when the user asks to find what's untested, understand an unfamiliar React/Next.js + Supabase codebase, write baseline or regression tests, check RLS/authorization coverage, or run the QA suite. Triggers on "what tests am I missing", "test my app", "trout scan", "regression tests", "is this covered".
---

# Driving TestTrout

`qa` builds and runs a **baseline regression suite** for React/Next.js apps
backed by Supabase, deployed on Vercel or similar.

**Always use `--json`.** Every command supports it, and the output is the
underlying model verbatim. Never parse rendered terminal output.

## The order things happen in

Commands assume the earlier ones have run. Check with `trout doctor --json` when
unsure — it reports exactly what is present and what is missing.

```
trout scan      → .trout/surfaces.yaml    no API key, no network, safe on any repo
trout init      → .trout/config.yaml      interactive: entrypoints, auth, safety
trout probe     → .trout/observed/        needs a running deployment
trout intent    → .trout/intent.yaml      conversational
trout gaps                             ranked list of missing tests
trout propose   → proposals            needs a model provider
trout approve                          human decision — see below
trout generate  → test files
trout run                              executes against a deployment
```

## Start here on any unfamiliar project

```bash
trout scan --json
```

Free, offline, and safe on a repository you have just cloned. It returns
screens, Supabase data operations, RLS policies, endpoints, server actions,
schema, and third-party dependencies — each with a stable `id`, a criticality
level, and `criticality_reasons` explaining the score.

Read `warnings` before anything else. Two codes matter most:

- `table_without_rls` — a table written to from client code with no row-level
  security. In this stack the anon key is in the browser, so that data is
  world-writable. Surface this to the user immediately; it is usually news.
- `unresolved_table` — a Supabase call with a computed table name. A blind spot
  in any suite that gets generated. Ask the user what table it hits.

## Reading the output

- `criticality`: `critical` > `high` > `medium` > `low`. Ordering is
  deterministic and every score carries its reasons — quote them rather than
  re-deriving your own judgement.
- `screens[].reaches`: ids of data operations reachable from that screen. This
  is how you answer "what breaks if this page breaks".
- `policies[]`: RLS policies with their `using` / `with_check` expressions
  verbatim. Each one is a testable authorization claim and usually the highest
  value test available.
- Surface ids are stable across scans. Safe to reference in notes and issues.

Useful filters:

```bash
trout surfaces --json --kind data_operation --min high
trout surfaces --json --kind policy
```

## Rules

**Never point a mutating operation at production.** Entrypoints are read-only
unless explicitly marked `disposable: true`. If the user asks to run write tests
against production, stop and confirm what the deployment actually is — this is
the one action in the tool that can destroy real data.

**Approval is the user's, not yours.** `trout propose` drafts scenarios; a person
approves them. Present proposals with their reasoning and let the user choose.
Do not run `trout approve` on their behalf unless they said so explicitly.

**Never edit generated tests by hand.** They compile from scenario specs in
`.trout/scenarios/`. Edit the spec and re-run `trout generate`, or the change is lost
on the next generation.

**Credentials are `env:` references.** Never write a literal secret into
`.trout/config.yaml`; it is a committed file.

## When a command fails

```bash
trout doctor --json      # exits non-zero and lists exactly what is missing
```

Common causes: no scan yet (`trout scan`), no entrypoint (`trout init`), Playwright
not installed (`pip install 'testtrout[probe]' && playwright install chromium`),
or a model provider with no API key (only `propose`, `intent`, and failure
explanation need one).

## Probing a deployment

```bash
trout init --yes --url <URL> --no-disposable --role owner --role member
trout probe --role owner --json
```

`trout init --yes` prompts for nothing — use that form. It asks only for
environment variable *names*, never values, so it cannot write a secret into
the committed config.

`--no-disposable` is the safe default and blocks mutating requests at the
network layer. Only pass `--disposable` when the user has confirmed the data
behind that URL can be destroyed. Never flip it to make a probe succeed.

`trout probe` navigates to routes; it never clicks or submits. Read
`divergences[]` — `policy_denial` (RLS refused something the UI expects),
`auth_wall_while_signed_in` (session not accepted, or role lacks access), and
`weak_selectors` (nothing stable to test against) are the ones worth raising.

## Finding the missing tests

```bash
trout intent --draft --json    # or --describe "..." — model-backed, needs a key
trout gaps --json              # deterministic, no model, no key
```

`trout gaps` ranks what to write first. Every gap carries `reasons[]` — the named
contributions that produced its score. **Quote those rather than re-deriving
your own ranking;** they are auditable and yours is not.

Useful views:

```bash
trout gaps --json --kind authorization   # cheapest high-value tests
trout gaps --json --ready                # only what can be written now
trout gaps --json --budget 300           # best suite that runs in 5 minutes
```

`blockers[]` on a gap names real work: `needs_two_roles` (add a second test
user), `unreachable` (fix the route first), `unresolved_table` (ask which table
a computed name hits). Report these as tasks, not failures.

`trout gaps` works from the scan alone but is much better after `trout intent` and
`trout probe` — read `.notes[]`, which says exactly what evidence is missing.

## Drafting and generating tests

```bash
trout propose --json -n 5      # drafts only; --no-model works with no API key
trout scenarios --json         # review
trout approve <id> [<id>...]   # only when the user has chosen
trout generate --json
```

**Never call `trout approve` on the user's behalf.** Present the drafts with their
assertions and provenance and let them decide.

A scenario with `open_questions` **cannot** be approved. That is correct
behaviour, not an error — approving it produces a test that passes vacuously.
Surface the question; the answer usually takes seconds.

**Never edit generated test files.** They carry a do-not-edit header and are
overwritten on every `trout generate`. Edit the scenario `.yaml` and regenerate.

Read each assertion's `provenance`: `derived` (from a policy or schema) and
`observed` (seen in a real browser) are evidence; `inferred` is a suggestion
and can never justify blocking a merge.

## Running the suite

```bash
trout run --json          # read `status` BEFORE `results`
trout certify --json      # prove scenarios are deterministic
trout report --json
```

`status` is the first thing to read:

| status | what to tell the user |
|---|---|
| `pass` | Everything asserted held. |
| `fail` | A real assertion failed — a product signal. |
| `warning` | A flake, a blocked third-party call, or a timeout. |
| `inconclusive` | **The run says nothing about the product.** Do not report it as a pass or a failure. |

Only `assertion_failure` is a product signal. `auth_failure`,
`environment_failure`, `dependency_failure`, and `contract_mismatch` are about
the harness — reporting them as regressions destroys the suite's credibility.
Each result carries `reproduce`, a single command that reruns just that
scenario.

**Never disable database isolation or the substitution boundary to make a run go
green.** They exist to stop a test run destroying real data or charging a real
card. If a run is blocked by them, that is information, not an obstacle.

## MCP server

If shelling out is awkward, the same capabilities are available as typed tools:

```bash
pip install 'testtrout[mcp]' && trout mcp /path/to/project
```

Thirteen tools plus `trout://surfaces`, `trout://intent`, `trout://config`, `trout://scenarios`
as resources. The server is bound to one project, so it cannot act on the wrong
repository.

## Current status

Implemented: `trout scan`, `trout surfaces`, `trout doctor`, `trout providers`, `trout init`,
`trout probe`, `trout intent`, `trout gaps`, `trout propose`, `trout scenarios`, `trout approve`,
`trout generate`, `trout run`, `trout certify`, `trout report`, `trout mcp`.

`trout web` serves a local UI over the same state. Not implemented: GitHub PR integration. Check `trout --help` before
promising a command exists.
