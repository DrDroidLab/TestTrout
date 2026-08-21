# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Testing no longer requires database credentials.** Tests reach the app over
  HTTP and through a browser; sign-in drives the application's own login form,
  which `trout probe` locates once and records. A URL and two test accounts are
  now the whole requirement.
- Authorization tests moved from Supabase queries to the interface: signed in as
  two accounts, whatever one can see the other must not. This exercises the
  policy *and* everything the app layers on top of it.
- Supabase settings are optional, and only enable resetting the database between
  runs. The Setup panel says so.
- **Scan now analyses, probes, and reports what is untested** — one action rather
  than three, since "what does this app do" and "what is untested about it" are
  the same question asked twice.
- **Run generates any approved scenario that has no code yet**, then executes.
  Pressing a second button to compile what you already approved was ceremony,
  not safety.

### Added

- **Credential discovery.** The scan now reports which environment variables the
  application reads, what each appears to be for, and where it is referenced —
  rather than asking a developer to re-derive something the code states plainly.
  Framework prefixes (`VITE_`, `NEXT_PUBLIC_`, `REACT_APP_`) are normalised, so
  one rule matches every spelling of the same requirement.
- **Graceful degradation.** `trout plan` and the Setup tab report each capability
  as ready or blocked, and every blocked one names a single concrete missing
  thing. A URL alone already unlocks probing and API tests.
- **Full configuration from the interface.** Deployments, Supabase, test
  accounts, model provider, and isolation are all editable in the Setup tab.
- Secret values are written to a gitignored `.env`, which is created if needed;
  committed configuration holds only `env:NAME` references, and a literal secret
  typed into a config field is rejected rather than saved.
- Making a deployment writable requires explicit confirmation — the one setting
  that can destroy real data is never incidental.
- `trout plan` and `trout config` give the CLI parity with everything the
  interface can do.
- Linking now behaves identically from the CLI and the API; they had drifted, and
  the symptom was a settings page reporting "0 policies" for an unscanned repo.

- **`trout up` — the application.** Storage, a background worker, and the web
  interface, started with one command. SQLite under `~/.testtrout` and an
  in-process worker: no Docker, no daemon, nothing to install first.
- **Repository linking.** `trout link <path>` for a folder you already have,
  which is never modified, or `trout link --github owner/name` to clone with a
  personal access token. The interface can do both.
- GitHub tokens are read from `GITHUB_TOKEN`, then the `gh` CLI, then a file at
  `~/.testtrout/github` with owner-only permissions — never the database, and
  never embedded in a git remote URL.
- A SQLite-backed job queue. Jobs are serialised per repository, because two
  runs against one database interfere in ways neither can explain; different
  repositories proceed in parallel.
- Jobs interrupted by a worker crash are failed rather than retried — a
  half-finished run may have left state behind.
- The web interface is now multi-repository, with a picker, run history, and an
  activity view.
- `fastapi` and `uvicorn` moved into the base install, since `trout up` is the
  primary entry point. The `web` extra is gone; `probe` and `mcp` remain.

- **Phase 6 — web interface.** `trout web` serves a local page over the same
  `.trout/` state: coverage, the ranked gap list with its reasoning, scenario
  review and approval, run history with evidence, and a live log over
  server-sent events.
- Binds to loopback by default, and there is deliberately no endpoint that can
  mark a deployment disposable — that stays a deliberate edit to a committed
  file.
- One background action at a time; concurrent runs against one database produce
  failures neither can account for.
- Config synchronisation after a scan is now shared by the CLI, the web
  interface, and the MCP server, so a scan leaves identical state whichever
  triggered it.

- **Phase 5 — execution.** `trout run` executes the generated suite through the
  project's own toolchain, `trout certify` proves scenarios are deterministic, and
  `trout report` shows results and evidence.
- Failures are classified deterministically: assertion failure, auth failure,
  environment failure, dependency failure, contract mismatch, timeout, or flake.
  **Only an assertion failure is a product signal**, so an unreachable database
  is never reported as a regression.
- An inconclusive run is never upgraded to a pass. Missing credentials or an
  uninstalled toolchain stop the run before anything executes, because a suite
  that fails for those reasons has said nothing about the application.
- Database isolation with three honest strategies: `local_reset` (real),
  `scoped_seed` (none, and every run reports the caveat), and `branch`
  (unimplemented, and says so).
- Third-party hosts are intercepted during browser tests, so a run cannot charge
  a card or email a customer. Hosts are populated by `trout scan`.
- Certification promotes only on a clean sweep; inconsistent scenarios are
  quarantined. A consistent failure is neither — that is a result about the
  product.
