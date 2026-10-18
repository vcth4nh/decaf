import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from decaf import check
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
    render_ending(console, report(), output="out[/bold]",
                  report_path="out[/bold]/decaf-report.json", verbose=False)  # must not raise
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
    count = render_artifact(console, report(), ["*lega*"])
    assert count == 1
    plain = console.file.getvalue()
    assert "legacy.jar" in plain
    assert "engine crashed: boom" in plain


def test_render_artifact_no_match_returns_zero():
    console = _console()
    assert render_artifact(console, report(), ["*nope*"]) == 0
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
    count = render_artifact(console, rep, ["noisy.jar"])  # must not raise
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


def test_render_artifact_overlapping_globs_dedupe():
    console = _console()
    count = render_artifact(console, report(), ["*lega*", "leg*", "*.jar"])
    plain = console.file.getvalue()
    assert count == len(report().artifacts)  # *.jar matches all, each rendered once
    assert plain.count("legacy.jar") == 1  # bold header appears exactly once per artifact


def test_render_artifact_bracketed_rel_renders_verbatim():
    artifacts = [
        ArtifactReport(rel="x[main].jar", kind="archive", outcome="failed", failure="boom"),
    ]
    console = _console()
    rep = report(artifacts=artifacts, totals=compute_totals(artifacts))
    assert render_artifact(console, rep, ["x*"]) == 1  # must not raise
    assert "x[main].jar" in console.file.getvalue()


_CASE_FAILURE = "case collision: 2 classes differ only by case (p/A vs p/a); case-insensitive output directory"


def test_failure_bucket_case_collision():
    r = ArtifactReport(rel="m.jar", kind="archive", outcome="failed", failure=_CASE_FAILURE)
    assert failure_bucket(r) == "case collision"


