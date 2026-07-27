import json
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import decaf.cli as cli
from decaf.cli import app
from decaf.pipeline import ArtifactReport, EngineAttempt, RunReport, compute_totals

runner = CliRunner(env={"COLUMNS": "200"})

# Typer forces terminal rendering when GITHUB_ACTIONS is set (checked at import
# time), styling "--" apart from the option word — so ANSI codes land inside
# flag names and plain substring asserts break. Strip escapes before matching.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def ok_report(**overrides) -> RunReport:
    artifacts = [
        ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven", java_files=3),
        ArtifactReport(rel="b.jar", kind="archive", outcome="ok", method="cfr", java_files=2, classes=2),
    ]
    base = dict(
        settings={"chain": ["vineflower"]},
        artifacts=artifacts,
        totals={
            "artifacts": 2, "ok": 2, "failed": 0, "skipped": 0,
            "maven_sources": 1, "extracted": 0, "decompiled": 1,
            "java_files": 5,
            "resources_copied": 0,
            "collisions": 0, "network_misses": 0,
        },
        duration_seconds=1.0,
    )
    base.update(overrides)
    return RunReport(**base)


def test_help_lists_flags():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    for flag in ["--output", "--engine", "--no-fallback", "--merge", "--no-maven",
                 "--fresh-maven",
                 "--max-depth", "--repo", "--config", "--jobs", "--cpus", "--timeout",
                 "--force"]:
        assert flag in plain


def test_group_help_lists_run():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "run" in plain and "--version" in plain


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "decaf" in result.output


def test_missing_input_exits_2(tmp_path: Path):
    result = runner.invoke(app, [str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_nonempty_output_needs_force(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.txt").write_text("boo")
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(out)])
    assert result.exit_code == 2
    assert "not empty" in result.output

    monkeypatch.setattr(cli, "run",
                        lambda settings, **kw: ok_report())
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(out), "--force"])
    assert result.exit_code == 0


def test_output_is_existing_file_exits_2(tmp_path: Path, make_jar):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    out = tmp_path / "somefile"
    out.write_text("hi")
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(out)])
    assert result.exit_code == 2
    assert "not a directory" in result.output


def test_bad_config_exits_2(tmp_path: Path, make_jar):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    cfg = tmp_path / "decaf.toml"
    cfg.write_text("repositories = 'oops'\n")
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"),
                                 "--config", str(cfg)])
    assert result.exit_code == 2


def test_bad_repo_url_exits_2(tmp_path: Path, make_jar):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    cfg = tmp_path / "decaf.toml"
    cfg.write_text("repositories = []\n")
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"),
                                 "--config", str(cfg), "--repo", "htp://typo.example/m2"])
    assert result.exit_code == 2
    assert "http" in result.output


def test_decaf_error_exits_2(tmp_path: Path, make_jar, monkeypatch):
    from decaf.pipeline import DecafError

    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")

    def boom(settings, **kw):
        raise DecafError("java not found on PATH (Java 11+ required)")

    monkeypatch.setattr(cli, "run", boom)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 2
    assert "java not found" in result.output


def test_exit_1_when_failures(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    failing = ok_report()
    failing.artifacts.append(
        ArtifactReport(rel="x.jar", kind="archive", outcome="failed", failure="all engines failed")
    )
    failing.totals = {**failing.totals, "artifacts": 3, "failed": 1}
    monkeypatch.setattr(cli, "run",
                        lambda settings, **kw: failing)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "x.jar" in result.output


def test_settings_wiring(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    captured = {}

    def capture(settings, **kw):
        captured["settings"] = settings
        captured["on_found"] = kw.get("on_found")
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(
        app,
        [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--engine", "cfr",
         "--no-fallback", "--merge", "--no-maven", "--max-depth", "3",
         "--repo", "https://r.test/m2", "-j", "2", "--cpus", "8", "--timeout", "30"],
    )
    assert result.exit_code == 0
    s = captured["settings"]
    assert s.engine == "cfr"
    assert s.fallback is False and s.mirror is False and s.maven is False
    assert s.max_depth == 3
    assert s.jobs == 2 and s.cpus == 8 and s.timeout == 30.0
    assert s.repos[0] == "https://r.test/m2"
    assert s.repos[-1] == "https://repo1.maven.org/maven2"
    assert callable(captured["on_found"])  # CLI feeds discovery counts to the progress total


def test_verbose_streams_engine_stderr(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    captured = {}

    def capture(settings, **kw):
        on_stderr = kw.get("on_stderr")
        captured["on_stderr"] = on_stderr
        if on_stderr is not None:
            on_stderr("vineflower a.jar: [warn] odd <input>")
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "-v"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "vineflower a.jar: [warn] odd <input>" in plain  # markup chars survive verbatim

    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2")])
    assert result.exit_code == 0
    assert captured["on_stderr"] is None  # no -v, no stream


def test_full_stack_through_cli(tmp_path: Path, make_jar, monkeypatch):
    """End-to-end with fake engines: real scan, pipeline, writers, report file."""
    import decaf.engines as engines
    from decaf.engines import EngineResult

    def fake_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, cds_dir=None):
        out = Path(dest) / "com/x/A.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("class A {}")
        return EngineResult(spec.name, 0, False, 1, "")

    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(
        engines, "ensure_engine",
        lambda spec, client, cache_dir=None, on_download=None: Path(f"/fake/{spec.name}.jar"),
    )
    monkeypatch.setattr(engines, "run_engine", fake_engine)

    make_jar("app.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    out = tmp_path / "out"
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(out), "--no-maven"])
    assert result.exit_code == 0, result.output
    assert (out / "app.jar/com/x/A.java").is_file()  # mirror layout is the default
    report = json.loads((out / "decaf-report.json").read_text())
    assert report["totals"]["ok"] == 1


def test_engine_overrides_reach_settings(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    cfgf = tmp_path / "decaf.toml"
    sha = "a" * 64
    cfgf.write_text(
        f'[engines.cfr]\nversion = "0.153"\nurl = "https://x.test/cfr.jar"\nsha256 = "{sha}"\n'
    )
    captured = {}

    def capture(settings, **kw):
        captured["s"] = settings
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"),
                                 "--config", str(cfgf)])
    assert result.exit_code == 0
    assert captured["s"].engine_overrides == {
        "cfr": {"version": "0.153", "url": "https://x.test/cfr.jar", "sha256": sha},
    }


