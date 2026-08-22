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

Storage, a worker, and the interface at `localhost:7411`. No Docker, no daemon,
no database to install — SQLite under `~/.testtrout`, worker in-process. **No API
key of any kind.** Nothing here calls a model.

## How it works

Add a project — a folder on this machine, and the URL it is deployed at.

```bash
trout add ~/code/my-app --url https://my-app.vercel.app
```

Then four steps, which are also four commands:

```bash
trout look     # read the code, ask the deployment, work out what is testable
trout facts    # what I still need from you — all optional
trout build    # write the baseline and prove it
trout run      # re-run it and report what changed
```

**A baseline is what your deployment does today.** TestTrout records that and
asserts it keeps happening. It does not know whether the current behaviour is
*correct* — nobody has told it — and it will never ask. It knows what the
behaviour is, and it notices the day that changes.

That single rule is why there is no interview, no approval queue, and no list of
questions about your product. The only things it asks for are concrete values it
cannot discover:

| It asks for | Because |
|---|---|
| A deployment URL | Nothing can be tested against code alone |
| An API URL | Only if your backend is deployed on its own host |
| An account | Only when something actually refused an unauthenticated request |
| A real id | Only when the probe could not reach `/jobs/:id` on its own |

Every one is answerable in seconds, every one is optional, and each says what it
unlocks. Give it a URL and nothing else, and you get every page that loads
signed out — which on most apps is already a useful suite.

Everything it works out is kept as an **artifact** in the sidebar — the project
map, the form, the test plan, the baseline suite — so nothing important is three
screens up in a chat scrollback.

## Or stay in your coding agent

The app is one way in, not the only one. Everything it does is available as typed MCP
tools and as CLI commands, so anyone who would rather not leave their editor does not
have to:

```bash
pip install 'testtrout[mcp]'
trout mcp /path/to/your/project
```

Point your agent at the skill in [`.claude/skills/`](.claude/skills/), or tell it:

> Use the TestTrout MCP server. Look at the repo, tell me what it still needs
> from me, then build the baseline and run it.

Seven tools, bound to one project so an agent cannot act on the wrong repository:

| | |
|---|---|
| `look` | Read the code, ask the deployment, work out what is testable. |
| `facts` `set_facts` | What concrete values are missing; save what the user gives. |
| `plan` | What can be tested now, and what each blocked item needs. |
| `build` | Write the baseline and prove it against the deployment. |
| `run` `suite` | Re-run it, and read what every test is doing. |

Plus `trout://map`, `trout://facts`, `trout://plan`, `trout://config`,
`trout://scenarios` as resources, so bulk state never crowds out an agent's
context window.

## What it understands

`trout look` reads code with no network access at all, so it is safe on a repo you just
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
`trout look` locates once so tests replay a known form instead of guessing.

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

**No model, anywhere.** Not in scanning, not in planning, not in writing a test.
Every assertion traces to something the deployment actually did, which is why the
same inputs always produce the same suite and why there is no API key to configure.

**Every assertion carries its provenance**, and in a baseline it is always
`observed` — with the evidence written into the generated file:

```typescript
// observed: the page title when /orders was loaded
await expect(page).toHaveTitle('Orders');
// observed: seen on /orders at baseline
await expect(page.getByTestId('orders-table')).toBeVisible();
```

**It only ever sends a GET.** Endpoint tests replay the request the probe made,
whatever methods the endpoint declares, so pointing this at production cannot
change anything there.

**A failure is classified before it is reported.** Only `assertion_failure` is a product
signal. Auth failures, unreachable databases, and blocked third-party calls are about
the harness, and an inconclusive run is *never* upgraded to a pass.

## Everything from the terminal

```bash
trout add ~/code/my-app --url https://my-app.vercel.app
trout look          # read the code, ask the deployment
trout facts         # what I still need — all optional
trout build         # write the baseline and prove it
trout run           # re-run it; a failure means behaviour changed
```

Every command supports `--json`. Full walkthrough in [docs/setup.md](docs/setup.md).

## How it stores things

Your test suite stays in your repository, committed and reviewable:

```
.trout/scenarios/*.yaml    what each test asserts, in plain language
.trout/facts.yaml          what was asked for — never a secret value
.trout/plan.yaml           what can be tested, and what is waiting
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
- **Nothing is hosted, and nothing leaves your machine.** There is no model to
  call and no account to make. No telemetry.

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
