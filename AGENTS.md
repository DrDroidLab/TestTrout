# Notes for coding agents

Setting this tool up on a project? Follow [`docs/setup.md`](docs/setup.md) top
to bottom. Driving it once set up? The skill at
`.claude/skills/qa-agent/SKILL.md` covers the workflow.

## The short version

```bash
trout scan --json                  # 1. no API key, no network, safe on any repo
                                # 2. write a gitignored .env with credentials
trout init --yes --url <URL> --no-disposable --role owner --role member
trout doctor --json                # verify; exits non-zero and says what is missing
trout probe --role owner --json    # needs: playwright install chromium
trout intent --draft --json        # or --describe "..." / --from FILE
trout gaps --json                  # deterministic; no model, no key
trout propose --json -n 5          # drafts only — never auto-approve
trout scenarios --json
trout approve <id>                 # only when the user has chosen
trout generate --json
trout run --json                   # read `status` before `results`
```

Or use the MCP server instead of shelling out:

```bash
pip install 'testtrout[mcp]' && trout mcp /path/to/project
```

There is also a web interface (`trout web`) over the same state, if the user
prefers a screen for reviewing gaps and approving scenarios.

## Rules

1. **Always use `--json`.** Every command supports it. Never parse rendered
   output.
2. **`trout doctor --json` first** whenever something fails. It reports exactly
   what is missing, so guessing is never necessary.
3. **Default to `--no-disposable`.** Only mark a deployment disposable when the
   user confirms its data can be destroyed. Writes against a non-disposable
   deployment are blocked at the network layer — that guard is the reason this
   is safe to point at production, so do not disable it to "make the probe
   work".
4. **Never write a secret into `.trout/config.yaml`.** It is committed. Use
   `env:NAME` references and put values in `.env`.
5. **Two test users minimum.** Authorization tests need a second account to be
   possible at all.
6. **Surface the `table_without_rls` warning to the user immediately.** It means
   data is world-writable through the browser. It is usually news.
7. **Approval belongs to the user.** Propose; do not approve on their behalf.
8. **Quote `criticality_reasons` and gap `reasons` rather than re-deriving your
   own ranking.** They are deterministic and auditable; your restatement is
   not.
9. **Never edit generated test files.** They are build artifacts, overwritten
   on every `trout generate`. Edit the scenario `.yaml` and regenerate.
10. **A scenario with open questions cannot be approved, and that is correct.**
   Approving it yields a test that passes vacuously. Surface the question to
   the user instead.
11. **On a run, read `status` first.** `inconclusive` means the run says
   nothing about the product — do not report it as a pass or a failure. Only
   `assertion_failure` is a product signal; `auth_failure` and
   `environment_failure` are about the harness.
12. **Never disable database isolation or the substitution boundary to make a
   run go green.** Both exist to stop a test run destroying real data or
   charging a real card.
13. **Report blocked gaps as work, not as failures.** `needs_two_roles`,
   `unreachable`, and `unresolved_table` each name a specific thing the user
   can fix in under a minute.

## Working on the tool itself

```bash
uv sync --all-extras --dev
uv run pytest          # full suite, offline, no API key — keep it that way
uv run ruff check . && uv run mypy
```

`testtrout.analysis` must never call a model. See
[`docs/adr/0003-deterministic-core.md`](docs/adr/0003-deterministic-core.md).
