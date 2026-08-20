"""Keeping ``.trout/config.yaml`` in step with the latest scan.

Shared by the CLI, the web interface, and the MCP server. It lives here rather
than in any one of them because the three must not drift: a scan triggered from
a web page has to leave exactly the state a scan from the terminal would, or
"which view is authoritative" becomes a real question with a bad answer.
"""

from __future__ import annotations

from testtrout.domain.config import Config, ExternalRule
from testtrout.domain.surface import ScanResult


def apply_scan(config: Config, result: ScanResult) -> Config:
    """Record what a scan discovered, preserving anything the user set.

    Two things are synced:

    *Project shape* — framework, backend, and auth provider, so later commands
    pick the right adapters without asking again.

    *The substitution boundary* — every side-effecting third-party host the scan
    found. Recording them means a test run cannot reach a real payment
    processor unless someone deliberately removed the entry. Existing entries
    are never touched, since a user may have edited or extended them.
    """
    config.project.framework = result.project.framework
    config.project.backend = result.project.backend
    config.project.auth = result.project.auth

    known = {rule.match for rule in config.substitution.external}
    for external in result.externals:
        if not external.side_effecting:
            continue
        for host in external.hosts:
            if host not in known:
                config.substitution.external.append(ExternalRule(name=external.vendor, match=host))
                known.add(host)
    return config
