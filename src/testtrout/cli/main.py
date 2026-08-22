"""``trout`` command-line entry point.

Command design rules:

* Every command works non-interactively when given complete arguments, so the
  same binary serves a human and an agent.
* Every command supports ``--json``, emitting the underlying model verbatim.
  Agents should use that rather than parsing rendered output.
* Commands never mutate a deployment without an explicit, separate opt-in.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from testtrout import __version__
from testtrout.cli import render
from testtrout.domain.config import (
    Config,
    ModelProvider,
    SecretResolutionError,
    resolve_secret,
)
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import ProbeResult
from testtrout.domain.overview import ProjectOverview, ScanDelta
from testtrout.domain.run import RunRecord
from testtrout.domain.scenario import ScenarioStatus
from testtrout.domain.surface import Criticality, ScanResult
from testtrout.store import QaPaths, apply_scan, load_dotenv, read_model, write_model

app = typer.Typer(
    name="trout",
    help="Automated regression testing for AI-built web applications.",
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


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def scan(
    path: PathArg = None,
    as_json: JsonOpt = False,
    depth: Annotated[
        int, typer.Option("--depth", min=1, max=8, help="Import hops to follow from a screen.")
    ] = 3,
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="Write .trout/surfaces.yaml.")
    ] = True,
) -> None:
    """Analyse a repository: screens, data operations, endpoints, and policies.

    Requires no API key and makes no network calls. Safe to run on any
    repository, including one you have just cloned and do not trust.
    """
    from testtrout.analysis.scanner import scan as run_scan

    paths = _resolve(path)
    if not paths.root.is_dir():
        render.error_console.print(f"[red]No such directory:[/red] {paths.root}")
        raise typer.Exit(2)

    # Announce the resolved root *before* doing any work. Root discovery walks
    # upward, so it can land somewhere surprising, and a scan of the wrong tree
    # is minutes of CPU before anything is printed.
    if not as_json:
        render.error_console.print(f"[dim]scanning {paths.root}[/dim]")

    result = run_scan(paths.root, max_depth=depth)

    if save:
        paths.ensure()
        write_model(paths.surfaces, result)
        _sync_project_config(paths, result)

    if as_json:
        typer.echo(result.model_dump_json(indent=2, exclude_none=True))
        return

    render.scan_summary(result)
    render.warnings(result)
    if not save:
        return

    changed, overview = _record_overview(paths, result)
    render.project_overview(overview, changed)

    render.console.print()
    render.console.print(f"[dim]written to {paths.surfaces.relative_to(paths.root)}[/dim]")
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    render.console.print(
        "[dim]next: `trout build` to write tests[/dim]"
        if config.entrypoint() is not None
        else "[dim]next: `trout init --url <url>` to connect a deployment[/dim]"
    )


def _record_overview(paths: QaPaths, result: ScanResult) -> tuple[ScanDelta, ProjectOverview]:
    """Describe the product, and say what moved since the last scan.

    The snapshot is kept so the *next* scan has something to compare against.
    Without it a rescan reprints the same list and gives no sense of progress.
    """
    from testtrout.planning.overview import build as build_overview
    from testtrout.planning.overview import delta as compare
    from testtrout.planning.readiness import assess

    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    from testtrout.authoring.store import load_all

    index, _ = load_all(paths.scenarios)
    previous = read_model(paths.overview, ProjectOverview) if paths.overview.is_file() else None
    overview = build_overview(result, index, assess(config, result, _load_probe(paths, config)))
    write_model(paths.overview, overview)
    return compare(previous, overview), overview


def _sync_project_config(paths: QaPaths, result: ScanResult) -> None:
    """Record what the scan detected into config, preserving user edits."""
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    write_model(paths.config, apply_scan(config, result))


@app.command()
def surfaces(
    path: PathArg = None,
    as_json: JsonOpt = False,
    kind: Annotated[
        str | None, typer.Option("--kind", "-k", help="Filter by kind, e.g. data_operation.")
    ] = None,
    min_criticality: Annotated[
        Criticality | None, typer.Option("--min", help="Only show this level or above.")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n")] = None,
) -> None:
    """List what the last scan found, ordered by criticality."""
    paths = _resolve(path)
    if not paths.surfaces.is_file():
        render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
        raise typer.Exit(2)

    result = read_model(paths.surfaces, ScanResult)
    items = result.all_surfaces()
    if kind:
        items = [s for s in items if s.kind == kind]
    if min_criticality:
        items = [s for s in items if s.criticality.rank <= min_criticality.rank]
    if limit:
        items = items[:limit]

    if as_json:
        typer.echo(
            json.dumps([s.model_dump(mode="json", exclude_none=True) for s in items], indent=2)
        )
        return

    filtered = result.model_copy(deep=True)
    filtered.screens = [s for s in items if s.kind == "screen"]  # type: ignore[misc]
    filtered.data_operations = [s for s in items if s.kind == "data_operation"]  # type: ignore[misc]
    filtered.endpoints = [s for s in items if s.kind == "endpoint"]  # type: ignore[misc]
    filtered.server_actions = [s for s in items if s.kind == "server_action"]  # type: ignore[misc]
    filtered.edge_functions = [s for s in items if s.kind == "edge_function"]  # type: ignore[misc]
    filtered.policies = [s for s in items if s.kind == "policy"]  # type: ignore[misc]
    filtered.externals = [s for s in items if s.kind == "external"]  # type: ignore[misc]
    render.surface_table(filtered)


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
    check(".trout initialised", paths.initialised, "run `trout scan` then `trout init`")
    check("scan present", paths.surfaces.is_file(), "run `trout scan`")

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
            "browser automation", False, "needed for `trout probe`: pip install 'testtrout[probe]'"
        )

    if paths.config.is_file():
        config = read_model(paths.config, Config)
        check(
            "entrypoints",
            bool(config.entrypoints),
            f"{len(config.entrypoints)} configured" if config.entrypoints else "run `trout init`",
        )
        check(
            "test users",
            len(config.test_users) >= 2,
            "at least two roles are needed for authorization tests",
        )

    model = (read_model(paths.config, Config) if paths.config.is_file() else Config()).model
    try:
        has_key = bool(resolve_secret(model.api_key)) or _provider_key_in_env(model.provider)
        detail = f"{model.provider.value} ({model.model or 'default model'})"
    except SecretResolutionError as exc:
        has_key, detail = False, str(exc)
    check("model provider", has_key, detail + " — only needed for propose/intent")

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


def _provider_key_in_env(provider: ModelProvider) -> bool:
    """Whether a key for this provider is reachable from the environment.

    Each SDK has its own lookup, so an unset config field does not mean an
    unset credential.
    """
    import os

    names = {
        ModelProvider.ANTHROPIC: ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        ModelProvider.OPENAI: ("OPENAI_API_KEY",),
        ModelProvider.KIMI: ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    }[provider]
    return any(os.environ.get(name) for name in names)


@app.command()
def providers(
    path: PathArg = None,
    as_json: JsonOpt = False,
    check: Annotated[
        bool, typer.Option("--check", help="Make one small live call to verify the setup.")
    ] = False,
) -> None:
    """Show the configured model provider, and optionally verify it works.

    Only scenario proposal, intent capture, and failure explanation need a
    model. `trout scan` never does, so an unconfigured provider is not an error.
    """
    from testtrout.llm.gateway import Gateway, GatewayError

    paths = _resolve(path)
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    model = config.model

    try:
        key = resolve_secret(model.api_key)
        key_state = "set" if key else "falling back to the provider SDK's own lookup"
    except SecretResolutionError as exc:
        key = None
        key_state = str(exc)

    info: dict[str, object] = {
        "provider": model.provider.value,
        "model": model.model or "provider default",
        "base_url": resolve_secret(model.base_url) or "provider default",
        "api_key": key_state,
        "temperature": model.temperature if model.temperature is not None else "provider default",
    }

    if check:
        gateway = Gateway(model, paths.cache)
        try:
            response = gateway.complete(
                system="Reply with the single word: ok",
                user="ping",
                max_tokens=64,
            )
            info["check"] = "ok"
            info["responded_as"] = response.model
        except GatewayError as exc:
            info["check"] = "failed"
            info["error"] = str(exc)

    if as_json:
        typer.echo(json.dumps(info, indent=2))
        raise typer.Exit(0 if info.get("check", "ok") == "ok" else 1)

    for label, value in info.items():
        if label == "error":
            render.console.print(f"[red]{value}[/red]")
        else:
            render.console.print(f"[bold]{label:14}[/bold] {value}")

    if not check:
        render.console.print("\n[dim]run with --check to make one live call[/dim]")
    raise typer.Exit(0 if info.get("check", "ok") == "ok" else 1)


@app.command()
def init(
    path: PathArg = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Deployment URL. Skips the prompt when given.")
    ] = None,
    name: Annotated[str, typer.Option("--name", help="Name for the deployment.")] = "local",
    disposable: Annotated[
        bool | None,
        typer.Option(
            "--disposable/--no-disposable",
            help="Whether the data behind this deployment can be destroyed freely.",
        ),
    ] = None,
    supabase_url: Annotated[str | None, typer.Option("--supabase-url")] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="anthropic | openai | kimi")
    ] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    roles: Annotated[
        list[str] | None,
        typer.Option("--role", help="Test-user role. Repeat for more than one."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Never prompt; accept defaults. For scripts and agents."),
    ] = False,
) -> None:
    """Configure deployments, authentication, and test users.

    Resumable: answers are written as they are given, so an interrupted run
    picks up where it stopped. Fully scriptable — pass every value as a flag
    with --yes and nothing is prompted.

    Secrets are never stored here. Questions ask for the *name* of an
    environment variable; put the values in a gitignored .env beside the
    project.
    """
    from testtrout.interview import Interview

    paths = _resolve(path)
    paths.ensure()
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    interview = Interview(config=config, interactive=not yes)

    if not paths.surfaces.is_file():
        render.console.print(
            "[yellow]No scan found.[/yellow] Run `trout scan` first so init knows what "
            "this project is.\n"
        )

    render.console.print("[bold]Deployment[/bold]")
    entrypoint = interview.add_entrypoint(name=name, url=url, disposable=disposable)
    write_model(paths.config, config)

    if config.project.backend == "supabase":
        render.console.print("\n[bold]Supabase[/bold]")
        if supabase_url:
            config.supabase.url = supabase_url
        if yes:
            # A scripted run still needs the key reference, or the first `trout run`
            # refuses with "not set in .trout/config.yaml" while the value sits in
            # .env being ignored.
            config.supabase.anon_key = config.supabase.anon_key or "env:SUPABASE_ANON_KEY"
        else:
            interview.add_supabase()
        write_model(paths.config, config)

    render.console.print("\n[bold]Test users[/bold]")
    render.console.print(
        "[dim]Two roles are needed to test authorization: proving user A cannot "
        "see user B's data requires a B.[/dim]"
    )
    # None means "prompt for the role name". Scripted runs get two sensible
    # defaults, because one test user cannot express an authorization test.
    requested: list[str | None]
    if roles:
        requested = list(roles)
    elif yes:
        requested = ["owner", "member"]
    else:
        requested = [None, None]

    for role in requested:
        interview.add_test_user(role)
        if not yes and not interview.confirm("Add another role?", default=False):
            break
    write_model(paths.config, config)

    render.console.print("\n[bold]Model provider[/bold]")
    render.console.print("[dim]Only used for proposing scenarios and explaining failures.[/dim]")
    interview.set_model(provider=provider, model=model)
    write_model(paths.config, config)

    render.console.print()
    render.console.print(f"[green]written to {paths.config.relative_to(paths.root)}[/green]")
    if not entrypoint.writable:
        render.console.print(
            f"[dim]{entrypoint.name} is read-only; mutating requests will be blocked.[/dim]"
        )
    render.console.print("[dim]next: `trout doctor` to verify, then `trout probe`[/dim]")


@app.command()
def probe(
    path: PathArg = None,
    as_json: JsonOpt = False,
    env: Annotated[
        str | None, typer.Option("--env", help="Entrypoint name. Defaults to the first.")
    ] = None,
    role: Annotated[
        str | None,
        typer.Option("--role", help="Sign in as this role. Omit to probe signed out."),
    ] = None,
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser window.")] = False,
    max_screens: Annotated[int | None, typer.Option("--max-screens", "-n")] = None,
) -> None:
    """Load the deployment in a real browser and record what it does.

    Read-only unless the entrypoint is marked disposable: mutating requests are
    blocked at the network layer, so this is safe to run against a shared or
    production URL.

    Navigates to routes; it never clicks buttons or submits forms.
    """
    from testtrout.deployment.prober import ProbeUnavailableError
    from testtrout.deployment.prober import probe as run_probe
    from testtrout.deployment.reconcile import persist_login, reconcile

    paths = _resolve(path)
    if not paths.surfaces.is_file():
        render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
        raise typer.Exit(2)
    if not paths.config.is_file():
        render.error_console.print("[red]Not configured.[/red] Run `trout init` first.")
        raise typer.Exit(2)

    scan_result = read_model(paths.surfaces, ScanResult)
    config = read_model(paths.config, Config)
    entrypoint = config.entrypoint(env)
    if entrypoint is None:
        render.error_console.print(
            f"[red]No entrypoint named {env!r}.[/red] Configured: "
            + (", ".join(e.name for e in config.entrypoints) or "none")
        )
        raise typer.Exit(2)

    try:
        result = run_probe(
            scan_result,
            config,
            entrypoint,
            role=role,
            headless=not headed,
            max_screens=max_screens,
        )
    except ProbeUnavailableError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    result.divergences.extend(reconcile(scan_result, result))
    persist_login(paths, result)
    destination = paths.observed / f"{entrypoint.name}.yaml"
    write_model(destination, result)

    if as_json:
        typer.echo(result.model_dump_json(indent=2, exclude_none=True))
        return

    render.probe_summary(result)
    render.divergences(result.divergences)
    render.console.print()
    render.console.print(f"[dim]written to {destination.relative_to(paths.root)}[/dim]")


def _load_probe(paths: QaPaths, config: Config) -> ProbeResult | None:
    """Most recent probe for the default entrypoint, if one exists."""
    entrypoint = config.entrypoint()
    if entrypoint is None:
        return None
    path = paths.observed / f"{entrypoint.name}.yaml"
    return read_model(path, ProbeResult) if path.is_file() else None


@app.command()
def intent(
    path: PathArg = None,
    as_json: JsonOpt = False,
    describe: Annotated[
        str | None,
        typer.Option("--describe", help="Describe the product in your own words."),
    ] = None,
    from_file: Annotated[
        Path | None, typer.Option("--from", help="Read the description from a file.")
    ] = None,
    draft_only: Annotated[
        bool,
        typer.Option("--draft", help="Only draft from the code; do not ask anything."),
    ] = False,
) -> None:
    """Capture what the product does and what must never break.

    Starts from a draft built out of your codebase rather than a blank page,
    because "here is what I think this app does, correct me" is a much easier
    question to answer than "what does your app do?".

    Everything drafted is marked `inferred` until you confirm it. Inferred
    intent never justifies blocking anything on its own.
    """
    from testtrout.llm.gateway import Gateway, GatewayError
    from testtrout.planning import intent as planner

    paths = _resolve(path)
    if not paths.surfaces.is_file():
        render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
        raise typer.Exit(2)

    scan_result = read_model(paths.surfaces, ScanResult)
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    gateway = Gateway(config.model, paths.cache)
    probe_result = _load_probe(paths, config)

    description = describe
    if from_file is not None:
        description = from_file.read_text(encoding="utf-8")

    try:
        if description:
            captured, warnings = planner.structure(gateway, description, scan_result, probe_result)
        else:
            # stderr, not stdout: --json output must stay machine-parseable.
            render.error_console.print("[dim]drafting from the codebase…[/dim]")
            captured, warnings = planner.draft(gateway, scan_result, probe_result)
    except GatewayError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if not description and not draft_only and not as_json:
        render.intent_summary(captured, warnings)
        render.console.print()
        correction = typer.prompt(
            "Correct anything that is wrong, or press enter to accept",
            default="",
            show_default=False,
        ).strip()
        if correction:
            render.error_console.print("[dim]re-reading with your corrections…[/dim]")
            combined = f"{captured.summary}\n\nCorrections from the developer:\n{correction}"
            captured, warnings = planner.structure(gateway, combined, scan_result, probe_result)

    paths.ensure()
    write_model(paths.intent, captured)

    if as_json:
        typer.echo(captured.model_dump_json(indent=2, exclude_none=True))
        return

    render.intent_summary(captured, warnings)
    render.console.print()
    render.console.print(f"[dim]written to {paths.intent.relative_to(paths.root)}[/dim]")
    render.console.print(
        "[dim]edit that file directly if it is easier — the tool reads it back[/dim]"
    )
    render.console.print("[dim]next: `trout gaps`[/dim]")


@app.command()
def gaps(
    path: PathArg = None,
    as_json: JsonOpt = False,
    limit: Annotated[int | None, typer.Option("--limit", "-n")] = None,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind", "-k", help="authorization | browser_journey | data_operation | endpoint"
        ),
    ] = None,
    ready: Annotated[
        bool, typer.Option("--ready", help="Only gaps that could be written right now.")
    ] = False,
    budget: Annotated[
        int | None,
        typer.Option("--budget", help="Seconds of runtime; returns the best suite that fits."),
    ] = None,
) -> None:
    """Rank the tests this application is missing, and say why.

    Deterministic: no model is used. Every rank is the sum of named
    contributions, so a ranking you disagree with can be argued with rather
    than merely overridden.

    Uses whatever evidence exists — the scan alone works, and `trout intent` plus
    `trout probe` make the ranking considerably better.
    """
    from testtrout.planning import gaps as planner
    from testtrout.planning.existing_tests import detect

    paths = _resolve(path)
    if not paths.surfaces.is_file():
        render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
        raise typer.Exit(2)

    scan_result = read_model(paths.surfaces, ScanResult)
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    captured = read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None

    from testtrout.authoring.store import load_all

    index, _ = load_all(paths.scenarios)
    result = planner.build(
        scan_result,
        intent=captured,
        probe=_load_probe(paths, config),
        existing=detect(paths.root, scan_result),
        roles=[u.role for u in config.test_users],
        scenarios=index,
    )

    if kind:
        result.gaps = [g for g in result.gaps if g.kind.value == kind]
    if budget:
        result.gaps = result.budget(budget)

    if as_json:
        typer.echo(result.model_dump_json(indent=2, exclude_none=True))
        return

    render.gap_map(result, limit=limit, ready_only=ready)


def _gap_map(paths: QaPaths, config: Config):  # type: ignore[no-untyped-def]
    """Rebuild the gap map from whatever evidence is on disk."""
    from testtrout.planning import gaps as planner
    from testtrout.planning.existing_tests import detect

    scan_result = read_model(paths.surfaces, ScanResult)
    captured = read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None
    return (
        scan_result,
        captured,
        planner.build(
            scan_result,
            intent=captured,
            probe=_load_probe(paths, config),
            existing=detect(paths.root, scan_result),
            roles=[u.role for u in config.test_users],
        ),
    )


@app.command()
def build(
    path: PathArg = None,
    as_json: JsonOpt = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many gaps to attempt.")] = 5,
    entrypoint: Annotated[
        str | None, typer.Option("--entrypoint", help="Which deployment to prove tests against.")
    ] = None,
    no_model: Annotated[
        bool, typer.Option("--no-model", help="Skip model enrichment. Works with no API key.")
    ] = False,
) -> None:
    """Write tests for what is untested, keeping only the ones that pass.

    Each test is drafted, written, and run against your deployment before the
    next one is started — so the suite grows a test at a time and you see each
    one prove itself rather than waiting for a batch.

    A test that passes is kept. A test that fails is held back with the failure
    recorded as a question, because a failing new test is either a wrong
    expectation or a real problem, and only you can say which. Nothing is
    approved on your behalf and nothing unproven counts as coverage.

    The same thing the Build tests button does.
    """
    from testtrout.authoring.build import build_suite
    from testtrout.llm.gateway import Gateway

    paths = _resolve(path)
    if not paths.surfaces.is_file():
        render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
        raise typer.Exit(2)

    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    target = config.entrypoint(entrypoint)
    if target is None:
        render.error_console.print(
            "[red]No deployment configured.[/red] Add one with `trout init --url <url>`."
        )
        raise typer.Exit(2)

    scan_result = read_model(paths.surfaces, ScanResult)
    captured = read_model(paths.intent, ProductIntent) if paths.intent.is_file() else None
    outcome = build_suite(
        paths,
        config,
        scan_result,
        target,
        intent=captured,
        probe=_load_probe(paths, config),
        gateway=None if no_model else Gateway(config.model, paths.cache),
        limit=limit,
        # Progress as it happens, not a report at the end: a build runs real
        # tests against a real deployment and can take minutes.
        log=(lambda line: None) if as_json else _build_line,
    )

    if as_json:
        typer.echo(json.dumps(outcome.as_dict()))
        return

    render.console.print()
    render.console.print(
        f"[green]{len(outcome.kept)} kept[/green]"
        + (f"  [yellow]{outcome.needs_you} need you[/yellow]" if outcome.needs_you else "")
    )
    if outcome.needs_you:
        render.console.print("[dim]see `trout questions`[/dim]")


def _build_line(line: str) -> None:
    """One line of build progress. Indented lines are detail about the test above.

    Printed with ``markup=False``: these lines carry test titles and failure
    messages, and a bracket in one would otherwise be read as markup and eaten.
    """
    render.console.print(line, style="" if line.startswith(" ") else "bold", markup=False)


@app.command()
def propose(
    path: PathArg = None,
    as_json: JsonOpt = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many gaps to draft.")] = 5,
    kind: Annotated[str | None, typer.Option("--kind", "-k")] = None,
    gap: Annotated[str | None, typer.Option("--gap", help="Draft one specific gap by id.")] = None,
    no_model: Annotated[
        bool,
        typer.Option("--no-model", help="Skip model enrichment. Works with no API key."),
    ] = False,
) -> None:
    """Draft scenario specifications for the highest-ranked gaps.

    Scenarios are built deterministically from the scan, the probe, and your
    schema; a model only refines the wording, picks which observed elements to
    assert on, and says what it could not determine. With --no-model you still
    get usable scenarios, just plainer ones.

    Everything lands as a draft. Nothing is approved on your behalf.
    """
    from testtrout.authoring import propose as authoring
    from testtrout.authoring.store import save
    from testtrout.llm.gateway import Gateway

    paths = _resolve(path)
    if not paths.surfaces.is_file():
        render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
        raise typer.Exit(2)

    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    scan_result, captured, gap_map = _gap_map(paths, config)

    candidates = [g for g in gap_map.ranked() if not kind or g.kind.value == kind]
    if gap:
        candidates = [g for g in candidates if g.id == gap]
        if not candidates:
            render.error_console.print(f"[red]No gap with id {gap!r}.[/red]")
            raise typer.Exit(2)
    else:
        candidates = [g for g in candidates if g.ready][:limit]

    gateway = None if no_model else Gateway(config.model, paths.cache)
    probe_result = _load_probe(paths, config)

    paths.ensure()
    drafted, warnings = [], []
    for candidate in candidates:
        render.error_console.print(f"[dim]drafting {candidate.id}…[/dim]")
        scenario, issues = authoring.propose(
            candidate,
            scan_result,
            config,
            probe=probe_result,
            intent=captured,
            gateway=gateway,
        )
        save(paths.scenarios, scenario)
        drafted.append(scenario)
        warnings.extend(issues)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "drafted": [s.model_dump(mode="json", exclude_none=True) for s in drafted],
                    "warnings": warnings,
                },
                indent=2,
            )
        )
        return

    from testtrout.domain.scenario import ScenarioIndex

    render.scenario_table(ScenarioIndex(scenarios=drafted))
    for warning in warnings:
        render.console.print(f"  [yellow]·[/yellow] {warning}")
    render.console.print()
    render.console.print(
        "[dim]review the .yaml files, then `trout approve <id>` or `trout approve --all`[/dim]"
    )


@app.command()
def scenarios(
    path: PathArg = None,
    as_json: JsonOpt = False,
    status: Annotated[
        str | None, typer.Option("--status", help="draft | approved | certified | rejected")
    ] = None,
) -> None:
    """List scenario specifications and their status."""
    from testtrout.authoring.store import load_all

    paths = _resolve(path)
    index, problems = load_all(paths.scenarios)
    if status:
        index.scenarios = [s for s in index.scenarios if s.status.value == status]

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "scenarios": [
                        s.model_dump(mode="json", exclude_none=True) for s in index.scenarios
                    ],
                    "problems": problems,
                },
                indent=2,
            )
        )
        return

    render.scenario_table(index)
    for problem in problems:
        render.error_console.print(f"[red]unreadable:[/red] {problem}")


@app.command()
def approve(
    scenario_ids: Annotated[list[str] | None, typer.Argument(help="Scenario ids.")] = None,
    path: PathArg = None,
    all_ready: Annotated[
        bool,
        typer.Option("--all", help="Approve every draft with no open questions."),
    ] = False,
    reject: Annotated[bool, typer.Option("--reject", help="Reject instead of approve.")] = False,
) -> None:
    """Accept scenarios into the suite.

    A scenario with unanswered questions is refused: approving it would produce
    a test that passes vacuously, which is worse than no test. Answer the
    questions in its .yaml first — editing that file directly is a supported
    workflow.
    """
    from testtrout.authoring.store import load_all, save

    paths = _resolve(path)
    index, _ = load_all(paths.scenarios)
    target = ScenarioStatus.REJECTED if reject else ScenarioStatus.APPROVED

    if all_ready:
        chosen = [s for s in index.by_status(ScenarioStatus.DRAFT) if reject or s.ready_to_approve]
    else:
        wanted = {i if i.startswith("scenario:") else f"scenario:{i}" for i in scenario_ids or []}
        chosen = [s for s in index.scenarios if s.id in wanted]
        missing = wanted - {s.id for s in chosen}
        for identifier in sorted(missing):
            render.error_console.print(f"[red]no scenario {identifier!r}[/red]")

    changed = 0
    for scenario in chosen:
        if not reject and not scenario.ready_to_approve:
            render.error_console.print(
                f"[yellow]{scenario.id}[/yellow] has unanswered questions — not approved"
            )
            for question in scenario.open_questions:
                render.error_console.print(f"  [dim]{question}[/dim]")
            continue
        scenario.status = target
        save(paths.scenarios, scenario)
        changed += 1

    verb = "rejected" if reject else "approved"
    render.console.print(f"[green]{changed} scenario(s) {verb}[/green]")
    if changed and not reject:
        render.console.print("[dim]next: `trout generate`[/dim]")


@app.command()
def generate(
    path: PathArg = None,
    as_json: JsonOpt = False,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output root. Defaults to the project root.")
    ] = None,
) -> None:
    """Compile approved scenarios into runnable test files.

    Generated code is a build artifact — it carries a do-not-edit header and is
    overwritten on every run. Change the scenario .yaml and regenerate.
    """
    from testtrout.authoring.base import select_emitter
    from testtrout.authoring.store import load_all

    paths = _resolve(path)
    root = (out or paths.root).resolve()
    config = read_model(paths.config, Config) if paths.config.is_file() else Config()
    index, _ = load_all(paths.scenarios)

    approved = index.by_status(ScenarioStatus.APPROVED)
    if not approved:
        render.error_console.print(
            "[yellow]No approved scenarios.[/yellow] Run `trout propose` then `trout approve`."
        )
        raise typer.Exit(1)

    written: list[tuple[str, list[str]]] = []
    shared: dict[str, str] = {}
    from testtrout.authoring.store import save
    from testtrout.runtime.toolchain import app_root

    for scenario in approved:
        emitter = select_emitter(scenario)
        if emitter is None:
            render.error_console.print(
                f"[red]no emitter for {scenario.kind.value}[/red] ({scenario.id})"
            )
            continue
        emitted = emitter.emit(scenario, config)
        destination = app_root(root) / emitted.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(emitted.content, encoding="utf-8")
        shared.update(emitted.shared)
        written.append((emitted.path, emitted.notes))

        scenario.emitted_to = emitted.path
        save(paths.scenarios, scenario)

    for rel, content in shared.items():
        destination = app_root(root) / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        written.append((rel, []))

    if as_json:
        typer.echo(json.dumps({"files": [{"path": p, "notes": n} for p, n in written]}, indent=2))
        return

    render.generated(written)


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
        render.error_console.print("[red]Not configured.[/red] Run `trout init` first.")
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


@app.command()
def run(
    path: PathArg = None,
    as_json: JsonOpt = False,
    env: Annotated[str | None, typer.Option("--env", help="Entrypoint name.")] = None,
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Run only this scenario.")
    ] = None,
    changed_from: Annotated[
        str | None,
        typer.Option(
            "--changed-from",
            help="Run only scenarios a diff against this git ref could affect, e.g. origin/main.",
        ),
    ] = None,
    compare_with: Annotated[
        str | None,
        typer.Option(
            "--compare-with",
            help="Entrypoint believed good. Failures are re-run there before being called regressions.",
        ),
    ] = None,
    no_reset: Annotated[bool, typer.Option("--no-reset", help="Skip database isolation.")] = False,
) -> None:
    """Execute the generated suite against a deployment.

    Tests run through your own toolchain (`playwright`, `vitest`), so they work
    without this tool installed. What this adds is database isolation, blocking
    third-party calls, and classifying each failure as a product problem, an
    environment problem, or a flake.

    An inconclusive run is never reported as a pass.
    """
    from testtrout.runtime.runner import run as execute

    paths = _resolve(path)
    config, entrypoint, index = _runnable(paths, env)
    paths.ensure()

    only = scenario
    if only and not only.startswith("scenario:"):
        only = f"scenario:{only}"

    picked: list[str] | None = None
    if changed_from:
        from testtrout.planning import selection as selector

        if not paths.surfaces.is_file():
            render.error_console.print("[red]No scan found.[/red] Run `trout scan` first.")
            raise typer.Exit(2)
        try:
            files = selector.changed_files(paths.root, changed_from)
        except selector.GitUnavailableError as exc:
            render.error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

        chosen = selector.select(read_model(paths.surfaces, ScanResult), index, files)
        picked = chosen.scenarios
        if not as_json:
            render.selection(chosen)
        if chosen.empty:
            render.console.print(
                "[green]Nothing to run — this change affects no covered surface.[/green]"
            )
            raise typer.Exit(0)

    record = execute(
        config,
        entrypoint,
        index,
        paths.root,
        report_dir=paths.runs / "latest",
        scenario_id=only,
        only=picked,
        reset=not no_reset,
    )

    if compare_with:
        from testtrout.runtime import differential

        baseline = config.entrypoint(compare_with)
        if baseline is None:
            render.error_console.print(f"[red]No entrypoint named {compare_with!r}.[/red]")
            raise typer.Exit(2)
        if record.regressions:
            render.error_console.print(
                f"[dim]re-running {len(record.regressions)} failure(s) against "
                f"{baseline.name} to confirm…[/dim]"
            )
            verdicts = differential.compare(
                record, config, baseline, index, paths.root, paths.runs / "baseline"
            )
            record = differential.apply(record, verdicts)

    write_model(paths.runs / f"{record.id}.yaml", record, header=False)

    if as_json:
        typer.echo(record.model_dump_json(indent=2, exclude_none=True))
    else:
        render.run_summary(record)
        render.console.print()
        render.console.print(
            f"[dim]written to {(paths.runs / f'{record.id}.yaml').relative_to(paths.root)}[/dim]"
        )

    raise typer.Exit(0 if record.status.value in {"pass", "warning"} else 1)


@app.command()
def certify(
    path: PathArg = None,
    as_json: JsonOpt = False,
    env: Annotated[str | None, typer.Option("--env")] = None,
    runs: Annotated[int | None, typer.Option("--runs", help="Consecutive passes required.")] = None,
) -> None:
    """Prove scenarios are deterministic before trusting them.

    Runs the suite repeatedly. A scenario that passes every time is certified;
    one that is inconsistent is quarantined, not admitted. A test that passed
    once has not shown it is stable, and an intermittently-failing test in a
    blocking suite is how a team learns to ignore the suite.
    """
    from testtrout.authoring.store import save
    from testtrout.runtime.runner import apply_verdicts
    from testtrout.runtime.runner import certify as run_certification

    paths = _resolve(path)
    config, entrypoint, index = _runnable(paths, env)
    paths.ensure()

    attempts = runs or config.run.certification_runs
    render.error_console.print(f"[dim]running the suite {attempts} times…[/dim]")
    verdicts, records = run_certification(
        config, entrypoint, index, paths.root, paths.runs, runs=attempts
    )
    changes = apply_verdicts(index, verdicts)
    for scenario in index.scenarios:
        if scenario.id in changes:
            save(paths.scenarios, scenario)

    payload = {
        "runs": attempts,
        "verdicts": {k: v.value for k, v in verdicts.items()},
        "changes": {k: v.value for k, v in changes.items()},
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    certified = [k for k, v in changes.items() if v is ScenarioStatus.CERTIFIED]
    quarantined = [k for k, v in changes.items() if v is ScenarioStatus.QUARANTINED]
    render.console.print()
    render.console.print(f"[green]{len(certified)} certified[/green]")
    for identifier in certified:
        render.console.print(f"  [dim]{identifier.replace('scenario:', '')}[/dim]")
    if quarantined:
        render.console.print(f"[yellow]{len(quarantined)} quarantined[/yellow] [dim](flaky)[/dim]")
        for identifier in quarantined:
            render.console.print(f"  [dim]{identifier.replace('scenario:', '')}[/dim]")
    if records and records[-1].notes:
        for note in records[-1].notes:
            render.console.print(f"[dim]note: {note}[/dim]")


@app.command()
def report(path: PathArg = None, as_json: JsonOpt = False) -> None:
    """Show the results and evidence from the last run."""
    paths = _resolve(path)
    records = sorted(paths.runs.glob("*.yaml")) if paths.runs.is_dir() else []
    if not records:
        render.error_console.print("[yellow]No runs yet.[/yellow] Run `trout run`.")
        raise typer.Exit(1)

    record = read_model(records[-1], RunRecord)
    if as_json:
        typer.echo(record.model_dump_json(indent=2, exclude_none=True))
        return
    render.run_summary(record)


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

    Link repositories from the interface, or with `trout link`.
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


@app.command()
def link(
    target: Annotated[
        str | None,
        typer.Argument(help="A local path, or an owner/name GitHub slug."),
    ] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    github: Annotated[
        bool, typer.Option("--github", help="Treat the argument as a GitHub repository to clone.")
    ] = False,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Where the project is deployed, e.g. https://app.vercel.app"),
    ] = None,
) -> None:
    """Add a project: a repository, and the URL it is deployed at.

    A local directory is linked in place and never modified. A GitHub
    repository is cloned into ~/.testtrout/repos first.

    Pass --url now if you can. Without a deployment the first scan can only
    read code; with one it can also check what the running system actually
    does, which is the difference between a guess and evidence.
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
    except (RepoError, Exception) as exc:  # surfaced with a usable message
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if url:
        from testtrout.app import settings

        try:
            settings.apply(
                QaPaths(root=Path(record.path)),
                {"entrypoints": [{"name": "deployment", "url": url}]},
            )
        except settings.SettingsError as exc:
            render.error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

    registry.queue_initial_scan(record)
    render.console.print(f"[green]added[/green] {record.name}")
    # style= rather than [dim]…[/dim]: a path or URL is data, and a bracket in
    # one would otherwise be read as markup and swallowed.
    render.console.print(f"  {record.path}", style="dim", markup=False)
    if url:
        render.console.print(f"  {url}", style="dim", markup=False)
    else:
        render.console.print("[dim]no deployment yet — add one with `trout init --url <url>`[/dim]")
    render.console.print(
        "[dim]a scan is queued — run `trout up` to process it, or `trout scan` now[/dim]"
    )


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
        render.console.print("[dim]no repositories linked — `trout link <path>`[/dim]")
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
def plan(path: PathArg = None, as_json: JsonOpt = False) -> None:
    """Show what can be tested now, and what each missing piece would unlock.

    A partial set of credentials gives a partial suite, not an error. This says
    exactly which one thing to supply next.
    """
    from testtrout.app import settings

    paths = _resolve(path)
    data = settings.view(paths)

    if as_json:
        typer.echo(json.dumps(data.as_dict(), indent=2))
        return
    render.plan(data)


