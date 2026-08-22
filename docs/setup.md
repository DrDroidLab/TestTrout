# Setup

Written to be followed top to bottom, by a person or by a coding agent. Every
step has a command, a way to verify it worked, and what to do when it doesn't.

**Agents:** pass `--json` to any command and parse the result. Never parse
rendered terminal output. `trout doctor --json` exits non-zero and lists exactly
what is missing — call it whenever a step fails, before trying anything else.

---

## The whole flow

```bash
pip install testtrout
trout up
```

Storage, worker, and interface, all local. **No API key of any kind** — nothing
here calls a model.

The interface is a conversation with a sidebar. The conversation says what just
happened and what to do next; the sidebar holds what is true now:

| Artifact | What it holds |
|---|---|
| **Project map** | Pages, endpoints, storage, deployment, third parties |
| **What I need from you** | An optional form of concrete values |
| **Test plan** | What can be tested now, and what each blocked item needs |
| **Baseline suite** | Every test, and what it is currently doing |

The same thing from the terminal:

```bash
trout add ~/code/my-app --url https://my-app.vercel.app
trout look          # read the code, ask the deployment, work it out
trout facts         # what I still need — all optional
trout build         # write the baseline and prove it
trout run           # re-run it; a failure means behaviour changed
```

### What it asks for

Only values a person holds and the tool cannot discover. Never a question about
what your product is supposed to do — that is read from your code and observed
from your deployment.

```bash
trout facts --set deployment_url=https://my-app.vercel.app
trout facts --set api_url=https://api.my-app.com
trout facts --set account_primary=test@example.com:hunter2
trout facts --set sample_id=7f3ab210
```

Partial answers are the normal case. Give it a URL and nothing else and you get
every page that loads signed out. Add an account and everything behind the
sign-in follows.

**Passwords never reach committed files.** They go to a gitignored `.env`; the
committed `config.yaml` holds only `env:NAME` references.

### The one thing that is a command, not a value

Tests can be written without a runner, but not proven — so the suite would be
empty. Install them in your app:

```bash
npm install -D vitest @playwright/test && npx playwright install chromium
```

In a monorepo that is the directory holding your `package.json`, which TestTrout
reports as the app root in the project map.

### GitHub access

Checked in order: `GITHUB_TOKEN`, then the `gh` CLI if you are logged in, then a token
stored by TestTrout. The first two mean it never holds a credential at all. To store
one explicitly:

```bash
trout github-login
```

It is written to `~/.testtrout/github` with owner-only permissions, never into the
database — database files get copied, backed up, and attached to bug reports in ways
people do not think about.

---

## Install

```bash
pip install testtrout            # scan, plan, build, run
pip install 'testtrout[probe]'   # plus a real browser
pip install 'testtrout[mcp]'     # plus the MCP server
```

Python 3.11+. Node is needed only in the project under test, to run the
generated Playwright and Vitest files.

## What a look actually does

```bash
trout look --json | jq '.counts'
```

Three things, in order, and always together — a scan whose consequences are not
worked out is a file on disk nobody asked for.

**1. It reads the code.** No network access at all. Finds routes and the
components behind them, HTTP endpoints (including calls made through a client
wrapper), Supabase reads and writes with their tables, row-level security
policies, third-party vendors, and the environment variables the app reads.

**2. It asks the deployment.** Loads every known route in a real browser and
records what happened: status, title, and the most durable selectors on the
page. Then asks each endpoint what it does with no credentials — with a GET,
whatever methods it declares, because a GET cannot change anything.

A `401` or `403` means the auth layer answered first, so an account is needed. A
`405 Method Not Allowed` means the router answered, so the request got past
auth. A `404` usually means the API is served somewhere else entirely, which is
why it asks for an API URL rather than reporting the endpoint as public.

**3. It works out what can be tested.** Anything the deployment answered for is
ready, because the baseline *is* what it answered. Anything else names the one
concrete value that would change that.

## What a build writes

For each ready candidate, one file, then it runs it, then it keeps it only if it
passes:

