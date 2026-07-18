"""decaf command-line interface."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import __version__
from .config import ConfigError, load_config
from .pipeline import ArtifactReport, DecafError, RunReport, Settings, run
from .scanner import ScanError

app = typer.Typer(add_completion=False, context_settings={"help_option_names": ["-h", "--help"]})
console = Console()


class Engine(str, Enum):
    vineflower = "vineflower"
    cfr = "cfr"
    procyon = "procyon"
    fernflower = "fernflower"
    jd = "jd"


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]error:[/] {message}")
    return typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"decaf {__version__}")
        raise typer.Exit()


def _status_line(r: ArtifactReport) -> str:
    if r.outcome == "ok":
        if r.method == "maven":
            detail = f"maven sources, {r.gav}"
        elif r.method == "extracted":
            detail = "extracted sources jar"
        else:
            detail = f"{r.method}, {r.classes} classes"
            if r.missing_classes:
                detail += f", [yellow]{r.missing_classes} missing[/]"
        return f"[green]✓[/] {r.rel} ({detail})"
    if r.outcome == "skipped":
        return f"[yellow]-[/] {r.rel} (resource-only, skipped)"
    reason = (r.failure or "failed").splitlines()[-1]
    return f"[red]✗[/] {r.rel} ({reason})"


def _print_summary(report: RunReport, verbose: bool) -> None:
    t = report.totals
    table = Table(title="decaf summary", show_header=False)
    table.add_row("Artifacts", str(t["artifacts"]))
    table.add_row("OK", f"{t['ok']} (maven {t['maven_sources']}, decompiled {t['decompiled']}, extracted {t['extracted']})")
    table.add_row("Skipped", str(t["skipped"]))
    table.add_row("Failed", str(t["failed"]))
    table.add_row("Java files", str(t["java_files"]))
    table.add_row("Collisions", str(t["collisions"]))
    table.add_row("Duration", f"{report.duration_seconds}s")
    console.print(table)
    failed = [r for r in report.artifacts if r.outcome == "failed"]
    for r in failed[:20]:
        console.print(_status_line(r))
        if verbose:
            for a in r.attempts:
                if a.stderr_tail:
                    console.print(f"    [dim]{a.engine} ({a.level}): {a.stderr_tail[-300:]}[/]")
    if report.interrupted:
        console.print("[yellow]interrupted — partial results written[/]")


@app.command()
def main(
    input: Annotated[Path, typer.Argument(help="Folder to scan recursively, or a single archive", show_default=False)],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output directory")] = Path("decaf-out"),
    engine: Annotated[Engine, typer.Option("--engine", help="Primary decompiler engine")] = Engine.vineflower,
    no_fallback: Annotated[bool, typer.Option("--no-fallback", help="Do not try other engines on failure")] = False,
    mirror: Annotated[bool, typer.Option("--mirror", help="Mirror the input layout instead of one merged src/ tree")] = False,
    no_maven: Annotated[bool, typer.Option("--no-maven", help="Skip Maven sources lookup, always decompile")] = False,
    repo: Annotated[Optional[list[str]], typer.Option("--repo", help="Extra Maven repository URL (repeatable)")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=0, help="Parallel workers (0 = min(4, cpus))")] = 0,
    cpus: Annotated[int, typer.Option("--cpus", min=0, help="Total CPU budget across all workers (0 = all cores)")] = 0,
    timeout: Annotated[float, typer.Option("--timeout", help="Per-archive engine timeout, seconds")] = 600.0,
    force: Annotated[bool, typer.Option("--force", help="Allow writing into a non-empty output directory")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show engine stderr for failures")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print the final summary")] = False,
    version: Annotated[bool, typer.Option("--version", callback=_version_callback, is_eager=True,
                                          help="Print version and exit")] = False,
) -> None:
    """Decompile every Java artifact under INPUT, preferring real Maven sources."""
    if not input.exists():
        raise _fail(f"input {input} does not exist")
    if output.exists() and any(output.iterdir()) and not force:
        raise _fail(f"output {output} is not empty (use --force to write anyway)")
    try:
        cfg = load_config(config, extra_repos=repo or [])
    except ConfigError as exc:
        raise _fail(str(exc))

    settings = Settings(
        input=input,
        output=output,
        engine=engine.value,
        fallback=not no_fallback,
        mirror=mirror,
        maven=not no_maven,
        jobs=jobs,
        cpus=cpus,
        timeout=timeout,
        repos=cfg.repositories,
        verbose=verbose,
        quiet=quiet,
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("decompiling"),
        MofNCompleteColumn(),
        console=console,
        transient=True,
        disable=quiet,
    )

    def on_done(r: ArtifactReport) -> None:
        progress.advance(task)
        if not quiet:
            progress.console.print(_status_line(r))

    try:
        with progress:
            task = progress.add_task("decompiling", total=None)
            report = run(settings, on_done=on_done)
    except (DecafError, ScanError) as exc:
        raise _fail(str(exc))

    _print_summary(report, verbose)
    if report.interrupted:
        raise typer.Exit(code=130)
    raise typer.Exit(code=0 if report.totals["failed"] == 0 else 1)
