import json
import threading
import zipfile
from pathlib import Path

import pytest

import decaf.engines as engines
from decaf.engines import EngineResult
from decaf.maven import Gav, Resolution
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
        Settings(input=input_dir, output=out, maven=False, mirror=False),
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


def test_run_mirror_is_default(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    run(
        Settings(input=input_dir, output=out, maven=False),
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


def make_deep_inputs(make_jar, tmp_path: Path) -> Path:
    """Two levels of archive nesting: app.war → dep.jar → inner.jar."""
    inner = make_jar("inner.jar", {"com/i/I.class": b"i"})
    dep = make_jar("deep-dep.jar", {"com/d/D.class": b"d", "lib/inner.jar": inner.read_bytes()})
    input_dir = tmp_path / "in"
    make_jar(
        "app.war",
        {
            "WEB-INF/classes/com/w/W.class": b"w",
            "WEB-INF/lib/dep.jar": dep.read_bytes(),
        },
        base=input_dir,
    )
    return input_dir


def test_run_default_depth_stops_after_one_level(fake_env, make_jar, tmp_path: Path):
    input_dir = make_deep_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, mirror=False),
        runner=perfect_engine,
    )
    rels = {r.rel: r for r in report.artifacts}
    assert rels["app.war!/WEB-INF/lib/dep.jar"].outcome == "ok"  # depth 1: still processed
    deep = rels["app.war!/WEB-INF/lib/dep.jar!/lib/inner.jar"]
    assert deep.outcome == "skipped"
    assert deep.kind == "beyond_depth"
    assert "--max-depth 1" in (deep.failure or "")
    assert (out / "src/com/d/D.java").is_file()
    assert not (out / "src/com/i/I.java").exists()
    assert report.settings["max_depth"] == 1


def test_run_max_depth_two_reaches_deeper(fake_env, make_jar, tmp_path: Path):
    input_dir = make_deep_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, mirror=False, max_depth=2),
        runner=perfect_engine,
    )
    assert report.totals["skipped"] == 0
    assert (out / "src/com/i/I.java").is_file()


def test_run_max_depth_zero_skips_all_nested(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, mirror=False, max_depth=0),
        runner=perfect_engine,
    )
    rels = {r.rel: r for r in report.artifacts}
    assert rels["app.war!/WEB-INF/lib/dep.jar"].outcome == "skipped"
    assert not (out / "src/com/d/D.java").exists()
    assert (out / "src/com/w/W.java").is_file()  # the war itself is still decompiled


def test_run_reports_found_counts(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    found: list[int] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_found=found.append,
        runner=perfect_engine,
    )
    assert found == [2, 1]  # initial scan, then the war's nested jar mid-run
    assert sum(found) == len(report.artifacts)


def test_run_resource_only_war_nested_jars_reached(fake_env, make_jar, tmp_path: Path):
    """A war whose only Java content is bundled jars (no loose .class) must
    still have those jars decompiled, even though the war itself is skipped."""
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    input_dir = tmp_path / "in"
    make_jar("only-libs.war", {"WEB-INF/lib/dep.jar": inner.read_bytes()}, base=input_dir)
    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, mirror=False),
        runner=perfect_engine,
    )
    rels = {r.rel: r for r in report.artifacts}
    assert rels["only-libs.war"].outcome == "skipped"
    assert rels["only-libs.war!/WEB-INF/lib/dep.jar"].outcome == "ok"
    assert (out / "src/com/d/D.java").is_file()


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


def test_run_streams_engine_stderr_with_prefix(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("app.jar", {"com/x/A.class": b"x"}, base=input_dir)
    lines: list[str] = []

    def chatty_engine(spec, jar_path, target, dest, timeout, java="java",
                      cpu_budget=None, on_stderr_line=None):
        on_stderr_line("warning: something odd")
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=chatty_engine,
        on_stderr=lines.append,
    )
    assert lines == ["vineflower app.jar: warning: something odd"]


