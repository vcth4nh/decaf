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
            "java_files": 5, "collisions": 0,
        },
        duration_seconds=1.0,
    )
    base.update(overrides)
    return RunReport(**base)


def test_help_lists_flags():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    for flag in ["--output", "--engine", "--no-fallback", "--merge", "--no-maven",
                 "--max-depth", "--repo", "--config", "--jobs", "--cpus", "--timeout",
                 "--force", "--version"]:
        assert flag in plain


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

    monkeypatch.setattr(cli, "run", lambda settings, on_done=None, on_found=None: ok_report())
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


def test_decaf_error_exits_2(tmp_path: Path, make_jar, monkeypatch):
    from decaf.pipeline import DecafError

    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")

    def boom(settings, on_done=None, on_found=None):
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
    monkeypatch.setattr(cli, "run", lambda settings, on_done=None, on_found=None: failing)
    result = runner.invoke(app, [str(tmp_path / "in"), "-o", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "x.jar" in result.output


def test_settings_wiring(tmp_path: Path, make_jar, monkeypatch):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    captured = {}

    def capture(settings, on_done=None, on_found=None):
        captured["settings"] = settings
        captured["on_found"] = on_found
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
