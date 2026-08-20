# Setup

Written to be followed top to bottom, by a person or by a coding agent. Every
step has a command, a way to verify it worked, and what to do when it doesn't.

**Agents:** pass `--json` to any command and parse the result. Never parse
rendered terminal output. `trout doctor --json` exits non-zero and lists exactly
what is missing — call it whenever a step fails, before trying anything else.

---

## Step 1 — Install

```bash
uv tool install testtrout          # or: pipx install testtrout
```

**Verify:**

```bash
trout version        # prints a version string
```

**If `trout` is not found:** the install location is not on `PATH`. Run
`uv tool update-shell` (or `pipx ensurepath`) and open a new shell.

---

## Step 2 — Scan the repository

Needs no API key, no network, and no running application. Safe on a repository
you have just cloned and do not trust.

```bash
cd /path/to/your/project
trout scan
```

**Verify:**

```bash
trout scan --json | jq '.project.framework, (.data_operations | length)'
```

A framework of `"unknown"` or zero data operations means the scan found
nothing. Check `.project.detected_from` in the JSON — it lists the evidence the
detection used, which is usually enough to see what went wrong.

**Supported:** React + Vite (Lovable, v0, Bolt output) and Next.js App Router,
in TypeScript, with Supabase. Anything else needs an adapter — see
[adapters.md](adapters.md).

**Read the warnings before continuing:**

```bash
trout scan --json | jq '.warnings'
```

| Code | Meaning | What to do |
|---|---|---|
| `table_without_rls` | A table is written to from client code with no row-level security. The anon key is in the browser, so this data is world-writable. | Tell the user. This is usually news, and usually urgent. |
| `unresolved_table` | A Supabase call whose table name is computed. | A blind spot — nothing will generate a test for it. Ask which table it hits. |
| `no_routes_found` | No React Router routes were found. | The app may route another way. `trout probe` can still discover screens. |

---

## Step 3 — Credentials

Create a **gitignored** `.env` beside your project. `trout` loads it
automatically and never overrides a variable you have already exported.

```bash
cat > .env <<'ENV'
SUPABASE_ANON_KEY=...
QA_OWNER_EMAIL=owner@example.test
QA_OWNER_PASSWORD=...
QA_MEMBER_EMAIL=member@example.test
QA_MEMBER_PASSWORD=...
MOONSHOT_API_KEY=...
ENV
echo ".env" >> .gitignore
```

**Two test users, not one.** Proving that user A cannot see user B's data
requires a B. Authorization tests are the highest-value tests this tool
generates and they are impossible with a single account.

**Never put a secret in `.trout/config.yaml`.** That file is committed. It holds
`env:NAME` references only, and `trout init` will only ever ask you for names.

---

## Step 4 — Configure

Interactive:

```bash
trout init
```

Non-interactive — **use this form in scripts and agents**, it prompts for
nothing:

```bash
trout init --yes \
  --url https://your-app.vercel.app \
  --name preview \
  --no-disposable \
  --supabase-url https://YOUR-REF.supabase.co \
  --provider kimi \
  --role owner --role member
```

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

## Step 5 — Browser probing

```bash
pip install 'testtrout[probe]'
playwright install chromium
```

Then:

```bash
trout probe --role owner
```

This signs in, loads every known route, and records what actually happens:
which queries fire, what the backend returns, what stable selectors exist, and
what the console logged. It **navigates only** — it never clicks buttons or
submits forms.

**Verify:**

```bash
trout probe --json | jq '[.screens[] | select(.reachable)] | length'
```

**Common results and what they mean:**

| Finding | Meaning |
|---|---|
| `auth_failed` | Sign-in did not work. Read the `detail` — it names the specific cause. |
| `auth_wall_while_signed_in` | The session was not accepted, or the role genuinely lacks access. |
| `policy_denial` | RLS refused a request the screen expects to succeed. A real bug, usually. |
| `write_blocked` | The guard stopped a write. Expected on `--no-disposable`. |
| `weak_selectors` | Nothing more stable than visible text to target. Add `data-testid` attributes. |
| `undeclared_table` / `undeclared_external` | Runtime traffic the static scan cannot account for. |

**If Supabase sign-in fails**, check in this order:

