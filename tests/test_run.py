import json
import threading
import time
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
        engines,
        "ensure_engine",
        lambda spec, client, cache_dir=None, on_download=None: Path(f"/fake/{spec.name}.jar"),
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
    assert found == [3]  # 2 top-level + 1 pre-counted nested: seeded upfront, no growth
    assert sum(found) == len(report.artifacts)


def test_run_beyond_depth_discovery_still_ticks_totals(fake_env, make_jar, tmp_path: Path):
    input_dir = make_deep_inputs(make_jar, tmp_path)
    found: list[int] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False),
        on_found=found.append,
        runner=perfect_engine,
    )
    assert found == [2, 1]  # seed: war + its dep.jar; +1 when inner.jar surfaces beyond depth
    assert sum(found) == len(report.artifacts)


def test_run_corrects_total_when_precounted_member_unextractable(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar(
        "app.war",
        {"WEB-INF/classes/com/w/W.class": b"w", "../evil.jar": b"junk"},
        base=input_dir,
    )
    found: list[int] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False),
        on_found=found.append,
        runner=perfect_engine,
    )
    assert found == [2, -1]  # pre-counted ../evil.jar never extracts; total self-corrects
    assert sum(found) == len(report.artifacts) == 1


def test_run_emits_scan_event_after_seeding(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    log: list = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_found=lambda n: log.append(("found", n)),
        on_event=lambda k, s, d: log.append((k, s, d)),
        runner=perfect_engine,
    )
    assert ("scan", "", "2 top-level + 1 nested") in log
    assert log.index(("found", 3)) < log.index(("scan", "", "2 top-level + 1 nested"))


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


def test_run_totals_count_network_misses(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    for i in range(2):
        make_jar(f"a{i}.jar", {"com/x/A.class": b"x"}, base=input_dir)
    make_jar("b.jar", {"com/y/B.class": b"y"}, base=input_dir)

    def resolver(jar_path, repos, client, cache_dir, **kw):
        if Path(jar_path).name == "b.jar":
            return Resolution(miss="no pom.properties; 0 candidates")
        return Resolution(miss="network: r.test: timeout during sources download; no pom.properties")

    report = run(
        Settings(input=input_dir, output=tmp_path / "out"),
        runner=perfect_engine,
        resolver=resolver,
    )
    assert report.totals["network_misses"] == 2

    report2 = run(
        Settings(input=input_dir, output=tmp_path / "out2"),
        runner=perfect_engine,
        resolver=lambda *a, **kw: Resolution(miss="no pom.properties; 0 candidates"),
    )
    assert report2.totals["network_misses"] == 0


def test_run_binds_netstate_with_on_warn_into_default_resolver(
    fake_env, make_jar, tmp_path: Path, monkeypatch
):
    from decaf import pipeline

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    seen = {}

    def spy(jar_path, repos, client, cache_dir, *, net=None, **kw):
        seen["net"] = net
        return Resolution(miss="no pom.properties; 0 candidates")

    monkeypatch.setattr(pipeline.maven, "resolve_sources", spy)

    def warn(msg):
        pass

    run(Settings(input=input_dir, output=tmp_path / "out"), runner=perfect_engine, on_warn=warn)
    assert seen["net"] is not None
    assert seen["net"].warn is warn
    assert not seen["net"].abort.is_set()


def test_interrupt_sets_resolver_abort_event(fake_env, make_jar, tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from decaf import pipeline

    input_dir = tmp_path / "in"
    for i in range(3):
        make_jar(f"a{i}.jar", {"com/x/A.class": b"x"}, base=input_dir)

    monkeypatch.setattr(
        pipeline.maven,
        "resolve_sources",
        lambda *a, **kw: Resolution(miss="no pom.properties; 0 candidates"),
    )
    created: list = []
    real_netstate = pipeline.maven.NetState
    monkeypatch.setattr(
        pipeline.maven,
        "NetState",
        lambda *a, **kw: created.append(real_netstate(*a, **kw)) or created[-1],
    )
    real_submit = ThreadPoolExecutor.submit
    calls = {"n": 0}

    def flaky_submit(self, fn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", flaky_submit)
    report = run(Settings(input=input_dir, output=tmp_path / "out"), runner=perfect_engine)
    assert report.interrupted is True
    assert len(created) == 1
    assert created[0].abort.is_set()


def test_run_emits_artifact_stage_events(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
    )
    assert [e for e in events if e[1] == "a.jar"] == [
        ("fetch", "a.jar", ""),
        ("queued", "a.jar", ""),
        ("decompile", "a.jar", "vineflower"),
    ]


def test_run_maven_hit_emits_no_decompile_events_and_carries_cached(
    fake_env, make_jar, tmp_path: Path
):
    input_dir = tmp_path / "in"
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})

    def hit_resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=sources,
            repo="https://r.test/m2",
            resolved_by="pom-properties",
            cached=True,
        )

    events: list[tuple[str, str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", mirror=False),
        runner=perfect_engine,
        resolver=hit_resolver,
        on_event=lambda k, s, d: events.append((k, s, d)),
    )
    assert [e[0] for e in events if e[1] == "lib-1.2.jar"] == ["fetch"]
    rels = {r.rel: r for r in report.artifacts}
    assert rels["lib-1.2.jar"].sources_cached is True


def test_run_fallback_refires_decompile_event(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)

    def flaky_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name == "vineflower":
            return EngineResult(spec.name, 1, False, 0, "boom")
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=flaky_engine,
        on_event=lambda k, s, d: events.append((k, s, d)),
    )
    assert [e[2] for e in events if e[0] == "decompile"] == ["vineflower", "cfr"]


