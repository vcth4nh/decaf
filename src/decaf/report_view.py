"""Report loading, failure buckets, and outcome-led post-run rendering."""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from .pipeline import ArtifactReport, EngineAttempt, RunReport, compute_totals, out_of_memory_runs
from .memory import gb

_AR = {f.name for f in dataclasses.fields(ArtifactReport)}
_EA = {f.name for f in dataclasses.fields(EngineAttempt)}
_RR = {f.name for f in dataclasses.fields(RunReport)}

_TOTALS_DEFAULTS = compute_totals([])  # every key compute_totals emits, zeroed

_BODY_LABEL_WIDTH = max(len(s) for s in ("Artifacts", "Sources", "Warnings"))
_FOOTER_LABEL_WIDTH = max(len(s) for s in ("Output", "Report"))


class ReportError(Exception):
    """A report file is missing or its JSON could not be parsed."""


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL, C1
_CONTROL_IN_LINES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # tab and line feed stay


def plain(text: object, *, lines: bool = False) -> str:
    """Text from an archive or an engine for a terminal: each control character becomes \\xNN (#109).

    With lines=True, tabs and line feeds stay (engine stderr).
    """
    pattern = _CONTROL_IN_LINES if lines else _CONTROL
    return pattern.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


def safe(text: object, *, lines: bool = False) -> str:
    """plain() text with its rich markup escaped, for console.print."""
    return escape(plain(text, lines=lines))


_MARKS = ("✓", "✗")


def marks(console: Console) -> tuple[str, str]:
    """The ok and failed marks, or ASCII marks when the console cannot encode them (#109)."""
    try:
        "".join(_MARKS).encode(console.encoding)
    except (UnicodeEncodeError, LookupError):
        return ("+", "x")
    return _MARKS


def _artifact(d: dict) -> ArtifactReport:
    r = ArtifactReport(**{k: v for k, v in d.items() if k in _AR and k != "attempts"})
    attempts = d.get("attempts")
    r.attempts = [
        EngineAttempt(**{k: v for k, v in a.items() if k in _EA})
        for a in (attempts if isinstance(attempts, list) else []) if isinstance(a, dict)
    ]
    return r


def _run_report(d: dict) -> RunReport:
    kwargs = {k: v for k, v in d.items() if k in _RR and k != "artifacts"}
    settings = d.get("settings")
    totals = d.get("totals")
    duration = d.get("duration_seconds")
    artifacts = d.get("artifacts")
    kwargs["settings"] = settings if isinstance(settings, dict) else {}
    kwargs["totals"] = totals if isinstance(totals, dict) else {}
    kwargs["duration_seconds"] = duration if isinstance(duration, (int, float)) else 0.0
    kwargs["artifacts"] = (
        [_artifact(a) for a in artifacts if isinstance(a, dict)]
        if isinstance(artifacts, list) else []
    )
    return RunReport(**kwargs)