1. The user exists **and is confirmed** in Supabase Auth.
2. The password grant is enabled for the project.
3. `supabase.url` and `supabase.anon_key` are correct.
4. You are using the right endpoint — `api.moonshot.ai` vs `.cn` style
   mismatches exist for Supabase regions too, and a key from one project is
   rejected by another.

---

## Step 6 — Model provider

Only scenario proposal, intent capture, and failure explanation use a model.
`trout scan` and `trout probe` never do.

```bash
trout providers --check      # makes one small live call
```

Supported: `anthropic`, `openai`, `kimi` (Moonshot, or any OpenAI-compatible
endpoint via `base_url`).

**Do not set `temperature`.** Reasoning models restrict it — Claude rejects the
parameter outright, and `kimi-k3` accepts only `1`. The default leaves it unset,
which is correct for every provider.

---

---

## Step 7 — Capture intent and see the gaps

```bash
trout intent          # conversational; starts from a draft of your codebase
trout gaps            # ranked list of the tests you are missing, and why
```

`trout intent` does not start from a blank page. It reads your surface map, drafts
what it thinks the product does, and asks you to correct it — a much easier
question to answer. Everything drafted is marked `inferred` until you confirm
it, and inferred intent never justifies blocking anything.

Scriptable:

```bash
trout intent --describe "A shop where customers place and pay for orders." --json
trout intent --from PRODUCT.md --json
trout intent --draft --json      # draft only, ask nothing
```

`.trout/intent.yaml` is committed and hand-editable. Correcting that file directly
is often faster than another round of conversation, and the tool reads it back.

`trout gaps` is **deterministic — no model is used.** Every rank is the sum of
named contributions, so a ranking you disagree with can be argued with:

```bash
trout gaps --json | jq '.gaps[0] | {title, score, reasons}'
trout gaps --kind authorization      # the cheapest high-value tests
trout gaps --ready                   # only what can be written right now
trout gaps --budget 300              # the best suite that runs in 5 minutes
```

It works from the scan alone, and gets substantially better with `trout intent`
and `trout probe`. Read `.notes[]` — it says what evidence is missing.

**Blocked gaps** are real work that cannot be done yet. `needs_two_roles` means
add a second test user; `unreachable` means fix the route before testing it;
`unresolved_table` means tell the tool which table a computed name refers to.

---

## Step 8 — Draft, approve, generate

```bash
trout propose -n 5            # draft the top-ranked gaps
trout scenarios               # review them
trout approve <id> [<id>...]  # or --all for every draft with no open questions
trout generate                # compile to runnable test files
```

`trout propose` builds scenarios **deterministically** from your schema, policies,
and probe data. A model only refines the wording, picks which observed elements
to assert on, and says what it could not determine. `--no-model` works with no
API key and still produces usable scenarios.

Scenarios live in `.trout/scenarios/*.yaml` — committed, hand-editable, and the
thing you review. Editing a `.yaml` and regenerating is the supported way to
change a test.

**Generated code is a build artifact.** It carries a do-not-edit header and is
overwritten on every `trout generate`. Never edit it.

A scenario with **open questions cannot be approved.** That is deliberate:
approving it produces a test that passes vacuously, which is worse than having
no test. Answer the question in the `.yaml`, then approve.

What gets generated:

| Kind | Output | Runner |
|---|---|---|
| `authorization` | Leak test: no row you can read belongs to another user | Vitest + supabase-js |
| `browser_journey` | Navigate and assert on observed elements | Playwright |
| `endpoint` | HTTP call and status assertion | Vitest |

Authorization tests are read-only, so they are safe against a shared
deployment, and they are the highest value per second of runtime — the policy
already states the expectation, so nothing is inferred.

---

## Step 9 — Run the suite

Generated tests run through **your** toolchain, so they work without this tool
installed:

```bash
npm install -D vitest @playwright/test
npx playwright install chromium
trout run
```

```bash
trout run --scenario <id>     # one scenario
trout run --no-reset          # skip database isolation
trout certify                 # prove scenarios are deterministic
trout report                  # results and evidence from the last run
```

### Read `status` before `results`

| Status | Meaning |
|---|---|
| `pass` | Everything asserted held. |
| `fail` | A real assertion failed. This is a product signal. |
| `warning` | A flake, a blocked third-party call, or a timeout. |
| `inconclusive` | **Says nothing about the product.** Something prevented a reliable decision. |

