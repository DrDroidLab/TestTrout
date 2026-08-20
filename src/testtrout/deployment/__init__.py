"""Connecting to a running deployment and observing it.

This package is where the tool stops reading files and starts touching a real
system, so it is also where the safety rules live. Two are absolute:

* A mutating request is never allowed to reach a deployment that is not
  explicitly marked ``disposable``. This is enforced at the network layer by
  blocking the request, not by asking the caller to be careful.
* Response bodies are never persisted. Shape and status are enough to reconcile
  against the static scan, and storing bodies is the fastest route to leaking
  customer data into a file someone commits.
"""
