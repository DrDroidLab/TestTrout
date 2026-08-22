"""Working out what to do, from what was read and what was seen.

Everything in this package is deterministic. No module here calls a model, and
none of them ask a person what the product is supposed to do — the two rules
that keep the answer reproducible and the queue of questions short.

:mod:`~testtrout.planning.candidates` decides what can be tested.
:mod:`~testtrout.planning.facts` decides what is still missing.
:mod:`~testtrout.planning.overview` describes the project in product language.
:mod:`~testtrout.planning.tests_view` says what each existing test is doing.
"""