def test_ending_prints_case_collision_hint_once():
    arts = [
        ArtifactReport(rel="m.jar", kind="archive", outcome="failed", failure=_CASE_FAILURE),
        ArtifactReport(rel="n.jar", kind="archive", outcome="failed",
                       failure="case collision: 4 classes differ only by case (q/B vs q/b, ...); case-insensitive output directory"),
    ]
    buf = io.StringIO()
    console = Console(file=buf, width=300, emoji=False, force_terminal=False, highlight=False)
    render_ending(console, report(artifacts=arts, totals=compute_totals(arts)),
                  output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    text = buf.getvalue()
    assert "  2  case collision" in text
    assert text.count("fsutil file setCaseSensitiveInfo") == 1
    assert "case-sensitive APFS volume" in text


def test_ending_has_no_case_hint_without_case_failures():
    buf = io.StringIO()
    console = Console(file=buf, width=300, emoji=False, force_terminal=False, highlight=False)
    render_ending(console, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "fsutil" not in buf.getvalue()


_FLAG = {"path": "a/B.java", "engine": "vineflower", "kinds": ["name"], "errors": 2,
         "first": "3: cannot find symbol: variable xx",
         "messages": ["3: cannot find symbol: variable xx", "8: incompatible types: a"]}


def test_status_line_counts_flagged():
    r = ArtifactReport(rel="j.jar", kind="archive", outcome="ok", method="vineflower",
                       classes=3, java_files=3, flagged=[_FLAG])
    assert _status_line(r) == "[yellow]![/] j.jar (vineflower, 3 classes, [yellow]1 flagged[/])"


def test_ending_warnings_and_needs_java_line():
    arts = [ArtifactReport(
        rel="j.jar", kind="archive", outcome="ok", method="vineflower", flagged=[_FLAG],
        check=check.summary([check.unchecked("newer class version", needs_java=25)] * 2),
    )]
    c = _console()
    render_ending(c, report(artifacts=arts, totals=compute_totals(arts), settings={"java_major": 21}),
                  output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    plain = c.file.getvalue()
    assert "1 flagged files" in plain
    assert "compile check: 2 files need Java 25+ (found Java 21)" in plain
    assert 'compile check: run "decaf report out --problems" for more information' in plain


def test_ending_has_no_check_hint_without_flagged_files():
    c = _console()
    render_ending(c, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "--problems" not in c.file.getvalue()


def test_render_artifact_check_lines():
    a = ArtifactReport(
        rel="j.jar", kind="archive", outcome="ok", method="vineflower",
        attempts=[EngineAttempt("vineflower", "archive", 0, False, 3, "", flagged=1)],
        flagged=[_FLAG],
        noted=[{"path": "a/C.java", "engine": "cfr", "kinds": ["type"], "errors": 1,
                "first": "4: incompatible types: List[]", "messages": ["4: incompatible types: List[]"]}],
        unchecked=[
            {"path": "a/D.java", "engine": "vineflower", "reason": "absent classes", "needs_java": None,
             "first": "1: package org.absent does not exist"},
            {"path": "a/E.java", "engine": "vineflower", "reason": "newer class version", "needs_java": 25,
             "first": None},
        ],
        check={**check.summary([check.unchecked("absent classes")]), "checked": 3, "flagged": 1},
    )
    c = _console()
    render_artifact(c, report(artifacts=[a], totals=compute_totals([a])), ["j.jar"])
    plain = c.file.getvalue()
    assert "check: checked=3 flagged=1 noted=0 unchecked=1 (absent classes 1)" in plain
    assert "flagged a/B.java (vineflower): name, 2 errors - 3: cannot find symbol: variable xx" in plain
    assert "\n    3: cannot find symbol: variable xx\n    8: incompatible types: a\n" in plain
    assert "  noted a/C.java (cfr): type, 1 errors - 4: incompatible types: List[]\n" in plain
    assert "  unchecked a/D.java (vineflower): absent classes - 1: package org.absent does not exist\n" in plain
    assert "  unchecked a/E.java (vineflower): newer class version (needs Java 25+)\n" in plain
    assert "java_files=3 stubbed=0 flagged=1" in plain


def test_old_report_without_check_keys_loads(tmp_path: Path):
    data = json.loads(report().to_json())
    for a in data["artifacts"]:
        for key in ("flagged", "noted", "unchecked", "check"):
            a.pop(key)
        for at in a["attempts"]:
            at.pop("flagged")
    path = tmp_path / "decaf-report.json"
    path.write_text(json.dumps(data))
    loaded, _ = load_report(path)
    assert all(a.flagged == a.noted == a.unchecked == [] and a.check is None for a in loaded.artifacts)
    assert all(at.flagged == 0 for a in loaded.artifacts for at in a.attempts)


_OOM_STUB = {"path": "p/A.java", "engine": "vineflower", "methods": 2, "errors": ["java.lang.OutOfMemoryError"]}


def test_status_line_shows_the_stubbed_count():
    r = ArtifactReport(rel="b.jar", kind="archive", outcome="ok", method="vineflower",
                       classes=3, java_files=3, stubs=[_OOM_STUB])
    assert _status_line(r) == "[yellow]![/] b.jar (vineflower, 3 classes, [yellow]1 stubbed[/])"


def test_ending_warns_about_stubbed_classes():
    arts = [ArtifactReport(rel="s.jar", kind="archive", outcome="ok", method="vineflower",
                           classes=2, java_files=2, stubs=[_OOM_STUB])]
    rep = RunReport(settings={}, artifacts=arts, totals=compute_totals(arts), duration_seconds=1.0)
    console = _console()
    render_ending(console, rep, output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    plain = console.file.getvalue()
    assert plain.splitlines()[0] == "Completed with problems in 1s"
    assert "1 processed · 0 complete · 1 partial" in plain
    assert "1 stubbed classes" in plain


def test_render_problems_lists_a_stubbed_artifact():
    arts = [ArtifactReport(rel="s.jar", kind="archive", outcome="ok", method="vineflower",
                           classes=2, java_files=2, stubs=[_OOM_STUB])]
    rep = RunReport(settings={}, artifacts=arts, totals=compute_totals(arts), duration_seconds=1.0)
    console = _console()
    render_problems(console, rep)
    assert "s.jar (vineflower, 2 classes, 1 stubbed)" in console.file.getvalue()


def test_render_artifact_lists_stubs_and_attempt_counts():
    bare = {"path": "p/B.java", "engine": "jd", "methods": 1, "errors": []}
    arts = [ArtifactReport(
        rel="s.jar", kind="archive", outcome="ok", method="vineflower", classes=2, java_files=2,
        stubs=[_OOM_STUB, bare],
        attempts=[
            EngineAttempt("vineflower", "archive", 0, False, 2, "", 2),
            EngineAttempt("cfr", "class", 0, False, 2, "", 0),
        ],
    )]
    rep = RunReport(settings={}, artifacts=arts, totals=compute_totals(arts), duration_seconds=1.0)
    console = _console()
    assert render_artifact(console, rep, ["s.jar"]) == 1
    plain = console.file.getvalue()
    assert "missing_classes=0 stubbed=2 collisions=0" in plain
    assert "stub p/A.java (vineflower, 2 methods): java.lang.OutOfMemoryError" in plain
    assert "stub p/B.java (jd, 1 methods): no exception name" in plain
    assert "vineflower archive rc=0 timed_out=False java_files=2 stubbed=2" in plain
    assert "cfr class rc=0 timed_out=False java_files=2 stubbed=0" in plain


def test_old_report_without_stub_keys_renders(tmp_path: Path):
    data = json.loads(report().to_json())
    for a in data["artifacts"]:
        a.pop("stubs")
        for att in a["attempts"]:
            att.pop("stubbed")
    data["totals"].pop("stubbed_classes")
    path = tmp_path / "decaf-report.json"
    path.write_text(json.dumps(data))
    loaded, _ = load_report(path)
    assert all(a.stubs == [] for a in loaded.artifacts)
    assert all(att.stubbed == 0 for a in loaded.artifacts for att in a.attempts)
    console = _console()
    render_ending(console, loaded, output=Path("out"), report_path=path, verbose=False)
    assert "stubbed" not in console.file.getvalue()
    detail = _console()
    assert render_artifact(detail, loaded, ["*"]) == 5
    assert "stubbed=0" in detail.file.getvalue()


def _mem_report(*arts, budget=26214):
    arts = list(arts)
    return RunReport(settings={"memory_budget_mb": budget}, artifacts=arts, totals=compute_totals(arts),
                     duration_seconds=1.0)


def _mem_art(rel, *attempts):
    return ArtifactReport(rel=rel, kind="archive", outcome="ok", method="vineflower",
                          classes=1, java_files=1, attempts=list(attempts))


def _att(peak, heap=6253, oom=False, engine="vineflower", level="archive"):
    return EngineAttempt(engine, level, 0, False, 1, "", 0, peak, heap, oom)


def test_ending_shows_the_memory_line():
    console = _console()
    rep = _mem_report(_mem_art("a.jar", _att(512)), _mem_art("w.jar", _att(5247)))
    render_ending(console, rep, output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "budget 25.6 GB · largest peak 5.1 GB (w.jar, vineflower)" in console.file.getvalue()


def test_ending_memory_line_without_a_cap_and_without_peaks():
    console = _console()
    rep = _mem_report(_mem_art("w.jar", _att(8204, heap=None)), budget=0)
    render_ending(console, rep, output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "no cap · largest peak 8.0 GB (w.jar, vineflower)" in console.file.getvalue()
    console = _console()
    render_ending(console, report(), output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "Memory" not in console.file.getvalue()  # no engine attempt with a peak


def test_ending_warns_and_hints_about_out_of_heap():
    console = _console()
    rep = _mem_report(_mem_art("b.jar", _att(1500, heap=1031, oom=True), _att(400, heap=1031, engine="cfr")))
    render_ending(console, rep, output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    plain = console.file.getvalue()
    assert "1 attempts ran out of heap" in plain
    assert "hint: raise --memory (now 25.6 GB) or lower -j" in plain
    console = _console()
    render_ending(console, _mem_report(_mem_art("b.jar", _att(1500, heap=None, oom=True)), budget=0),
                  output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert "hint: set --memory or lower -j" in console.file.getvalue()


def test_ending_hint_names_the_auto_cap():
    console = _console()
    rep = _mem_report(_mem_art("w.jar", _att(8300, heap=8022, oom=True)), budget=25670)
    rep.settings["heap_cap_mb"] = 8022  # the auto budget: a lower -j cannot give one JVM more
    render_ending(console, rep, output=Path("out"), report_path=Path("out/decaf-report.json"), verbose=False)
    assert (
        "hint: raise --memory (now 25.1 GB; the auto budget caps one JVM at 7.8 GB) or lower -j"
    ) in console.file.getvalue()


def test_render_artifact_shows_the_memory_fields():
    console = _console()
    render_artifact(console, _mem_report(_mem_art("w.jar", _att(5247, oom=True))), ["w.jar"])
    assert "peak_rss_mb=5247 heap_mb=6253 out_of_memory=True" in console.file.getvalue()


def test_render_memory_lists_the_ten_largest_peaks():
    from decaf.report_view import render_memory

    arts = [_mem_art("b.jar", _att(9999, oom=True)), _mem_art("a.jar", _att(512))]
    arts += [_mem_art(f"c{i:02d}.jar", _att(100 + i)) for i in range(12)]
    console = _console()
    render_memory(console, _mem_report(*arts))
    lines = console.file.getvalue().splitlines()
    assert len(lines) == 10
    assert lines[0] == "  9.8 GB  heap 6.1 GB   vineflower archive  b.jar  out of heap"
    assert lines[1].endswith("a.jar")
    assert lines[2].endswith("c11.jar") and lines[9].endswith("c04.jar")


def test_render_memory_without_data():
    from decaf.report_view import render_memory

    console = _console()
    render_memory(console, report())
    assert console.file.getvalue().strip() == (
        "no memory data in this report (an older decaf wrote it, or no engine ran)"
    )