- Scoped Playwright and Vitest configs written to `tests/qa/`, never touching an
  existing config at the project root. `retries: 0` deliberately.
- Every failure carries a one-command reproduction and, for browser tests, a
  Playwright trace.
- MCP server gains `run`, `certify`, and `report` (13 tools total).

- **Phase 4 — scenario authoring.** `trout propose` drafts scenario specifications,
  `trout approve` accepts them, `trout generate` compiles them into runnable tests.
- Scenarios are language-agnostic YAML, committed and hand-editable; generated
  code is a build artifact that is overwritten on every run.
- Scenarios are built **deterministically**; a model only refines wording,
  chooses which observed elements to assert on, and reports what it could not
  determine. `--no-model` produces usable scenarios with no API key.
- A selector the model returns is dropped unless the prober actually observed
  it, so a generated test can never target an element that does not exist.
- Ownership columns are derived from the policy text (`auth.uid() = id`) rather
  than guessed from column names, which no name-based heuristic would find. A
  policy that reaches through a join is admitted as unknown rather than guessed.
- Emitters for authorization (Vitest + supabase-js), browser journeys
  (Playwright), and endpoints (Vitest), registered under `testtrout.emitters`.
- A scenario with open questions cannot be approved — approving it would
  produce a test that passes vacuously.
- Blocking eligibility requires certification **and** at least one assertion
  backed by real evidence, not inference.
- Accepted scenarios are authoritative coverage and close their gaps, rather
  than being re-proposed or re-detected heuristically.
- **`trout mcp`** — an MCP server exposing ten tools and four resources, scoped to
  one project so an agent cannot operate on the wrong repository.

- **Phase 3 — intent and gaps.** `trout intent` captures what the product does and
  what must never break, starting from a draft of the codebase rather than a
  blank page. `trout gaps` ranks the tests the application is missing.
- Gap ranking is fully deterministic — no model — and every rank is the sum of
  named contributions, so a ranking can be argued with rather than merely
  overridden.
- Stated intent raises a surface's criticality above the scanner's prior and
  records why; it can never lower one.
- Blocked gaps name what is missing: a second test-user role, an unreachable
  route, or an unresolved table name.
- Heuristic detection of existing tests, reported as *possible* coverage only —
  a false positive would silently mark a critical surface protected.
- `model.effort` setting for reasoning models. Interactive commands request low
  effort by default: kimi-k3 defaults to `max`, which took >150s versus 38s.

- **Phase 2 — deployment connection.** `trout init` configures deployments, auth,
  and test users; resumable, and fully scriptable with `--yes`. `trout probe`
  loads every known route in a real browser and records what actually happens.
- Mutating requests are blocked at the network layer against any deployment not
  explicitly marked `disposable` — enforced below navigation, because these
  apps write on mount.
- Auth adapters for Supabase (via the Auth REST API plus session injection,
  which works on an app the tool has never seen) and generic form login for
  Clerk and NextAuth. Registered under the `testtrout.auth` entry point group.
- Reconciliation of the static scan against observed behaviour: unreachable
  screens, policy denials, undeclared tables and third-party hosts, unexercised
  reads, console errors, and screens with no stable selectors.
- Selector candidate extraction ranked by durability (test id > role > label >
  text > css), as groundwork for test authoring.
- Route parameters resolved from row ids harvested during the probe, so
  `/orders/:id` is actually visitable.
- `.env` loading from the project root, so credentials stay out of the committed
  config.
- `trout providers [--check]` to verify a model provider with one live call.

- **Phase 1 — stack comprehension.** `trout scan` analyses a React/Vite or Next.js
  App Router repository and produces a complete surface map: screens, Supabase
  data operations, Route Handlers, Server Actions, RLS policies, database
  schema, and third-party dependencies. Fully deterministic — no API key, no
  network, no model calls.
- Screen-to-data reachability through the module import graph, so a route
  inherits the risk of everything it can trigger.
- Explainable criticality scoring: every score carries the facts that produced
  it.
- Scan warnings for blind spots: tables written from client code with no
  row-level security, and Supabase calls with unresolvable table names.
- `trout surfaces` with filtering by kind and minimum criticality.
- `trout doctor` for configuration and dependency diagnostics.
- `--json` on every command, for agent and script consumption.
- Model gateway supporting Anthropic, OpenAI, and Kimi, with cassette record
  and replay so the test suite runs offline.
- Claude Code skill at `.claude/skills/qa-agent/`.
- Framework adapter protocol with entry-point registration, so a new stack can
  be supported from a separate package.

[Unreleased]: https://github.com/DrDroidLab/TestTrout