def test_report_json_carries_sources_cached(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    out = tmp_path / "out"
    run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    on_disk = json.loads((out / "decaf-report.json").read_text())
    assert on_disk["artifacts"][0]["sources_cached"] is False


def test_run_emits_engine_preflight_events(monkeypatch, make_jar, tmp_path: Path):
    from decaf.engines import ENGINES

    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))

    def fake_ensure(spec, client, cache_dir=None, on_download=None):
        if spec.name == "cfr" and on_download is not None:
            on_download()
        return Path(f"/fake/{spec.name}.jar")

    monkeypatch.setattr(engines, "ensure_engine", fake_ensure)
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine,
        on_event=lambda k, s, d: events.append((k, s, d)),
    )
    eng = [e for e in events if e[0] == "engines"]
    ver = ENGINES["cfr"].version
    assert eng[0] == ("engines", "", "verifying")
    assert ("engines", "cfr", f"downloading {ver}") in eng
    assert ("engines", "cfr", f"downloaded {ver}") in eng
    assert eng[-1] == ("engines", "", "ready")
    assert eng.index(("engines", "cfr", f"downloading {ver}")) < eng.index(
        ("engines", "cfr", f"downloaded {ver}")
    )


def test_run_cached_engines_emit_no_download_events(fake_env, make_jar, tmp_path: Path):
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine,
        on_event=lambda k, s, d: events.append((k, s, d)),
    )
    eng = [e for e in events if e[0] == "engines"]
    assert eng == [("engines", "", "verifying"), ("engines", "", "ready")]


def test_preflight_omits_hook_without_on_event(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(
        engines,
        "ensure_engine",
        lambda spec, client, cache_dir=None: Path(f"/fake/{spec.name}.jar"),
    )  # strict 3-arg fake: run() without on_event must never pass on_download
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine,
    )
    assert report.totals["ok"] == 1


def test_fetch_admission_pops_largest_first(fake_env, make_jar, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from decaf import pipeline

    input_dir = tmp_path / "in"
    make_jar("a-small.jar", {"com/s/A.class": b"x"}, base=input_dir)
    make_jar("m-big.jar", {f"com/b/C{i}.class": b"x" for i in range(40)}, base=input_dir)
    make_jar("z-mid.jar", {f"com/m/M{i}.class": b"x" for i in range(10)}, base=input_dir)
    order: list[str] = []
    real_submit = ThreadPoolExecutor.submit

    def spy_submit(self, fn, *args, **kwargs):
        if fn is pipeline._fetch_stage:
            order.append(args[0].rel)
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", spy_submit)
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=perfect_engine,
    )
    # scan order is alphabetical (a-small, m-big, z-mid); size order must win
    assert order == ["m-big.jar", "z-mid.jar", "a-small.jar"]


