<div align="center">

<img src="assets/trout-256.png" alt="" width="112" height="112">

# TestTrout

**The testing assistant for AI-built apps.**

Run it as an app, or hand it to your coding agent over MCP.
Either way it writes the tests your app never had, and tells you when they break.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-alpha-orange)](#roadmap)

</div>

---

Trout are an indicator species. They only live in clean, well-oxygenated water — find
one in a stream and you know the water is healthy without testing it yourself.

That is what a good test suite is: not a chore, but a signal you can read at a glance.

---

## The problem

Coding agents ship fast. They also break things quietly, and the apps they build —
Lovable, v0, Bolt, hand-rolled React on Vercel — almost never have tests. So the loop
ends with *"looks good to me"* from the thing that just wrote the code.

TestTrout closes that loop. It reads the repository, connects to the running
deployment, works out what is untested and in what order it matters, writes real tests,
runs them, and reports results you can actually act on.

## Get it running

```bash
pip install testtrout
trout up
```

That starts everything: storage, a background worker, and the interface at
`localhost:7411`. No Docker, no daemon, no database to install — storage is SQLite
under `~/.testtrout` and the worker runs in-process.

Then link a repository, from the interface or the terminal:

```bash
trout link ~/code/my-app          # a folder you already have
trout link --github owner/name    # cloned with your GitHub token
```

Linking a local folder never modifies it. A scan starts automatically, and from there
the interface walks you through connecting a deployment, reviewing what is untested,
approving tests, and running them.

## Or stay in your coding agent

The app is one way in, not the only one. Everything it does is available as typed MCP
tools and as CLI commands, so anyone who would rather not leave their editor does not
have to:

```bash
pip install 'testtrout[mcp]'
trout mcp /path/to/your/project
```

Point your agent at the skill in [`.claude/skills/`](.claude/skills/), or tell it:

> Use the TestTrout MCP server. Scan the repo, show me what's untested ranked by
> importance, draft tests for the top five, and run them.

Thirteen tools, bound to one project so an agent cannot act on the wrong repository:

| | |
|---|---|
| `scan` `surfaces` | Understand the codebase. No API key, no network. |
| `probe` | See what the deployed app actually does, in a real browser. |
| `intent` `gaps` | Rank what is untested, and say why. |
| `propose` `approve` `generate` | Draft, review, compile to real test files. |
| `run` `certify` `report` | Execute, prove determinism, read evidence. |
| `doctor` | What is missing, and how to fix it. |

Plus `trout://surfaces`, `trout://intent`, `trout://config`, `trout://scenarios` as
resources, so bulk state never crowds out an agent's context window.

## What it understands

`trout scan` is fully deterministic — no model, no network, safe on a repo you just
cloned. On a typical Supabase app it finds:

| Surface | Example |
|---|---|
| Screens | `/orders/:id` → `OrderDetail`, and the data it reaches |
| Data operations | `supabase.from('orders').delete().eq('id', …)` |
| RLS policies | `Users manage own orders` — a testable authorization claim |
| Server actions | `'use server'` functions — endpoints that look like helpers |
| Route handlers | `app/api/checkout/route.ts` → `POST` |
| Third parties | Stripe, Resend — the substitution boundary |
| Schema | Tables, columns, foreign keys, RLS status |

It also tells you when a table is written from browser code with **no row-level
security** — meaning it is world-writable through the anon key. That is usually news.

## Why the tests are worth trusting

**It builds a baseline, not per-PR guesses.** A test derived from the code you just
changed asserts the new behaviour is correct by construction — it cannot catch a
regression. TestTrout certifies a suite against a *working* deployment first, so a
failure means something.

**Deterministic core, model at the edges.** Scanning, ranking, execution, and failure
classification never call a model. The model only interprets your intent, refines
wording, and picks which observed elements to assert on. Every ranking is the sum of
named contributions:

```
critical  authorization  A user cannot read another user's rows in payments   100
  · critical surface
  · policy: exists (select 1 from orders o where o.id = payments.order_id …)
```

**Every assertion carries its provenance.** `derived` from a policy, `observed` in a
real browser, or `inferred` by a model — and inferred alone can never block anything.

**A failure is classified before it is reported.** Only `assertion_failure` is a product
signal. Auth failures, unreachable databases, and blocked third-party calls are about
the harness, and an inconclusive run is *never* upgraded to a pass.

## Everything from the terminal

```bash
trout scan          # understand the code
trout init          # connect a deployment
trout gaps          # what's missing, ranked, with reasons
trout run           # execute, with evidence behind every result
```

Every command supports `--json`. Full walkthrough in [docs/setup.md](docs/setup.md).

## How it stores things

Your test suite stays in your repository, committed and reviewable:

```
.trout/scenarios/*.yaml    what each test asserts, in plain language
.trout/config.yaml         deployments and env: references, never secrets
tests/trout/               generated Playwright and Vitest files
```

Run history, coverage over time, and the job queue live in SQLite under
`~/.testtrout`. That split is deliberate: the suite belongs next to the code where a
pull request can review it, and the questions files cannot answer — is this test
getting flakier, is coverage going up — belong in a database.

### Optional web view

```bash
trout web
```

Coverage at a glance, the ranked gap list, scenario review, run history with evidence,
live log. Same `.trout/` files as the CLI — no database, nothing hosted, loopback only.
Entirely optional; the CLI and MCP are complete on their own.

## Safety

The tool needs your database credentials and can drive your deployment, so the
guarantees are enforced in code rather than documented:

- **Production is read-only by default.** Mutating requests are *blocked at the network
  layer* unless an entrypoint is explicitly marked `disposable`. The guard sits below
  navigation, because "just loading a page is read-only" is false — plenty of these apps
  write on mount. No agent, and no web click, can change that setting.
- **Third parties are intercepted.** A test run cannot charge a card or email a customer.
  Unmatched outbound requests fail loudly; a mock that silently matches nothing is how a
  suite reports green while testing nothing.
- **Secrets stay out of committed files.** `.trout/config.yaml` holds `env:` references
  only. Values live in a gitignored `.env`.
- **Nothing is hosted.** Nothing leaves your machine except calls to the model provider
  you chose. No telemetry.

## Model providers

Anthropic, OpenAI, or Kimi — or any OpenAI-compatible endpoint via `base_url`.

```yaml
model:
  provider: anthropic
  api_key: env:ANTHROPIC_API_KEY
```

Analysis never calls a model, so `trout scan`, `trout gaps`, and `trout run` all work
with no key at all.

## Supported stacks

React + Vite (Lovable, v0, Bolt) and Next.js App Router, in TypeScript, with Supabase,
deployed anywhere reachable over HTTP. Auth via Supabase, Clerk, or NextAuth.

Deliberately narrow. Depth on one stack beats shallow coverage of many — and these
codebases are regular enough that static analysis is genuinely accurate on them. Other
stacks are an adapter away: see [docs/adapters.md](docs/adapters.md).

## Roadmap

| | |
|---|---|
| ✅ | Repository analysis, deployment probing, gap ranking |
| ✅ | Scenario authoring, generation, execution, certification |
| ✅ | MCP server, CLI, and a local app with storage and a worker |
| ✅ | Change-based test selection, base-branch differential |
| ✅ | Multi-repository: link local folders or clone with a GitHub token |
| 🔜 | GitHub pull-request checks |
| 🔜 | Observed coverage index (today's selection uses declared coverage) |

## Contributing

Adding a framework, auth provider, or test runner means implementing one protocol and
registering an entry point — no fork required. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [docs/adapters.md](docs/adapters.md).

The full test suite runs offline with no API key. That is deliberate and worth keeping.

## License

Apache 2.0
