import json
import zipfile
from pathlib import Path

import pytest

import decaf.engines as engines
from decaf.engines import EngineResult
from decaf.pipeline import DecafError, Settings, run


def perfect_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
    """Fake engine: emits one .java per top-level .class entry, mirroring entry paths."""
    dest = Path(dest)
    target = Path(target)
    if target.is_dir():
        entries = [p.relative_to(target).as_posix() for p in target.rglob("*.class")]
    else:
        with zipfile.ZipFile(target) as zf:
            entries = [n for n in zf.namelist() if n.endswith(".class")]
    count = 0
    for entry in entries:
        if "$" in entry.rsplit("/", 1)[-1]:
            continue
        out = dest / (entry[: -len(".class")] + ".java")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"// decompiled from {entry}\nclass X {{}}\n")
        count += 1
    return EngineResult(spec.name, 0, False, count, "")


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(
        engines, "ensure_engine", lambda spec, client, cache_dir=None: Path(f"/fake/{spec.name}.jar")
    )


def make_inputs(make_jar, tmp_path: Path) -> Path:
    input_dir = tmp_path / "in"
    make_jar("libs/app.jar", {"com/x/A.class": b"x"}, base=input_dir)
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar(
        "app.war",
        {
            "WEB-INF/classes/com/w/W.class": b"w",
            "WEB-INF/lib/dep.jar": inner.read_bytes(),
        },
        base=input_dir,
    )
    return input_dir


def test_run_merged_end_to_end(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    done: list[str] = []
    report = run(
        Settings(input=input_dir, output=out, maven=False),
        on_done=lambda r: done.append(r.rel),
        runner=perfect_engine,
    )
    assert [r.rel for r in report.artifacts] == [
        "app.war",
        "app.war!/WEB-INF/lib/dep.jar",
        "libs/app.jar",
    ]
    assert report.totals["ok"] == 3 and report.totals["failed"] == 0
    assert sorted(done) == [r.rel for r in report.artifacts]
    assert (out / "src/com/x/A.java").is_file()
    assert (out / "src/com/w/W.java").is_file()      # WEB-INF/classes/ stripped
    assert (out / "src/com/d/D.java").is_file()      # nested jar reached
    on_disk = json.loads((out / "decaf-report.json").read_text())
    assert on_disk["totals"] == report.totals
    assert on_disk["settings"]["chain"] == ["vineflower", "cfr", "procyon", "fernflower", "jd"]


def test_run_mirror_mode(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    run(
        Settings(input=input_dir, output=out, maven=False, mirror=True),
        runner=perfect_engine,
    )
    assert (out / "libs/app.jar/com/x/A.java").is_file()
    assert (out / "app.war/WEB-INF/lib/dep.jar/com/d/D.java").is_file()


def test_run_mirror_mode_nested_archive_resource_no_collision(fake_env, make_jar, tmp_path: Path):
    """Real engines pass a nested archive through as a resource file alongside the
    decompiled .java files; the mirror output for the nested artifact's own
    decompile needs a directory at that same path, so the blob must not land."""

    def resource_emitting_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        result = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        target = Path(target)
        if not target.is_dir():
            with zipfile.ZipFile(target) as zf:
                if "WEB-INF/lib/dep.jar" in zf.namelist():
                    out = Path(dest) / "WEB-INF/lib/dep.jar"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(zf.read("WEB-INF/lib/dep.jar"))
        return result

    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, mirror=True),
        runner=resource_emitting_engine,
    )
    assert report.totals["failed"] == 0
    assert (out / "app.war/WEB-INF/lib/dep.jar/com/d/D.java").is_file()
    assert (out / "app.war/WEB-INF/lib/dep.jar").is_dir()


def test_run_requires_java(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: None)
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError, match="java not found"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out"))


def test_run_primary_engine_needs_newer_java(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 11))
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError, match="needs Java 17"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))


def test_run_drops_fallback_engine_too_new_for_runtime(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 17))
    monkeypatch.setattr(
        engines, "ensure_engine", lambda spec, client, cache_dir=None: Path(f"/fake/{spec.name}.jar")
    )
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine,
    )
    assert report.settings["chain"] == ["vineflower", "cfr", "procyon", "jd"]  # no fernflower


def test_run_primary_download_failure_is_fatal(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))

    def failing_ensure(spec, client, cache_dir=None):
        raise engines.EngineError(f"{spec.name}: download failed")

    monkeypatch.setattr(engines, "ensure_engine", failing_ensure)
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError, match="vineflower: download failed"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))


def test_run_counts_failed_artifacts(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("good.jar", {"com/x/A.class": b"x"}, base=input_dir)
    bad = input_dir / "bad.jar"
    bad.write_bytes(b"not a zip at all")
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=perfect_engine,
    )
    assert report.totals["ok"] == 1
    assert report.totals["failed"] == 1
    failed = [r for r in report.artifacts if r.outcome == "failed"]
    assert failed[0].rel == "bad.jar"


def test_interrupt_during_submission_still_writes_report(fake_env, make_jar, tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    input_dir = tmp_path / "in"
    for i in range(3):
        make_jar(f"a{i}.jar", {"com/x/A.class": b"x"}, base=input_dir)
    real_submit = ThreadPoolExecutor.submit
    calls = {"n": 0}

    def flaky_submit(self, fn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", flaky_submit)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    assert report.interrupted is True
    assert (out / "decaf-report.json").is_file()


def test_run_resets_closed_registry(fake_env, make_jar, tmp_path: Path):
    import decaf.engines as _e

    _e.PROCESSES.kill_all()  # simulate leftover state from an interrupted run
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=perfect_engine)
    assert report.totals["ok"] == 1


def test_run_cpu_budget_and_jobs_clamp(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    seen: list[int | None] = []

    def spy_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        seen.append(cpu_budget)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=4, cpus=2),
        runner=spy_engine,
    )
    assert report.settings["jobs"] == 2  # clamped: workers never exceed --cpus
    assert report.settings["cpus"] == 2
    assert report.settings["cpu_budget"] == 1
    assert seen == [1]  # each engine JVM sees a 1-core budget
