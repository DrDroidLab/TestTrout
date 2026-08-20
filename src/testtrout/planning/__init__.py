"""Turning analysis into a plan.

Deliberately split by determinism. :mod:`testtrout.planning.gaps` is pure
computation over the scan, the probe, and stated intent — it never calls a
model, so a ranked plan is reproducible and every rank can be interrogated.
:mod:`testtrout.planning.intent` is the one part that does use a model, and it
uses it for the only thing a model is genuinely better at here: turning a
developer's prose into structure, and asking a good follow-up question.
"""
