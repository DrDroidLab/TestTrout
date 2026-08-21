"""Project detection: framework, backend, auth provider, and path aliases.

Everything here is evidence-based. Each detection records *why* it fired in
``ProjectInfo.detected_from``, because a wrong framework guess silently
produces an empty scan, and a user staring at "0 screens found" needs to see
what the tool believed about their project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from testtrout.domain.surface import ProjectInfo

# Directories that never contain first-party application source.
IGNORED_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".next",
        "dist",
        "build",
        "out",
        "coverage",
        ".turbo",
        ".vercel",
        ".venv",
        "__pycache__",
        ".trout",
        "playwright-report",
        "test-results",
    }
)

SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx"})


@dataclass
class ProjectContext:
    """Everything the scanner needs to know about a repository's layout."""

    root: Path
    info: ProjectInfo
    aliases: dict[str, list[str]] = field(default_factory=dict)
    """tsconfig ``paths`` mapping, e.g. ``{'@/*': ['src/*']}``."""
    dependencies: dict[str, str] = field(default_factory=dict)

    def source_files(self) -> list[Path]:
        """All first-party source files, in stable sorted order.

        Sorted so that scan output is deterministic, which is what makes the
        golden-file tests meaningful.
        """
        found: list[Path] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if any(part in IGNORED_DIRS for part in path.relative_to(self.root).parts):
                continue
            if path.name.endswith((".d.ts", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
                continue
            found.append(path)
        return sorted(found)


def _read_json(path: Path) -> dict[str, object]:
    """Read JSON, tolerating the trailing commas and comments tsconfig allows."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return dict(json.loads(text))
    except json.JSONDecodeError:
        stripped = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("//")
        )
        stripped = stripped.replace(",}", "}").replace(",]", "]")
        try:
            return dict(json.loads(stripped))
        except json.JSONDecodeError:
            return {}


def _package_manager(root: Path) -> str | None:
    """Infer the package manager from the lockfile present."""
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
    ):
        if (root / lockfile).exists():
            return manager
    return None


def _aliases(root: Path) -> dict[str, list[str]]:
    """Extract tsconfig path aliases, resolved relative to the repository root.

    Lovable and v0 projects lean heavily on ``@/`` imports, so without this the
    module graph is disconnected and screens appear to reach no data at all.
    """
    for name in ("tsconfig.json", "tsconfig.app.json", "jsconfig.json"):
        config = _read_json(root / name)
        options = config.get("compilerOptions")
        if not isinstance(options, dict):
            continue
        paths = options.get("paths")
        if not isinstance(paths, dict):
            continue
        base = options.get("baseUrl")
        prefix = str(base).strip("./") if isinstance(base, str) else ""
        resolved: dict[str, list[str]] = {}
        for pattern, targets in paths.items():
            if not isinstance(targets, list):
                continue
            resolved[str(pattern)] = [
                f"{prefix}/{t!s}".lstrip("/") if prefix else str(t).lstrip("./") for t in targets
            ]
        if resolved:
            return resolved
    return {}


# Where a frontend lives when the repository root is not itself the app.
# A monorepo with `frontend/` beside `backend/` is a common shape, and scanning
# the root of one otherwise finds nothing at all.
APP_SUBDIRECTORIES = ("frontend", "web", "client", "ui", "www", "site", "app")


def find_app_root(root: Path) -> Path | None:
    """Locate the frontend when it is not at the repository root.

    Returns the subdirectory holding a JavaScript project, or ``None`` when the
    root itself is the app (or nothing recognisable is there). Only well-known
    names and one level of `apps/` or `packages/` are searched — walking the
    whole tree would find every fixture and example in the repository.
    """
    if (root / "package.json").is_file():
        return None

    for name in APP_SUBDIRECTORIES:
        if (root / name / "package.json").is_file():
            return root / name

    for workspace in ("apps", "packages"):
        parent = root / workspace
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if (child / "package.json").is_file():
                return child
    return None


def detect_project(root: Path) -> ProjectContext:
    """Identify the framework, backend, and auth provider of a repository.

    Detection is intentionally conservative. When the framework cannot be
    established the scan still runs — Supabase calls, migrations, and policies
    are found regardless of the frontend framework — but no screens are
    enumerated, and the reason is reported.
    """
    package = _read_json(root / "package.json")
    deps: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        raw = package.get(section)
        if isinstance(raw, dict):
            deps.update({str(k): str(v) for k, v in raw.items()})

    evidence: list[str] = []

    framework = "unknown"
    if "next" in deps:
        framework = (
            "nextjs-app" if (root / "app").is_dir() or (root / "src/app").is_dir() else "nextjs"
        )
        evidence.append("package.json: next")
    elif "vite" in deps and "react" in deps:
        framework = "vite-react"
        evidence.append("package.json: vite + react")
    elif "react" in deps:
        framework = "vite-react"
        evidence.append("package.json: react (assuming vite-style SPA)")

    backend = None
    if "@supabase/supabase-js" in deps:
        backend = "supabase"
        evidence.append("package.json: @supabase/supabase-js")
    elif (root / "supabase").is_dir():
        backend = "supabase"
        evidence.append("supabase/ directory present")

    auth = None
    if any(k.startswith("@clerk/") for k in deps):
        auth = "clerk"
        evidence.append("package.json: @clerk/*")
    elif "next-auth" in deps or "@auth/core" in deps:
        auth = "nextauth"
        evidence.append("package.json: next-auth")
    elif backend == "supabase":
        auth = "supabase"
        evidence.append("inferred from Supabase backend")

    info = ProjectInfo(
        root=str(root),
        framework=framework,
        backend=backend,
        auth=auth,
        package_manager=_package_manager(root),
        typescript=(root / "tsconfig.json").exists(),
        detected_from=evidence,
    )
    return ProjectContext(root=root, info=info, aliases=_aliases(root), dependencies=deps)
