"""Terminal rendering helpers.

Kept separate from command definitions so that output formatting can change
without touching argument parsing, and so the same renderers can be reused by
the web interface's text views.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from testtrout.domain.gap import GapMap
from testtrout.domain.intent import ProductIntent
from testtrout.domain.observation import Divergence, ProbeResult
from testtrout.domain.run import RunRecord, RunStatus
from testtrout.domain.scenario import ScenarioIndex
from testtrout.domain.surface import Criticality, ScanResult
from testtrout.planning.selection import Selection

console = Console()
error_console = Console(stderr=True)

CRITICALITY_STYLE = {
    Criticality.CRITICAL: "bold red",
    Criticality.HIGH: "yellow",
    Criticality.MEDIUM: "cyan",
    Criticality.LOW: "dim",
}


def criticality_text(level: Criticality) -> Text:
    """Render a criticality level with a consistent colour."""
    return Text(level.value, style=CRITICALITY_STYLE[level])


def scan_summary(result: ScanResult) -> None:
    """Print the headline result of a scan."""
    project = result.project
    console.print()
    console.print(
        f"[bold]{project.framework}[/bold]"
        + (f" + [bold]{project.backend}[/bold]" if project.backend else "")
        + (f" · auth: {project.auth}" if project.auth else "")
    )
    if project.detected_from:
        console.print(f"[dim]detected from: {'; '.join(project.detected_from)}[/dim]")
    console.print()

    counts = result.counts
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="bold", justify="right")
    table.add_column()
    labels = {
        "screens": "screens",
        "data_operations": "data operations",
        "endpoints": "endpoints",
        "server_actions": "server actions",
        "edge_functions": "edge functions",
        "policies": "RLS policies",
        "externals": "third-party dependencies",
        "tables": "tables",
    }
    for key, label in labels.items():
        if counts.get(key):
            table.add_row(str(counts[key]), label)
    console.print(table)

    by_level: dict[Criticality, int] = {}
    for surface in result.all_surfaces():
        by_level[surface.criticality] = by_level.get(surface.criticality, 0) + 1
    if by_level:
        console.print()
        parts = [
            f"[{CRITICALITY_STYLE[level]}]{by_level[level]} {level.value}[/]"
            for level in Criticality
            if level in by_level
        ]
        console.print("  ".join(parts))


def surface_table(result: ScanResult, limit: int | None = None) -> None:
    """Print surfaces ordered by criticality."""
    surfaces = result.all_surfaces()
    if limit is not None:
        surfaces = surfaces[:limit]

    table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
    table.add_column("criticality")
    table.add_column("id", style="bold")
    table.add_column("detail", overflow="fold")
    table.add_column("source", style="dim")

    for surface in surfaces:
        table.add_row(
            criticality_text(surface.criticality),
            surface.id,
            _detail(surface),
            str(surface.location),
        )
    console.print(table)


def _detail(surface: object) -> str:
    """One-line description appropriate to the surface kind."""
    kind = getattr(surface, "kind", None)
    if kind == "screen":
        reaches = len(getattr(surface, "reaches", []))
        return (
            f"{getattr(surface, 'path', '')} → {getattr(surface, 'component', '')} ({reaches} ops)"
        )
    if kind == "data_operation":
        table = getattr(surface, "table", None) or getattr(surface, "function", None) or "?"
        filters = getattr(surface, "filters", [])
        suffix = f" · {', '.join(filters[:2])}" if filters else ""
        return f"{getattr(surface, 'operation', '')} {table}{suffix}"
    if kind == "endpoint":
        return f"{'/'.join(getattr(surface, 'methods', []))} {getattr(surface, 'path', '')}"
    if kind == "server_action":
        return f"{getattr(surface, 'name', '')}()"
    if kind == "policy":
        return f"{getattr(surface, 'command', '')} on {getattr(surface, 'table', '')}"
    if kind == "external":
        return getattr(surface, "package", "")
    return ""


def warnings(result: ScanResult) -> None:
    """Print scan warnings, which are blind spots rather than errors."""
    if not result.warnings:
        return
    console.print()
    console.print(f"[yellow]{len(result.warnings)} warning(s)[/yellow]")
    for warning in result.warnings[:20]:
        location = f" [dim]{warning.location}[/dim]" if warning.location else ""
        console.print(f"  [yellow]·[/yellow] {warning.message}{location}")
    if len(result.warnings) > 20:
        console.print(f"  [dim]… and {len(result.warnings) - 20} more[/dim]")


def probe_summary(result: ProbeResult) -> None:
    """Print what a probe observed."""
    from testtrout.domain.observation import CallKind

    console.print()
    auth = (
        f"as [bold]{result.role}[/bold]" if result.authenticated else "[yellow]signed out[/yellow]"
    )
    console.print(f"probed [bold]{result.entrypoint}[/bold] ({result.base_url}) {auth}")
    console.print()

    table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
    table.add_column("screen", style="bold")
    table.add_column("status")
    table.add_column("queries")
    table.add_column("selectors")

    for screen in result.screens:
        rest = [c for c in screen.calls if c.kind is CallKind.SUPABASE_REST]
        if not screen.reachable:
            state = "[red]unreachable[/red]"
        elif screen.requires_auth:
            state = "[yellow]redirected to sign-in[/yellow]"
        else:
            state = f"[green]{screen.status or 'ok'}[/green]"
        tables = sorted({c.table for c in rest if c.table})
        table.add_row(
            screen.path,
            state,
            ", ".join(tables) if tables else "[dim]none[/dim]",
            str(len(screen.selectors)) if screen.selectors else "[dim]0[/dim]",
        )
    console.print(table)

    if result.external_hosts:
        console.print()
        console.print(f"[dim]third-party hosts contacted: {', '.join(result.external_hosts)}[/dim]")


def divergences(items: list[Divergence]) -> None:
    """Print reconciliation findings, grouped by code."""
    if not items:
        console.print()
        console.print("[green]no divergences between the code and the deployment[/green]")
        return

    grouped: dict[str, list[Divergence]] = {}
    for item in items:
        grouped.setdefault(item.code, []).append(item)

    console.print()
    console.print(f"[bold]{len(items)} finding(s)[/bold]")
    for code, entries in grouped.items():
        style = "green" if code in {"auth_ok", "protected_route"} else "yellow"
        console.print()
        console.print(f"[{style}]{code}[/{style}] [dim]x{len(entries)}[/dim]")
        for entry in entries[:6]:
            console.print(f"  · {entry.message}")
            if entry.detail:
                console.print(f"    [dim]{entry.detail}[/dim]")
        if len(entries) > 6:
            console.print(f"  [dim]… and {len(entries) - 6} more[/dim]")


def intent_summary(intent: ProductIntent, warnings: list[str] | None = None) -> None:
    """Print captured product intent."""
    console.print()
    if intent.summary:
        console.print(intent.summary)
    if intent.audience:
        console.print(f"[dim]for: {intent.audience}[/dim]")

    if intent.journeys:
        console.print()
        table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
        table.add_column("criticality")
        table.add_column("journey", style="bold")
        table.add_column("consequence if it breaks", overflow="fold")
        table.add_column("surfaces", justify="right")
        for journey in sorted(intent.journeys, key=lambda j: j.criticality.rank):
            table.add_row(
                criticality_text(journey.criticality),
                journey.name,
                journey.consequence or "[dim]not stated[/dim]",
                str(len(journey.surfaces)),
            )
        console.print(table)

    if intent.never_break:
        console.print()
        console.print("[bold]Must always hold[/bold]")
        for item in intent.never_break:
            console.print(f"  · {item}")

    if intent.unanswered:
        console.print()
        console.print(f"[yellow]{len(intent.unanswered)} open question(s)[/yellow]")
        for question in intent.unanswered[:8]:
            console.print(f"  [yellow]?[/yellow] {question.question}")
            if question.context:
                console.print(f"    [dim]{question.context}[/dim]")

    for warning in warnings or []:
        console.print(f"  [yellow]·[/yellow] {warning}")


def gap_map(result: GapMap, limit: int | None = None, ready_only: bool = False) -> None:
    """Print the ranked gap map."""
    coverage = result.coverage
    console.print()
    console.print(
        f"[bold]{coverage.percent}%[/bold] of surfaces covered "
        f"([bold]{coverage.critical_percent}%[/bold] of critical ones) · "
        f"{coverage.policies_covered}/{coverage.policies_total} policies"
    )

    gaps = result.ranked(limit=limit, ready_only=ready_only)
    if not gaps:
        console.print()
        console.print("[green]nothing to propose[/green]")
        return

    total = sum(g.estimated_seconds for g in gaps)
    console.print(
        f"[dim]{len(gaps)} gap(s) shown · ~{total // 60}m {total % 60}s to run them all[/dim]"
    )
    console.print()

    table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
    table.add_column("criticality")
    table.add_column("kind")
    table.add_column("what the test would assert", overflow="fold")
    table.add_column("why", overflow="fold", style="dim")

    for gap in gaps:
        prefix = "" if gap.ready else "[red]blocked[/red] "
        table.add_row(
            criticality_text(gap.criticality),
            gap.kind.value.replace("_", " "),
            prefix + gap.title,
            "; ".join(gap.reasons[1:3]) or gap.reasons[0],
        )
    console.print(table)

    blocked_gaps = [g for g in result.gaps if not g.ready]
    if blocked_gaps and not ready_only:
        console.print()
        console.print(f"[yellow]{len(blocked_gaps)} gap(s) blocked[/yellow]")
        seen: set[str] = set()
        for gap in blocked_gaps:
            for blocker in gap.blockers:
                if blocker.code in seen:
                    continue
                seen.add(blocker.code)
                console.print(f"  [yellow]·[/yellow] {blocker.message}")

    for note in result.notes:
        console.print()
        console.print(f"[dim]note: {note}[/dim]")


def scenario_table(index: ScenarioIndex, limit: int | None = None) -> None:
    """Print scenarios with their status."""
    scenarios = index.scenarios[:limit] if limit else index.scenarios
    if not scenarios:
        console.print()
        console.print("[dim]no scenarios yet — run `qa propose`[/dim]")
        return

    styles = {
        "draft": "yellow",
        "approved": "cyan",
        "certified": "green",
        "quarantined": "red",
        "rejected": "dim",
    }
    table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
    table.add_column("status")
    table.add_column("criticality")
    table.add_column("id", style="bold")
    table.add_column("asserts", overflow="fold")

    for scenario in sorted(scenarios, key=lambda s: (s.criticality.rank, s.id)):
        style = styles.get(scenario.status.value, "white")
        marker = "" if scenario.ready_to_approve else " [yellow]?[/yellow]"
        table.add_row(
            f"[{style}]{scenario.status.value}[/{style}]",
            criticality_text(scenario.criticality),
            scenario.id.replace("scenario:", ""),
            scenario.title + marker,
        )
    console.print()
    console.print(table)

    unresolved = [s for s in scenarios if s.open_questions]
    if unresolved:
        console.print()
        console.print(
            f"[yellow]{len(unresolved)} scenario(s) have open questions[/yellow] "
            "[dim](answer them in the .yaml, then approve)[/dim]"
        )
        for scenario in unresolved[:5]:
            console.print(f"  [yellow]?[/yellow] {scenario.id.replace('scenario:', '')}")
            for question in scenario.open_questions[:2]:
                console.print(f"    [dim]{question}[/dim]")


def generated(files: list[tuple[str, list[str]]]) -> None:
    """Print what was written and anything the developer must do."""
    console.print()
    console.print(f"[green]{len(files)} file(s) written[/green]")
    for path, _ in files:
        console.print(f"  [dim]{path}[/dim]")

    notes = [note for _, notes in files for note in notes]
    if notes:
        console.print()
        console.print(f"[yellow]{len(notes)} note(s)[/yellow]")
        for note in dict.fromkeys(notes):
            console.print(f"  [yellow]·[/yellow] {note}")


def run_summary(record: RunRecord) -> None:
    """Print the outcome of a run."""
    styles = {
        "pass": "green",
        "warning": "yellow",
        "fail": "red",
        "inconclusive": "magenta",
    }
    status = record.status
    console.print()
    console.print(
        f"[{styles[status.value]}]{status.value.upper()}[/{styles[status.value]}] "
        f"[dim]{record.entrypoint} · {record.duration_seconds}s · "
        f"isolation: {record.isolation}[/dim]"
    )

    if record.results:
        console.print()
        table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
        table.add_column("result")
        table.add_column("scenario", style="bold")
        table.add_column("detail", overflow="fold")

        marks = {
            "passed": "[green]pass[/green]",
            "assertion_failure": "[red]FAIL[/red]",
            "flake": "[yellow]flake[/yellow]",
            "contract_mismatch": "[yellow]blocked[/yellow]",
            "skipped": "[dim]skip[/dim]",
        }
        for result in record.results:
            mark = marks.get(
                result.classification.value, f"[magenta]{result.classification.value}[/magenta]"
            )
            table.add_row(
                mark,
                result.scenario_id.replace("scenario:", ""),
                result.message or result.title,
            )
        console.print(table)

    regressions = record.regressions
    if regressions:
        console.print()
        console.print(f"[red]{len(regressions)} assertion failure(s)[/red]")
        for result in regressions:
            console.print(f"  [red]·[/red] {result.title or result.scenario_id}")
            if result.message:
                console.print(f"    {result.message}")
            if result.evidence.reproduce:
                console.print(f"    [dim]reproduce: {result.evidence.reproduce}[/dim]")
            if result.evidence.trace:
                console.print(f"    [dim]trace: {result.evidence.trace}[/dim]")

    if status is RunStatus.INCONCLUSIVE:
        console.print()
        console.print(
            "[magenta]This run says nothing about the product.[/magenta] "
            "[dim]Something prevented a reliable decision — see the notes below.[/dim]"
        )

    for note in record.notes:
        console.print(f"[dim]note: {note}[/dim]")


def selection(chosen: Selection) -> None:
    """Explain which scenarios a change selected, and why."""
    console.print()
    console.print(
        f"[bold]{len(chosen.scenarios)}[/bold] scenario(s) selected from "
        f"{len(chosen.changed_files)} changed file(s)"
    )
    if chosen.changed_surfaces:
        console.print(f"[dim]changed surfaces: {', '.join(chosen.changed_surfaces[:6])}[/dim]")

    for scenario_id, why in list(chosen.reasons.items())[:12]:
        console.print(f"  · {scenario_id.replace('scenario:', '')}")
        console.print(f"    [dim]{'; '.join(why)}[/dim]")

    if chosen.uncovered_surfaces:
        console.print()
        console.print(
            f"[yellow]{len(chosen.uncovered_surfaces)} changed surface(s) have no test[/yellow]"
        )
        for surface_id in chosen.uncovered_surfaces[:8]:
            console.print(f"  [yellow]·[/yellow] {surface_id}")

    for note in chosen.notes:
        console.print(f"[dim]note: {note}[/dim]")