def test_ready_buffer_drains_largest_first(fake_env, make_jar, tmp_path):
    """Gate every engine start until all three artifacts are queued; with
    jobs=1 the buffered pair must then start in size order."""
    input_dir = tmp_path / "in"
    make_jar("a-small.jar", {"com/s/A.class": b"x"}, base=input_dir)
    make_jar("m-big.jar", {f"com/b/C{i}.class": b"x" for i in range(40)}, base=input_dir)
    make_jar("z-mid.jar", {f"com/m/M{i}.class": b"x" for i in range(10)}, base=input_dir)
    sizes = {"m-big.jar": 40, "z-mid.jar": 10, "a-small.jar": 1}
    all_queued = threading.Event()
    queued: list[str] = []

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued.append(subject)
            if len(queued) == 3:
                all_queued.set()

    started: list[str] = []
    lock = threading.Lock()

    def gated_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        with lock:
            started.append(Path(target).name)
        all_queued.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1),
        runner=gated_engine,
        on_event=on_event,
    )
    # started[0] is whichever fetch finished first; the buffered rest must be size-ordered
    assert len(started) == 3
    assert [sizes[n] for n in started[1:]] == sorted((sizes[n] for n in started[1:]), reverse=True)


def test_whale_reserves_headroom(fake_env, make_jar, tmp_path):
    """A whale weighs 2 slots: with jobs=4, at most weight 4 runs concurrently,
    so the 4th artifact must wait for a completion."""
    input_dir = tmp_path / "in"
    make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)}, base=input_dir)
    for i in range(3):
        make_jar(f"s{i}.jar", {f"com/s{i}/A.class": b"x"}, base=input_dir)
    started: list[str] = []
    lock = threading.Lock()
    release = threading.Event()

    def blocking_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        with lock:
            started.append(Path(target).name)
        release.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    result: dict = {}

    def drive():
        result["report"] = run(
            Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=4, cpus=8),
            runner=blocking_engine,
        )

    t = threading.Thread(target=drive)
    t.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with lock:
            if len(started) == 3:
                break
        time.sleep(0.01)
    time.sleep(0.3)  # generous window for an over-admitted 4th start
    with lock:
        assert len(started) == 3  # whale(2) + two smalls(1+1) fill jobs=4
    release.set()
    t.join(timeout=30)
    assert result["report"].totals["ok"] == 4


def test_whale_progresses_with_single_job(fake_env, make_jar, tmp_path):
    """weight 2 > jobs=1 must not deadlock: an idle pool admits anything."""
    input_dir = tmp_path / "in"
    make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1),
        runner=perfect_engine,
    )
    assert report.totals["ok"] == 1


def kotlin_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
    """Fake engine emitting .kt per top-level class (a vineflower Kotlin-plugin run)."""
    dest = Path(dest)
    with zipfile.ZipFile(Path(target)) as zf:
        entries = [n for n in zf.namelist() if n.endswith(".class")]
    n = 0
    for entry in entries:
        if "$" in entry.rsplit("/", 1)[-1]:
            continue
        out = dest / (entry[: -len(".class")] + ".kt")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("fun x() {}\n")
        n += 1
    return EngineResult(spec.name, 0, False, n, "")


def test_run_accepts_kotlin_output_without_fallback(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("k.jar", {"okio/Buffer.class": b"k", "okio/Okio.class": b"k"}, base=input_dir)
    engines_used: list[str] = []

    def spy(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        engines_used.append(spec.name)
        return kotlin_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False),
        runner=spy,
    )
    r = report.artifacts[0]
    assert engines_used == ["vineflower"]  # success on first engine, no fallback
    assert r.outcome == "ok" and r.method == "vineflower"
    assert r.missing_classes == 0
    assert r.java_files == 2
    assert (tmp_path / "out/src/okio/Buffer.kt").is_file()
    assert report.totals["java_files"] == 2


