"""Choosing which scenarios a change can affect.

Running the whole suite on every change is the fallback, not the strategy. This
answers the question a pull request actually poses: *given these changed files,
which of my tests could possibly be affected?*

The chain is entirely deterministic and every link is inspectable:

    changed file → surfaces defined in that file (from the scan)
                 → scenarios that assert on those surfaces
                 → plus anything reaching them, and anything critical

No model, no heuristics over file names. If a scenario is selected, the reason
is a fact about the code.

**What this does not do yet:** the index is built from what each scenario
*declares* it covers, not from what it was *observed* to touch at runtime. A
scenario that incidentally exercises a third table will not be selected by a
change to that table. That is a known blind spot, reported rather than hidden,
and it is why :func:`select` always offers a full-suite fallback.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import Criticality, ScanResult


@dataclass
class Selection:
    """Which scenarios to run, and why each was chosen."""

    scenarios: list[str] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    changed_surfaces: list[str] = field(default_factory=list)
    uncovered_surfaces: list[str] = field(default_factory=list)
    """Surfaces the change touches that no scenario protects. The most
    important output here: it is the gap the change just walked into."""
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Whether nothing was selected."""
        return not self.scenarios


class GitUnavailableError(RuntimeError):
    """The repository could not be read to determine what changed."""


def changed_files(root: Path, ref: str) -> list[str]:
    """Files changed against a git ref.

    Raises:
        GitUnavailableError: if git is missing, this is not a repository, or
            the ref does not exist. Guessing at a diff would be worse than
            saying so.
    """
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{ref}...HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitUnavailableError(f"could not run git: {exc}") from exc

    if completed.returncode != 0:
        raise GitUnavailableError(
            f"git diff against {ref!r} failed: "
            f"{(completed.stderr or '').strip().splitlines()[-1] if completed.stderr else ''}"
        )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def surfaces_in(scan: ScanResult, files: set[str]) -> list[str]:
    """Surfaces defined in any of the given files.

    Migrations count for the tables they define: a changed policy file affects
    every scenario asserting on that policy, even though no TypeScript moved.
    """
    return sorted(surface.id for surface in scan.all_surfaces() if surface.location.file in files)


def select(
    scan: ScanResult,
    scenarios: ScenarioIndex,
    files: list[str],
    include_critical: bool = True,
) -> Selection:
    """Pick the scenarios a set of changed files could affect.

    Args:
        scan: The surface map, which says where each surface is defined.
        scenarios: Every scenario; only generated, accepted ones are eligible.
        files: Repository-relative paths that changed.
        include_critical: Always include critical scenarios regardless of the
            diff. Cheap insurance against the declared-coverage blind spot: the
            handful of tests protecting the business should run every time.
    """
    selection = Selection(changed_files=sorted(files))
    changed = set(files)

    runnable = [
        s
        for s in scenarios.scenarios
        if s.emitted_to and s.status in {ScenarioStatus.APPROVED, ScenarioStatus.CERTIFIED}
    ]
    if not runnable:
        selection.notes.append(
            "No generated scenarios to select from. Approve and generate some first."
        )
        return selection

    touched = surfaces_in(scan, changed)
    selection.changed_surfaces = touched

    # A screen is affected by a change to anything it reaches, not only to its
    # own file — which is where the interesting breakage usually is.
    reachable: set[str] = set(touched)
    for screen in scan.screens:
        if screen.id in touched or set(screen.reaches) & set(touched):
            reachable.add(screen.id)
            reachable.update(screen.reaches)

    for scenario in runnable:
        why: list[str] = []
        overlap = sorted(set(scenario.surfaces) & reachable)
        if overlap:
            why.append(f"asserts on changed surface(s): {', '.join(overlap[:3])}")
        elif include_critical and scenario.criticality is Criticality.CRITICAL:
            why.append("critical scenario — always run")
        if why:
            selection.scenarios.append(scenario.id)
            selection.reasons[scenario.id] = why

    protected = {sid for s in runnable for sid in s.surfaces}
    selection.uncovered_surfaces = [s for s in touched if s not in protected]

    if selection.uncovered_surfaces:
        selection.notes.append(
            f"{len(selection.uncovered_surfaces)} changed surface(s) have no scenario "
            "protecting them. This change is not covered — run `trout gaps` to see what "
            "is missing."
        )
    if not touched and changed:
        selection.notes.append(
            "None of the changed files define a known surface. Either the change is "
            "outside the tested area, or the scan is stale — try `trout scan`."
        )
    selection.notes.append(
        "Selection uses each scenario's declared surfaces. A scenario that "
        "incidentally touches other code will not be selected by a change to it; "
        "run the full suite when that matters."
    )
    return selection


def scenario_by_id(scenarios: ScenarioIndex, scenario_id: str) -> Scenario | None:
    """Convenience lookup used by the runner."""
    return scenarios.get(scenario_id)
