"""A local web interface over the same ``.trout/`` state the CLI uses.

There is no separate database and no hosted component. The web app, the CLI,
and the MCP server all read and write the same files, so there is never a
question of which view is authoritative — and a developer can hand-edit any of
it while the page is open.

It exists for the parts that are genuinely better with a screen: working
through a ranked gap list, reading a scenario's assertions and provenance
before approving it, watching a run produce results, and inspecting the
evidence behind a failure.
"""