def merged_batch_engine(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
    """Batch fake: one .java per top-level class of every target, merged dest."""
    dest = Path(dest)
    n = 0
    for t in targets:
        with zipfile.ZipFile(Path(t)) as zf:
            for entry in zf.namelist():
                if not entry.endswith(".class") or "$" in entry.rsplit("/", 1)[-1]:
                    continue
                out = dest / (entry[: -len(".class")] + ".java")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("class X {}\n")
                n += 1
    return EngineResult(spec.name, 0, False, n, "")


def test_run_batches_ready_smalls_into_one_jvm(fake_env, make_jar, tmp_path):
    """The smalls are nested INSIDE the big jar, so the big jar's hand-off is
    processed in the same scheduler iteration that discovers them: with jobs=1
    the big jar deterministically occupies the worker first, the smalls pile
    into the batch buffer, and on release they form one batch. Two smalls keep
    the in-flight admission cap (jobs + 2*jobs = 3) from stalling the gate."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar(
        "big.jar",
        {
            **{f"com/big/C{i}.class": b"x" for i in range(900)},
            "lib/s0.jar": s0.read_bytes(),
            "lib/s1.jar": s1.read_bytes(),
        },
        base=input_dir,
    )
    batches: list[list[str]] = []
    all_queued = threading.Event()
    queued_count = [0]

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued_count[0] += 1
            if queued_count[0] == 3:
                all_queued.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def recording_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        batches.append(sorted(Path(t).name for t in targets))
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False, jobs=1, cpus=1),
        runner=gated_solo,
        batch_runner=recording_batch,
        on_event=on_event,
    )
    assert report.totals["ok"] == 3
    assert batches == [["s0.jar", "s1.jar"]]
    rels = {r.rel: r for r in report.artifacts}
    for i in range(2):
        assert rels[f"big.jar!/lib/s{i}.jar"].attempts[0].level == "batch"
        assert rels[f"big.jar!/lib/s{i}.jar"].method == "vineflower"
    assert rels["big.jar"].attempts[0].level == "archive"
    assert (tmp_path / "out/src/com/s0/A.java").is_file()


def test_run_overlapping_stems_never_share_a_batch(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("x1.jar", {"com/dup/Same.class": b"a"}, base=input_dir)
    make_jar("x2.jar", {"com/dup/Same.class": b"b"}, base=input_dir)
    solo: list[str] = []
    batches: list[list[str]] = []

    def solo_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        solo.append(Path(target).name)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def recording_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        batches.append([Path(t).name for t in targets])
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1),
        runner=solo_engine,
        batch_runner=recording_batch,
    )
    assert report.totals["ok"] == 2
    assert batches == []  # overlap → both went solo (a 1-member batch submits solo)
    assert sorted(solo) == ["x1.jar", "x2.jar"]


def test_run_batch_failure_requeues_members_solo(fake_env, make_jar, tmp_path):
    """Same nested-smalls structure as the formation test, but the batch engine
    fails — every member must come back through the solo chain and succeed."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar(
        "big.jar",
        {
            **{f"com/big/C{i}.class": b"x" for i in range(900)},
            "lib/s0.jar": s0.read_bytes(),
            "lib/s1.jar": s1.read_bytes(),
        },
        base=input_dir,
    )
    all_queued = threading.Event()
    queued_count = [0]

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued_count[0] += 1
            if queued_count[0] == 3:
                all_queued.set()

    solo: list[str] = []

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)
        solo.append(Path(target).name)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def broken_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        return EngineResult(spec.name, 1, False, 0, "boom")

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1),
        runner=gated_solo,
        batch_runner=broken_batch,
        on_event=on_event,
    )
    assert report.totals["ok"] == 3 and report.totals["failed"] == 0
    rels = {r.rel: r for r in report.artifacts}
    for i in range(2):
        levels = [at.level for at in rels[f"big.jar!/lib/s{i}.jar"].attempts]
        assert levels[0] == "batch" and "archive" in levels  # failed batch, then solo
    assert sorted(n for n in solo if n != "big.jar") == ["s0.jar", "s1.jar"]


def test_run_passes_cds_dir_to_default_runner(monkeypatch, make_jar, tmp_path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(
        engines, "ensure_engine",
        lambda spec, client, cache_dir=None, on_download=None: Path(f"/fake/{spec.name}.jar"),
    )
    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "cache")
    seen = {}

    def spy(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, cds_dir=None):
        seen["cds"] = cds_dir
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    monkeypatch.setattr(engines, "run_engine", spy)
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))
    assert seen["cds"] == tmp_path / "cache" / "engines"


def test_run_no_cds_below_java_19(monkeypatch, make_jar, tmp_path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 17))
    monkeypatch.setattr(
        engines, "ensure_engine",
        lambda spec, client, cache_dir=None, on_download=None: Path(f"/fake/{spec.name}.jar"),
    )
    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "cache")
    seen = {"cds": "unset"}

    def spy(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, cds_dir=None):
        seen["cds"] = cds_dir
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    monkeypatch.setattr(engines, "run_engine", spy)
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))
    assert seen["cds"] is None
