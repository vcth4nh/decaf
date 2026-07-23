import json
import re
from pathlib import Path

from typer.testing import CliRunner

import decaf.cli as cli
from decaf.cli import app
from decaf.pipeline import ArtifactReport, RunReport

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


def test_summary_warns_on_network_misses(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    totals = dict(ok_report().totals, network_misses=2)
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report(totals=totals))
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "2 artifact(s) fell back to decompilation without sources due to network failures" in plain


def test_summary_silent_when_no_network_misses(tmp_path: Path, make_jar, monkeypatch):
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert "network failures" not in ANSI.sub("", result.output)


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


def test_status_line_cached_suffix():
    r = ArtifactReport(
        rel="a.jar", kind="archive", outcome="ok", method="maven",
        gav="com.example:lib:1.2", sources_cached=True,
    )
    assert cli._status_line(r) == "[green]✓[/] a.jar (maven sources, com.example:lib:1.2, cached)"
    r.sources_cached = False
    assert "cached" not in cli._status_line(r)


def test_summary_counts_cached_sources(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.artifacts[0].sources_cached = True
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "maven 1 (1 cached), decompiled 1, extracted 0" in plain


def test_summary_wording_unchanged_without_cache_hits(tmp_path: Path, make_jar, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings, **kw: ok_report())
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "maven 1, decompiled 1, extracted 0" in plain


def make_display():
    import io

    from rich.console import Console
    from rich.progress import SpinnerColumn, TextColumn

    console = Console(file=io.StringIO(), force_terminal=True, width=120)
    progress = cli._GroupedProgress(
        SpinnerColumn(), TextColumn("{task.description}"),
        console=console, transient=True,
    )
    return progress, cli._RunDisplay(progress)


def descriptions(progress):
    return [t.description for t in progress._ordered_tasks()]


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
    disp.on_event("fetch", "a.jar", "")
    assert any("fetching" in d and "a.jar" in d for d in descriptions(progress))
    assert descriptions(progress)[0] == "0/3 done · 1 fetching · 0 decompiling · 2 queued"
    disp.on_event("queued", "a.jar", "")
    assert not any("a.jar" in d for d in descriptions(progress))  # between stages: queued only
    disp.on_event("decompile", "a.jar", "vineflower")
    # "decompiling" is exactly 11 chars, so :<11 adds no padding — single space
    assert any(d.strip() == "decompiling a.jar (vineflower)" for d in descriptions(progress))
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


def test_summary_shows_resources_row(tmp_path: Path, make_jar, monkeypatch):
    rep = ok_report()
    rep.totals = {**rep.totals, "resources_copied": 7}
    monkeypatch.setattr(cli, "run", lambda settings, **kw: rep)
    make_jar("in/a.jar", {"A.class": b"x"}, base=tmp_path)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    plain = ANSI.sub("", result.output)
    assert "Resources" in plain
    assert "7" in plain


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
    assert "maven 1, decompiled 1, extracted 0, resources 1" in plain