def test_routing_positional_and_option_first(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    monkeypatch.setattr(cli, "run",
                        lambda settings, **kw: ok_report())
    assert runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "o1")]).exit_code == 0
    assert runner.invoke(app, ["-o", str(tmp_path / "o2"), str(tmp_path / "in")]).exit_code == 0
    assert runner.invoke(app, ["run", str(tmp_path / "in"), "-o", str(tmp_path / "o3")]).exit_code == 0


def test_bare_decaf_shows_group_help():
    result = runner.invoke(app, [])
    plain = ANSI.sub("", result.output)
    assert "run" in plain
    assert result.exit_code in (0, 2)  # click's no_args_is_help exit code varies by version


def test_verdict_line_clean_run(tmp_path: Path, make_jar, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "Completed in " in plain
    assert "with" not in plain


def test_summary_warns_on_network_misses(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    totals = dict(ok_report().totals, network_misses=2)
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report(totals=totals))
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "2 network fallbacks" in plain


def test_summary_silent_when_no_network_misses(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert "network fallbacks" not in ANSI.sub("", result.output)


def test_on_warn_wired_unless_quiet(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    captured = {}

    def capture(settings, **kw):
        captured.update(kw)
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert callable(captured["on_warn"])
    runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2"), "--quiet"])
    assert captured["on_warn"] is None


def test_warn_sink_renders_message_body(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)

    def capture(settings, **kw):
        kw["on_warn"]("maven: r.test: [boom] persisted <odd>")
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "maven: r.test: [boom] persisted <odd>" in plain  # escape(): markup-like text renders verbatim


@pytest.mark.parametrize("name", ["run", "engines", "cache", "report"])
def test_dot_slash_escapes_reserved_words(name, tmp_path: Path, make_jar, monkeypatch):
    captured = {}

    def fake_run(settings, **kw):
        captured["input"] = settings.input
        return ok_report()

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    make_jar(f"{name}/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [f"./{name}", "-o", "out"])
    assert result.exit_code == 0
    assert captured["input"] == Path(f"./{name}")


def test_status_line_escapes_bracketed_free_text():
    import io

    from rich.console import Console

    def rendered(line: str) -> str:
        c = Console(file=io.StringIO(), width=200, emoji=False)  # mirrors cli.console
        c.print(line)
        return c.file.getvalue()

    failed = ArtifactReport(
        rel="x[main].jar", kind="archive", outcome="failed", failure="boom [/bold] tail"
    )
    out = rendered(cli._status_line(failed))
    assert "x[main].jar" in out and "boom [/bold] tail" in out

    ok = ArtifactReport(
        rel="a[1].jar", kind="archive", outcome="ok", method="maven", gav="g:a:1[x]"
    )
    out = rendered(cli._status_line(ok))
    assert "a[1].jar" in out and "g:a:1[x]" in out

    skipped = ArtifactReport(
        rel="s[2].jar", kind="archive", outcome="skipped", failure="odd [tag]"
    )
    out = rendered(cli._status_line(skipped))
    assert "s[2].jar" in out and "odd [tag]" in out


def test_emoji_shortcode_gavs_render_verbatim():
    """`:id:`/`:a:`-style artifactIds must not become emoji in the real console."""
    r = ArtifactReport(
        rel="a.jar", kind="archive", outcome="ok", method="maven", gav="com.x:id:1[x]"
    )
    with cli.console.capture() as cap:
        cli.console.print(cli._status_line(r))
    assert "com.x:id:1[x]" in cap.get()


def test_report_tolerates_null_fields(tmp_path: Path):
    p = tmp_path / "decaf-report.json"
    p.write_text(json.dumps({
        "settings": None, "totals": None, "artifacts": None, "duration_seconds": None,
    }))
    result = runner.invoke(app, ["report", str(p)])
    assert result.exit_code == 0
    assert "Completed" in ANSI.sub("", result.output)


def test_status_line_cached_suffix():
    r = ArtifactReport(
        rel="a.jar", kind="archive", outcome="ok", method="maven",
        gav="com.example:lib:1.2", sources_cached=True,
    )
    assert cli._status_line(r) == (
        "[dim][green]✓[/] a.jar (maven sources, com.example:lib:1.2, cached)[/]"
    )
    r.sources_cached = False
    assert "cached" not in cli._status_line(r)


def test_status_line_dim_only_for_cached_maven():
    fresh = ArtifactReport(
        rel="a.jar", kind="archive", outcome="ok", method="maven", gav="g:a:1"
    )
    assert not cli._status_line(fresh).startswith("[dim]")
    decompiled = ArtifactReport(rel="b.jar", kind="archive", outcome="ok", method="vineflower")
    assert not cli._status_line(decompiled).startswith("[dim]")
    extracted = ArtifactReport(rel="c.jar", kind="sources_jar", outcome="ok", method="extracted")
    assert not cli._status_line(extracted).startswith("[dim]")
    failed = ArtifactReport(rel="d.jar", kind="archive", outcome="failed", failure="boom")
    assert cli._status_line(failed).startswith("[red]")


def test_summary_prints_paths_footer(tmp_path: Path, make_jar, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    out = tmp_path / "out"
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(out)])
    plain = ANSI.sub("", result.output)
    assert "Output" in plain
    assert str(out) in plain
    assert "Report" in plain
    assert str(out / "decaf-report.json") in plain


def test_failure_recap_names_remainder(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.artifacts = [
        ArtifactReport(rel=f"f{i:02d}.jar", kind="archive", outcome="failed", failure="boom")
        for i in range(25)
    ]
    rep.totals = dict(rep.totals, artifacts=25, ok=0, failed=25)
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "…and 5 more failures (see report)" in plain
    assert result.exit_code == 1


def test_interrupted_summary_shows_denominator(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.interrupted = True
    rep.discovered = 10
    rep.duration_seconds = 250.0
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "Interrupted after" in plain
    assert "2/10 artifacts completed" in plain
    assert result.exit_code == 130


def test_status_line_partial_glyph():
    r = ArtifactReport(
        rel="a.jar", kind="archive", outcome="ok", method="cfr",
        classes=10, missing_classes=2,
    )
    line = cli._status_line(r)
    assert line.startswith("[yellow]![/]")
    assert "2 missing" in line

    degraded = ArtifactReport(
        rel="b.jar", kind="archive", outcome="ok", method="vineflower",
        sources_miss="network: sources download 503 persisted",
    )
    assert cli._status_line(degraded).startswith("[yellow]![/]")


def test_summary_partial_row_only_when_nonzero(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.totals["partial"] = 0
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert " partial" not in plain

    rep2 = ok_report()
    rep2.artifacts[1].missing_classes = 1
    rep2.totals["partial"] = 1
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep2)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2")])
    plain = ANSI.sub("", result.output)
    assert "1 partial" in plain


def test_summary_counts_cached_sources(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.artifacts[0].sources_cached = True
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "1 Maven (1 cached) · 1 decompiled · 0 extracted" in plain


def test_summary_wording_unchanged_without_cache_hits(tmp_path: Path, make_jar, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "1 Maven · 1 decompiled · 0 extracted" in plain


def make_display():
    import io

    from rich.console import Console
    from rich.progress import SpinnerColumn

    console = Console(file=io.StringIO(), force_terminal=True, width=120)
    progress = cli._GroupedProgress(
        SpinnerColumn(), cli._RowColumn(),
        console=console, transient=True,
    )
    return progress, cli._RunDisplay(progress)


def descriptions(progress):
    return [t.description for t in progress._ordered_tasks()]


def rendered(progress):
    col = progress.columns[1]
    return [str(col.render(t)) for t in progress._ordered_tasks()]


def test_display_row_lifecycle():
    progress, disp = make_display()
    disp.on_found(3)
    assert descriptions(progress) == ["scanning…"]  # header only, until the scan event
    disp.on_event("scan", "", "2 top-level + 1 nested")
    assert descriptions(progress)[0] == "0/3 done · 0 fetching · 0 decompiling · 3 queued"
    # Console(force_terminal=True) highlights bare numbers/parens even with no markup
    # in the string — strip ANSI before matching (same idiom as the module-level ANSI
    # regex used for CliRunner output elsewhere in this file).
    plain = ANSI.sub("", progress.console.file.getvalue())
    assert "found 3 artifacts (2 top-level + 1 nested)" in plain
    disp.on_event("fetch", "a.jar", "resolving")
    assert any(d.startswith("resolving ") and "a.jar" in d for d in descriptions(progress))
    assert descriptions(progress)[0] == "0/3 done · 1 fetching · 0 decompiling · 2 queued"
    disp.on_event("queued", "a.jar", "")
    assert not any("a.jar" in d for d in descriptions(progress))  # between stages: queued only
    disp.on_event("decompile", "a.jar", "vineflower · 3 classes")
    # 'decompiling' is 11 chars padded to 17, plus the separator space: 7 spaces total
    assert any(d == "decompiling       a.jar" for d in descriptions(progress))
    assert any("(vineflower · 3 classes · " in r for r in rendered(progress))
    disp.on_done(ArtifactReport(rel="a.jar", kind="archive", outcome="ok"))
    assert not any("a.jar" in d for d in descriptions(progress))
    assert descriptions(progress)[0] == "1/3 done · 0 fetching · 0 decompiling · 2 queued"


def test_display_no_cap_no_overflow():
    progress, disp = make_display()
    disp.on_found(20)
    disp.on_event("scan", "", "20 top-level + 0 nested")
    for i in range(10):
        disp.on_event("fetch", f"j{i:02d}.jar", "")
    descs = descriptions(progress)
    assert sum("fetching" in d for d in descs) == 11  # header + every executing jar has a row
    assert not any("more active" in d for d in descs)  # overflow line is gone
    assert descs[0] == "0/20 done · 10 fetching · 0 decompiling · 10 queued"


def test_display_groups_fetching_before_decompiling():
    progress, disp = make_display()
    disp.on_found(4)
    disp.on_event("scan", "", "4 top-level + 0 nested")
    disp.on_event("fetch", "a.jar", "")
    disp.on_event("fetch", "b.jar", "")
    disp.on_event("queued", "a.jar", "")
    disp.on_event("decompile", "a.jar", "vineflower")
    disp.on_event("fetch", "c.jar", "")  # starts after a.jar moved to decompiling
    descs = descriptions(progress)
    assert descs[0] == "0/4 done · 2 fetching · 1 decompiling · 1 queued"
    assert [d.split()[0] for d in descs[1:]] == ["fetching", "fetching", "decompiling"]
    assert "b.jar" in descs[1] and "c.jar" in descs[2] and "a.jar" in descs[3]
    disp.on_event("engines", "", "verifying")
    assert descriptions(progress)[1] == "engines: verifying…"  # engines sorts before jar rows


def test_display_engine_rows():
    progress, disp = make_display()
    disp.on_event("engines", "", "verifying")
    assert "engines: verifying…" in descriptions(progress)
    disp.on_event("engines", "vineflower", "downloading 1.11.1")
    assert "engines: downloading vineflower 1.11.1…" in descriptions(progress)
    disp.on_event("engines", "vineflower", "downloaded 1.11.1")
    assert "engines: verifying…" in descriptions(progress)
    plain = ANSI.sub("", progress.console.file.getvalue())
    assert "vineflower 1.11.1 downloaded" in plain
    disp.on_event("engines", "", "ready")
    assert not any(d.startswith("engines:") for d in descriptions(progress))


def test_shorten_keeps_leaf():
    rel = "app.war!/WEB-INF/lib/some-very-long-artifact-name-2.11.0.jar"
    out = cli._shorten(rel, 50)
    assert len(out) <= 50
    assert out.endswith("some-very-long-artifact-name-2.11.0.jar")
    assert "…" in out
    assert cli._shorten("a.jar", 50) == "a.jar"


def test_on_event_wired_unless_quiet(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    captured = {}

    def capture(settings, **kw):
        captured.update(kw)
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert callable(captured["on_event"])
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2"), "--quiet"])
    assert captured["on_event"] is None
    assert "found" not in ANSI.sub("", result.output)  # -q: no mid-run lines at all


def test_run_output_shows_found_and_download_lines(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)

    def fake_run(settings, **kw):
        kw["on_found"](3)
        kw["on_event"]("scan", "", "2 top-level + 1 nested")
        kw["on_event"]("engines", "cfr", "downloaded 0.152")
        return ok_report()

    monkeypatch.setattr(cli, "run", fake_run)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "found 3 artifacts (2 top-level + 1 nested)" in plain
    assert "cfr 0.152 downloaded" in plain


def test_no_resource_flag_wiring(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    captured = {}

    def capture(settings, **kw):
        captured["settings"] = settings
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--no-resource"])
    assert result.exit_code == 0
    assert captured["settings"].resources is False
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2")])
    assert result.exit_code == 0
    assert captured["settings"].resources is True


def test_no_resource_with_merge_exits_2(tmp_path: Path, make_jar):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    result = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--merge", "--no-resource"]
    )
    assert result.exit_code == 2
    plain = ANSI.sub("", result.output)
    assert "--no-resource only applies to mirror mode" in plain


def test_help_lists_no_resource():
    result = runner.invoke(app, ["run", "--help"])
    plain = ANSI.sub("", result.output)
    assert "--no-resource" in plain


def test_status_line_resource_only_mirrored():
    r = ArtifactReport(rel="r.jar", kind="resource_only", outcome="ok", resources_copied=3)
    assert cli._status_line(r) == "[green]✓[/] r.jar (resources only, 3 files)"


def test_summary_ok_row_counts_mirrored_resource_only(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.artifacts.append(
        ArtifactReport(rel="r.jar", kind="resource_only", outcome="ok", resources_copied=2)
    )
    rep.totals = {**rep.totals, "artifacts": 3, "ok": 3, "resources_copied": 2}
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "· 1 resource-only" in plain


def test_fresh_maven_flag_wiring(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    captured = {}

    def capture(settings, **kw):
        captured["settings"] = settings
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--fresh-maven"])
    assert result.exit_code == 0
    assert captured["settings"].fresh_maven is True
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2")])
    assert result.exit_code == 0
    assert captured["settings"].fresh_maven is False


def test_fresh_maven_with_no_maven_exits_2(tmp_path: Path, make_jar):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    result = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--fresh-maven", "--no-maven"]
    )
    assert result.exit_code == 2
    plain = ANSI.sub("", result.output)
    assert "--fresh-maven has no effect with --no-maven" in plain


def test_cache_clean_removes_sources_and_verdicts(tmp_path: Path, monkeypatch):
    from decaf import engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "g_a_1-sources.jar").write_bytes(b"x" * 100)
    (tmp_path / "verdicts" / "sha1").mkdir(parents=True)
    (tmp_path / "verdicts" / "sha1" / ("a" * 40 + ".json")).write_text("{}")
    (tmp_path / "engines").mkdir()
    (tmp_path / "engines" / "cfr-0.152.jar").write_bytes(b"e")

    result = runner.invoke(app, ["cache", "clean"])
    assert result.exit_code == 0
    assert "removed 2 files" in result.output
    assert not (tmp_path / "sources").exists()
    assert not (tmp_path / "verdicts").exists()
    assert (tmp_path / "engines" / "cfr-0.152.jar").exists()  # engines untouched


def test_cache_clean_nothing_to_clean(tmp_path: Path, monkeypatch):
    from decaf import engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    result = runner.invoke(app, ["cache", "clean"])
    assert result.exit_code == 0
    assert "nothing to clean" in result.output


def test_elapsed_format():
    assert cli._elapsed(5) == "5s"
    assert cli._elapsed(42) == "42s"
    assert cli._elapsed(61) == "1m01s"
    assert cli._elapsed(102) == "1m42s"
    assert cli._elapsed(3720) == "1h2m"


def test_row_column_elapsed_ticks_and_resets(monkeypatch):
    progress, disp = make_display()
    now = {"t": 1000.0}
    monkeypatch.setattr(cli, "monotonic", lambda: now["t"])
    disp.on_event("decompile", "a.jar", "vineflower · 13,271 classes")
    now["t"] += 102
    row = [r for r in rendered(progress) if "a.jar" in r]
    assert row == ["decompiling       a.jar (vineflower · 13,271 classes · 1m42s)"]
    disp.on_event("decompile", "a.jar", "cfr · 13,271 classes")  # fallback attempt: clock resets
    now["t"] += 5
    row = [r for r in rendered(progress) if "a.jar" in r]
    assert row == ["decompiling       a.jar (cfr · 13,271 classes · 5s)"]


def test_fetch_rows_have_no_elapsed(monkeypatch):
    progress, disp = make_display()
    monkeypatch.setattr(cli, "monotonic", lambda: 0.0)
    disp.on_event("fetch", "a.jar", "downloading")
    row = [r for r in rendered(progress) if "a.jar" in r]
    assert row == ["downloading       a.jar"]


def test_display_progress_updates_detail_without_clock_reset():
    progress, disp = make_display()
    disp.on_found(1)
    disp.on_event("scan", "", "1 top-level + 0 nested")
    disp.on_event("decompile", "a.jar", "vineflower · batch of 2 · 3 classes")
    task = next(t for t in progress.tasks if t.description == "decompiling       a.jar")
    before = task.fields["since"]
    disp.on_event("progress", "a.jar", "vineflower · batch of 2 · 1/3 classes")
    assert task.fields["since"] == before  # the per-attempt clock must NOT restart
    assert task.fields["detail"] == "vineflower · batch of 2 · 1/3 classes"
    assert task.description == "decompiling       a.jar"
    assert any("(vineflower · batch of 2 · 1/3 classes · " in r for r in rendered(progress))


def test_display_progress_ignores_unknown_and_fetch_rows():
    progress, disp = make_display()
    disp.on_found(2)
    disp.on_event("scan", "", "2 top-level + 0 nested")
    disp.on_event("progress", "ghost.jar", "vineflower · batch of 2 · 1/3 classes")
    assert not any("ghost.jar" in d for d in descriptions(progress))
    disp.on_event("fetch", "b.jar", "resolving")
    disp.on_event("progress", "b.jar", "vineflower · batch of 2 · 1/3 classes")
    task = next(t for t in progress.tasks if "b.jar" in t.description)
    assert task.fields["detail"] == "" and task.fields["since"] is None


def test_heartbeat_line_none_before_scan():
    progress, disp = make_display()
    disp.on_found(3)
    assert disp.heartbeat_line(570.0) is None


def test_heartbeat_line_shape():
    progress, disp = make_display()
    disp.on_found(3)
    disp.on_event("scan", "", "3 top-level + 0 nested")
    disp.on_event("fetch", "a.jar", "resolving")
    disp.on_event("decompile", "b.jar", "vineflower · 2 classes")
    disp.on_done(ArtifactReport(rel="c.jar", kind="archive", outcome="ok"))
    line = disp.heartbeat_line(570.0)
    assert line.startswith("[+9m30s] ")
    assert "1/3 done" in line
    assert "1 fetching" in line
    assert "1 decompiling" in line
    assert "0 queued" in line
    assert "longest active b.jar" in line


def test_heartbeat_thread_gated_by_quiet_and_format(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    monkeypatch.setattr(cli, "_HEARTBEAT_INTERVAL", 0.02)

    def fake_run(settings, **kw):
        kw["on_found"](3)
        if kw["on_event"] is not None:
            kw["on_event"]("scan", "", "3 top-level + 0 nested")
        time.sleep(0.2)
        return ok_report()

    monkeypatch.setattr(cli, "run", fake_run)

    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0
    assert "[+" in ANSI.sub("", result.output)

    result_q = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2"), "-q"])
    assert result_q.exit_code == 0
    assert "[+" not in ANSI.sub("", result_q.output)

    result_json = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out3"), "--format", "json"]
    )
    assert result_json.exit_code == 0
    assert "[+" not in ANSI.sub("", result_json.stdout)
    assert "[+" not in ANSI.sub("", result_json.stderr)


def test_cli_engine_args_passthrough(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    captured = {}

    def capture(settings, **kw):
        captured["s"] = settings
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"),
                                 "--engine", "cfr", "--no-fallback", "--",
                                 "--renameillegalidents", "true"])
    assert result.exit_code == 0
    assert captured["s"].engine_args == ("--renameillegalidents", "true")
    assert captured["s"].engine == "cfr" and captured["s"].fallback is False

    # tokens after -- never bind to decaf options, even lookalikes
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out2"),
                                 "--no-fallback", "--", "--engine", "procyon"])
    assert result.exit_code == 0
    assert captured["s"].engine == "vineflower"
    assert captured["s"].engine_args == ("--engine", "procyon")

    # DefaultGroup re-attachment keeps -- working with options before INPUT
    result = runner.invoke(app, ["--engine", "cfr", str(tmp_path / "in"),
                                 "-o", str(tmp_path / "out3"), "--no-fallback", "--", "-dgs=1"])
    assert result.exit_code == 0
    assert captured["s"].engine == "cfr"
    assert captured["s"].engine_args == ("-dgs=1",)


def test_cli_engine_args_require_no_fallback(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"),
                                 "--", "-dgs=1"])
    assert result.exit_code == 2
    assert "--no-fallback" in ANSI.sub("", result.output)


# --format json|ndjson: typer's vendored click (0.27) CliRunner has no `mix_stderr`
# kwarg (its __init__ only takes charset/env) — but it always keeps stdout and
# stderr separate internally and exposes both via result.stdout / result.stderr
# (result.output is the combined stream), so no combined-output filtering
# fallback is needed; these tests read the separated streams directly.


def test_format_json_stdout_is_report(tmp_path: Path, make_jar, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--format", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["totals"]["ok"] == 2
    assert "Completed" not in result.stdout  # ending narration went to stderr, not stdout


def test_format_ndjson_streams_events(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report(status="completed")

    def fake_run(settings, **kw):
        kw["on_done"](rep.artifacts[0])
        kw["on_warn"]("net sad")
        return rep

    monkeypatch.setattr(cli, "run", fake_run)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--format", "ndjson"]
    )
    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.stdout.splitlines() if line]
    events = {line["event"] for line in lines}
    assert events >= {"artifact", "warning", "summary"}
    artifact_event = next(line for line in lines if line["event"] == "artifact")
    assert artifact_event["rel"] == rep.artifacts[0].rel
    summary = next(line for line in lines if line["event"] == "summary")
    assert summary["status"] == "completed"
    assert summary["report"] == str(tmp_path / "out" / "decaf-report.json")


def test_format_ndjson_scan_event_fires_once(tmp_path: Path, make_jar, monkeypatch):
    """pipeline.run() calls on_found more than once by design: the initial
    discovered total, then bare deltas when fetch-time nested discovery
    diverges from the scan_counted estimate. Only the first call is a true
    total, so only it may become a scan event."""
    rep = ok_report()

    def fake_run(settings, **kw):
        kw["on_found"](3)
        kw["on_found"](1)  # a later delta, not a total — must not re-emit "scan"
        return rep

    monkeypatch.setattr(cli, "run", fake_run)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--format", "ndjson"]
    )
    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.stdout.splitlines() if line]
    scan_events = [line for line in lines if line["event"] == "scan"]
    assert len(scan_events) == 1
    assert scan_events[0]["artifacts"] == 3


def test_format_ndjson_quiet_still_streams_events(tmp_path: Path, make_jar, monkeypatch):
    """-q is orthogonal to --format: it silences stderr narration only, same as
    today — ndjson's stdout event stream keeps flowing regardless."""
    rep = ok_report()

    def fake_run(settings, **kw):
        kw["on_warn"]("net sad")
        return rep

    monkeypatch.setattr(cli, "run", fake_run)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(
        app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"),
              "--format", "ndjson", "--quiet"],
    )
    assert result.exit_code == 0
    events = {json.loads(line)["event"] for line in result.stdout.splitlines() if line}
    assert "warning" in events
    assert "summary" in events


def test_format_human_unchanged(tmp_path: Path, make_jar, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0
    assert "Completed in " in ANSI.sub("", result.stdout)


def test_machine_usage_error_stdout_empty(tmp_path: Path):
    result = runner.invoke(app, [str(tmp_path / "nope"), "--format", "json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "does not exist" in ANSI.sub("", result.stderr)


def test_format_json_swaps_console_to_stderr_and_restores(tmp_path: Path, make_jar, monkeypatch):
    captured = {}

    def capture(settings, **kw):
        captured["mid_run_stderr"] = cli.console.stderr
        return ok_report()

    monkeypatch.setattr(cli, "run", capture)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--format", "json"])
    assert captured["mid_run_stderr"] is True
    assert cli.console.stderr is False  # restored in `finally` for the next invocation


def _mixed_report(**over) -> RunReport:
    """1 ok / 1 partial / 1 failed / 1 network fallback, for `decaf report` CLI tests."""
    artifacts = [
        ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven",
                        gav="com.example:a:1.0", classes=3, java_files=3),
        ArtifactReport(rel="foo.jar", kind="archive", outcome="ok", method="cfr",
                        classes=5, java_files=4, missing_classes=1),  # partial
        ArtifactReport(
            rel="legacy.jar", kind="archive", outcome="failed",
            failure="engine timeout after 5 attempts",
            attempts=[EngineAttempt("vineflower", "archive", -1, True, 0, "engine crashed: boom")],
        ),
        ArtifactReport(rel="net.jar", kind="archive", outcome="ok", method="cfr",
                        sources_miss="network: sources download 503"),
    ]
    base = dict(
        settings={"chain": ["vineflower"]},
        artifacts=artifacts,
        totals=compute_totals(artifacts),
        duration_seconds=42.0,
        schema_version=1,
        decaf_version="1.8.0",
        ended_at="2026-07-27T00:00:42Z",
    )
    base.update(over)
    return RunReport(**base)


def _write_report(dir_path: Path, rep: RunReport) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "decaf-report.json").write_text(rep.to_json())
    return dir_path


def test_report_resolves_dir_and_file(tmp_path: Path):
    out = _write_report(tmp_path / "out", _mixed_report())

    result = runner.invoke(app, ["report", str(out)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert str(out / "decaf-report.json") in plain
    assert "schema 1" in plain
    assert "decaf 1.8.0" in plain
    assert "2026-07-27T00:00:42Z" in plain
    assert "Completed" in plain

    result_file = runner.invoke(app, ["report", str(out / "decaf-report.json")])
    assert result_file.exit_code == 0
    plain_file = ANSI.sub("", result_file.output)
    assert str(out / "decaf-report.json") in plain_file
    assert "Completed" in plain_file


def test_report_problems_groups_failures(tmp_path: Path):
    out = _write_report(tmp_path / "out", _mixed_report())
    result = runner.invoke(app, ["report", str(out), "--problems"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "engine timeout" in plain
    assert "legacy.jar" in plain
    assert "foo.jar" in plain  # partial artifact listed too


def test_report_artifact_glob_shows_detail(tmp_path: Path):
    out = _write_report(tmp_path / "out", _mixed_report())
    result = runner.invoke(app, ["report", str(out), "--artifact", "f*"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "foo.jar" in plain
    assert "missing_classes=1" in plain


def test_report_artifact_no_match_prints_message(tmp_path: Path):
    out = _write_report(tmp_path / "out", _mixed_report())
    result = runner.invoke(app, ["report", str(out), "--artifact", "nope*"])
    assert result.exit_code == 0
    assert "no artifacts match" in ANSI.sub("", result.output)


def test_report_network_fallbacks_lists_misses(tmp_path: Path):
    out = _write_report(tmp_path / "out", _mixed_report())
    result = runner.invoke(app, ["report", str(out), "--network-fallbacks"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "net.jar" in plain
    assert "network: sources download 503" in plain


def test_report_missing_dir_exits_2(tmp_path: Path):
    result = runner.invoke(app, ["report", str(tmp_path / "nope")])
    assert result.exit_code == 2
    plain = ANSI.sub("", result.output)
    assert "error:" in plain
    assert "report not found" in plain  # ReportError text, not run's "input ... does not exist"


def test_report_schema_version_warns(tmp_path: Path):
    out = _write_report(tmp_path / "out", _mixed_report(schema_version=99))
    result = runner.invoke(app, ["report", str(out)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "schema 99" in plain
    assert "warning" in plain.lower()


def test_report_word_is_command_not_input_path(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report").mkdir()  # a literal folder named 'report' in cwd
    result = runner.invoke(app, ["report"])  # must hit report_cmd, not scan ./report as run's input
    assert result.exit_code == 2
    plain = ANSI.sub("", result.output)
    assert "error:" in plain
    assert "decaf-out" in plain
    assert "input report does not exist" not in plain


def test_report_header_escapes_bracketed_decaf_version(tmp_path: Path):
    """Unescaped, '1.8.0[snapshot]' silently drops '[snapshot]' (Rich treats it as an
    unclosed style tag) instead of raising — either way the real value must survive."""
    out = _write_report(tmp_path / "out", _mixed_report(decaf_version="1.8.0[snapshot]"))
    result = runner.invoke(app, ["report", str(out)])
    assert result.exit_code == 0
    assert "[snapshot]" in ANSI.sub("", result.output)


def test_report_footer_escapes_bracketed_output_setting(tmp_path: Path):
    """Unescaped, a settings['output'] value containing a stray closing tag like
    '[/bold]' raises rich.errors.MarkupError, breaking the 'always exit 0 unless
    ReportError' mandate."""
    out = _write_report(tmp_path / "out", _mixed_report(settings={"output": "out[/bold]"}))
    result = runner.invoke(app, ["report", str(out)])
    assert result.exit_code == 0
    assert "out[/bold]" in ANSI.sub("", result.output)


def test_report_tolerates_pre_1_8_totals(tmp_path: Path):
    """decaf <=1.7 reports have a totals dict without 'network_misses' (added in
    1.8.0) or 'partial', and lack the schema/status/discovered fields entirely.
    render_ending must render best-effort, not KeyError."""
    data = {
        "settings": {"chain": ["vineflower"]},
        "artifacts": [
            {"rel": "a.jar", "kind": "archive", "outcome": "ok", "method": "maven",
             "classes": 3, "java_files": 3},
        ],
        "totals": {
            "artifacts": 1, "ok": 1, "failed": 0, "skipped": 0,
            "maven_sources": 1, "extracted": 0, "decompiled": 0,
            "java_files": 3, "collisions": 0,
            # no "partial", no "network_misses" -- both postdate this report
        },
        "duration_seconds": 10.0,
        # no schema_version/decaf_version/status/started_at/ended_at/discovered
    }
    out = tmp_path / "out"
    out.mkdir()
    (out / "decaf-report.json").write_text(json.dumps(data))

    result = runner.invoke(app, ["report", str(out)])
    assert result.exit_code == 0
    assert "Completed" in ANSI.sub("", result.output)


def test_report_empty_json_object_renders_zeros(tmp_path: Path):
    """A parseable-but-junk report ({}) must render best-effort zeros, not crash."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "decaf-report.json").write_text("{}")

    result = runner.invoke(app, ["report", str(out)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "Completed" in plain
    assert "0 processed · 0 complete" in plain
