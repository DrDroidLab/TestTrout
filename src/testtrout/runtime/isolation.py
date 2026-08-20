"""Returning the database to a known state between runs.

This replaced environment orchestration as the primary technical risk once the
tool started targeting deployments that already exist. A suite that runs
against dirty data produces failures nobody can reproduce, and the second time
that happens the team stops believing the suite.

Three strategies, honestly ranked. ``local_reset`` gives real isolation.
``scoped_seed`` gives none and says so — it is the only option against a shared
deployment, and the caller needs to know the results carry that caveat rather
than discovering it later.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from testtrout.domain.config import Config, IsolationStrategy

RESET_TIMEOUT_SECONDS = 180


@dataclass
class IsolationResult:
    """What isolation was actually achieved, as opposed to requested."""

    strategy: IsolationStrategy
    applied: bool
    detail: str = ""
    caveats: list[str] = field(default_factory=list)


def prepare(config: Config, root: Path) -> IsolationResult:
    """Put the database into a known state before a run.

    Never raises. A failed reset is reported as an unapplied strategy with a
    caveat, so the run can proceed with results the caller knows to discount —
    which beats aborting and telling them nothing.
    """
    strategy = config.supabase.isolation

    if strategy is IsolationStrategy.LOCAL_RESET:
        return _local_reset(root)

    if strategy is IsolationStrategy.BRANCH:
        return IsolationResult(
            strategy=strategy,
            applied=False,
            detail="database branching is not implemented yet",
            caveats=[
                "Branch isolation was requested but is not implemented. The run proceeded "
                "against whatever state the database was already in, so failures may be "
                "caused by leftover data rather than by the product."
            ],
        )

    return IsolationResult(
        strategy=strategy,
        applied=True,
        detail="scoped seed: no reset performed",
        caveats=[
            "No database reset was performed. Scenarios run against existing data, so a "
            "failure may reflect the state of the database rather than a change in the "
            "product. Use local_reset for real isolation."
        ],
    )


def _local_reset(root: Path) -> IsolationResult:
    """Reset a local Supabase stack via its CLI."""
    strategy = IsolationStrategy.LOCAL_RESET

    if shutil.which("supabase") is None:
        return IsolationResult(
            strategy=strategy,
            applied=False,
            detail="the supabase CLI is not on PATH",
            caveats=[
                "local_reset was requested but the supabase CLI was not found, so the "
                "database was not reset. Install it, or switch "
                "`supabase.isolation` to scoped_seed and accept the caveat."
            ],
        )

    try:
        completed = subprocess.run(
            ["supabase", "db", "reset", "--local"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=RESET_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return IsolationResult(
            strategy=strategy,
            applied=False,
            detail=f"reset failed: {exc}",
            caveats=["The database could not be reset; results may reflect leftover data."],
        )

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return IsolationResult(
            strategy=strategy,
            applied=False,
            detail=f"supabase db reset exited {completed.returncode}: {tail[-1] if tail else ''}",
            caveats=[
                "The database reset failed, so this run may be affected by leftover data. "
                "Check that a local Supabase stack is running (`supabase start`)."
            ],
        )

    return IsolationResult(
        strategy=strategy, applied=True, detail="local database reset and migrations applied"
    )
