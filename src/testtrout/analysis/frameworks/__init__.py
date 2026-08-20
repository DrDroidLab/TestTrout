"""Framework adapters: how to find screens and endpoints in a given stack.

Adding support for a new frontend framework means implementing
:class:`~testtrout.analysis.frameworks.base.FrameworkAdapter` and registering it
under the ``testtrout.frameworks`` entry point group. Nothing else in the
codebase needs to change — see ``docs/adapters.md``.
"""