An inconclusive run is never reported as a pass. If credentials are missing or
the toolchain is not installed, nothing executes and the run says so — a suite
that fails for those reasons has told you nothing about your application.

Only `assertion_failure` is a product signal. `auth_failure`,
`environment_failure`, `dependency_failure`, and `contract_mismatch` are about
the harness, and reporting them as regressions is how a suite loses credibility.

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

Hosts in `substitution.external` — populated by `trout scan` from the SDKs it finds
— are intercepted during browser tests. A test run cannot charge a card or email
a customer. An unmatched request fails loudly rather than passing through,
because a mock that silently matches nothing is how a suite starts reporting
green while testing nothing.

### Certification

```bash
trout certify --runs 3
```

Runs the suite repeatedly. Consistent passes → `certified`. Inconsistent →
`quarantined`, excluded from blocking. A consistent *failure* is neither: that
is a result about your product, not about the test.

Blocking eligibility requires certification **and** at least one assertion
backed by real evidence — a certified test built only on inference proves
nothing worth blocking on.

Note that `retries: 0` is set deliberately in the generated runner configs.
Retrying hides the flakiness it should surface; repeats belong in `trout certify`,
where inconsistency is the signal being measured.

---

## The web interface

```bash
pip install 'testtrout[web]'
trout web            # http://127.0.0.1:7411
```

Same state as the CLI, different view. Useful for the parts that are better
with a screen: working through the ranked gap list, reading a scenario's
assertions and provenance before approving it, and watching a run produce
results.

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

It exposes thirteen tools (`scan`, `surfaces`, `gaps`, `intent`, `probe`, `propose`,
`scenarios`, `approve`, `generate`, `run`, `certify`, `report`, `doctor`) and four resources
(`trout://surfaces`, `trout://gaps` via the tool, `trout://intent`, `trout://config`,
`trout://scenarios`).

The server is bound to **one project** at startup, so an agent cannot operate
on the wrong repository by passing a stray path. Tools return summaries;
resources carry the bulk, so a tool result does not crowd out an agent's
context.

---

## Performance

`trout intent` calls a reasoning model. Some default to their slowest setting —
`kimi-k3` defaults to `max`, which took over 150 seconds on a small app versus
38 at `low`. Intent capture asks for `low` automatically. To change it
globally:

```yaml
model:
  effort: low     # low | high | max — providers that don't know it ignore it
```

---

## Command reference

| Command | Needs | Purpose |
|---|---|---|
| `trout scan` | nothing | Analyse the repository |
| `trout surfaces` | a scan | List what was found, filterable |
| `trout doctor` | nothing | Diagnose what is missing |
| `trout init` | a scan | Configure deployments, auth, users |
| `trout providers --check` | config | Verify the model provider |
| `trout probe` | config + chromium | Observe the running deployment |
| `trout intent` | a scan + a model | Capture what matters |
| `trout gaps` | a scan | Rank the missing tests. No model. |
| `trout propose` | a scan | Draft scenario specifications |
| `trout scenarios` | — | List scenarios and their status |
| `trout approve` | drafts | Accept scenarios into the suite |
| `trout generate` | approved scenarios | Compile to test files |
| `trout run` | generated tests + toolchain | Execute the suite |
| `trout certify` | generated tests | Prove scenarios are deterministic |
| `trout report` | a run | Results and evidence |
| `trout web` | — | Local web interface |
| `trout mcp` | — | MCP server for coding agents |

Every command in the MVP is implemented. GitHub pull-request integration is the
next milestone.

---

## What gets committed

| Path | Committed | Why |
|---|---|---|
| `.trout/config.yaml` | yes | Configuration. Holds `env:` references, never secrets. |
| `.trout/surfaces.yaml` | yes | The surface map. Deterministic, so it diffs cleanly. |
| `.trout/intent.yaml` | yes | What matters, in your words. Edit it directly. |
| `.trout/scenarios/` | yes | Your test suite. Reviewing changes to it is the point. |
| `.trout/observed/` | no | A snapshot of a running system at one moment. |
| `.trout/runs/`, `.trout/evidence/` | no | Outputs. Large. |
| `.env` | **never** | Secrets. |
