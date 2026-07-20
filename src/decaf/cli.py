"""decaf command-line interface."""

from __future__ import annotations

import shutil
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from typer.core import TyperGroup

from . import __version__, engines
from . import update
from .config import ConfigError, default_config_path, load_config, write_engine_pins
from .pipeline import ArtifactReport, DecafError, RunReport, Settings, run
from .scanner import ScanError


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"decaf {__version__}")
        raise typer.Exit()


class DefaultGroup(TyperGroup):
    """Route unknown first tokens to the run command, so `decaf INPUT` still works.

    Modeled on click-default-group: unknown options are ignored at group level
    and re-attached in front of the default command's args.
    """

    default_command = "run"
    ignore_unknown_options = True

    def get_command(self, ctx, cmd_name):
        if cmd_name not in self.commands:
            ctx._default_arg0 = cmd_name  # type: ignore[attr-defined]
            cmd_name = self.default_command
        return super().get_command(ctx, cmd_name)

    def resolve_command(self, ctx, args):
        cmd_name, cmd, rest = super().resolve_command(ctx, args)
        arg0 = getattr(ctx, "_default_arg0", None)
        if arg0 is not None:
            rest = [arg0, *rest]
        return cmd_name, cmd, rest


app = typer.Typer(
    cls=DefaultGroup,
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()


@app.callback()
def _root(
    version: Annotated[bool, typer.Option("--version", callback=_version_callback, is_eager=True,
                                          help="Print version and exit")] = False,
) -> None:
    """All-in-one Java decompiler."""


engines_app = typer.Typer(name="engines", help="Manage decompiler engines", no_args_is_help=True)
app.add_typer(engines_app)


def _active_specs_from(config: Optional[Path]):
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        raise _fail(str(exc))
    return cfg, engines.active_specs(cfg.engine_overrides)


@engines_app.command("list")
def engines_list(
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
) -> None:
    """Show engine pins, cache state, and Java compatibility."""
    cfg, specs = _active_specs_from(config)
    found = engines.find_java()
    table = Table("ENGINE", "PIN", "CACHED", "JAVA")
    for name in engines.ENGINE_ORDER:
        spec = specs[name]
        pin = spec.version + ("†" if name in cfg.engine_overrides else "")
        cached = "yes" if engines.cache_status(spec) else "no"
        if found is None:
            java = "no java"
        elif found[1] >= spec.min_java:
            java = "ok"
        else:
            java = f"needs {spec.min_java}+"
        table.add_row(name, pin, cached, java)
    console.print(table)
    if cfg.engine_overrides:
        console.print("[dim]† pinned in config[/]")
    console.print(f"[dim]cache: {engines.cache_root() / 'engines'}[/]")
    console.print(f"[dim]java: {found[0]} (major {found[1]})[/]" if found else "[dim]java: not found[/]")


@engines_app.command("fetch")
def engines_fetch(
    names: Annotated[Optional[list[Engine]], typer.Argument(help="Engines to fetch (default: all)")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
) -> None:
    """Download engines into the cache ahead of time (offline/CI prep)."""
    cfg, specs = _active_specs_from(config)
    wanted = [n.value for n in names] if names else list(engines.ENGINE_ORDER)
    failed = False
    with httpx.Client() as client:
        for name in wanted:
            spec = specs[name]
            if engines.cache_status(spec):
                console.print(f"[green]✓[/] {name} {spec.version} already cached")
                continue
            try:
                engines.ensure_engine(spec, client)
            except engines.EngineError as exc:
                console.print(f"[red]✗[/] {exc}")
                failed = True
                continue
            console.print(f"[green]✓[/] {name} {spec.version} downloaded")
    raise typer.Exit(code=1 if failed else 0)


def _tree_size(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
    return len(files), sum(p.stat().st_size for p in files)


@engines_app.command("clean")
def engines_clean(
    stale: Annotated[bool, typer.Option("--stale", help="Only remove files no active pin claims")] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
) -> None:
    """Delete the engine cache (or just superseded files with --stale)."""
    cfg, specs = _active_specs_from(config)
    cache = engines.cache_root() / "engines"
    if not cache.is_dir():
        console.print("nothing to clean")
        return
    keep = {f"{s.name}-{s.version}.jar" for s in specs.values()}
    victims = [p for p in cache.iterdir() if not (stale and p.name in keep)]
    files = size = 0
    for p in victims:
        n, s = _tree_size(p)
        files += n
        size += s
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    if not stale:
        shutil.rmtree(cache, ignore_errors=True)
    console.print(f"removed {files} files ({size / 1e6:.1f} MB)")


class Engine(str, Enum):
    vineflower = "vineflower"
    cfr = "cfr"
    procyon = "procyon"
    fernflower = "fernflower"
    jd = "jd"


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]error:[/] {message}")
    return typer.Exit(code=2)


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
        return f"[yellow]-[/] {r.rel} ({r.failure or 'resource-only'}, skipped)"
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


@app.command(name="run")
def main(
    input: Annotated[Path, typer.Argument(help="Folder to scan recursively, or a single archive", show_default=False)],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output directory")] = Path("decaf-out"),
    engine: Annotated[Engine, typer.Option("--engine", help="Primary decompiler engine")] = Engine.vineflower,
    no_fallback: Annotated[bool, typer.Option("--no-fallback", help="Do not try other engines on failure")] = False,
    merge: Annotated[bool, typer.Option("--merge", help="Merge all sources into one src/ tree instead of mirroring the input layout")] = False,
    no_maven: Annotated[bool, typer.Option("--no-maven", help="Skip Maven sources lookup, always decompile")] = False,
    max_depth: Annotated[int, typer.Option("--max-depth", min=0, help="Archive-in-archive levels to unpack (0 = none; folders are always fully scanned)")] = 1,
    repo: Annotated[Optional[list[str]], typer.Option("--repo", help="Extra Maven repository URL (repeatable)")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=0, help="Parallel workers (0 = min(4, cpus))")] = 0,
    cpus: Annotated[int, typer.Option("--cpus", min=0, help="Total CPU budget across all workers (0 = all cores minus one)")] = 0,
    timeout: Annotated[float, typer.Option("--timeout", help="Per-archive engine timeout, seconds")] = 600.0,
    force: Annotated[bool, typer.Option("--force", help="Allow writing into a non-empty output directory")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Stream engine stderr live and show it for failures")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print the final summary")] = False,
) -> None:
    """Decompile every Java artifact under INPUT, preferring real Maven sources."""
    if not input.exists():
        raise _fail(f"input {input} does not exist")
    if output.exists() and not output.is_dir():
        raise _fail(f"output {output} exists and is not a directory")
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
        mirror=not merge,
        maven=not no_maven,
        max_depth=max_depth,
        jobs=jobs,
        cpus=cpus,
        timeout=timeout,
        repos=cfg.repositories,
        verbose=verbose,
        quiet=quiet,
        engine_overrides=cfg.engine_overrides,
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("decompiling"),
        MofNCompleteColumn(),
        console=console,
        transient=True,
        disable=quiet,
    )

    found_total = 0

    def on_found(count: int) -> None:
        nonlocal found_total
        found_total += count
        progress.update(task, total=found_total)

    def on_done(r: ArtifactReport) -> None:
        progress.advance(task)
        if not quiet:
            progress.console.print(_status_line(r))

    def on_stderr(text: str) -> None:
        progress.console.print(f"[dim]{escape(text)}[/]")

    try:
        with progress:
            task = progress.add_task("decompiling", total=None)
            report = run(settings, on_done=on_done, on_found=on_found,
                         on_stderr=on_stderr if verbose else None)
    except (DecafError, ScanError) as exc:
        raise _fail(str(exc))

    _print_summary(report, verbose)
    if report.interrupted:
        raise typer.Exit(code=130)
    raise typer.Exit(code=0 if report.totals["failed"] == 0 else 1)


