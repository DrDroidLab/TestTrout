"""Command-line interface.

This layer is presentation only. It parses arguments, calls into the library,
and renders results. Any logic that a test would want to exercise belongs in
``testtrout.analysis`` or a sibling package, not here.

Every command supports ``--json`` so that an agent — Claude Code included — can
drive the tool without parsing rendered terminal output.
"""
