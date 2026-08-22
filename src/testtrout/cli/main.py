"""``trout`` command-line entry point.

The command set mirrors the flow exactly, because two ways to describe the same
work is one too many:

    trout add <path> --url <url>   a project: a repo, and where it is deployed
    trout look                     read the code, ask the deployment, work it out
    trout facts                    what I still need, and how to give it
    trout plan                     what I can test, and what is waiting
    trout build                    write the baseline and prove it
    trout run                      re-run the baseline and report what changed

Design rules:

* Every command works non-interactively when given complete arguments, so the
  same binary serves a person and an agent.
* Every command supports ``--json``, emitting the underlying model verbatim.
  Agents should use that rather than parsing rendered output.
* No command mutates a deployment without an explicit, separate opt-in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from testtrout import __version__
from testtrout.app.session import Session
from testtrout.cli import render
from testtrout.domain.config import Config
from testtrout.store import QaPaths, load_dotenv, read_model

app = typer.Typer(
    name="trout",
    help="Baseline regression testing for web applications.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

PathArg = Annotated[
    Path | None,
    typer.Argument(
        help="Project root. Defaults to the nearest project above the current directory."
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON on stdout.")]


def _resolve(path: Path | None) -> QaPaths:
    """Locate the project to operate on and load its ``.env``.

    Loading here rather than at import time means the file next to the *target*
    project is used, not whichever directory the shell happened to be in.
    """
    paths = QaPaths.find(path) if path is None else QaPaths(root=path.resolve())
    load_dotenv(paths.root)
    return paths


def _session(path: Path | None, quiet: bool = False) -> Session:
    """A project, wired so its progress prints as it happens."""
    return Session(
        paths=_resolve(path),
        log=(lambda _: None) if quiet else _line,
    )


def _line(message: str) -> None:
    """One line of progress.

    Printed with markup off: these carry paths and failure messages, and a
    bracket in one would otherwise be read as markup and eaten.
    """
    render.console.print(message, style="" if message.startswith(" ") else "bold", markup=False)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def look(
    path: PathArg = None,
    as_json: JsonOpt = False,
    rescan: Annotated[
        bool,
        typer.Option("--rescan/--no-rescan", help="Re-read the code, not just the deployment."),
    ] = True,
) -> None:
    """Read the code, ask the deployment, and work out what can be tested.

    One command rather than three. A scan whose consequences are not worked out
    is a file on disk nobody asked for, so scanning, probing, and deriving
    always happen together.

    Safe on a repository you have just cloned: reading code makes no network
    calls, and the only requests sent to a deployment are GETs.
    """
    session = _session(path, quiet=as_json)
    from testtrout.app import session as pipeline

    sheet, plan = pipeline.refresh(session, rescan=rescan)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "counts": plan.counts(),
                    "asking": [f.model_dump(mode="json") for f in sheet.outstanding],
                }
            )
        )
        return

    render.plan_summary(plan, sheet)


@app.command()
def facts(
    path: PathArg = None,
    as_json: JsonOpt = False,
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="fact=value, repeatable. Accounts take email:password."),
    ] = None,
) -> None:
    """What I still need from you, and how to give it.

    Every item here is a concrete value — a URL, an account, a real id. Nothing
    asks what your product is supposed to do; that is read from the code and
    observed from the deployment.

    Partial answers are expected. Give what you have and the plan updates.
    """
    paths = _resolve(path)
    session = Session(paths=paths)

    if set_:
        from testtrout.app import facts as fact_writer
        from testtrout.app import session as pipeline

        payload: dict[str, object] = {}
        for item in set_:
            key, _, value = item.partition("=")
            key, value = key.strip(), value.strip()
            if key.startswith("account"):
                email, _, password = value.partition(":")
                payload[key] = {"email": email, "password": password}
            else:
                payload[key] = value
        try:
            applied = fact_writer.apply(paths, payload)
        except fact_writer.FactError as exc:
            render.error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        pipeline.derive(Session(paths=paths))
        render.console.print(f"[green]saved[/green] {', '.join(applied) or 'nothing'}")
        session = Session(paths=paths)

    sheet = session.facts
    if as_json:
        typer.echo(json.dumps(sheet.model_dump(mode="json")))
        return
    render.fact_sheet(sheet)


@app.command()
def plan(path: PathArg = None, as_json: JsonOpt = False) -> None:
    """What I can test right now, and what each blocked item is waiting for."""
    session = Session(paths=_resolve(path))
    current = session.plan
    if as_json:
        typer.echo(json.dumps(current.model_dump(mode="json")))
        return
    render.plan_detail(current, session.facts)


@app.command()
def build(
    path: PathArg = None,
    as_json: JsonOpt = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to write.")] = 20,
) -> None:
    """Write a test for everything ready, prove it, and keep what passes.

    Each test asserts what the deployment did when it was observed. That is the
    baseline: it does not claim the current behaviour is correct — nobody has
    said what correct is — it notices the day the behaviour changes.
    """
    session = _session(path, quiet=as_json)
    from testtrout.app import session as pipeline

    try:
        outcome = pipeline.build(session, limit=limit)
    except RuntimeError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if as_json:
        typer.echo(json.dumps(outcome))
        return
    render.console.print()
    render.console.print(f"[green]{outcome['kept']} test(s) in the baseline[/green]")


@app.command()
def run(
    path: PathArg = None,
    as_json: JsonOpt = False,
    env: Annotated[
        str | None, typer.Option("--env", help="Which deployment to run against.")
    ] = None,
) -> None:
    """Re-run the baseline and report what changed.

    Every assertion came from watching this deployment once, so a failure here
    means it stopped doing something it used to do.
    """
    from testtrout.authoring.store import load_all
    from testtrout.domain.run import RunRecord
    from testtrout.runtime.runner import run as execute
    from testtrout.store import write_model

    paths = _resolve(path)
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    entrypoint = config.entrypoint(env)
    if entrypoint is None:
        render.error_console.print(
            "[red]No deployment configured.[/red] Add one with `trout facts --set "
            "deployment_url=https://...`."
        )
        raise typer.Exit(2)

    index, _ = load_all(paths.scenarios)
    paths.ensure()
    record: RunRecord = execute(
        config, entrypoint, index, paths.root, report_dir=paths.runs / "latest"
    )
    write_model(paths.runs / f"{record.id}.yaml", record, header=False)

    if as_json:
        typer.echo(record.model_dump_json(indent=2, exclude_none=True))
        raise typer.Exit(0 if record.status.value == "pass" else 1)
    render.run_summary(record)
    raise typer.Exit(0 if record.status.value == "pass" else 1)


@app.command()
def add(
    target: Annotated[
        str | None, typer.Argument(help="A local path, or owner/name for GitHub.")
    ] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    github: Annotated[bool, typer.Option("--github", help="Clone from GitHub.")] = False,
    url: Annotated[str | None, typer.Option("--url", help="Where the project is deployed.")] = None,
) -> None:
    """Add a project: a repository, and where it is deployed.

    Pass --url if you can. Without a deployment the first look can only read
    code; with one it can also see what the running system does, which is the
    difference between a guess and a baseline.
    """
    from testtrout.app import Database, RepoRegistry
    from testtrout.app.repos import RepoError

    registry = RepoRegistry(Database())
    try:
        if github:
            from testtrout.app import github as gh

            if target is None:
                render.error_console.print(
                    "[red]Give an owner/name, e.g. DrDroidLab/TestTrout[/red]"
                )
                raise typer.Exit(2)
            token = gh.read_token()
            if not token:
                render.error_console.print(
                    "[red]No GitHub token found.[/red] Set GITHUB_TOKEN, run `gh auth login`, "
                    "or store one with `trout github-login`."
                )
                raise typer.Exit(2)
            render.console.print(f"[dim]cloning {target}…[/dim]")
            record = registry.link_github(target, token)
        else:
            record = registry.link_local(Path(target or "."), name=name)
    except (RepoError, Exception) as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if url:
        from testtrout.app import facts as fact_writer

        try:
            fact_writer.apply(QaPaths(root=Path(record.path)), {"deployment_url": url})
        except fact_writer.FactError as exc:
            render.error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

    registry.queue_initial_scan(record)
    render.console.print(f"[green]added[/green] {record.name}")
    render.console.print(f"  {record.path}", style="dim", markup=False)
    if url:
        render.console.print(f"  {url}", style="dim", markup=False)
    else:
        render.console.print(
            "[dim]no deployment yet — add one with `trout facts --set deployment_url=...`[/dim]"
        )
    render.console.print("[dim]next: `trout look`, or `trout up` for the interface[/dim]")


@app.command()
def doctor(path: PathArg = None, as_json: JsonOpt = False) -> None:
    """Diagnose configuration, dependencies, and connectivity.

    Run this first when something is not working. It reports what is present
    and what is missing without changing anything.
    """
    paths = _resolve(path)
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("project root", paths.root.is_dir(), str(paths.root))
    check(
        "package.json", (paths.root / "package.json").is_file(), "required to detect the framework"
    )
    check(".trout initialised", paths.initialised, "run `trout look` then `trout facts`")
    check("scan present", paths.surfaces.is_file(), "run `trout look`")

    try:
        import tree_sitter_typescript  # noqa: F401

        check("TypeScript parser", True, "tree-sitter-typescript available")
    except ImportError:  # pragma: no cover - dependency is required
        check("TypeScript parser", False, "pip install tree-sitter-typescript")

    try:
        import playwright  # noqa: F401

        check("browser automation", True, "playwright available")
    except ImportError:
        check(
            "browser automation", False, "needed for `trout look`: pip install 'testtrout[probe]'"
        )

    if paths.config.is_file():
        config = read_model(paths.config, Config)
        check(
            "entrypoints",
            bool(config.entrypoints),
            f"{len(config.entrypoints)} configured" if config.entrypoints else "run `trout facts`",
        )
        check(
            "test users",
            len(config.test_users) >= 2,
            "at least two roles are needed for authorization tests",
        )

    if as_json:
        typer.echo(json.dumps({"checks": checks}, indent=2))
        raise typer.Exit(0 if all(c["ok"] for c in checks) else 1)

    for entry in checks:
        mark = "[green]✓[/green]" if entry["ok"] else "[red]✗[/red]"
        render.console.print(mark, entry["name"], end=" ")
        # Detail is data, not markup — a string like "[probe]" must render as
        # written rather than being parsed as a rich style tag.
        render.console.print(str(entry["detail"]), style="dim", markup=False)
    raise typer.Exit(0 if all(c["ok"] for c in checks) else 1)



@app.command()
def up(
    port: Annotated[int, typer.Option("--port", "-p")] = 7411,
    host: Annotated[
        str, typer.Option("--host", help="Bind address. Loopback by default.")
    ] = "127.0.0.1",
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
    link: Annotated[
        Path | None, typer.Option("--link", help="Link this directory on the way up.")
    ] = None,
) -> None:
    """Start TestTrout: storage, worker, and web interface.

    Everything runs on this machine. Storage is SQLite under ~/.testtrout, the
    worker runs in-process, and the interface binds to loopback — there is no
    daemon to install and no container to pull.

    Link repositories from the interface, or with `trout add`.
    """
    from testtrout.app import Database, RepoRegistry
    from testtrout.app.worker import Worker

    try:
        import uvicorn

        from testtrout.web.app import create_app
    except ImportError as exc:
        render.error_console.print(
            "[red]The web server could not be imported.[/red] "
            "Reinstall with: pip install --force-reinstall testtrout"
        )
        raise typer.Exit(2) from exc

    database = Database()
    registry = RepoRegistry(database)

    if link is not None:
        record = registry.link_local(link)
        queued = registry.queue_initial_scan(record)
        render.console.print(
            f"[green]linked[/green] {record.name} [dim]{record.path}[/dim]"
            + (" [dim](scanning…)[/dim]" if queued else "")
        )

    worker = Worker(database)
    worker.start()

    url = f"http://{host}:{port}"
    render.console.print()
    render.console.print("[bold]TestTrout[/bold]")
    render.console.print(f"  interface  {url}")
    render.console.print(f"  storage    {database.path}")
    render.console.print("  worker     running [dim](in-process)[/dim]")
    linked = registry.all()
    render.console.print(
        f"  repos      {len(linked)} linked"
        + (
            f" [dim]({', '.join(r.name for r in linked[:4])})[/dim]"
            if linked
            else " [dim]— link one from the interface[/dim]"
        )
    )
    if host not in {"127.0.0.1", "localhost"}:
        render.console.print(
            "[yellow]  warning:[/yellow] bound to a non-loopback address. This interface can "
            "trigger runs against your deployments."
        )
    render.console.print("[dim]  ctrl-c to stop[/dim]")
    render.console.print()

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(create_app(database=database), host=host, port=port, log_level="warning")
    finally:
        worker.stop()


@app.command(name="github-login")
def github_login(
    token: Annotated[
        str | None, typer.Option("--token", help="Paste a token. Prompted for if omitted.")
    ] = None,
    forget: Annotated[bool, typer.Option("--forget", help="Remove a stored token.")] = False,
) -> None:
    """Store a GitHub personal access token for cloning private repositories.

    Checked first: GITHUB_TOKEN, then the `gh` CLI. If either is present you do
    not need this — TestTrout would rather not hold a credential at all.

    A stored token is written to ~/.testtrout/github with owner-only
    permissions, never into the database.
    """
    from testtrout.app import github as gh

    if forget:
        removed = gh.forget_token()
        render.console.print(
            "[green]token removed[/green]" if removed else "[dim]no stored token[/dim]"
        )
        return

    for name, value in (("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN")),):
        if value:
            render.console.print(f"[dim]{name} is already set — nothing to store.[/dim]")
            return

    supplied = token or typer.prompt("GitHub token", hide_input=True)
    try:
        account = gh.whoami(supplied)
    except gh.GitHubError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    path = gh.store_token(supplied)
    render.console.print(f"[green]stored[/green] token for [bold]{account}[/bold]")
    render.console.print(f"  [dim]{path} (owner-only)[/dim]")


@app.command()
def repos(as_json: JsonOpt = False) -> None:
    """List linked repositories."""
    from testtrout.app import Database, RepoRegistry

    registry = RepoRegistry(Database())
    linked = registry.all()

    if as_json:
        typer.echo(
            json.dumps([r.model_dump(mode="json") | {"exists": r.exists} for r in linked], indent=2)
        )
        return

    if not linked:
        render.console.print("[dim]no projects yet — `trout add <path>`[/dim]")
        return
    render.repo_table(linked)


@app.command()
def worker() -> None:
    """Run a standalone worker.

    `trout up` already runs one in-process. This is for keeping work going with
    the interface closed, or for a second worker on a busy machine.
    """
    from testtrout.app import Database
    from testtrout.app.worker import Worker

    database = Database()
    instance = Worker(database)
    reaped = instance.queue.reap_stale()
    if reaped:
        render.console.print(
            f"[yellow]failed {reaped} job(s) left running by a previous worker[/yellow]"
        )
    render.console.print(f"[bold]worker[/bold] [dim]{database.path}[/dim]")
    render.console.print("[dim]ctrl-c to stop[/dim]")
    try:
        instance.loop()
    except KeyboardInterrupt:
        render.console.print("stopped")


@app.command()
def web(
    path: PathArg = None,
    port: Annotated[int, typer.Option("--port", "-p")] = 7411,
    host: Annotated[
        str, typer.Option("--host", help="Bind address. Loopback by default, deliberately.")
    ] = "127.0.0.1",
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open a browser window.")
    ] = True,
) -> None:
    """Start the web interface for one repository.

    A convenience wrapper around `trout up`: links the resolved repository if it
    is not already linked, then starts storage, the worker, and the interface.
    Use `trout up` directly to work across several repositories.
    """
    paths = _resolve(path)
    up(port=port, host=host, open_browser=open_browser, link=paths.root)


@app.command()
def mcp(path: PathArg = None) -> None:
    """Run an MCP server over stdio, scoped to this project.

    Exposes the same capabilities as the CLI as typed tools, plus the `.trout/`
    state as resources. For agent hosts without a shell, or where typed tool
    schemas beat remembering flags.

    Register it with a client as: `trout mcp /path/to/project`
    """
    paths = _resolve(path)
    try:
        from testtrout.mcp.server import run as run_server
    except ImportError as exc:
        render.error_console.print(
            "[red]The MCP SDK is not installed.[/red] Run: pip install 'testtrout[mcp]'"
        )
        raise typer.Exit(2) from exc
    run_server(paths.root)


def _runnable(paths: QaPaths, env: str | None):  # type: ignore[no-untyped-def]
    """Load config, entrypoint, and scenarios, or exit with a clear reason."""
    from testtrout.authoring.store import load_all

    if not paths.config.is_file():
        render.error_console.print("[red]Not configured.[/red] Run `trout facts` first.")
        raise typer.Exit(2)
    config = read_model(paths.config, Config)
    entrypoint = config.entrypoint(env)
    if entrypoint is None:
        render.error_console.print(
            f"[red]No entrypoint named {env!r}.[/red] Configured: "
            + (", ".join(e.name for e in config.entrypoints) or "none")
        )
        raise typer.Exit(2)
    index, problems = load_all(paths.scenarios)
    for problem in problems:
        render.error_console.print(f"[red]unreadable scenario:[/red] {problem}")
    return config, entrypoint, index


def main() -> None:
    """Console entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