@engines_app.command("update")
def engines_update(
    names: Annotated[Optional[list[Engine]], typer.Argument(help="Engines to update (default: all)")] = None,
    version: Annotated[Optional[str], typer.Option("--version", help="Pin this exact version (one engine only)")] = None,
    reset: Annotated[bool, typer.Option("--reset", help="Remove overrides, restoring built-in pins")] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
) -> None:
    """Update engine pins to upstream latest (or --version) and record them in config."""
    if version is not None and (reset or len(names or []) != 1):
        raise _fail("--version needs exactly one engine name (and no --reset)")
    cfg_path = config or default_config_path()
    cfg, specs = _active_specs_from(config)
    overrides = dict(cfg.engine_overrides)
    wanted = [n.value for n in names] if names else list(engines.ENGINE_ORDER)

    if reset:
        removed = [n for n in wanted if overrides.pop(n, None) is not None]
        if not removed:
            console.print("no overrides to reset")
            return
        write_engine_pins(cfg_path, overrides)
        for name in removed:
            console.print(f"[green]✓[/] {name}: restored built-in pin {engines.ENGINES[name].version}")
        return

    failed = False
    with httpx.Client() as client:
        for name in wanted:
            spec = specs[name]
            try:
                res = update.update_engine(
                    spec, client, engines.cache_root() / "engines", version=version,
                    warn=lambda msg: console.print(f"[yellow]{msg}[/]"),
                )
            except engines.EngineError as exc:
                console.print(f"[red]✗[/] {exc}")
                failed = True
                continue
            if res is None:
                console.print(f"[green]✓[/] {name} {spec.version} already at latest")
                continue
            overrides[name] = res.pin
            write_engine_pins(cfg_path, overrides)
            console.print(f"[green]✓[/] {name} {res.old_version} → {res.version} (pinned in {cfg_path})")
    raise typer.Exit(code=1 if failed else 0)
