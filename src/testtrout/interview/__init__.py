"""Capturing what only the developer can tell us.

Two rules shape everything in this package:

*Never ask for a secret value.* Questions ask for the **name** of an
environment variable, never its contents. That is what makes it structurally
impossible to write a credential into ``.trout/config.yaml``, which is a committed
file — a rule enforced by the interface rather than by a warning in the docs.

*Never ask twice.* Answers persist immediately, so an interrupted setup resumes
where it stopped instead of restarting.
"""

from testtrout.interview.session import Interview

__all__ = ["Interview"]