def load_report(path: Path) -> tuple[RunReport, Path]:
    """Load a decaf-report.json, tolerating v0 reports and unknown/future keys."""
    resolved = path / "decaf-report.json" if path.is_dir() else path
    if not resolved.is_file():
        raise ReportError(f"report not found: {resolved}")
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError: no JSON, or no UTF-8
        raise ReportError(f"could not parse {resolved}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError(f"could not parse {resolved}: not a JSON object")
    return _run_report(data), resolved


_CASE_HINT = (
    "case collision: the output tree ignores case. Windows: install WSL (enables "
    "per-directory case sensitivity) or run \"fsutil file setCaseSensitiveInfo <empty dir> enable\"; "
    "macOS: use a case-sensitive APFS volume for the output and point TMPDIR at it too."
)


def failure_bucket(r: ArtifactReport) -> str:
    """Best-effort cause grouping for a failed artifact, without new pipeline data."""
    if any(a.timed_out for a in r.attempts):
        return "engine timeout"
    if r.failure == "unreadable archive":
        return "unreadable archive"
    if r.failure == "all engines failed":
        return "all engines failed"
    if (r.failure or "").startswith("case collision"):
        return "case collision"
    return "other"


def _bucket_groups(failed: list[ArtifactReport]) -> dict[str, list[ArtifactReport]]:
    groups: dict[str, list[ArtifactReport]] = {}
    for r in failed:
        groups.setdefault(failure_bucket(r), []).append(r)
    return groups


def _status_line(r: ArtifactReport, mark: tuple[str, str] = _MARKS) -> str:
    if r.outcome == "ok":
        if r.method == "maven":
            detail = f"maven sources, {safe(r.gav)}"
            if r.sources_cached:
                detail += ", cached"
        elif r.method == "extracted":
            detail = "extracted sources jar"
        elif r.method is None and r.kind == "resource_only":
            detail = f"resources only, {r.resources_copied} files"
        else:
            detail = f"{safe(r.method)}, {r.classes} classes"
            if r.missing_classes:
                detail += f", [yellow]{r.missing_classes} missing[/]"
            if r.stubs:
                detail += f", [yellow]{len(r.stubs)} stubbed[/]"
            if r.flagged:
                detail += f", [yellow]{len(r.flagged)} flagged[/]"
        glyph = "[yellow]![/]" if r.partial else f"[green]{mark[0]}[/]"
        line = f"{glyph} {safe(r.rel)} ({detail})"
        if r.method == "maven" and r.sources_cached:
            return f"[dim]{line}[/]"
        return line
    if r.outcome == "skipped":
        return f"[yellow]-[/] {safe(r.rel)} ({safe(r.failure or 'resource-only')}, skipped)"
    reason = safe((r.failure or "failed").splitlines()[-1])
    return f"[red]{mark[1]}[/] {safe(r.rel)} ({reason})"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def render_ending(
    console: Console, report: RunReport, *, output: Path, report_path: Path, verbose: bool
) -> None:
    """Print the outcome-led run ending: verdict, body lines, failures, footer."""
    t = {**_TOTALS_DEFAULTS, **report.totals}  # tolerate pre-1.8.0 / junk-but-parseable reports
    partial = t.get("partial", 0)
    dur = _fmt_duration(report.duration_seconds)
    if report.interrupted:
        denom = report.discovered or t["artifacts"]
        stopped = "Aborted" if report.status == "aborted" else "Interrupted"  # an error stopped the run (#112)
        console.print(
            f"[bold yellow]{stopped} after {dur} — {t['artifacts']}/{denom} artifacts completed[/]"
        )
    elif t["failed"] > 0:
        console.print(f"[bold red]Completed with failures in {dur}[/]")
    elif partial > 0 or t["network_misses"] > 0 or t["collisions"] > 0:
        console.print(f"[bold yellow]Completed with problems in {dur}[/]")
    else:
        console.print(f"[bold green]Completed in {dur}[/]")
    console.print()

    artifacts_line = f"{t['artifacts']} processed · {t['ok'] - partial} complete"
    if partial:
        artifacts_line += f" · {partial} partial"
    if t["failed"]:
        artifacts_line += f" · {t['failed']} failed"
    if t["skipped"]:
        artifacts_line += f" · {t['skipped']} skipped"
    console.print(f"[dim]{'Artifacts':<{_BODY_LABEL_WIDTH}}[/]  {artifacts_line}")

    cached = sum(1 for r in report.artifacts if r.sources_cached)
    resource_only = sum(1 for r in report.artifacts if r.outcome == "ok" and r.method is None)
    sources_line = f"{t['maven_sources']} Maven"
    if cached:
        sources_line += f" ({cached} cached)"
    sources_line += f" · {t['decompiled']} decompiled · {t['extracted']} extracted"
    if resource_only:
        sources_line += f" · {resource_only} resource-only"
    console.print(f"[dim]{'Sources':<{_BODY_LABEL_WIDTH}}[/]  {sources_line}")
    peaks = [
        (a.peak_rss_mb, r.rel, a.engine)
        for r in report.artifacts for a in r.attempts if a.peak_rss_mb is not None
    ]
    budget = report.settings.get("memory_budget_mb") or 0
    if peaks:
        peak, rel, engine = min(peaks, key=lambda p: (-p[0], p[1]))
        lead = f"budget {gb(budget)}" if budget else "no cap"
        console.print(
            f"[dim]{'Memory':<{_BODY_LABEL_WIDTH}}[/]  {lead} · largest peak {gb(peak)} "
            f"({safe(rel)}, {safe(engine)})"
        )

    missing = sum(r.missing_classes for r in report.artifacts)
    stubbed = sum(len(r.stubs) for r in report.artifacts)
    flagged = sum(len(r.flagged) for r in report.artifacts)
    out_of_heap = out_of_memory_runs(a for r in report.artifacts for a in r.attempts)
    warnings = []
    if missing:
        warnings.append(f"{missing} missing classes")
    if stubbed:
        warnings.append(f"{stubbed} stubbed classes")
    if flagged:
        warnings.append(f"{flagged} flagged files")
    if t["collisions"]:
        warnings.append(f"{t['collisions']} collisions")
    if t["network_misses"]:
        warnings.append(f"{t['network_misses']} network fallbacks")
    if out_of_heap:
        warnings.append(f"{out_of_heap} attempts ran out of heap")
    if warnings:
        console.print(f"[dim]{'Warnings':<{_BODY_LABEL_WIDTH}}[/]  " + " · ".join(warnings))
    newer = sum(
        (r.check or {}).get("unchecked_reasons", {}).get("newer class version", 0) for r in report.artifacts
    )
    needs = max(((r.check or {}).get("needs_java") or 0 for r in report.artifacts), default=0)
    if newer and needs:
        found = report.settings.get("java_major")
        console.print(f"[dim]compile check: {newer} files need Java {needs}+ (found Java {found})[/]")
    stopped = [f for r in report.artifacts for f in r.unchecked if f.get("reason") in ("javac crash", "timeout")]
    if stopped:  # files that the check could not finish: the cause shows here (#108)
        counts = ", ".join(f"{n} {reason}" for reason, n in sorted(Counter(f.get("reason") for f in stopped).items()))
        why = next((f.get("first") for f in stopped if f.get("first")), None)
        console.print(f"[dim]compile check: {len(stopped)} files not checked ({counts})" + (f": {safe(why)}" if why else "") + "[/]")
    if flagged:
        console.print(f'[dim]compile check: run "decaf report {safe(output)} --problems" for more information[/]')
    if out_of_heap:
        cap = report.settings.get("heap_cap_mb")
        if not budget:
            hint = "set --memory or lower -j"
        elif cap:  # a lower -j cannot give one JVM more than the cap of the auto budget
            hint = f"raise --memory (now {gb(budget)}; the auto budget caps one JVM at {gb(cap)}) or lower -j"
        else:
            hint = f"raise --memory (now {gb(budget)}) or lower -j"
        console.print(f"[dim]hint: {hint}[/]")

    failed = [r for r in report.artifacts if r.outcome == "failed"]
    if failed:
        console.print()
        console.print("Failures")
        for bucket, rs in sorted(_bucket_groups(failed).items(), key=lambda kv: -len(kv[1])):
            console.print(f"  {len(rs)}  {bucket}")
        console.print()
        for r in failed[:20]:
            console.print(_status_line(r, marks(console)))
            if verbose:
                for a in r.attempts:
                    if a.stderr_tail:
                        console.print(f"    [dim]{safe(a.engine)} ({safe(a.level)}): {safe(a.stderr_tail[-300:], lines=True)}[/]")
        if len(failed) > 20:
            console.print(f"…and {len(failed) - 20} more failures (see report)")
        if any(failure_bucket(r) == "case collision" for r in failed):
            console.print(f"[dim]{_CASE_HINT}[/]")

    console.print()
    console.print(f"[dim]{'Output':<{_FOOTER_LABEL_WIDTH}}[/]  {safe(output)}")
    console.print(f"[dim]{'Report':<{_FOOTER_LABEL_WIDTH}}[/]  {safe(report_path)}")


def render_problems(console: Console, report: RunReport) -> None:
    """Failed artifacts grouped by cause (desc count), then partial artifacts."""
    failed = [r for r in report.artifacts if r.outcome == "failed"]
    groups = _bucket_groups(failed)
    for bucket, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        console.print(f"[dim]{len(rs)}  {bucket}[/]")
        for r in rs:
            console.print(_status_line(r, marks(console)))
    partial = [r for r in report.artifacts if r.partial]
    if partial:
        if failed:
            console.print()
        for r in partial:
            console.print(_status_line(r, marks(console)))


def render_artifact(console: Console, report: RunReport, patterns: Sequence[str]) -> int:
    """Print full detail for artifacts whose rel matches any glob, once each; return match count."""
    matches = [
        r for r in report.artifacts
        if any(fnmatch.fnmatch(r.rel, p) for p in patterns)
    ]
    for i, r in enumerate(matches):
        if i:
            console.print()
        console.print(f"[bold]{safe(r.rel)}[/]")
        console.print(f"  kind={safe(r.kind)} outcome={safe(r.outcome)} method={safe(r.method)}")
        console.print(
            f"  gav={safe(r.gav)} repo={safe(r.repo)} "
            f"resolved_by={safe(r.resolved_by)} sources_miss={safe(r.sources_miss)}"
        )
        console.print(
            f"  classes={r.classes} java_files={r.java_files} "
            f"resources_copied={r.resources_copied} resources_skipped={r.resources_skipped} "
            f"missing_classes={r.missing_classes} stubbed={len(r.stubs)} collisions={len(r.collisions)}"
        )
        if r.check:
            reasons = ", ".join(f"{k} {v}" for k, v in (r.check.get("unchecked_reasons") or {}).items() if v)
            console.print(
                f"  check: checked={r.check.get('checked', 0)} flagged={r.check.get('flagged', 0)} "
                f"noted={r.check.get('noted', 0)} unchecked={r.check.get('unchecked', 0)}"
                + (f" ({safe(reasons)})" if reasons else "")
            )
        for f in r.flagged:
            console.print(_check_line("flagged", f))
            for m in f.get("messages") or []:
                console.print(f"    {safe(m)}")
        for f in r.noted:
            console.print(_check_line("noted", f))
        for f in r.unchecked:
            needs = f" (needs Java {f.get('needs_java')}+)" if f.get("needs_java") else ""
            first = f" - {safe(f.get('first'))}" if f.get("first") else ""
            console.print(
                f"  unchecked {safe(f.get('path'))} ({safe(f.get('engine'))}): "
                f"{safe(f.get('reason'))}{needs}{first}"
            )
        if r.failure:
            console.print(f"  failure={safe(r.failure, lines=True)}")
        for s in r.stubs:
            errors = ", ".join(s.get("errors") or []) or "no exception name"
            console.print(
                f"  stub {safe(s.get('path'))} ({safe(s.get('engine'))}, "
                f"{s.get('methods')} methods): {safe(errors)}"
            )
        for a in r.attempts:
            console.print(
                f"  {safe(a.engine)} {safe(a.level)} rc={a.returncode} timed_out={a.timed_out} "
                f"java_files={a.java_files} stubbed={a.stubbed} flagged={a.flagged} "
                f"peak_rss_mb={a.peak_rss_mb} heap_mb={a.heap_mb} out_of_memory={a.out_of_memory}"
            )
            if a.stderr_tail:
                console.print(f"    [dim]{safe(a.stderr_tail, lines=True)}[/]")
    return len(matches)


def _check_line(label: str, f: dict) -> str:
    """One flagged or noted file: kinds, error count, and the error that decided its status (#99)."""
    n = f.get("errors", 0)
    return (
        f"  {label} {safe(f.get('path'))} ({safe(f.get('engine'))}): "
        f"{safe(', '.join(f.get('kinds') or []))}, {n} error{'' if n == 1 else 's'} - "
        f"{safe(f.get('first'))}"
    )


def render_network_fallbacks(console: Console, report: RunReport) -> None:
    """List artifacts that lost sources to network failures, with the miss text."""
    for r in report.artifacts:
        if (r.sources_miss or "").startswith("network:"):
            console.print(f"{safe(r.rel)}: {safe(r.sources_miss)}")


def render_memory(console: Console, report: RunReport) -> None:
    """The 10 engine attempts with the largest peak memory (#98)."""
    rows = [
        (a.peak_rss_mb, r.rel, a)
        for r in report.artifacts for a in r.attempts if a.peak_rss_mb is not None
    ]
    if not rows:
        console.print("no memory data in this report (an older decaf wrote it, or no engine ran)")
        return
    rows.sort(key=lambda row: (-row[0], row[1]))
    for peak, rel, a in rows[:10]:
        heap = "no cap" if a.heap_mb is None else f"heap {gb(a.heap_mb)}"
        line = f"{gb(peak):>8}  {heap:<12}  {safe(a.engine)} {safe(a.level)}  {safe(rel)}"
        if a.out_of_memory:
            line += "  [yellow]out of heap[/]"
        console.print(line)
