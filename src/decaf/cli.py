"""decaf command-line interface."""

from __future__ import annotations

import dataclasses
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.progress import Progress, ProgressColumn, SpinnerColumn, TaskID
from rich.table import Column, Table
from rich.text import Text
from typer.core import TyperGroup

from . import __version__, check, engines, memory
from . import report_view, runlock, selfupdate, update
from .config import ConfigError, default_config_path, load_config, write_engine_pins
from .pipeline import ArtifactReport, DecafError, Settings, _engine_failure, run
from .report_view import _status_line, marks, plain, safe
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
            cmd_name = self.default_command  # the usage text names `run`, not the routed token
        return cmd_name, cmd, rest


app = typer.Typer(
    cls=DefaultGroup,
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console(emoji=False)


@app.callback()
def _root(
    version: Annotated[bool, typer.Option("--version", callback=_version_callback, is_eager=True,
                                          help="Print version and exit")] = False,
) -> None:
    """All-in-one Java decompiler.

    For AI coding agents, `npx skills add vcth4nh/decaf` installs the decaf
    skill: what each feature is for, and what a run gives back.
    """
    for stream in (sys.stdout, sys.stderr):  # a character that the stream cannot encode becomes an escape (#109)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    _for_logs(console)


def _for_logs(c: Console) -> Console:
    """Without a terminal, rich does not wrap lines, so a log file keeps each line whole (#109)."""
    c.soft_wrap = not c.is_terminal
    return c


_NDJSON_LOCK = threading.Lock()


def _emit(event: dict) -> None:
    """One ndjson line in one write: the lines of two threads never mix (#109)."""
    line = json.dumps(event) + "\n"
    with _NDJSON_LOCK:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except BrokenPipeError:
            if threading.current_thread() is threading.main_thread():
                raise
            # A fetch thread would fail its artifact with this error. The main
            # thread gets the same error at its next line and stops the run.


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
    errors: list[str] = []
    for name in engines.ENGINE_ORDER:
        spec = specs[name]
        pin = spec.version + ("†" if name in cfg.engine_overrides else "")
        try:
            cached = "yes" if engines.cache_status(spec) else "no"
        except OSError as exc:  # e.g. an unreadable jar (#113)
            cached = "error"
            errors.append(f"{name}: {_engine_failure(name, exc)}")
        jar = engines.cache_root() / "engines" / f"{name}-{spec.version}.jar"
        need = engines.required_java(spec, jar)  # a pin can need a newer Java than its spec (#113)
        if found is None:
            java = "no java"
        elif found[1] >= need:
            java = "ok"
        else:
            java = f"needs {need}+"
        table.add_row(name, pin, cached, java)
    console.print(table)
    for text in errors:
        console.print(f"[red]{marks(console)[1]}[/] {safe(text)}")
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
    ok, bad = marks(console)
    with httpx.Client() as client:
        for name in wanted:
            spec = specs[name]
            try:
                if engines.cache_status(spec):
                    console.print(f"[green]{ok}[/] {name} {safe(spec.version)} already cached")
                    continue
                engines.ensure_engine(spec, client)
            except (engines.EngineError, OSError) as exc:  # e.g. a read-only engine cache (#113)
                console.print(f"[red]{bad}[/] {safe(f'{name}: {_engine_failure(name, exc)}')}")
                failed = True
                continue
            console.print(f"[green]{ok}[/] {name} {safe(spec.version)} downloaded")
    raise typer.Exit(code=1 if failed else 0)


_MOUNT_POINT = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT: the reparse tag of a Windows junction


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows junction (Path.is_junction comes only with Python 3.12)."""
    return path.is_symlink() or getattr(path.lstat(), "st_reparse_tag", 0) == _MOUNT_POINT


def _tree_size(path: Path) -> tuple[int, int]:
    if _is_link(path):
        return 1, path.lstat().st_size  # the link, not its target (#113)
    files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
    return len(files), sum(p.stat().st_size for p in files)


@engines_app.command("clean")
def engines_clean(
    stale: Annotated[bool, typer.Option("--stale", help="Only remove files no active pin claims")] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
) -> None:
    """Delete the engine cache and the compiled checker (or just superseded files with --stale)."""
    cfg, specs = _active_specs_from(config)
    cache = engines.cache_root() / "engines"
    compiled = engines.cache_root() / "checker"  # the checker of each source hash and Java version (#101)
    if not cache.is_dir() and not compiled.is_dir():
        console.print("nothing to clean")
        return
    # The CDS archives of the active pins and of the current checker are no superseded files (#113).
    keep = {f"{s.name}-{s.version}{ext}" for s in specs.values() for ext in (".jar", ".jsa")}
    digest = check.source_digest()
    victims = [p for p in (cache.iterdir() if cache.is_dir() else [])
               if not (stale and (p.name in keep or p.name.startswith(f"checker-{digest}-")))]
    victims += [p for p in (compiled.iterdir() if compiled.is_dir() else [])
                if not (stale and p.name.startswith(f"{digest}-"))]
    files = size = 0
    for p in victims:
        n, s = _tree_size(p)
        files += n
        size += s
        shutil.rmtree(p) if p.is_dir() and not _is_link(p) else p.unlink()  # unlink removes a junction too
    if not stale:
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(compiled, ignore_errors=True)
    console.print(f"removed {files} files ({size / 1e6:.1f} MB)")


cache_app = typer.Typer(name="cache", help="Manage the Maven sources/verdict cache", no_args_is_help=True)
app.add_typer(cache_app)


@cache_app.command("clean")
def cache_clean() -> None:
    """Delete cached Maven sources jars and lookup verdicts."""
    files = size = 0
    removed = False
    for sub in ("sources", "verdicts"):
        d = engines.cache_root() / sub
        if not d.is_dir():
            continue
        n, s = _tree_size(d)
        files += n
        size += s
        shutil.rmtree(d, ignore_errors=True)
        removed = True
    if not removed:
        console.print("nothing to clean")
        return
    console.print(f"removed {files} files ({size / 1e6:.1f} MB)")


class Engine(str, Enum):
    vineflower = "vineflower"
    cfr = "cfr"
    procyon = "procyon"
    fernflower = "fernflower"
    jd = "jd"


class Format(str, Enum):
    human = "human"
    json = "json"
    ndjson = "ndjson"


def _fail(message: str, code: int = 2) -> typer.Exit:
    console.print(f"[red]error:[/] {safe(message)}")
    return typer.Exit(code=code)


def _end_like_ctrl_c() -> dict:
    """SIGTERM and SIGHUP end the run like Ctrl-C, so the report is written and the temp files go (#112).

    Returns the old handlers. Python sets handlers only in the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    return {
        sig: signal.signal(sig, interrupt)
        for sig in (getattr(signal, name, None) for name in ("SIGTERM", "SIGHUP"))
        if sig is not None and signal.getsignal(sig) != signal.SIG_IGN  # nohup: a hangup stays ignored
    }


_STAGE_RANK = {"header": 0, "engines": 1, "fetch": 2, "decompile": 3}
_MAX_TIMEOUT = 4294967  # seconds: about 49 days, the longest wait of Windows
_HEARTBEAT_INTERVAL = 30.0


def _shorten(rel: str, limit: int = 60) -> str:
    """Middle-elide a long artifact rel, always keeping the leaf name."""
    if len(rel) <= limit:
        return rel
    leaf = rel.rsplit("/", 1)[-1]
    if leaf == rel or len(leaf) + 2 >= limit:
        return "…" + leaf[-(limit - 1):]
    prefix = rel[: limit - len(leaf) - 3]
    if "/" in prefix:
        prefix = prefix.rsplit("/", 1)[0]
    return f"{prefix}/…/{leaf}" if prefix else f"…/{leaf}"


def _elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60}m"


class _RowColumn(ProgressColumn):
    """Task description, plus a live '(engine · N classes · elapsed)' suffix on
    decompiling rows — recomputed each render, so it ticks with the spinner."""

    def render(self, task) -> Text:
        since = task.fields.get("since")
        if since is None:
            return Text(task.description)
        detail = task.fields["detail"]
        return Text(f"{task.description} ({detail} · {_elapsed(monotonic() - since)})")


class _GroupedProgress(Progress):
    """Progress that renders tasks grouped by their `stage` field.

    Stable sort: header, then the engines row, then fetching rows, then
    decompiling rows — insertion order preserved within each group, and
    TaskIDs never churn (no remove/re-add on stage transitions).
    """

    def _ordered_tasks(self) -> list:
        return sorted(self.tasks, key=lambda t: _STAGE_RANK.get(t.fields.get("stage"), 4))

    def get_renderables(self):
        yield self.make_tasks_table(self._ordered_tasks())


class _RunDisplay:
    """Routes pipeline events onto transient rich Progress rows.

    Runs under one lock: events arrive from the scheduler and worker threads,
    and the rel→row map must mutate atomically with the rich task list. rich
    never calls back into this class, so the single-lock order is safe.
    """

    def __init__(self, progress: Progress) -> None:
        self._p = progress
        self._lock = threading.Lock()
        self._header = progress.add_task("scanning…", total=None, stage="header")
        self._scanned = False
        self._total = 0
        self._done = 0
        self._rows: dict[str, TaskID] = {}
        self._stage: dict[str, str] = {}  # rel -> "fetch" | "decompile", rows only
        self._engines: TaskID | None = None

    def on_found(self, count: int) -> None:
        with self._lock:
            self._total += count
            self._refresh_header()

    def on_done(self, report: ArtifactReport) -> None:
        with self._lock:
            self._done += 1
            self._drop(report.rel)
            self._refresh_header()

    def on_event(self, kind: str, subject: str, detail: str) -> None:
        with self._lock:
            if kind == "scan":
                self._scanned = True
                self._p.console.print(f"found {self._total} artifacts ({detail})")
            elif kind == "memory":
                self._p.console.print(detail)
            elif kind == "engines":
                self._engines_event(subject, detail)
            elif kind == "queued":
                self._drop(subject)
            elif kind == "fetch":
                verb = detail or "fetching"
                self._upsert(subject, f"{verb:<17} {plain(_shorten(subject))}", kind)
            elif kind == "decompile":
                self._upsert(
                    subject, f"{'decompiling':<17} {plain(_shorten(subject))}", kind, detail=detail
                )
            elif kind == "progress":
                row = self._rows.get(subject)
                if row is not None and self._stage.get(subject) == "decompile":
                    self._p.update(row, detail=detail)  # detail only: clock and verb untouched
            self._refresh_header()

    def heartbeat_line(self, elapsed: float) -> str | None:
        with self._lock:
            if not self._scanned:
                return None
            fetching = sum(1 for s in self._stage.values() if s == "fetch")
            decompiling = len(self._stage) - fetching
            queued = max(0, self._total - self._done - len(self._stage))
            line = (
                f"[+{_elapsed(elapsed)}] {self._done}/{self._total} done"
                f" · {fetching} fetching · {decompiling} decompiling · {queued} queued"
            )
            longest = self._longest_active()
            if longest is not None:
                rel, since = longest
                line += f" · longest active {safe(_shorten(rel))} ({_elapsed(monotonic() - since)})"
            return line

    # -- internals; every method below expects the lock to be held --

    def _engines_event(self, subject: str, detail: str) -> None:
        if detail == "ready":
            if self._engines is not None:
                self._p.remove_task(self._engines)
                self._engines = None
            return
        if detail.startswith("downloaded "):
            ver = detail.split(" ", 1)[1]
            self._p.console.print(f"[green]{marks(self._p.console)[0]}[/] {subject} {safe(ver)} downloaded")
            text = "engines: verifying…"
        elif detail.startswith("downloading "):
            ver = detail.split(" ", 1)[1]
            text = f"engines: downloading {subject} {ver}…"
        else:  # "verifying"
            text = "engines: verifying…"
        if self._engines is None:
            self._engines = self._p.add_task(text, total=None, stage="engines")
        else:
            self._p.update(self._engines, description=text)

    def _upsert(self, rel: str, text: str, stage: str, detail: str = "") -> None:
        self._stage[rel] = stage
        fields = {"detail": detail, "since": monotonic() if detail else None}
        if rel in self._rows:
            self._p.update(self._rows[rel], description=text, stage=stage, **fields)
        else:
            self._rows[rel] = self._p.add_task(text, total=None, stage=stage, **fields)

    def _drop(self, rel: str) -> None:
        self._stage.pop(rel, None)
        row = self._rows.pop(rel, None)
        if row is not None:
            self._p.remove_task(row)

    def _refresh_header(self) -> None:
        if not self._scanned:
            return
        fetching = sum(1 for s in self._stage.values() if s == "fetch")
        decompiling = len(self._stage) - fetching
        queued = max(0, self._total - self._done - len(self._stage))
        self._p.update(
            self._header,
            description=(
                f"{self._done}/{self._total} done · {fetching} fetching"
                f" · {decompiling} decompiling · {queued} queued"
            ),
        )

    def _longest_active(self) -> tuple[str, float] | None:
        by_id = {t.id: t for t in self._p.tasks}
        best: tuple[str, float] | None = None
        for rel, task_id in self._rows.items():
            since = by_id[task_id].fields.get("since")
            if since is not None and (best is None or since < best[1]):
                best = (rel, since)
        return best


def _is_terminal() -> bool:
    """stdout is a terminal. rich also takes FORCE_COLOR as a terminal, so the notice asks the stream (#155).

    A missing stdout (fd 1 closed at the start) and a closed stream count as no terminal: a hint never stops a run.
    """
    isatty = getattr(sys.stdout, "isatty", None)
    try:
        return isatty is not None and isatty()
    except ValueError:  # a closed stream
        return False


@app.command(name="run")
def main(
    input: Annotated[Path, typer.Argument(help="Folder to scan recursively, or a single archive", show_default=False)],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output directory")] = Path("decaf-out"),
    engine: Annotated[Engine, typer.Option("--engine", help="Primary decompiler engine")] = Engine.vineflower,
    no_fallback: Annotated[bool, typer.Option("--no-fallback", help="Do not try other engines on failure")] = False,
    merge: Annotated[bool, typer.Option("--merge", help="Merge all sources into one src/ tree instead of mirroring the input layout")] = False,
    no_resource: Annotated[bool, typer.Option("--no-resource", help="Mirror decompiled/extracted sources only, no resource files")] = False,
    no_maven: Annotated[bool, typer.Option("--no-maven", help="Skip Maven sources lookup, always decompile")] = False,
    fresh_maven: Annotated[bool, typer.Option("--fresh-maven", help="Re-derive Maven lookup verdicts, ignoring cached ones")] = False,
    no_check: Annotated[bool, typer.Option("--no-check", help="Skip the javac compile check of decompiled sources")] = False,
    max_depth: Annotated[int, typer.Option("--max-depth", min=0, help="Archive-in-archive levels to unpack (0 = none; folders are always fully scanned)")] = 1,
    repo: Annotated[Optional[list[str]], typer.Option("--repo", help="Extra Maven repository URL (repeatable)")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Config file (default: user config dir)")] = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=0, help="Parallel decompile workers (0 = min(4, cpus))")] = 0,
    cpus: Annotated[int, typer.Option("--cpus", min=0, help="Total CPU budget across all workers (0 = all cores minus one)")] = 0,
    memory_budget: Annotated[Optional[str], typer.Option("--memory", help="Engine heap budget for all JVMs together, e.g. 8g (0 = no cap; default: 80% of RAM, at most 25% of RAM for one JVM)")] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Per-archive engine timeout, seconds; also limits each compile check")] = 600.0,
    force: Annotated[bool, typer.Option("--force", help="Allow writing into a non-empty output directory")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Stream engine stderr live and show it for failures")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print the final summary")] = False,
    format: Annotated[Format, typer.Option("--format", help="Output mode: human (default), json (single report to stdout), or ndjson (streaming events to stdout)")] = Format.human,
    engine_args: Annotated[Optional[list[str]], typer.Argument(
        help="Engine options after -- , passed verbatim to the primary engine (requires --no-fallback)",
        show_default=False,
    )] = None,
) -> None:
    """Decompile every Java artifact under INPUT, preferring real Maven sources."""
    global console
    machine = format is not Format.human
    prior_console = console
    if machine:
        console = _for_logs(Console(stderr=True, emoji=False))
    run_lock = None
    try:
        if not input.exists():
            raise _fail(f"input {input} does not exist")
        if output.exists() and not output.is_dir():
            raise _fail(f"output {output} exists and is not a directory")
        if output.exists() and any(output.iterdir()) and not force:
            raise _fail(f"output {output} is not empty (use --force to write anyway)")
        if no_resource and merge:
            raise _fail("--no-resource only applies to mirror mode (remove --merge)")
        if fresh_maven and no_maven:
            raise _fail("--fresh-maven has no effect with --no-maven (remove one)")
        if engine_args and not no_fallback:
            raise _fail("engine options after -- apply to the primary engine only (add --no-fallback)")
        # Other values fail each artifact, the report, or the kill (#113). Windows waits in 32-bit
        # milliseconds, so above 4294967 s its wait wraps around; above about 9.2e9 s, a wait fails on Linux.
        if not (math.isfinite(timeout) and 0 < timeout <= _MAX_TIMEOUT):
            raise _fail(f"--timeout: use a number of seconds above 0 and at most {_MAX_TIMEOUT}")
        memory_mb: int | None = None  # None = auto: 80% of the RAM (#98)
        if memory_budget is not None:
            try:
                memory_mb = memory.parse_size(memory_budget)
            except ValueError:
                raise _fail("--memory: use a size like 8g or 8192m, or 0 for no cap")
            if 0 < memory_mb < memory.SLOT_MIN_MB:
                raise _fail(
                    f"--memory: at least {memory.SLOT_MIN_MB}m (1 GiB of heap and "
                    f"{memory.SLOT_MIN_MB - memory.FLOOR_MB} MB of JVM overhead for one job)"
                )
        try:
            cfg = load_config(config, extra_repos=repo or [])
        except ConfigError as exc:
            raise _fail(str(exc))
        try:  # self-update takes the exclusive lock: it cannot start while a run holds this shared lock (#155)
            run_lock = runlock.acquire(exclusive=False)
        except runlock.LockBusy:
            raise _fail("decaf is updating: run again when it ends")
        notice = None  # the line about a newer release: its check never makes the run wait (#155)
        if format is Format.human and not quiet and cfg.update_check and _is_terminal():
            notice = selfupdate.NoticeCheck(engines.cache_root(), __version__)
            notice.start()

        settings = Settings(
            input=input,
            output=output,
            engine=engine.value,
            fallback=not no_fallback,
            engine_args=tuple(engine_args or ()),
            mirror=not merge,
            resources=not no_resource,
            maven=not no_maven,
            fresh_maven=fresh_maven,
            check=not no_check,
            max_depth=max_depth,
            jobs=jobs,
            cpus=cpus,
            memory_mb=memory_mb,
            timeout=timeout,
            repos=cfg.repositories,
            verbose=verbose,
            quiet=quiet,
            engine_overrides=cfg.engine_overrides,
        )

        progress = _GroupedProgress(
            SpinnerColumn(),
            _RowColumn(table_column=Column(no_wrap=True)),
            console=console,
            transient=True,
            disable=quiet or machine,
        )
        display = _RunDisplay(progress)  # also in the machine formats: their notes go to stderr (#109)
        status_marks = marks(console)
        scan_sent = False

        def on_done(r: ArtifactReport) -> None:
            display.on_done(r)
            if format is Format.ndjson:
                _emit({"event": "artifact", **dataclasses.asdict(r)})
            if not quiet:
                progress.console.print(_status_line(r, status_marks))

        def on_found(count: int) -> None:
            nonlocal scan_sent
            display.on_found(count)
            # pipeline calls on_found more than once: the initial discovered
            # total, then bare deltas as fetch-time nested discovery diverges
            # from the scan estimate. Only the first call is a true total.
            if format is Format.ndjson and not scan_sent:
                scan_sent = True
                _emit({"event": "scan", "artifacts": count})

        def on_stderr(text: str) -> None:
            progress.console.print(f"[dim]{safe(text, lines=True)}[/]")

        def on_warn(text: str) -> None:
            if format is Format.ndjson:
                _emit({"event": "warning", "text": text})
            if not quiet:
                progress.console.print(f"[yellow]{safe(text)}[/]")

        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(_HEARTBEAT_INTERVAL):
                try:
                    line = display.heartbeat_line(monotonic() - t0)
                    if line:
                        progress.console.print(line)
                except Exception:
                    pass

        t0 = monotonic()
        thread = None
        if not quiet and format is Format.human and not console.is_terminal:
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()

        prior_handlers = _end_like_ctrl_c()
        try:
            with progress:
                report = run(settings, on_done=on_done, on_found=on_found,
                             on_event=None if quiet else display.on_event,
                             on_stderr=on_stderr if verbose else None,
                             on_warn=on_warn if (format is Format.ndjson or not quiet) else None)
        except (DecafError, ScanError) as exc:
            raise _fail(str(exc))
        finally:
            for sig, handler in prior_handlers.items():
                signal.signal(sig, signal.SIG_DFL if handler is None else handler)
            stop.set()
            if thread is not None:
                thread.join(timeout=2.0)

        report_view.render_ending(console, report, output=output, report_path=output / "decaf-report.json", verbose=verbose)
        text = notice.line() if notice is not None else None
        if text is not None:
            console.print(f"[dim]{text}[/]")
        if format is Format.json:
            print(report.to_json(), flush=True)
        elif format is Format.ndjson:
            _emit({
                "event": "summary",
                "status": report.status,
                "totals": report.totals,
                "discovered": report.discovered,
                "duration_seconds": report.duration_seconds,
                "output": str(output),
                "report": str(output / "decaf-report.json"),
            })
        if report.interrupted:
            raise typer.Exit(code=130)
        raise typer.Exit(code=0 if report.totals["failed"] == 0 else 1)
    finally:
        if run_lock is not None:
            run_lock.release()
        console = prior_console


@app.command(name="report")
def report_cmd(
    path: Annotated[Path, typer.Argument(help="Report file, or an output directory containing decaf-report.json")] = Path("decaf-out"),
    problems: Annotated[bool, typer.Option("--problems", help="List failed and partial artifacts, grouped by cause")] = False,
    artifact: Annotated[Optional[list[str]], typer.Option("--artifact", help="Show full detail for artifacts matching this glob (repeatable)")] = None,
    network_fallbacks: Annotated[bool, typer.Option("--network-fallbacks", help="List artifacts that lost sources to network failures")] = False,
    memory_peaks: Annotated[bool, typer.Option("--memory", help="List the 10 engine JVMs with the largest peak memory")] = False,
) -> None:
    """Inspect a decaf-report.json offline."""
    try:
        report, resolved = report_view.load_report(path)
    except report_view.ReportError as exc:
        raise _fail(str(exc))

    schema = report.schema_version or 0
    console.print(
        f"[dim]{safe(resolved)} · schema {schema} · "
        f"decaf {safe(report.decaf_version or 'unknown')} · "
        f"{safe(report.ended_at or 'no timestamp')}[/]"
    )
    if schema > 1:
        console.print(
            f"[yellow]warning: schema {schema} is newer than this decaf understands; "
            f"rendering is best-effort[/]"
        )

    if not (problems or artifact or network_fallbacks or memory_peaks):
        report_view.render_ending(
            console, report,
            output=report.settings.get("output", str(resolved.parent)),
            report_path=resolved,
            verbose=False,
        )
        return

    if problems:
        report_view.render_problems(console, report)
    if artifact:
        if report_view.render_artifact(console, report, artifact) == 0:
            console.print("no artifacts match")
    if network_fallbacks:
        report_view.render_network_fallbacks(console, report)
    if memory_peaks:
        report_view.render_memory(console, report)


@app.command(name="self-update")
def self_update(
    check_only: Annotated[bool, typer.Option("--check", help="Show the latest release; install nothing")] = False,
) -> None:
    """Install the latest decaf release (uv tool installs only)."""
    cur = __version__
    version = selfupdate.parse_version(cur)
    if version is None:
        raise _fail(f"cannot compare versions: this decaf is {cur!r}", code=1)
    try:
        http = selfupdate.client()
    except Exception as exc:  # httpx reads the proxy settings here: a bad HTTPS_PROXY raises ValueError (#155)
        raise _fail(f"cannot read the latest release: {exc}", code=1)
    with http:
        try:
            release = selfupdate.fetch_latest(http, timeout=30)
        except selfupdate.SelfUpdateError as exc:
            raise _fail(f"cannot read the latest release: {exc}", code=1)
    if release.version == version:
        console.print(f"decaf {cur} is the latest release")
        return
    if release.version < version:
        console.print(f"decaf {cur} is newer than the latest release ({release.text})")
        return
    available = f"decaf {release.text} is available (this is {cur})."
    if check_only:
        console.print(selfupdate.available_text(release.text, cur))
        return

    install = selfupdate.find_install(Path(sys.prefix), selfupdate.direct_url())
    if install.kind == "editable":
        raise _fail("this decaf is an editable install from a source folder: update the folder with git pull", code=1)
    by_uv = f"  uv tool install {release.spec}"  # soft_wrap: rich must not break the command on a narrow terminal (#155)
    if install.kind == "other":
        console.print(f"{available}\nThis decaf is not a uv tool install, so decaf cannot update it.\n"
                      f"Update it the way you installed it, or install the release with:\n{by_uv}", soft_wrap=True)
        raise typer.Exit(code=1)
    uv = selfupdate.find_uv()
    if uv is None:
        console.print(f"{available}\nuv is not on PATH, so decaf cannot update itself. "
                      f"Install the release with:\n{by_uv}", soft_wrap=True)
        raise typer.Exit(code=1)

    try:  # a run takes the shared lock: no update starts under an active run (#155)
        lock = runlock.acquire(exclusive=True)
    except runlock.LockBusy:
        raise _fail("another decaf run is active: let it end, then run `decaf self-update` again", code=1)
    if lock is None:
        console.print("[yellow]warning: cannot check for active decaf runs[/]")
    try:
        windows = selfupdate.WINDOWS
        cmd = selfupdate.uv_command(uv, release, install, windows=windows,
                                    base=selfupdate.base_python() if windows else None)
        env = selfupdate.uv_env(Path(sys.prefix), install, os.environ)  # the folders of this install, not of the shell (#155)
        # uv can replace the files of decaf and of its packages, so a later import can get new
        # code. After uv starts, decaf imports nothing: it makes each text here, writes it with
        # print(), and leaves with typer.Exit (#155).
        shown = subprocess.list2cmdline(cmd) if windows else shlex.join(cmd)
        rerun = f"Run this command in a new terminal after decaf ends:\n  {shown}"
        stopped = "error: the update stopped. Run `decaf self-update` again"
        no_rev = f"error: uv ended, but the receipt does not show {release.tag}"
        no_start = "error: cannot start uv: "
        done = f"decaf {cur} -> {release.text}\n{release.url}"
        aside, by_hand = None, ""  # a Ctrl-C can come before the lines that set them
        try:
            if windows and install.exe is not None:  # uv cannot replace an active exe on Windows
                try:
                    aside = selfupdate.move_exe_aside(install.exe)
                except OSError as exc:
                    raise _fail(f"cannot move {install.exe} aside: {exc}", code=1)
            by_hand = f"\nrename {aside} to {install.exe} by hand"

            try:
                code, interrupted = selfupdate.run_uv(cmd, env=env)
            except OSError as exc:  # for example, antivirus software blocks uv.exe
                text, exit_code = f"{no_start}{exc}", 1
            else:
                if code == 0:  # uv finished the install, also after a Ctrl-C: the receipt decides
                    if selfupdate.receipt_rev(install.receipt) == release.tag:
                        print(done)
                        raise typer.Exit(code=0)
                    text, exit_code = no_rev, 1
                elif interrupted:
                    text, exit_code = stopped, 130
                else:
                    text, exit_code = f"error: uv exited with code {code}. {rerun}", 1
        except KeyboardInterrupt:  # a Ctrl-C outside the handler of run_uv: the exe must get its name back too (#155)
            text, exit_code = stopped, 130
        try:
            selfupdate.restore_exe(install.exe, aside)  # no-op when decaf moved no exe
        except OSError:
            text += by_hand
        print(text)
        raise typer.Exit(code=exit_code)
    finally:
        if lock is not None:
            lock.release()


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
        try:
            write_engine_pins(cfg_path, overrides)
        except (ConfigError, OSError) as exc:
            raise _fail(f"config write failed: {exc}")
        for name in removed:
            console.print(f"[green]{marks(console)[0]}[/] {name}: restored built-in pin {engines.ENGINES[name].version}")
        return

    failed = False
    ok, bad = marks(console)
    java: tuple[str, int] | None = None
    with httpx.Client() as client:
        for name in wanted:
            spec = specs[name]
            try:
                res = update.update_engine(
                    spec, client, engines.cache_root() / "engines", version=version,
                    warn=lambda msg: console.print(f"[yellow]{msg}[/]"),
                )
            except (engines.EngineError, OSError) as exc:  # e.g. a read-only engine cache (#113)
                console.print(f"[red]{bad}[/] {safe(f'{name}: {_engine_failure(name, exc)}')}")
                failed = True
                continue
            if res is None:
                console.print(f"[green]{ok}[/] {name} {safe(spec.version)} already at latest")
                continue
            overrides[name] = res.pin
            try:
                write_engine_pins(cfg_path, overrides)
            except (ConfigError, OSError) as exc:
                overrides.pop(name, None)
                console.print(f"[red]{bad}[/] {name}: config write failed: {safe(exc)}")
                failed = True
                continue
            console.print(
                f"[green]{ok}[/] {name} {safe(res.old_version)} → {safe(res.version)} (pinned in {safe(cfg_path)})"
            )
            pinned = engines.active_specs({name: res.pin})[name]
            need = engines.required_java(pinned, engines.cache_root() / "engines" / f"{name}-{res.version}.jar")
            java = java or engines.find_java()
            if java is not None and java[1] < need:  # the runs on this Java skip it, or stop (#113)
                console.print(
                    f"[yellow]{name} {safe(res.version)} needs Java {need}+, and the java on PATH is Java {java[1]}[/]"
                )
    raise typer.Exit(code=1 if failed else 0)
