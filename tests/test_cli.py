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
            "java_files": 5, "collisions": 0, "network_misses": 0,
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

    def fake_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        out = Path(dest) / "com/x/A.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("class A {}")
        return EngineResult(spec.name, 0, False, 1, "")

    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(engines, "ensure_engine",
                        lambda spec, client, cache_dir=None: Path(f"/fake/{spec.name}.jar"))
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
