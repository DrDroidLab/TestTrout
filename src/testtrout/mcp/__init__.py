"""An MCP server exposing the tool to coding agents.

Every capability is already available through the CLI with ``--json``, so this
exists for the cases where shelling out is not the right shape: agent hosts
with no shell, and agents that benefit from typed tool schemas and
discoverability rather than from remembering flags.

Two design rules keep it useful rather than merely available:

*Tools return decisions, not dumps.* A tool result lands directly in an agent's
context window. Returning an entire surface map would crowd out the reasoning
it was supposed to support, so tools return summaries and the bulk is available
as a resource the agent can read on demand.

*No logic lives here.* Every handler calls the same library function the CLI
calls. A behaviour that differs between the two interfaces is a bug.
"""