@app.command(name="config")
def config_command(
    path: PathArg = None,
    as_json: JsonOpt = False,
    set_secret: Annotated[
        list[str] | None,
        typer.Option("--set-secret", help="NAME=value, written to .env. Repeatable."),
    ] = None,
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    model_name: Annotated[str | None, typer.Option("--model")] = None,
    isolation: Annotated[
        str | None, typer.Option("--isolation", help="local_reset | scoped_seed | branch")
    ] = None,
) -> None:
    """Show or change repository configuration.

    The same settings the interface edits. Secrets go to the gitignored .env;
    configuration only ever stores their names.
    """
    from testtrout.app import settings

    paths = _resolve(path)
    patch: dict[str, object] = {}
    if provider or model_name:
        patch["model"] = {k: v for k, v in (("provider", provider), ("model", model_name)) if v}
    if isolation:
        patch["supabase"] = {"isolation": isolation}

    try:
        if patch:
            settings.apply(paths, patch)
        if set_secret:
            pairs = {}
            for item in set_secret:
                if "=" not in item:
                    render.error_console.print(f"[red]expected NAME=value, got {item!r}[/red]")
                    raise typer.Exit(2)
                name, value = item.split("=", 1)
                pairs[name.strip()] = value
            written = settings.set_secrets(paths, pairs)
            render.console.print(
                f"[green]wrote {len(written)} value(s) to .env[/green] [dim]{', '.join(written)}[/dim]"
            )
    except settings.SettingsError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    data = settings.view(paths)
    if as_json:
        typer.echo(json.dumps(data.as_dict(), indent=2))
        return
    render.config_view(data)


