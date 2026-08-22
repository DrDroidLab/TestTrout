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

Then add a project — a repository, and the URL it is deployed at:

```bash
trout link ~/code/my-app --url https://my-app.vercel.app
trout link --github owner/name --url https://my-app.vercel.app
```

Both halves matter. The repository says what your product is supposed to do; the
deployment says what it actually does, and a test is only worth keeping once it has
been run against the second. Your folder is never modified, and the deployment is
read-only until you mark it disposable.

A scan starts automatically and answers three questions:

- **What is this?** Your pages, your APIs, and the transactions they add up to — a
  page together with the state it can change, which is where regressions hurt.
- **What does TestTrout still need from you?** Discovered by reading your source: which
  variables the app reaches for and what each is likely for. A partial set gives a
  partial suite — with only a URL you can probe and run API tests; add a second test
  account and authorization tests become possible. Every blocked capability names the
  one next thing it needs, never "configure it properly".
- **What is untested?** Ranked, most worth doing first.

Then press **Build tests**. Each test is written, run against your deployment, and kept
only if it passes — a test nobody has run is a guess, and approving one is guessing
twice. Anything that cannot be settled becomes a question rather than an assumption.

Scan again whenever you like: the second scan reports what moved — new areas in the
code, what the suite now covers, what is gone — rather than reprinting the same list.

Two things make that safe and useful:

**It tells you what credentials your app needs**, discovered by reading your source —
which variables it reaches for, what each is likely for, and the line it appears on.
No re-deriving something the code already states.

**A partial set gives a partial suite.** With only a URL you can probe and run API
tests; add an anon key and a second account and authorization tests become possible.
Each blocked capability names the single next thing it needs, never "configure it
properly".

Secret values are written to a gitignored `.env`; committed configuration holds only
`env:NAME` references, and a literal secret typed into a config field is rejected.

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

## It never asks for your database

Tests reach your app the way a user does: HTTP against your endpoints, and a real
browser against your interface. Sign-in goes through your own login form, which
`trout probe` locates once so tests replay a known form instead of guessing.

So the whole thing needs **a URL and two test accounts**. No anon key, no service
role, no project credentials — nothing most teams would reasonably refuse to hand a
testing tool.

Authorization is still covered, and arguably better: signed in as two ordinary
accounts, whatever one can see the other must not. That exercises your policies *and*
everything your application layers on top of them, which is what a real user meets.

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
| ✅ | Full configuration from the interface or the CLI, with credential discovery |
| 🔜 | GitHub pull-request checks |
| 🔜 | Observed coverage index (today's selection uses declared coverage) |

## Contributing

Adding a framework, auth provider, or test runner means implementing one protocol and
registering an entry point — no fork required. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [docs/adapters.md](docs/adapters.md).

The full test suite runs offline with no API key. That is deliberate and worth keeping.

## License

Apache 2.0
