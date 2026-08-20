"""Executing the generated suite.

Tests run through the *project's own* toolchain — `npx playwright test`,
`npx vitest run` — not through a bespoke runner. That is a deliberate
constraint: a developer must be able to run their tests without this tool
installed, or the tests are not really theirs.

What this package adds around that is the part a raw test runner cannot do:
returning the database to a known state between runs, refusing to let a test
reach a real payment processor, classifying a failure as a product problem
versus an environment one, and proving a test is deterministic before it is
allowed to block anything.
"""