@app.command()
def questions(
    path: PathArg = None,
    as_json: JsonOpt = False,
    answer: Annotated[
        list[str] | None,
        typer.Option("--answer", help="ID=your answer. Repeatable."),
    ] = None,
    dismiss: Annotated[
        list[str] | None, typer.Option("--dismiss", help="Question id to set aside.")
    ] = None,
) -> None:
    """What TestTrout needs from you to write better tests.

    Everything the tool could not determine, in one list: unresolved code,
    blocked capabilities, and tests it could not prove. Answering one usually
    unblocks several tests.
    """
    from testtrout.domain.question import QuestionLog

    paths = _resolve(path)
    log = read_model(paths.questions, QuestionLog) if paths.questions.is_file() else QuestionLog()

    changed = 0
    for item in answer or []:
        if "=" not in item:
            render.error_console.print(f"[red]expected ID=answer, got {item!r}[/red]")
            raise typer.Exit(2)
        identifier, text = item.split("=", 1)
        question = log.get(identifier.strip())
        if question is None:
            render.error_console.print(f"[red]no question {identifier.strip()!r}[/red]")
            raise typer.Exit(2)
        question.resolve(text.strip())
        changed += 1
    for identifier in dismiss or []:
        question = log.get(identifier.strip())
        if question is not None:
            question.dismiss()
            changed += 1
    if changed:
        paths.ensure()
        write_model(paths.questions, log)

    if as_json:
        typer.echo(log.model_dump_json(indent=2, exclude_none=True))
        return
    render.questions(log)


def main() -> None:
    """Console script entry point."""
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
