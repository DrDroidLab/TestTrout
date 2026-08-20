"""The only component permitted to call a model provider.

Everything model-related funnels through :class:`~testtrout.llm.gateway.Gateway`.
That single choke point is what makes three properties true at once:

* Prompts live in versioned files under ``prompts/`` rather than inline in
  business logic, so they can be reviewed and improved by contributors who are
  not reading Python.
* Every request and response is recorded to disk as a cassette, so a run can be
  replayed and audited.
* The project's own test suite runs offline with no API key, which is what
  keeps contribution cheap and CI honest.

If you are adding a feature that needs a model, call the gateway. Do not import
a provider SDK anywhere else.
"""

from testtrout.llm.gateway import Gateway, GatewayError

__all__ = ["Gateway", "GatewayError"]
