---
name: testtrout
description: Drive TestTrout (the `trout` CLI or its MCP server) to read a web app repository, check what its deployment actually does, and build a baseline regression suite that notices when behaviour changes. Use when the user asks what is untested, wants regression tests for a React/Next.js app, or wants to know whether a deploy broke something. Triggers on "what tests am I missing", "test my app", "trout look", "regression tests", "did anything break".
---

# Driving TestTrout

TestTrout builds a **baseline regression suite** for a web application. A
baseline is what the deployment does today: the tool records that and asserts it
keeps happening.

**It never asks what the product is supposed to do, and neither should you.**
It does not know whether current behaviour is correct — nobody has told it — and
inventing expectations on the user's behalf is the failure mode this design
exists to prevent.

**Always use `--json`.** Every command supports it, and the output is the
underlying model verbatim. Never parse rendered terminal output.

## The order things happen in

```
trout add <path> --url <url>    a repo, and where it is deployed
trout look                      read the code, ask the deployment, work it out
trout facts                     what concrete values are still missing
trout facts --set k=v           save one; the plan updates immediately
trout build                     write the baseline and prove it
trout run                       re-run it; a failure means behaviour changed
```

`trout look` is safe on anything: reading code makes no network calls, and the
only requests sent to a deployment are GETs.

## What `facts` asks for, and what it never asks

Everything on the sheet is a concrete value the user holds:

| id | what it is |
|---|---|
| `deployment_url` | where the app is running |
| `api_url` | only if the backend is on its own host |
| `account_primary` | an account to sign in as — `--set account_primary=email:password` |
| `account_second` | only for checking one user cannot see another's data |
| `sample_<name>` | a real value for a route parameter, e.g. a job id that exists |
| `toolchain` | a command to run, not a value to type |

Each carries `why` (what it unlocks) and `blocks` (how many candidate tests are
waiting on it). Fill in the highest `blocks` first.

If you find yourself wanting to answer one of these from the repository, that is
a bug in the tool — say so rather than guessing. And if a value is missing, ask
the user; do not invent one. A wrong URL produces a suite that asserts a 404.

## Reading a run

- Read `status` before `results`. An `inconclusive` run says nothing about the
  product.
- Only `assertion_failure` means behaviour changed. Everything else — auth
  failure, environment failure, timeout — is about the harness.
- Every assertion carries `provenance: observed` and a `source` naming what was
  seen. Quote that when reporting a failure.

## Rules

- Never mark a deployment disposable on the user's behalf. Writes against a
  non-disposable deployment are blocked, and that guard is why pointing this at
  production is defensible.
- Never edit files under `tests/trout/` — they are regenerated from
  `.trout/scenarios/`.
- A test that has not been run is not in the baseline. `build` keeps only what
  passes, and that is the point.

## State on disk

```
.trout/surfaces.yaml     the last scan
.trout/facts.yaml        what was asked for — never a secret value
.trout/plan.yaml         what can be tested, and what is waiting
.trout/scenarios/*.yaml  what each test asserts
tests/trout/             generated Playwright and Vitest files
```

Also available over MCP as `trout://map`, `trout://facts`, `trout://plan`,
`trout://config`, `trout://scenarios`.
