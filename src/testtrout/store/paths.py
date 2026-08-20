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
    def search_path(cls, start: Path) -> list[Path]:
        """Directories to consider, from ``start`` up to a hard boundary.

        The walk **stops at the first repository root** — a directory holding
        ``.git`` — and never looks above it. Without that boundary, a project
        with no top-level ``package.json`` walks straight out of itself: one
        real run resolved the root to a folder containing 184 unrelated
        repositories, found a stray ``package.json`` there, and spent minutes
        parsing every checkout on the machine.

        The home directory and the filesystem root are also boundaries, so a
        directory outside any repository still cannot escape upward.
        """
        boundaries = {Path.home().resolve(), Path(start.anchor)}
        walk: list[Path] = []
        for candidate in (start, *start.parents):
            walk.append(candidate)
            if (candidate / ".git").exists() or candidate in boundaries:
                break
        return walk

    @classmethod
    def find(cls, start: Path | None = None) -> QaPaths:
        """Locate the project root by walking up from ``start``.

        Prefers a directory that already contains ``.trout/``, then the nearest
        ``package.json``, then the repository root. The search never crosses a
        repository boundary in any of those steps — a stray ``.trout/`` in a
        parent directory must not silently shadow the real project either.
        """
        current = (start or Path.cwd()).resolve()
        candidates = cls.search_path(current)

        for candidate in candidates:
            if (candidate / ".trout").is_dir():
                return cls(root=candidate)
        for candidate in candidates:
            if (candidate / "package.json").is_file():
                return cls(root=candidate)

        # No project marker anywhere inside the repository. The repository root
        # is a better guess than the current directory, and either way the
        # answer stays inside the boundary.
        repository_root = next((c for c in candidates if (c / ".git").exists()), None)
        return cls(root=repository_root or current)

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