```typescript
test('/orders still loads', async ({ page }) => {
    await page.goto('/orders');
    // observed: the page title when /orders was loaded
    await expect(page).toHaveTitle('Orders');
    // observed: seen on /orders at baseline
    await expect(page.getByTestId('orders-table')).toBeVisible();
});
```

Every assertion names what was seen. Nothing asserts what the page *should* say,
because nobody told it.

Selectors are chosen by durability: a `data-testid` beats a role, which beats
visible text. A test pinned to copy breaks on a wording change and teaches
people to ignore failures.

### `--disposable` is the important flag

It answers one question: **can the data behind this deployment be destroyed
freely?**

- `--disposable` — a local or throwaway database. Writes are permitted.
- `--no-disposable` — anything shared, staging, or production. **Mutating
  requests are blocked at the network layer**, not merely discouraged.

When unsure, use `--no-disposable`. Navigation alone is not safe: plenty of
these apps write on mount, so the guard sits below navigation rather than
relying on you to avoid clicking things.

**Verify:**

```bash
trout doctor --json | jq '.checks'
```

---

### Database isolation

```yaml
supabase:
  isolation: local_reset    # local_reset | scoped_seed | branch
```

- `local_reset` — real isolation via `supabase db reset`. Needs the Supabase CLI
  and a local stack.
- `scoped_seed` — **no reset.** Works against a shared deployment, and every run
  reports the caveat that a failure may reflect leftover data.
- `branch` — not implemented yet; requesting it reports that plainly rather than
  pretending.

### Third-party calls are blocked

Hosts in `substitution.external` — populated by `trout look` from the SDKs it finds
— are intercepted during browser tests. A test run cannot charge a card or email
a customer. An unmatched request fails loudly rather than passing through,
because a mock that silently matches nothing is how a suite starts reporting
green while testing nothing.

### Retries are off, deliberately

`retries: 0` is set in the generated runner configs. Retrying hides the
flakiness it should surface, and a baseline test that only passes sometimes is
telling you something real about the page.

---

## The web interface

```bash
trout web            # http://127.0.0.1:7411
```

Same state as the CLI, different shape: a conversation in the middle, and every
artifact it produces listed beside it. The conversation says what just happened
and what is worth doing next; the sidebar holds what is currently true, so
nothing important scrolls away.

It binds to **loopback only**. It reads your credentials and can trigger runs
against your deployment, so it is not something to expose on a network.

There is deliberately **no way to mark a deployment disposable from the web
page**. One click away from pointing test writes at production is exactly the
mistake the guard exists to prevent, so that stays a deliberate edit to
`.trout/config.yaml`.

Only one action runs at a time. Two concurrent runs against one database
produce failures neither run can account for.

---

## Using it from a coding agent

Two options. The CLI with `--json` works everywhere. For agent hosts without a
shell, or where typed tool schemas beat remembering flags, there is an MCP
server:

```bash
pip install 'testtrout[mcp]'
trout mcp /path/to/your/project
```

It exposes seven tools (`look`, `facts`, `set_facts`, `plan`, `build`, `run`,
`suite`) and five resources (`trout://map`, `trout://facts`, `trout://plan`,
`trout://config`, `trout://scenarios`).

The server is bound to **one project** at startup, so an agent cannot operate
on the wrong repository by passing a stray path. Tools return summaries;
resources carry the bulk, so a tool result does not crowd out an agent's
context.

---

## What gets committed

| Path | Committed | Why |
|---|---|---|
| `.trout/config.yaml` | yes | Configuration. Holds `env:` references, never secrets. |
| `.trout/surfaces.yaml` | yes | The surface map. Deterministic, so it diffs cleanly. |
| `.trout/facts.yaml` | yes | What was asked for. Never a secret value. |
| `.trout/plan.yaml` | yes | What can be tested, and what is waiting. |
| `.trout/overview.yaml` | yes | The project map, in product language. |
| `.trout/scenarios/` | yes | Your test suite. Reviewing changes to it is the point. |
| `.trout/observed/` | no | A snapshot of a running system at one moment. |
| `.trout/runs/`, `.trout/evidence/` | no | Outputs. Large. |
| `.env` | **never** | Secrets. |