def test_second_interrupt_during_teardown_still_writes_report(fake_env, make_jar, tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    input_dir = tmp_path / "in"
    for i in range(3):
        make_jar(f"a{i}.jar", {"com/x/A.class": b"x"}, base=input_dir)
    real_submit = ThreadPoolExecutor.submit
    calls = {"n": 0}

    def flaky_submit(self, fn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt  # first Ctrl-C
        return real_submit(self, fn, *args, **kwargs)

    def second_interrupt():
        raise KeyboardInterrupt  # second Ctrl-C while the handler tears down

    monkeypatch.setattr(ThreadPoolExecutor, "submit", flaky_submit)
    monkeypatch.setattr(engines.PROCESSES, "kill_all", second_interrupt)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    on_disk = json.loads((out / "decaf-report.json").read_text())
    assert on_disk["interrupted"] is True


def test_run_resets_closed_registry(fake_env, make_jar, tmp_path: Path):
    import decaf.engines as _e

    _e.PROCESSES.kill_all()  # simulate leftover state from an interrupted run
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=perfect_engine)
    assert report.totals["ok"] == 1


def test_run_enforces_cpu_budget_with_affinity(fake_env, make_jar, tmp_path: Path, monkeypatch):
    import os

    calls: list[set] = []
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, mask: calls.append(set(mask)), raising=False)
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, cpus=8),
        runner=perfect_engine,
    )
    assert calls[0] == set(range(8))  # pinned to the first 8 allowed cores; JVMs inherit
    assert calls[-1] == set(range(16))  # original mask restored on the way out
    assert len(calls) == 2


def test_run_default_cpu_budget_leaves_one_core_free(fake_env, make_jar, tmp_path: Path, monkeypatch):
    import os

    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=perfect_engine,
    )
    assert report.settings["cpus"] == 15  # auto budget = cores - 1
    assert report.settings["jobs"] == 4
    assert report.settings["cpu_budget"] == 3


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


def test_run_maven_hit_never_touches_engines(fake_env, make_jar, tmp_path: Path):
    """A maven sources hit completes without occupying an engine slot."""
    input_dir = tmp_path / "in"
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "// real source\nclass A {}"})

    def hit_resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=sources,
            repo="https://r.test/m2",
            resolved_by="pom-properties",
        )

    calls: list[str] = []

    def spy_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        calls.append(spec.name)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", mirror=False),
        runner=spy_engine,
        resolver=hit_resolver,
    )
    assert calls == []
    rels = {r.rel: r for r in report.artifacts}
    assert rels["lib-1.2.jar"].method == "maven"
    assert report.totals["maven_sources"] == 1
    assert "real source" in (tmp_path / "out/src/com/x/A.java").read_text()


def test_fetch_stage_overlaps_blocked_decompile(fake_env, make_jar, tmp_path: Path):
    """With jobs=1, resolution keeps flowing on the fetch pool while the single
    decompile worker is busy. The old fused loop fails this: its one worker
    resolved and decompiled serially, so the gate below never opened."""
    input_dir = tmp_path / "in"
    for i in range(3):
        make_jar(f"a{i}.jar", {"com/x/A.class": b"x"}, base=input_dir)

    resolved: list[str] = []
    all_resolved = threading.Event()
    violations: list[str] = []  # asserted from the test thread; an assert inside the
    # fake engine would be swallowed by the stage's except-Exception handler

    def counting_resolver(jar_path, repos, client, cache_dir, **kw):
        resolved.append(Path(jar_path).name)
        if len(resolved) == 3:
            all_resolved.set()
        return Resolution(miss="no pom.properties; 0 candidates")

    def gated_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if not all_resolved.wait(timeout=30):
            violations.append(
                f"decompile ran with only {len(resolved)} artifacts resolved"
            )
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", jobs=1),
        runner=gated_engine,
        resolver=counting_resolver,
    )
    assert violations == []  # stage 1 must not be serialized behind stage 2
    assert report.totals["ok"] == 3
    assert sorted(resolved) == ["a0.jar", "a1.jar", "a2.jar"]


