import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from decaf.pipeline import ArtifactReport, EngineAttempt, RunReport, compute_totals
from decaf.report_view import (
    ReportError,
    _fmt_duration,
    _status_line,
    failure_bucket,
    load_report,
    render_artifact,
    render_ending,
    render_network_fallbacks,
    render_problems,
)


def report(**over) -> RunReport:
    """2 ok / 1 partial / 2 failed (one timed-out attempt, one unreadable archive)."""
    artifacts = [
        ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven",
                        gav="com.example:a:1.0", classes=3, java_files=3),
        ArtifactReport(rel="b.jar", kind="archive", outcome="ok", method="cfr",
                        classes=2, java_files=2),
        ArtifactReport(rel="p.jar", kind="archive", outcome="ok", method="cfr",
                        classes=5, java_files=4, missing_classes=1),
        ArtifactReport(
            rel="legacy.jar", kind="archive", outcome="failed",
            failure="engine timeout after 5 attempts",
            attempts=[EngineAttempt("vineflower", "archive", -1, True, 0, "engine crashed: boom")],
        ),
        ArtifactReport(rel="corrupt.jar", kind="archive", outcome="failed", failure="unreadable archive"),
    ]
    base = dict(
        settings={"chain": ["vineflower"]},
        artifacts=artifacts,
        totals=compute_totals(artifacts),
        duration_seconds=42.0,
    )
    base.update(over)
    return RunReport(**base)


def _console() -> Console:
    return Console(file=io.StringIO(), width=200)


