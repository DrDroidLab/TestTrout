"""Project root discovery.

These are regression tests for a bug that mattered: an unbounded upward walk
resolved the root to a directory holding 184 unrelated repositories, found a
stray ``package.json`` there, and spent minutes parsing every checkout on the
machine before it was killed.
"""

from __future__ import annotations

from pathlib import Path

from testtrout.store import QaPaths


def _repo(tmp_path: Path, name: str = "app") -> Path:
    """A directory that looks like a git repository."""
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


def test_the_walk_stops_at_a_repository_boundary(tmp_path: Path):
    """The bug. A parent's package.json must not capture a nested repository."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")  # stray, outside
    repo = _repo(tmp_path)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)

    assert QaPaths.find(nested).root == repo


def test_a_stray_trout_dir_above_the_repo_cannot_shadow_it(tmp_path: Path):
    """A diagnostic run leaving `.trout/` in a parent must not hijack later runs."""
    (tmp_path / ".trout").mkdir()
    repo = _repo(tmp_path)
    (repo / "package.json").write_text("{}", encoding="utf-8")

    assert QaPaths.find(repo).root == repo


def test_package_json_inside_the_repo_still_wins(tmp_path: Path):
    """A monorepo package directory is a legitimate root."""
    repo = _repo(tmp_path)
    package = repo / "apps" / "web"
    package.mkdir(parents=True)
    (package / "package.json").write_text("{}", encoding="utf-8")

    assert QaPaths.find(package).root == package


def test_an_existing_trout_dir_is_preferred_over_package_json(tmp_path: Path):
    repo = _repo(tmp_path)
    package = repo / "apps" / "web"
    package.mkdir(parents=True)
    (package / "package.json").write_text("{}", encoding="utf-8")
    (repo / ".trout").mkdir()

    assert QaPaths.find(package / "src").root == repo


def test_a_repo_with_no_package_json_resolves_to_itself(tmp_path: Path):
    """The reported case: a Python repo with no top-level package.json.

    Previously this escaped the repository entirely.
    """
    repo = _repo(tmp_path, "backend-service")
    (repo / "src").mkdir()

    assert QaPaths.find(repo / "src").root == repo


def test_the_search_path_never_escapes_the_repository(tmp_path: Path):
    repo = _repo(tmp_path)
    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)

    walk = QaPaths.search_path(nested)
    assert walk[-1] == repo
    assert tmp_path not in walk


def test_home_is_a_boundary_even_without_git(tmp_path: Path, monkeypatch):
    """Outside any repository, the walk still cannot reach the filesystem root."""
    home = tmp_path / "home"
    (home / "work" / "thing").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    walk = QaPaths.search_path(home / "work" / "thing")
    assert walk[-1] == home
    assert tmp_path not in walk


def test_an_explicit_path_is_never_second_guessed(tmp_path: Path):
    """`trout scan <dir>` must use exactly that directory."""
    target = tmp_path / "explicit"
    target.mkdir()
    assert QaPaths(root=target).root == target


def test_no_user_facing_string_names_a_command_that_does_not_exist():
    """Regression guard for text that tells users to run something.

    Twice now a string has told users to run a command that does not exist:
    68 `qa …` references survived the rename to `trout`, and later a scan
    printed "next: `trout build`" months before that command was written.
    Nothing caught either, because they are all just strings.

    So the check is against the real command table rather than a list of names
    written down here, which would go stale the same way.
    """
    import re

    from testtrout.cli.main import app as cli

    known = {command.name or command.callback.__name__ for command in cli.registered_commands}
    assert "scan" in known, "command table looks wrong; the guard would pass vacuously"

    source = Path(__file__).resolve().parents[2] / "src" / "testtrout"
    # Only inside backticks. That is how every instruction in this codebase is
    # written, and it keeps prose like ".trout initialised" out of the results
    # without a stopword list that would go stale on its own.
    reference = re.compile(r"`(?:trout|qa) ([a-z][a-z-]+)")
    offenders = [
        f"{path.relative_to(source)}:{number}: {name}"
        for path in source.rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".md"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for name in reference.findall(line)
        if name not in known
    ]
    assert offenders == [], f"references to commands that do not exist: {offenders}"


def test_an_app_in_a_subdirectory_is_found(tmp_path: Path):
    """A monorepo with frontend/ beside backend/ is a common shape.

    Regression: a repository with no root package.json scanned to nothing —
    no framework, no screens — because detection only looked at the root.
    """
    from testtrout.analysis.detect import find_app_root

    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "frontend" / "package.json").write_text("{}", encoding="utf-8")

    assert find_app_root(repo) == repo / "frontend"


def test_a_root_level_app_is_left_alone(tmp_path: Path):
    from testtrout.analysis.detect import find_app_root

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    assert find_app_root(repo) is None


def test_a_workspace_package_is_found(tmp_path: Path):
    from testtrout.analysis.detect import find_app_root

    repo = tmp_path / "repo"
    (repo / "apps" / "web").mkdir(parents=True)
    (repo / "apps" / "web" / "package.json").write_text("{}", encoding="utf-8")
    assert find_app_root(repo) == repo / "apps" / "web"


def test_nothing_recognisable_returns_none(tmp_path: Path):
    from testtrout.analysis.detect import find_app_root

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    assert find_app_root(repo) is None