def test_run_reports_fetch_pool_size(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1),
        runner=perfect_engine,
    )
    assert report.settings["jobs"] == 1
    assert report.settings["fetch_jobs"] == 2  # min(8, 2 * jobs)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out2", maven=False, jobs=16, cpus=16),
        runner=perfect_engine,
    )
    assert report.settings["fetch_jobs"] == 8  # capped, from the clamped jobs
    report = run(
        Settings(input=input_dir, output=tmp_path / "out3", maven=False, jobs=16, cpus=2),
        runner=perfect_engine,
    )
    assert report.settings["jobs"] == 2  # clamped by --cpus
    assert report.settings["fetch_jobs"] == 4  # derived from the clamped jobs, not the flag


def test_fetch_admission_bounded_by_decompile_backlog(fake_env, make_jar, tmp_path: Path):
    """Stage 1 must not run unbounded ahead: with jobs=1 the in-flight cap is
    jobs + 2*jobs = 3, so a 4th resolution cannot start while the first
    decompile is still running. The negative wait is an upper bound for the
    unwanted event, not a synchronization point — it never flakes a pass."""
    input_dir = tmp_path / "in"
    for i in range(12):
        make_jar(f"a{i:02d}.jar", {"com/x/A.class": b"x"}, base=input_dir)

    resolver_calls: list[str] = []
    overflow = threading.Event()  # set if a 4th resolution starts too early
    first_decompile_done = threading.Event()
    violations: list[str] = []  # asserted from the test thread, not inside the fake

    def counting_resolver(jar_path, repos, client, cache_dir, **kw):
        resolver_calls.append(Path(jar_path).name)
        if len(resolver_calls) > 3 and not first_decompile_done.is_set():
            overflow.set()
        return Resolution(miss="no pom.properties; 0 candidates")

    def slow_first_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if not first_decompile_done.is_set():
            # Give stage 1 a generous window to overrun the bound if it can.
            if overflow.wait(timeout=0.5):
                violations.append(
                    f"stage 1 started {len(resolver_calls)} resolutions while the "
                    "first decompile was still running (in-flight cap is 3)"
                )
            first_decompile_done.set()
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", jobs=1),
        runner=slow_first_engine,
        resolver=counting_resolver,
    )
    assert violations == []
    assert report.totals["ok"] == 12
    assert len(resolver_calls) == 12


def test_run_maven_hit_war_still_surfaces_nested_jar(fake_env, make_jar, tmp_path: Path):
    """A sources hit completes in the fetch stage, but nested archives inside
    the hit artifact must still be discovered and decompiled (discovery runs
    in stage 1 precisely so hits don't swallow their nested jars)."""
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    input_dir = tmp_path / "in"
    make_jar(
        "app.war",
        {
            "WEB-INF/classes/com/w/W.class": b"w",
            "WEB-INF/lib/dep.jar": inner.read_bytes(),
        },
        base=input_dir,
    )
    war_sources = make_jar("app-sources.jar", {"com/w/W.java": "// war source\nclass W {}"})

    def war_only_resolver(jar_path, repos, client, cache_dir, **kw):
        if Path(jar_path).name == "app.war":
            return Resolution(
                gav=Gav("com.example", "app", "1.0"),
                sources_jar=war_sources,
                repo="https://r.test/m2",
                resolved_by="verified-guess",
            )
        return Resolution(miss="no pom.properties; 0 candidates")

    calls: list[tuple[str, str]] = []

    def spy_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        calls.append((spec.name, Path(target).name))
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", mirror=False),
        runner=spy_engine,
        resolver=war_only_resolver,
    )
    rels = {r.rel: r for r in report.artifacts}
    assert rels["app.war"].method == "maven"
    assert rels["app.war"].outcome == "ok"
    nested = rels["app.war!/WEB-INF/lib/dep.jar"]
    assert nested.outcome == "ok"
    assert nested.method == "vineflower"
    assert calls == [("vineflower", "dep.jar")]  # engines ran for the nested jar only
    assert "war source" in (tmp_path / "out/src/com/w/W.java").read_text()
    assert (tmp_path / "out/src/com/d/D.java").is_file()