def test_verdict_tiers():
    # interrupted wins even though this report also has failures
    c1 = _console()
    render_ending(c1, report(interrupted=True, discovered=10),
                  output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    first1 = c1.file.getvalue().splitlines()[0]
    assert first1 == "Interrupted after 42s — 5/10 artifacts completed"

    # failed > 0
    c2 = _console()
    render_ending(c2, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    first2 = c2.file.getvalue().splitlines()[0]
    assert first2 == "Completed with failures in 42s"

    # problems tier: no failures, but a partial artifact
    problems_artifacts = [
        ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven"),
        ArtifactReport(rel="b.jar", kind="archive", outcome="ok", method="cfr", missing_classes=1),
    ]
    c3 = _console()
    render_ending(c3, report(artifacts=problems_artifacts, totals=compute_totals(problems_artifacts)),
                  output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    first3 = c3.file.getvalue().splitlines()[0]
    assert first3 == "Completed with problems in 42s"

    # clean run
    clean_artifacts = [ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven")]
    c4 = _console()
    render_ending(c4, report(artifacts=clean_artifacts, totals=compute_totals(clean_artifacts)),
                  output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    first4 = c4.file.getvalue().splitlines()[0]
    assert first4 == "Completed in 42s"


def test_ending_lines():
    console = _console()
    render_ending(console, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    plain = console.file.getvalue()
    assert "5 processed · 2 complete · 1 partial · 2 failed" in plain
    assert "1 Maven · 2 decompiled · 0 extracted" in plain
    assert "1 missing classes" in plain
    assert "1  engine timeout" in plain


def test_ending_footer_paths():
    console = _console()
    out = Path("decaf-out")
    report_path = out / "decaf-report.json"
    render_ending(console, report(), output=out, report_path=report_path, verbose=False)
    plain = console.file.getvalue()
    assert "Output" in plain and str(out) in plain
    assert "Report" in plain and str(report_path) in plain


def test_ending_footer_escapes_bracketed_paths():
    """output/report_path can be untrusted (loaded from a report's settings dict by
    Task 4's `decaf report`) or just a directory a user happened to name with brackets
    for the live run path — must render verbatim, not raise MarkupError."""
    console = _console()
    render_ending(console, report(), output=Path("out[/bold]"),
                  report_path=Path("out[/bold]/decaf-report.json"), verbose=False)  # must not raise
    plain = console.file.getvalue()
    assert "out[/bold]" in plain


def test_ending_verbose_shows_stderr_tail():
    console = _console()
    render_ending(console, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=True)
    assert "engine crashed: boom" in console.file.getvalue()

    console2 = _console()
    render_ending(console2, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "engine crashed: boom" not in console2.file.getvalue()


def test_ending_verbose_escapes_bracketed_stderr_tail():
    """Engine stderr routinely contains rich-lookalike markup; an interrupted -v
    run's stderr_tail containing e.g. '[/bold]' must render verbatim, not raise
    rich.errors.MarkupError (which would turn exit 130 into exit 1)."""
    artifacts = [
        ArtifactReport(
            rel="noisy.jar", kind="archive", outcome="failed", failure="all engines failed",
            attempts=[EngineAttempt("vineflower", "archive", 1, False, 0,
                                     "[main] boom [/bold] more text")],
        ),
    ]
    console = _console()
    rep = report(artifacts=artifacts, totals=compute_totals(artifacts))
    render_ending(console, rep, output=Path("out"), report_path=Path("out/decaf-report.json"),
                  verbose=True)  # must not raise
    plain = console.file.getvalue()
    assert "[main] boom [/bold] more text" in plain


def test_failure_bucket_table():
    timeout = ArtifactReport(
        rel="t.jar", kind="archive", outcome="failed", failure="all engines failed",
        attempts=[EngineAttempt("vineflower", "archive", -1, True, 0, "")],
    )
    assert failure_bucket(timeout) == "engine timeout"  # timed_out wins over failure text

    unreadable = ArtifactReport(rel="u.jar", kind="archive", outcome="failed", failure="unreadable archive")
    assert failure_bucket(unreadable) == "unreadable archive"

    all_failed = ArtifactReport(rel="f.jar", kind="archive", outcome="failed", failure="all engines failed")
    assert failure_bucket(all_failed) == "all engines failed"

    other = ArtifactReport(rel="o.jar", kind="archive", outcome="failed", failure="boom: unexpected")
    assert failure_bucket(other) == "other"


def test_load_report_roundtrip(tmp_path: Path):
    rep = report()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    report_path = out_dir / "decaf-report.json"
    report_path.write_text(rep.to_json())

    loaded, resolved = load_report(report_path)
    assert resolved == report_path
    assert loaded.totals == rep.totals
    assert [a.rel for a in loaded.artifacts] == [a.rel for a in rep.artifacts]

    loaded_dir, resolved_dir = load_report(out_dir)
    assert resolved_dir == report_path
    assert loaded_dir.totals == rep.totals


def test_load_report_v0_and_unknown_keys(tmp_path: Path):
    rep = report()
    data = json.loads(rep.to_json())
    for key in ("schema_version", "decaf_version", "status", "started_at", "ended_at"):
        del data[key]
    data["future"] = 1
    data["artifacts"][0]["unknown_field"] = "surprise"
    path = tmp_path / "decaf-report.json"
    path.write_text(json.dumps(data))

    loaded, _ = load_report(path)
    assert loaded.schema_version == 0
    assert loaded.decaf_version == ""
    assert loaded.status == ""
    assert loaded.started_at == ""
    assert loaded.ended_at == ""
    assert loaded.artifacts[0].rel == rep.artifacts[0].rel


def test_load_report_errors(tmp_path: Path):
    with pytest.raises(ReportError):
        load_report(tmp_path / "nope.json")
    with pytest.raises(ReportError):
        load_report(tmp_path)  # existing dir, no decaf-report.json inside
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ReportError):
        load_report(bad)


def test_render_artifact_glob_and_count():
    console = _console()
    count = render_artifact(console, report(), "*lega*")
    assert count == 1
    plain = console.file.getvalue()
    assert "legacy.jar" in plain
    assert "engine crashed: boom" in plain


def test_render_artifact_no_match_returns_zero():
    console = _console()
    assert render_artifact(console, report(), "*nope*") == 0
    assert console.file.getvalue() == ""


def test_render_artifact_escapes_bracketed_stderr():
    """Engine stderr routinely contains [main]/[WARNING]-style tags; must render verbatim,
    not raise rich.errors.MarkupError and not silently swallow the bracketed text."""
    artifacts = [
        ArtifactReport(
            rel="noisy.jar", kind="archive", outcome="failed", failure="all engines failed",
            attempts=[EngineAttempt("vineflower", "archive", 1, False, 0,
                                     "[main] [WARNING] bad class file")],
        ),
    ]
    console = _console()
    rep = report(artifacts=artifacts, totals=compute_totals(artifacts))
    count = render_artifact(console, rep, "noisy.jar")  # must not raise
    assert count == 1
    plain = console.file.getvalue()
    assert "[main] [WARNING] bad class file" in plain


def test_render_problems_groups_then_partial():
    console = _console()
    render_problems(console, report())
    plain = console.file.getvalue()
    assert "engine timeout" in plain and "unreadable archive" in plain
    assert "legacy.jar" in plain and "corrupt.jar" in plain
    assert "p.jar" in plain  # partial artifact listed too


def test_render_network_fallbacks_lists_misses():
    artifacts = [
        ArtifactReport(rel="n.jar", kind="archive", outcome="ok", method="cfr",
                        sources_miss="network: sources download 503 [connection reset]"),
        ArtifactReport(rel="ok.jar", kind="archive", outcome="ok", method="maven"),
    ]
    console = _console()
    render_network_fallbacks(console, report(artifacts=artifacts, totals=compute_totals(artifacts)))
    plain = console.file.getvalue()
    assert "n.jar" in plain and "network: sources download 503 [connection reset]" in plain
    assert "ok.jar" not in plain


def test_fmt_duration_matches_elapsed_shapes():
    assert _fmt_duration(42) == "42s"
    assert _fmt_duration(12 * 60 + 4) == "12m04s"
    assert _fmt_duration(3600 + 2 * 60) == "1h2m"


def test_status_line_still_works_moved():
    r = ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven", gav="g:a:1")
    assert _status_line(r) == "[green]✓[/] a.jar (maven sources, g:a:1)"
