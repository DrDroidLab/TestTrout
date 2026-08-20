"""Reading and writing scenario specifications.

One file per scenario under ``.trout/scenarios/``. A directory of small files
rather than one large index, so that approving a scenario produces a one-file
diff that a reviewer can actually read.
"""

from __future__ import annotations

from pathlib import Path

from testtrout.domain.scenario import Scenario, ScenarioIndex
from testtrout.store.yaml_io import read_model, write_model


def _filename(scenario_id: str) -> str:
    """Filesystem-safe name for a scenario id."""
    return scenario_id.replace(":", "_").replace("/", "-") + ".yaml"


def path_for(directory: Path, scenario_id: str) -> Path:
    """Where a scenario is stored."""
    return directory / _filename(scenario_id)


def save(directory: Path, scenario: Scenario) -> Path:
    """Write one scenario, creating the directory if needed."""
    destination = path_for(directory, scenario.id)
    write_model(destination, scenario)
    return destination


def load_all(directory: Path) -> tuple[ScenarioIndex, list[str]]:
    """Load every scenario, reporting any that could not be read.

    A malformed file is reported rather than raised: one hand-edit that broke
    the schema should not make every other scenario invisible.
    """
    index = ScenarioIndex()
    problems: list[str] = []
    if not directory.is_dir():
        return index, problems

    for file in sorted(directory.glob("*.yaml")):
        try:
            index.scenarios.append(read_model(file, Scenario))
        except Exception as exc:
            problems.append(f"{file.name}: {exc}")
    return index, problems
