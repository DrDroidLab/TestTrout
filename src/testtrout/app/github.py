"""Linking repositories from GitHub with a personal access token.

Token handling is the only interesting part. In order of preference:

1. ``GITHUB_TOKEN`` or ``GH_TOKEN`` in the environment.
2. The ``gh`` CLI's own credential, if the developer is already logged in.
3. A token stored by TestTrout at ``~/.testtrout/github`` with ``0600``.

The first two mean TestTrout never holds a credential at all, which is the
better outcome and worth trying before asking anyone to paste one. When a
token is stored, it goes in its own file with restrictive permissions rather
than into the database — a database file gets copied, backed up, and attached
to bug reports in ways a developer does not think about.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

API = "https://api.github.com"
TIMEOUT_SECONDS = 30.0
CLONE_TIMEOUT_SECONDS = 600

_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    """A GitHub operation failed, with a message meant for a person."""


def token_path() -> Path:
    """Where a TestTrout-stored token lives."""
    from testtrout.app.db import default_database_path

    return default_database_path().parent / "github"


def read_token() -> str | None:
    """Find a usable token without prompting."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()

    if shutil.which("gh"):
        try:
            completed = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

    path = token_path()
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def store_token(token: str) -> Path:
    """Persist a token with owner-only permissions.

    Written with the mode set at creation rather than chmod-ed afterwards, so
    there is no window where the file is world-readable.
    """
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token.strip())
    return path


def forget_token() -> bool:
    """Remove a stored token. Returns whether one was there."""
    path = token_path()
    if path.is_file():
        path.unlink()
        return True
    return False


@dataclass(frozen=True)
class RemoteRepo:
    """A repository as GitHub describes it."""

    full_name: str
    default_branch: str
    private: bool
    clone_url: str
    description: str = ""
    pushed_at: str = ""


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def whoami(token: str) -> str:
    """The account a token belongs to, used to verify it before storing."""
    try:
        response = httpx.get(f"{API}/user", headers=_headers(token), timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from exc
    if response.status_code == 401:
        raise GitHubError("that token was rejected — check it has not expired or been revoked")
    if response.status_code >= 400:
        raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")
    return str(response.json().get("login", "unknown"))


def list_repos(token: str, limit: int = 100) -> list[RemoteRepo]:
    """Repositories the token can see, most recently pushed first."""
    try:
        response = httpx.get(
            f"{API}/user/repos",
            headers=_headers(token),
            params={
                "per_page": min(limit, 100),
                "sort": "pushed",
                "affiliation": "owner,collaborator,organization_member",
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from exc
    if response.status_code >= 400:
        raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")

    return [
        RemoteRepo(
            full_name=item["full_name"],
            default_branch=item.get("default_branch") or "main",
            private=bool(item.get("private")),
            clone_url=item["clone_url"],
            description=item.get("description") or "",
            pushed_at=item.get("pushed_at") or "",
        )
        for item in response.json()
    ]


def get_repo(token: str, slug: str) -> RemoteRepo:
    """One repository by ``owner/name``."""
    if not _SLUG.match(slug):
        raise GitHubError(f"{slug!r} is not an owner/name pair, e.g. DrDroidLab/TestTrout")
    try:
        response = httpx.get(
            f"{API}/repos/{slug}", headers=_headers(token), timeout=TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from exc
    if response.status_code == 404:
        raise GitHubError(
            f"{slug} was not found. Either it does not exist, or this token cannot see it — "
            "a fine-grained token needs explicit access to the repository."
        )
    if response.status_code >= 400:
        raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")

    item = response.json()
    return RemoteRepo(
        full_name=item["full_name"],
        default_branch=item.get("default_branch") or "main",
        private=bool(item.get("private")),
        clone_url=item["clone_url"],
        description=item.get("description") or "",
        pushed_at=item.get("pushed_at") or "",
    )


def clone(remote: RemoteRepo, token: str, destination: Path) -> Path:
    """Clone a repository, keeping the token out of the working tree.

    The token is passed to git through the environment for one command rather
    than embedded in the remote URL. A URL with a token in it gets written into
    ``.git/config``, where it survives, gets committed by accident, and shows up
    in screenshots.
    """
    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise GitHubError(f"{destination} already exists and is not empty")
    destination.parent.mkdir(parents=True, exist_ok=True)

    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        # Supplies credentials for this invocation only.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": f"!f() {{ echo username=x-access-token; echo password={token}; }}; f",
    }

    try:
        completed = subprocess.run(
            ["git", "clone", "--depth", "50", remote.clone_url, str(destination)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=CLONE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"clone failed: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        # Never echo the environment back; the helper string contains the token.
        raise GitHubError(f"clone failed: {detail[-1] if detail else 'unknown error'}")
    return destination
