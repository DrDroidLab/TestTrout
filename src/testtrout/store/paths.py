"""Layout of the ``.trout/`` directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QaPaths:
    """Resolved paths inside a project's ``.trout/`` directory.

    Committed to version control: ``config.yaml``, ``surfaces.yaml``,
    ``intent.yaml``, ``scenarios/``, ``contracts/``. These describe what the
    suite *is*, and reviewing a change to them in a pull request is a feature.

    Not committed (see ``.gitignore``): ``runs/``, ``evidence/``, ``.cache/``.
    These are outputs, and they are large.
    """

    root: Path

    @classmethod
    def find(cls, start: Path | None = None) -> QaPaths:
        """Locate the project root by walking up from ``start``.

        Prefers a directory that already contains ``.trout/``; otherwise falls
        back to the nearest ``package.json``, which is where a first run should
        create it.
        """
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            if (candidate / ".trout").is_dir():
                return cls(root=candidate)
        for candidate in (current, *current.parents):
            if (candidate / "package.json").is_file():
                return cls(root=candidate)
        return cls(root=current)

    @property
    def dir(self) -> Path:
        """The ``.trout/`` directory itself."""
        return self.root / ".trout"

    @property
    def config(self) -> Path:
        """Repository configuration."""
        return self.dir / "config.yaml"

    @property
    def surfaces(self) -> Path:
        """Output of the most recent scan."""
        return self.dir / "surfaces.yaml"

    @property
    def intent(self) -> Path:
        """Captured product intent."""
        return self.dir / "intent.yaml"

    @property
    def scenarios(self) -> Path:
        """Approved scenario specifications."""
        return self.dir / "scenarios"

    @property
    def contracts(self) -> Path:
        """Third-party substitution contracts."""
        return self.dir / "contracts"

    @property
    def observed(self) -> Path:
        """Probe observations. Not committed.

        They describe a running system at one moment, not the suite itself.
        """
        return self.dir / "observed"

    @property
    def runs(self) -> Path:
        """Run records. Not committed."""
        return self.dir / "runs"

    @property
    def evidence(self) -> Path:
        """Traces, screenshots, and logs. Not committed."""
        return self.dir / "evidence"

    @property
    def cache(self) -> Path:
        """Model cassettes and other regenerable data. Not committed."""
        return self.dir / ".cache"

    def ensure(self) -> None:
        """Create the directory structure if it does not exist."""
        for path in (
            self.dir,
            self.scenarios,
            self.contracts,
            self.runs,
            self.evidence,
            self.cache,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def initialised(self) -> bool:
        """Whether this project has been configured."""
        return self.config.is_file()
