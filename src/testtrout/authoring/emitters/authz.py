"""Authorization tests driven through the interface.

The concern is the same one that matters most in these applications — one user
must not reach another user's data — but it is now tested the way a real
attacker would encounter it: through the running application, signed in as an
ordinary account.

That choice is forced and, on reflection, better. Testing row-level security by
talking to the database needs an anon key and a project URL, which most teams
will not hand to a testing tool. Driving the interface needs a URL and two test
passwords. It is also more faithful: it exercises the policy *and* everything
the application layers on top of it, which is what a user actually meets.

The shape is a leak test, which needs no fixtures and no seeded ids: sign in as
one account, record what is visible, sign in as another, and require the two
sets to be disjoint. If B can see any of A's rows, something is wrong —
whether the cause is a missing policy, a permissive one, or a query that
forgot its filter.
"""

from __future__ import annotations

import re
from typing import ClassVar

from testtrout.authoring.base import EmittedFile, header, ts_string
from testtrout.domain.config import Config
from testtrout.domain.scenario import Scenario

SETUP_PATH = "tests/trout/_browser.ts"


class BrowserAuthorizationEmitter:
    """Emits Playwright authorization tests that need no database access."""

    id: ClassVar[str] = "authorization-browser"
    handles: ClassVar[tuple[str, ...]] = ("authorization",)

    def emit(self, scenario: Scenario, config: Config) -> EmittedFile:
        """Compile one authorization scenario into a cross-account leak test."""
        from testtrout.authoring.emitters.playwright import SETUP_SOURCE, _setup_source

        roles = [r for r in (scenario.role, scenario.other_role) if r]
        notes: list[str] = []
        if len(roles) < 2:
            notes.append(
                f"{scenario.id}: needs two test accounts. With one, the generated test can "
                "only check that the screen loads — which proves nothing about isolation."
            )

        first = roles[0] if roles else "owner"
        second = roles[1] if len(roles) > 1 else None
        screen = _screen_path(scenario)
        evidence = "\n".join(
            f"// {a.provenance.value}: {a.source}" for a in scenario.then if a.source
        )

        body = (
            _leak_test(scenario, screen, first, second)
            if second
            else _smoke_test(scenario, screen, first)
        )

        assert SETUP_SOURCE  # the helper is shared with the browser emitter
        return EmittedFile(
            path=f"tests/trout/authz/{scenario.id.replace(':', '_')}.spec.ts",
            content=header(scenario.id)
            + f"""
import {{ expect, test }} from '@playwright/test';

import {{ signIn, visibleRowText }} from '../_browser';

{evidence}
{body}
""",
            shared={SETUP_PATH: _setup_source(config)},
            notes=notes,
        )


def _screen_path(scenario: Scenario) -> str:
    """The route the test should visit."""
    from testtrout.domain.scenario import Action

    for step in scenario.when:
        if step.action is Action.NAVIGATE and step.target.url:
            return step.target.url
    return "/"


def _leak_test(scenario: Scenario, screen: str, first: str, second: str) -> str:
    """Two accounts, disjoint views."""
    return f"""test({ts_string(scenario.title)}, async ({{ page }}) => {{
  // What the first account can see.
  await signIn(page, {ts_string(first)});
  await page.goto({ts_string(screen)});
  const mine = await visibleRowText(page);

  // A second account must not see any of it.
  await page.context().clearCookies();
  await page.evaluate(() => {{ localStorage.clear(); sessionStorage.clear(); }});
  await signIn(page, {ts_string(second)});
  await page.goto({ts_string(screen)});
  const theirs = await visibleRowText(page);

  const leaked = theirs.filter((row) => mine.includes(row));
  expect(
    leaked,
    `${{leaked.length}} row(s) visible to {first} are also visible to {second} on ` +
      `{screen}. Either row-level security is missing or too permissive, or a query ` +
      `is not filtering by the signed-in user.`,
  ).toHaveLength(0);
}});"""


def _smoke_test(scenario: Scenario, screen: str, role: str) -> str:
    """One account only — honest about proving very little."""
    # Computed outside the f-string: Python 3.11 rejects backslashes inside one.
    pattern = re.escape(screen)
    title = f"{scenario.title} (screen loads)"
    return f"""// Only one test account is configured, so this cannot check isolation.
// Add a second account and regenerate for a real authorization test.
test({ts_string(title)}, async ({{ page }}) => {{
  await signIn(page, {ts_string(role)});
  await page.goto({ts_string(screen)});
  await expect(page).toHaveURL(new RegExp({ts_string(pattern)}));
}});"""
