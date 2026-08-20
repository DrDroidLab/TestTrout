"""Authentication adapters.

Every application in the target category sits behind a login wall, which makes
this the first real obstacle rather than a detail. Getting a durable session
into the browser is what separates "the tool works on my app" from "the tool
loaded the login page five times".
"""

from testtrout.deployment.auth.base import AuthAdapter, AuthOutcome, load_adapters

__all__ = ["AuthAdapter", "AuthOutcome", "load_adapters"]
