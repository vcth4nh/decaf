import json
import os
import re
import struct
import sys
import threading
import time
import warnings
import zipfile
from pathlib import Path

import pytest

import decaf.engines as engines
from decaf import casefs, check
from decaf.engines import EngineResult
from decaf.maven import Gav, Resolution
from decaf.pipeline import DecafError, Settings, _case_failure, run


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
    assert on_disk["settings"]["chain"] == ["vineflower", "cfr", "fernflower", "procyon", "jd"]


def test_run_mirror_is_default(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    run(
        Settings(input=input_dir, output=out, maven=False),
        runner=perfect_engine,
    )
    assert (out / "libs/app.jar/com/x/A.java").is_file()
    assert (out / "app.war/WEB-INF/lib/dep.jar/com/d/D.java").is_file()


def test_run_kotlin_sources_jar_extracted(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("kt-lib-sources.jar", {"com/k/K.kt": "fun k() {}"}, base=input_dir)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    (art,) = report.artifacts
    assert art.kind == "sources_jar"
    assert art.method == "extracted"
    assert art.java_files == 1
    assert (out / "kt-lib-sources.jar/com/k/K.kt").is_file()


def test_report_discovered_counts_nested(fake_env, make_jar, tmp_path: Path):
    input_dir = make_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    assert report.discovered == 3 == report.totals["artifacts"]
    assert json.loads((out / "decaf-report.json").read_text())["discovered"] == 3


def test_report_settings_repos_redacted(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("app.jar", {"com/x/A.class": b"x"}, base=input_dir)
    out = tmp_path / "out"
    report = run(
        Settings(
            input=input_dir, output=out, maven=False,
            repos=("https://deploy:s3cr3t@r.test/m2",),
        ),
        runner=perfect_engine,
    )
    assert report.settings["repos"] == ["https://r.test/m2"]
    assert "s3cr3t" not in (out / "decaf-report.json").read_text()


def test_run_uppercase_source_suffixes_counted(fake_env, make_jar, tmp_path: Path):
    """Case-insensitive extraction already keeps A.JAVA/K.KT; the writers must count them."""
    input_dir = tmp_path / "in"
    make_jar(
        "up-lib-sources.jar",
        {"com/k/K.kt": "fun k() {}", "com/k/A.JAVA": "class A {}"},
        base=input_dir,
    )
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    (art,) = report.artifacts
    assert art.method == "extracted"
    assert art.java_files == 2
    assert (out / "up-lib-sources.jar/com/k/K.kt").is_file()
    assert (out / "up-lib-sources.jar/com/k/A.JAVA").is_file()


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
    with pytest.raises(DecafError, match="needs Java 17") as err:
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))
    assert "--engine" in str(err.value) and "decaf engines list" in str(err.value)  # the hint (#113)


def test_run_drops_fallback_engine_too_new_for_runtime(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 17))
    monkeypatch.setattr(
        engines, "ensure_engine", lambda spec, client, cache_dir=None: Path(f"/fake/{spec.name}.jar")
    )
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    warns: list[str] = []
    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine,
        on_warn=warns.append,
    )
    assert report.settings["chain"] == ["vineflower", "cfr", "procyon", "jd"]  # no fernflower
    assert "fallback chain: skipped fernflower (needs Java 21+, found 17)" in warns


def test_dropped_fallback_warning_silent_when_chain_full(fake_env, make_jar, tmp_path: Path):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    warns: list[str] = []
    run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine,
        on_warn=warns.append,
    )
    assert not [w for w in warns if w.startswith("fallback chain:")]


def test_run_primary_download_failure_is_fatal(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))

    def failing_ensure(spec, client, cache_dir=None):
        raise engines.EngineError(f"{spec.name}: download failed")

    monkeypatch.setattr(engines, "ensure_engine", failing_ensure)
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError, match="vineflower: download failed"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))


def test_run_warns_about_fallback_engines_that_fail_to_download(monkeypatch, make_jar, tmp_path: Path):
    """#113: a fallback engine that failed to download left the chain with no word, and a cache error gave a traceback."""
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))

    def ensure(spec, client, cache_dir=None):
        if spec.name == "procyon":
            raise engines.EngineError("procyon: download failed: boom")
        if spec.name == "jd":
            raise PermissionError(13, "Permission denied", "/cache/engines/jd-1.2.0.jar")
        return Path(f"/fake/{spec.name}.jar")

    monkeypatch.setattr(engines, "ensure_engine", ensure)
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    warns: list[str] = []
    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine, on_warn=warns.append,
    )
    assert report.settings["chain"] == ["vineflower", "cfr", "fernflower"]
    assert (
        "fallback chain: skipped procyon (download failed: boom), jd (Permission denied: /cache/engines/jd-1.2.0.jar)"
        in warns
    )


def test_run_primary_engine_cache_error_is_a_clean_error(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))

    def ensure(spec, client, cache_dir=None):
        raise PermissionError(13, "Permission denied", "/cache/engines")

    monkeypatch.setattr(engines, "ensure_engine", ensure)
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError, match="^vineflower: Permission denied: /cache/engines$"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))


def _engine_jar(path: Path, java: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("e/Main.class", b"\xca\xfe\xba\xbe\x00\x00" + (java + 44).to_bytes(2, "big"))
    return path


def test_run_skips_a_pinned_fallback_that_needs_a_newer_java(monkeypatch, make_jar, tmp_path: Path):
    """#113: the newest Fernflower needs Java 25, but its built-in spec says 21."""
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(
        engines, "ensure_engine",
        lambda spec, client, cache_dir=None: _engine_jar(tmp_path / "jars" / f"{spec.name}.jar",
                                                         25 if spec.name == "fernflower" else 17),
    )
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    warns: list[str] = []
    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
        runner=perfect_engine, on_warn=warns.append,
    )
    assert report.settings["chain"] == ["vineflower", "cfr", "procyon", "jd"]
    assert "fallback chain: skipped fernflower (needs Java 25+, found 21)" in warns


def test_run_stops_with_a_hint_when_the_primary_jar_needs_a_newer_java(monkeypatch, make_jar, tmp_path: Path):
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    monkeypatch.setattr(
        engines, "ensure_engine",
        lambda spec, client, cache_dir=None: _engine_jar(tmp_path / "jars" / f"{spec.name}.jar", 25),
    )
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError) as err:
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, engine="cfr"))
    assert str(err.value).startswith("engine cfr needs Java 25+, found Java 21")
    assert "--engine" in str(err.value) and "decaf engines list" in str(err.value)


def test_run_stops_when_the_java_version_is_unknown(monkeypatch, make_jar, tmp_path: Path):
    """#113: java -version gave no version (no answer, or a text that decaf cannot read)."""
    monkeypatch.setattr(engines, "find_java", lambda: ("/opt/java/bin/java", 0))
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    with pytest.raises(DecafError, match="cannot read the Java version of /opt/java/bin/java"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))


_PINS = {name: {"version": "9.9", "url": f"https://x.test/{name}.jar", "sha256": "c" * 64} for name in ("vineflower", "cfr")}


def test_run_gives_the_runners_the_pinned_spec(fake_env, make_jar, tmp_path: Path):
    """#113: the runners got the built-in spec, so a pinned engine got the CDS archive name of the built-in version."""
    seen: set[tuple[str, str]] = set()

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        seen.add((spec.name, spec.version))
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower":
            (Path(dest) / "com/x/B.java").unlink()  # B goes missing: a cfr class retry follows
            res.java_files -= 1
        return res

    make_jar("app.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=tmp_path / "in")
    run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, engine_overrides=_PINS), runner=engine)
    assert seen == {("vineflower", "9.9"), ("cfr", "9.9")}


def test_run_gives_the_batch_runner_the_pinned_spec(fake_env, make_jar, tmp_path: Path):
    """The smalls wait behind the gated big jar and form one batch, as in test_run_batches_ready_smalls_into_one_jvm."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar(
        "big.jar",
        {**{f"com/big/C{i}.class": b"x" for i in range(900)}, "lib/s0.jar": s0.read_bytes(), "lib/s1.jar": s1.read_bytes()},
        base=input_dir,
    )
    all_queued = threading.Event()
    queued = [0]
    seen: set[tuple[str, str]] = set()

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued[0] += 1
            if queued[0] == 3:
                all_queued.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        seen.add((spec.name, spec.version))
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False, jobs=1, cpus=1, engine_overrides=_PINS),
        runner=gated_solo, batch_runner=batch, on_event=on_event,
    )
    assert seen == {("vineflower", "9.9")}


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
    assert report.discovered == 3  # denominator survives the interrupt
    assert report.totals["artifacts"] < 3
    assert report.status == "interrupted"


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


def test_error_in_a_callback_aborts_the_run_with_a_report(fake_env, make_jar, tmp_path: Path):
    """#112: the JVMs get a kill, the report says aborted, and the error goes on to the caller."""
    input_dir = tmp_path / "in"
    for i in range(3):
        make_jar(f"a{i}.jar", {f"com/x{i}/A.class": b"x"}, base=input_dir)

    def broken_pipe(report):
        raise BrokenPipeError(32, "Broken pipe")

    out = tmp_path / "out"
    try:
        with pytest.raises(BrokenPipeError):
            run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine, on_done=broken_pipe)
        assert engines.PROCESSES.closed
    finally:
        engines.PROCESSES.reset()
    on_disk = json.loads((out / "decaf-report.json").read_text())
    assert (on_disk["status"], on_disk["interrupted"]) == ("aborted", True)


def test_an_error_in_on_done_keeps_the_other_jars_of_the_batch(fake_env, make_jar, tmp_path: Path):
    """Review S3: an error at one jar of a batch lost the other jars, though their files were written."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar("big.jar", {**{f"com/big/C{i}.class": b"x" for i in range(900)},
                         "lib/s0.jar": s0.read_bytes(), "lib/s1.jar": s1.read_bytes()}, base=input_dir)
    all_queued = threading.Event()
    queued = [0]

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued[0] += 1
            if queued[0] == 3:
                all_queued.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)  # the two small jars wait together and form one batch
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def on_done(report):
        if "!/" in report.rel:
            raise BrokenPipeError(32, "Broken pipe")

    out = tmp_path / "out"
    try:
        with pytest.raises(BrokenPipeError):
            run(Settings(input=input_dir, output=out, maven=False, mirror=False, jobs=1, cpus=1),
                runner=gated_solo, batch_runner=merged_batch_engine, on_event=on_event, on_done=on_done)
    finally:
        engines.PROCESSES.reset()
    rels = {a["rel"] for a in json.loads((out / "decaf-report.json").read_text())["artifacts"]}
    assert {"big.jar!/lib/s0.jar", "big.jar!/lib/s1.jar"} <= rels


def test_an_error_in_on_done_keeps_a_job_that_ended_before_the_abort(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """Review S3: a job that ended while on_done failed for another job had its files, but no row."""
    import decaf.pipeline as pipeline

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/a/A.class": b"x"}, base=input_dir)
    make_jar("b.jar", {"com/b/B.class": b"x"}, base=input_dir)
    a_reported, b_ended = threading.Event(), threading.Event()
    real_stage = pipeline._decompile_stage

    def stage(a, target, ctx, report):
        result = real_stage(a, target, ctx, report)
        if a.rel == "b.jar":
            b_ended.set()
        return result

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "b.jar":
            a_reported.wait(timeout=30)  # b.jar ends while on_done of a.jar runs
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def on_done(report):
        if report.rel == "a.jar":
            a_reported.set()
            assert b_ended.wait(timeout=30)
            time.sleep(0.3)  # the future of b.jar is done just after its stage returns
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(pipeline, "_decompile_stage", stage)
    out = tmp_path / "out"
    try:
        with pytest.raises(BrokenPipeError):
            run(Settings(input=input_dir, output=out, maven=False, jobs=2, cpus=2), runner=engine, on_done=on_done)
    finally:
        engines.PROCESSES.reset()
    assert (out / "b.jar/com/b/B.java").is_file()
    rels = {a["rel"] for a in json.loads((out / "decaf-report.json").read_text())["artifacts"]}
    assert rels == {"a.jar", "b.jar"}


def test_an_error_in_on_done_keeps_a_lookup_that_ended_before_the_abort(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """Review S3: sources that a lookup wrote while on_done failed for another job had no row.
    A jar that its fetch only unpacked for the engines gets no row, and a fetch that failed
    does not replace the error that stopped the run."""
    import decaf.pipeline as pipeline

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/a/A.class": b"x"}, base=input_dir)
    make_jar("b.jar", {"com/b/B.class": b"x"}, base=input_dir)
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "// real source\nclass A {}"})
    make_jar("c.jar", {"com/c/C.class": b"x"}, base=input_dir)
    a_reported = threading.Event()
    fetched = {name: threading.Event() for name in ("b.jar", "c.jar", "lib-1.2.jar")}
    real_fetch = pipeline._fetch_stage

    def fetch(a, ctx):
        if a.rel in fetched:
            a_reported.wait(timeout=30)  # these fetches end while on_done of a.jar runs
        try:
            if a.rel == "c.jar":
                raise RuntimeError("a fault in the fetch of c.jar")
            return real_fetch(a, ctx)
        finally:
            if a.rel in fetched:
                fetched[a.rel].set()

    def resolver(jar_path, repos, client, cache_dir, **kw):
        if Path(jar_path).name != "lib-1.2.jar":
            return Resolution(miss="no sources")
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources,
                          repo="https://r.test/m2", resolved_by="pom-properties")

    def on_done(report):
        if report.rel == "a.jar":
            a_reported.set()
            assert all(e.wait(timeout=30) for e in fetched.values())
            time.sleep(0.3)  # the futures are done just after their stages return
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(pipeline, "_fetch_stage", fetch)
    out = tmp_path / "out"
    try:
        with pytest.raises(BrokenPipeError):
            run(Settings(input=input_dir, output=out, jobs=2, cpus=2), runner=perfect_engine,
                resolver=resolver, on_done=on_done)
    finally:
        engines.PROCESSES.reset()
    assert "real source" in (out / "lib-1.2.jar/com/x/A.java").read_text()
    rels = {a["rel"] for a in json.loads((out / "decaf-report.json").read_text())["artifacts"]}
    assert rels == {"a.jar", "lib-1.2.jar"}  # b.jar was not decompiled, and the fetch of c.jar failed


def test_an_error_in_on_done_keeps_a_batch_that_ended_before_the_abort(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """Review S3: a batch that ended while on_done failed for another job had its files, but no rows."""
    import decaf.pipeline as pipeline

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/a/A.class": b"x"}, base=input_dir)
    make_jar("c.jar", {"com/c/C.class": b"x"}, base=input_dir)
    smalls = {f"WEB-INF/lib/s{i}.jar": make_jar(f"s{i}.jar", {f"com/s{i}/A.class": b"x"}).read_bytes()
              for i in range(2)}
    make_jar("app.war", smalls, base=input_dir)
    nested = {f"app.war!/WEB-INF/lib/s{i}.jar" for i in range(2)}
    lock = threading.Lock()
    started: set[str] = set()
    queued: set[str] = set()
    smalls_queued, batch_started, batch_ended, c_reported = (threading.Event() for _ in range(4))

    def wait_for(names, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock:
                if names <= started:
                    return
            time.sleep(0.01)

    def on_event(kind, subject, detail):
        if kind == "queued":
            with lock:
                queued.add(subject)
                if nested <= queued:
                    smalls_queued.set()

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        name = Path(target).name
        with lock:
            started.add(name)
        if name == "a.jar":
            smalls_queued.wait(timeout=30)  # the two small jars wait together and form one batch
        else:
            batch_started.wait(timeout=30)  # c.jar ends while the batch runs
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        batch_started.set()
        c_reported.wait(timeout=30)  # the batch ends while on_done of c.jar runs
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    real_fetch, real_batch = pipeline._fetch_stage, pipeline._decompile_batch

    def fetch(a, ctx):
        if a.rel == "c.jar":
            wait_for({"a.jar"})  # a.jar and c.jar run alone, not as one batch
        elif a.rel == "app.war":
            wait_for({"a.jar", "c.jar"})
        return real_fetch(a, ctx)

    def decompile_batch(batch_items, ctx):
        result = real_batch(batch_items, ctx)
        batch_ended.set()
        return result

    def on_done(report):
        if report.rel == "c.jar":
            c_reported.set()
            assert batch_ended.wait(timeout=30)
            time.sleep(0.3)  # the future of the batch is done just after the batch returns
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(pipeline, "_fetch_stage", fetch)
    monkeypatch.setattr(pipeline, "_decompile_batch", decompile_batch)
    out = tmp_path / "out"
    try:
        with pytest.raises(BrokenPipeError):
            run(Settings(input=input_dir, output=out, maven=False, jobs=2, cpus=2), runner=engine,
                batch_runner=batch, on_event=on_event, on_done=on_done)
    finally:
        engines.PROCESSES.reset()
    rels = {a["rel"] for a in json.loads((out / "decaf-report.json").read_text())["artifacts"]}
    assert {"a.jar", "c.jar", *nested} <= rels

def test_an_interrupt_before_the_scheduler_writes_the_report(fake_env, make_jar, tmp_path: Path):
    """Review S4: Ctrl-C or SIGTERM before the scheduler, for example in the javac build of the
    checker, wrote no report, so the report of an older run stayed."""
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    out = tmp_path / "out"
    out.mkdir()
    (out / "decaf-report.json").write_text('{"status": "completed", "ended_at": "an older run"}')

    def on_found(count):
        raise KeyboardInterrupt

    try:
        report = run(Settings(input=tmp_path / "in", output=out, maven=False),
                     runner=perfect_engine, on_found=on_found)
    finally:
        engines.PROCESSES.reset()
    assert report.status == "interrupted"
    on_disk = json.loads((out / "decaf-report.json").read_text())
    assert (on_disk["status"], on_disk["interrupted"]) == ("interrupted", True)


def test_a_callback_error_before_the_scheduler_writes_the_report(fake_env, make_jar, tmp_path: Path):
    """Review S4: a broken pipe at the first ndjson line wrote no report."""
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    out = tmp_path / "out"

    def on_found(count):
        raise BrokenPipeError(32, "Broken pipe")

    try:
        with pytest.raises(BrokenPipeError):
            run(Settings(input=tmp_path / "in", output=out, maven=False), runner=perfect_engine, on_found=on_found)
    finally:
        engines.PROCESSES.reset()
    assert json.loads((out / "decaf-report.json").read_text())["status"] == "aborted"


def test_a_setup_error_writes_no_report(make_jar, tmp_path: Path, monkeypatch):
    """#109 stays: a setup error ends the run with exit 2 and writes no report."""
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))

    def no_engine(spec, client, cache_dir=None, on_download=None):
        raise engines.EngineError(f"{spec.name}: download failed")

    monkeypatch.setattr(engines, "ensure_engine", no_engine)
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    out = tmp_path / "out"
    with pytest.raises(DecafError):
        run(Settings(input=tmp_path / "in", output=out, maven=False), runner=perfect_engine)
    assert not (out / "decaf-report.json").exists()


def test_no_sources_reach_the_output_after_an_abort(fake_env, make_jar, tmp_path: Path):
    """Review S5: sources that a lookup gave after the abort reached the output with no row."""
    input_dir = tmp_path / "in"
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "// real source\nclass A {}"})

    def late_resolver(jar_path, repos, client, cache_dir, **kw):
        engines.PROCESSES.kill_all()  # the run ends while the lookup runs
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources,
                          repo="https://r.test/m2", resolved_by="pom-properties")

    out = tmp_path / "out"
    try:
        run(Settings(input=input_dir, output=out), runner=perfect_engine, resolver=late_resolver)
    finally:
        engines.PROCESSES.reset()
    assert not list(out.rglob("*.java"))


def test_scheduler_wait_has_a_timeout(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """#112: on Windows, a wait without a timeout sees Ctrl-C only when a future completes."""
    import decaf.pipeline as pipeline

    timeouts: list = []
    real_wait = pipeline.wait

    def wait(fs, timeout=None, return_when="ALL_COMPLETED"):
        timeouts.append(timeout)
        return real_wait(fs, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(pipeline, "wait", wait)
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), runner=perfect_engine)
    assert timeouts and None not in timeouts


def test_run_sends_no_event_before_a_preflight_error(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """An exit-2 error leaves the stdout of --format ndjson empty (#109)."""
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 16))  # Vineflower needs Java 17
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    seen: list = []
    with pytest.raises(DecafError, match="needs Java 17"):
        run(
            Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, memory_mb=4096),
            runner=perfect_engine, on_found=seen.append, on_warn=seen.append,
            on_event=lambda k, s, d: seen.append(k) if k != "engines" else None,
        )
    assert seen == []


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
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, mask: None, raising=False)
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=perfect_engine,
    )
    assert report.settings["cpus"] == 15  # auto budget = cores - 1
    assert report.settings["jobs"] == 4
    assert report.settings["cpu_budget"] == 3


def test_run_default_cpu_budget_counts_the_cpus_of_the_affinity(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """#114: under taskset or a container cpuset, the default budget counted all the cores of the host."""
    calls: list[set] = []
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, mask: calls.append(set(mask)), raising=False)
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=perfect_engine)
    assert (report.settings["cpus"], report.settings["jobs"], report.settings["cpu_budget"]) == (3, 3, 1)
    assert calls[0] == {0, 1, 2}
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    report = run(Settings(input=input_dir, output=tmp_path / "out2", maven=False, cpus=8), runner=perfect_engine)
    assert report.settings["jobs"] == 2  # the default jobs come from the CPUs of the affinity too


def test_a_waiting_whale_gets_the_next_freed_slots(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """#114: while a whale waited for two free slots, each freed slot went to a small jar.

    The whale is in a war, so it is ready only after the small jars hold both slots (jobs=2).
    Each small jar is its own task (one jar per batch). When s0 ends, one slot is free: the
    whale needs two, and no small jar may take the slot. When s1 ends, the whale starts.
    """
    import decaf.pipeline as pipeline

    monkeypatch.setattr(pipeline, "_BATCH_MAX_JARS", 1)
    whale = make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)})
    input_dir = tmp_path / "in"
    make_jar("app.war", {"WEB-INF/lib/whale.jar": whale.read_bytes()}, base=input_dir)
    for i in range(4):
        make_jar(f"s{i}.jar", {f"com/s{i}/A.class": b"x"}, base=input_dir)
    started: list[str] = []
    finished: list[str] = []
    lock = threading.Lock()
    gates = {f"s{i}.jar": threading.Event() for i in range(4)}
    whale_queued = threading.Event()

    def on_event(kind, subject, detail):
        if kind == "queued" and subject.endswith("whale.jar"):
            whale_queued.set()

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        name = Path(target).name
        with lock:
            started.append(name)
        if name in gates:
            gates[name].wait(timeout=30)
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        with lock:
            finished.append(name)
        return res

    def wait_for(test) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with lock:
                if test():
                    return
            time.sleep(0.01)
        raise AssertionError(f"timed out: started={started} finished={finished}")

    result: dict = {}

    def drive():
        result["report"] = run(
            Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=2, cpus=2),
            runner=engine, batch_runner=merged_batch_engine, on_event=on_event,
        )

    t = threading.Thread(target=drive)
    t.start()
    try:
        wait_for(lambda: len(started) == 2)  # two small jars hold both slots
        assert whale_queued.wait(timeout=30)
        first = started[0]
        gates[first].set()
        wait_for(lambda: first in finished)
        time.sleep(1.5)  # three ticks of the scheduler: no small jar may take the freed slot
        with lock:
            assert len(started) == 2, started
        gates[started[1]].set()
        wait_for(lambda: "whale.jar" in started)
        with lock:
            assert started[2] == "whale.jar", started
    finally:
        for gate in gates.values():
            gate.set()
        t.join(timeout=60)
    assert result["report"].totals["ok"] == 5  # the whale and the four small jars; the war has no files of its own


def test_a_small_jar_takes_the_free_slot_while_a_whale_waits_for_a_whale(fake_env, make_jar, tmp_path: Path, monkeypatch):
    """Review S1: with jobs=3, a whale that waits for a running whale cannot use the third slot,
    so the small jars may use it. The hold of #133 left that slot empty."""
    import decaf.pipeline as pipeline

    input_dir = tmp_path / "in"
    for name in ("w0.jar", "w1.jar"):
        make_jar(name, {f"com/{name[:2]}/C{i}.class": b"x" for i in range(3000)}, base=input_dir)
    smalls = {f"WEB-INF/lib/s{i}.jar": make_jar(f"s{i}.jar", {f"com/s{i}/A.class": b"x"}).read_bytes()
              for i in range(2)}
    make_jar("app.war", smalls, base=input_dir)
    lock = threading.Lock()
    started: list[str] = []
    queued: set[str] = set()
    whale_gate = threading.Event()

    def on_event(kind, subject, detail):
        if kind == "queued":
            with lock:
                queued.add(subject)

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        name = Path(target).name
        with lock:
            started.append(name)
        if name.startswith("w"):
            whale_gate.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        with lock:
            started.extend(Path(t).name for t in targets)
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    real_fetch = pipeline._fetch_stage

    def fetch(a, ctx):
        if a.rel == "app.war":  # the small jars come after one whale runs and the other waits
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with lock:
                    if {"w0.jar", "w1.jar"} <= queued and any(n.startswith("w") for n in started):
                        break
                time.sleep(0.01)
        return real_fetch(a, ctx)

    monkeypatch.setattr(pipeline, "_fetch_stage", fetch)
    result: dict = {}

    def drive():
        result["report"] = run(
            Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=3, cpus=3),
            runner=engine, batch_runner=batch, on_event=on_event,
        )

    t = threading.Thread(target=drive)
    t.start()
    try:
        deadline = time.monotonic() + 10
        small_started = False
        while time.monotonic() < deadline and not small_started:
            with lock:
                small_started = any(n.startswith("s") for n in started)
            time.sleep(0.01)
        with lock:
            assert small_started, started  # while the first whale still runs
            assert sum(n.startswith("w") for n in started) == 1, started
    finally:
        whale_gate.set()
        t.join(timeout=60)
    outcomes = {r.rel: r.outcome for r in result["report"].artifacts}
    assert all(outcomes[rel] == "ok" for rel in ("w0.jar", "w1.jar", "app.war!/WEB-INF/lib/s0.jar",
                                                   "app.war!/WEB-INF/lib/s1.jar")), outcomes


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
        ("fetch", "a.jar", "scanning"),
        ("fetch", "a.jar", "copying resources"),
        ("queued", "a.jar", ""),
        ("decompile", "a.jar", "vineflower · 1 classes"),
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
    assert [(e[0], e[2]) for e in events if e[1] == "lib-1.2.jar"] == [
        ("fetch", "scanning"),
        ("fetch", "copying resources"),
        ("fetch", "resolving"),
        ("fetch", "extracting"),
    ]
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
    assert [e[2] for e in events if e[0] == "decompile"] == [
        "vineflower · 1 classes",
        "cfr · 1 classes",
    ]


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
    decompile_details: list[tuple[str, str]] = []

    def on_event(kind, subject, detail):
        if kind == "decompile":
            decompile_details.append((subject, detail))
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
    assert ("big.jar!/lib/s0.jar", "vineflower · batch of 2 · 1 classes") in decompile_details
    assert ("big.jar!/lib/s1.jar", "vineflower · batch of 2 · 1 classes") in decompile_details
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


def test_run_metadata_claimants_never_share_a_batch(fake_env, make_jar, tmp_path):
    """#75: two modular smalls both claim module-info, so they must not share a
    batch — each keeps its module-info.java exactly as a solo run would. Same
    gated-big structure as the formation test above."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x", "module-info.class": b"m"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x", "module-info.class": b"m"})
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
    assert batches == []  # claim overlap defers; a 1-member batch submits solo
    rels = {r.rel: r for r in report.artifacts}
    for i in range(2):
        assert rels[f"big.jar!/lib/s{i}.jar"].attempts[0].level == "archive"
    assert (tmp_path / "out/src/module-info.java").is_file()
    assert (tmp_path / "out/src/com/s0/A.java").is_file()
    assert (tmp_path / "out/src/com/s1/A.java").is_file()


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
    run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, check=False))
    assert seen["cds"] == tmp_path / "cache" / "engines"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="a read-only folder needs POSIX modes and no root")
def test_run_turns_cds_off_when_the_engine_cache_is_read_only(monkeypatch, make_jar, tmp_path):
    """#113: a JVM that cannot write its CDS archive exits with code 1 after its work, so each batch ran two times."""
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
    cache = tmp_path / "cache/engines"
    cache.mkdir(parents=True)
    cache.chmod(0o555)
    try:
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, check=False))
    finally:
        cache.chmod(0o755)
    assert seen["cds"] is None


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
    run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, check=False))
    assert seen["cds"] is None


def test_run_beyond_depth_blob_copied_in_mirror(fake_env, make_jar, tmp_path: Path):
    input_dir = make_deep_inputs(make_jar, tmp_path)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    deep = {r.rel: r for r in report.artifacts}["app.war!/WEB-INF/lib/dep.jar!/lib/inner.jar"]
    assert deep.outcome == "skipped"
    assert deep.resources_copied == 1
    blob = out / "app.war/WEB-INF/lib/dep.jar/lib/inner.jar"
    assert blob.is_file()
    with zipfile.ZipFile(blob) as zf:
        assert "com/i/I.class" in zf.namelist()


def test_run_beyond_depth_merge_counts_skipped(fake_env, make_jar, tmp_path: Path):
    input_dir = make_deep_inputs(make_jar, tmp_path)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False),
        runner=perfect_engine,
    )
    deep = {r.rel: r for r in report.artifacts}["app.war!/WEB-INF/lib/dep.jar!/lib/inner.jar"]
    assert deep.resources_copied == 0
    assert deep.resources_skipped == 1


def test_run_no_resource_settings_respected(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("app.jar", {"com/x/A.class": b"x", "cfg.properties": "k=v"}, base=input_dir)
    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, resources=False),
        runner=perfect_engine,
    )
    assert (out / "app.jar/com/x/A.java").is_file()
    assert not (out / "app.jar/cfg.properties").exists()
    rep = report.artifacts[0]
    assert rep.resources_copied == 0
    assert rep.resources_skipped == 1
    assert report.settings["resources"] is False


def test_run_binds_verdict_cache(fake_env, make_jar, tmp_path: Path, monkeypatch):
    from decaf.verdicts import VerdictCache

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "cachedir")
    captured = {}

    def fake_resolve(jar_path, repos, client, cache_dir, *, net=None, verdicts=None):
        captured["verdicts"] = verdicts
        return Resolution(miss="stub")

    monkeypatch.setattr("decaf.maven.resolve_sources", fake_resolve)
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"A.class": b"x"}, base=input_dir)

    report = run(Settings(input=input_dir, output=tmp_path / "out"), runner=perfect_engine)
    v = captured["verdicts"]
    assert isinstance(v, VerdictCache)
    assert v.root == tmp_path / "cachedir" / "verdicts"
    assert v.fresh is False
    assert report.settings["fresh_maven"] is False

    report2 = run(
        Settings(input=input_dir, output=tmp_path / "out2", fresh_maven=True),
        runner=perfect_engine,
    )
    assert captured["verdicts"].fresh is True
    assert report2.settings["fresh_maven"] is True


def test_run_sources_jar_emits_extract_then_copy(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("lib-sources.jar", {"com/x/A.java": "class A {}"}, base=input_dir)
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
    )
    assert [e[2] for e in events if e[1] == "lib-sources.jar" and e[0] == "fetch"] == [
        "scanning",  # nested archives of a sources jar (#110)
        "extracting",
        "copying resources",
    ]


def test_run_stray_java_entry_keeps_the_jars_of_a_war(fake_env, make_jar, tmp_path: Path):
    """#110: a war with source entries and no class entries is a sources jar; its jars still count."""
    dep = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar("app.war", {"WEB-INF/src/Note.java": "class Note {}", "WEB-INF/lib/dep.jar": dep.read_bytes()},
             base=tmp_path / "in")
    found: list[int] = []
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
                 runner=perfect_engine, on_found=found.append)
    assert {a.rel: (a.outcome, a.method) for a in report.artifacts} == {
        "app.war": ("ok", "extracted"),
        "app.war!/WEB-INF/lib/dep.jar": ("ok", "vineflower"),
    }
    assert found == [2]  # the scan counted the nested jar


def test_run_resource_only_emits_scan_then_copy(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("res.jar", {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"}, base=input_dir)
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
    )
    assert [e[2] for e in events if e[1] == "res.jar" and e[0] == "fetch"] == [
        "scanning",
        "copying resources",
    ]


def test_run_beyond_depth_and_class_tree_events(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar("app.war", {"WEB-INF/lib/dep.jar": inner.read_bytes()}, base=input_dir)
    (input_dir / "com").mkdir(parents=True)
    (input_dir / "com" / "L.class").write_bytes(b"x")
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, max_depth=0),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
    )
    beyond = "app.war!/WEB-INF/lib/dep.jar"
    assert [e[2] for e in events if e[1] == beyond and e[0] == "fetch"] == ["copying resources"]
    assert [e[2] for e in events if e[1] == "_classes" and e[0] == "fetch"] == ["copying classes"]


def test_run_maven_miss_emits_resolving_without_extracting(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out"),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
        resolver=lambda *a, **kw: Resolution(miss="no pom.properties; 0 candidates"),
    )
    assert [e[2] for e in events if e[1] == "a.jar" and e[0] == "fetch"] == [
        "scanning",
        "copying resources",
        "resolving",
    ]


def test_run_corrupt_artifact_emits_no_fetch_events(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "bad.jar").write_bytes(b"not a zip")
    events: list[tuple[str, str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
    )
    assert [e for e in events if e[1] == "bad.jar"] == []
    assert report.totals["failed"] == 1


def test_run_retry_event_reports_missing_count(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x", "com/x/B.class": b"y"}, base=input_dir)

    def half_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name == "vineflower":
            dest = Path(dest)
            out = dest / "com/x/A.java"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("class A {}")
            return EngineResult(spec.name, 0, False, 1, "")
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=half_engine,
    )
    assert [e[2] for e in events if e[0] == "decompile"] == [
        "vineflower · 2 classes",
        "cfr · retry 1 classes",
    ]


def test_run_without_on_event_never_passes_on_state(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)

    def strict_resolver(jar_path, repos, client, cache_dir):  # no **kw: must not get on_state
        return Resolution(miss="no pom.properties; sha1 skipped")

    report = run(
        Settings(input=input_dir, output=tmp_path / "out"),
        runner=perfect_engine,
        resolver=strict_resolver,
    )
    assert report.totals["ok"] == 1


def test_run_binds_on_state_into_resolver(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)

    def downloading_resolver(jar_path, repos, client, cache_dir, **kw):
        assert "on_state" in kw
        kw["on_state"]("downloading")
        return Resolution(miss="no pom.properties; 0 candidates")

    events: list[tuple[str, str, str]] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out"),
        on_event=lambda k, s, d: events.append((k, s, d)),
        runner=perfect_engine,
        resolver=downloading_resolver,
    )
    assert [e[2] for e in events if e[1] == "lib-1.2.jar" and e[0] == "fetch"] == [
        "scanning",
        "copying resources",
        "resolving",
        "downloading",
    ]


def test_run_binds_subject_into_resolver(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar(
        "app.jar",
        {"com/a/A.class": b"a", "lib/dep.jar": inner.read_bytes()},
        base=input_dir,
    )

    subjects: list[str] = []

    def recording_resolver(jar_path, repos, client, cache_dir, **kw):
        subjects.append(kw["subject"])
        return Resolution(miss="no pom.properties; 0 candidates")

    run(
        Settings(input=input_dir, output=tmp_path / "out"),
        on_event=lambda k, s, d: None,
        runner=perfect_engine,
        resolver=recording_resolver,
    )
    assert sorted(subjects) == ["app.jar", "app.jar!/lib/dep.jar"]


@pytest.mark.parametrize("engine", ["cfr", "procyon"])
def test_run_batches_with_new_primary_engines(fake_env, make_jar, tmp_path, engine):
    """chain[0] in {cfr, procyon} passes batch eligibility now (#74)."""
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
        assert spec.name == engine
        batches.append(sorted(Path(t).name for t in targets))
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False,
                 jobs=1, cpus=1, engine=engine),
        runner=gated_solo,
        batch_runner=recording_batch,
        on_event=on_event,
    )
    assert report.totals["ok"] == 3
    assert batches == [["s0.jar", "s1.jar"]]
    rels = {r.rel: r for r in report.artifacts}
    for i in range(2):
        assert rels[f"big.jar!/lib/s{i}.jar"].attempts[0].level == "batch"
        assert rels[f"big.jar!/lib/s{i}.jar"].method == engine


def test_run_batch_eligibility_boundary_at_whale_threshold(fake_env, make_jar, tmp_path):
    """2,999 classes batches; 3,000 is a whale and never enters a batch (#74).

    Two-stage gate against the in-flight admission cap of 3 (jobs + 2*jobs at
    jobs=1): big.jar's solo waits until whale and edge are queued (they fit
    alongside it under the cap), then frees the slot that lets buddy through
    the admission gate; whale's solo — dispatched next off `ready` at weight
    2 — waits until buddy is queued. Every wait is releasable and every
    ordering is Event-enforced, so edge and buddy are always in ready_small
    together when the weight frees, and batch together.
    """
    edge = make_jar("edge.jar", {f"com/e/C{i}.class": b"x" for i in range(2999)})
    whale = make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)})
    buddy = make_jar("buddy.jar", {"com/b/A.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar(
        "big.jar",
        {
            **{f"com/big/C{i}.class": b"x" for i in range(900)},
            "lib/edge.jar": edge.read_bytes(),
            "lib/whale.jar": whale.read_bytes(),
            "lib/buddy.jar": buddy.read_bytes(),
        },
        base=input_dir,
    )
    batches: list[list[str]] = []
    three_queued = threading.Event()
    all_queued = threading.Event()
    queued_count = [0]

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued_count[0] += 1
            if queued_count[0] == 3:
                three_queued.set()
            if queued_count[0] == 4:
                all_queued.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            three_queued.wait(timeout=30)
        if Path(target).name == "whale.jar":
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
    assert report.totals["ok"] == 4
    assert batches == [["buddy.jar", "edge.jar"]]
    rels = {r.rel: r for r in report.artifacts}
    assert rels["big.jar!/lib/edge.jar"].attempts[0].level == "batch"
    assert rels["big.jar!/lib/whale.jar"].attempts[0].level == "archive"


def test_run_engine_args_require_no_fallback(tmp_path):
    """engine_args are primary-engine-only by contract: fallback must be off (#70)."""
    (tmp_path / "in").mkdir()
    with pytest.raises(DecafError, match="fallback"):
        run(Settings(input=tmp_path / "in", output=tmp_path / "out", engine_args=("-dgs=1",)))


def test_run_engine_args_reach_solo_runner(fake_env, make_jar, tmp_path):
    """The decompile call site forwards Settings.engine_args to the runner and
    the report settings echo them. (The missing-class retry site never runs
    with engine_args: retries go to LATER chain engines, and the no-fallback
    contract makes the chain a single engine.)"""
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    calls = []

    def recording(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, engine_args=None):
        calls.append(engine_args)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, mirror=False,
                 fallback=False, engine_args=("-dgs=1",)),
        runner=recording,
    )
    assert report.totals["ok"] == 1
    assert calls == [("-dgs=1",)]
    assert report.settings["engine_args"] == ["-dgs=1"]


def test_run_engine_args_reach_batch_runner(fake_env, make_jar, tmp_path):
    """The batch_runner call site forwards engine_args too — batch ≡ solo (#70)."""
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

    seen = {}

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, engine_args=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)
        seen.setdefault("solo", engine_args)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def recording_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None, engine_args=None):
        seen["batch"] = engine_args
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False,
                 jobs=1, cpus=1, fallback=False, engine_args=("-dgs=1",)),
        runner=gated_solo,
        batch_runner=recording_batch,
        on_event=on_event,
    )
    assert report.totals["ok"] == 3
    assert seen["batch"] == ("-dgs=1",)
    assert seen["solo"] == ("-dgs=1",)


def test_report_schema_v1_fields(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    raw = (out / "decaf-report.json").read_text()
    on_disk = json.loads(raw)
    assert on_disk["schema_version"] == 1
    assert on_disk["decaf_version"] == report.decaf_version != ""
    assert on_disk["status"] == "completed"
    import re as _re
    ts = _re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    assert ts.match(on_disk["started_at"]) and ts.match(on_disk["ended_at"])
    assert not list(out.glob("*.tmp"))  # atomic write leaves no droppings
    assert raw.endswith("}\n")  # trailing newline: byte-identical to stdout's print(to_json())


def test_report_status_reflects_failures(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"x"}, base=input_dir)
    def failing_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, **kw):
        return EngineResult(spec.name, 1, False, 0, "boom")
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=failing_engine)
    assert report.status == "completed_with_failures"


def test_run_case_insensitive_merge_records_cross_jar_clash(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    make_jar("x.jar", {"p/A.class": b"A"}, base=input_dir)
    make_jar("y.jar", {"p/a.class": b"a"}, base=input_dir)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False, mirror=False), runner=perfect_engine)
    assert report.totals["failed"] == 0
    (clash,) = [c for a in report.artifacts for c in a.collisions]
    assert clash == {"path": "p/A.java", "kept": "x.jar", "dropped": "y.jar", "dropped_path": "p/a.java"}
    assert sorted(q.name for q in (out / "src/p").iterdir()) == ["A.java"]
    assert report.totals["collisions"] == 1


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
def test_run_unwritable_output_is_a_clean_error(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("x.jar", {"p/A.class": b"A"}, base=input_dir)
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o500)
    try:
        with pytest.raises(DecafError, match=f"cannot write to {re.escape(str(out))}: "):
            run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    finally:
        out.chmod(0o700)


def _mixed_jar(make_jar, input_dir: Path, name: str = "mixed.jar") -> Path:
    return make_jar(
        name,
        {"p/a.class": b"a", "p/A.class": b"A", "p/A$Inner.class": b"i", "p/B.class": b"B"},
        base=input_dir,
    )


def test_case_failure_text():
    groups = [["p/A", "p/a"], ["q/B", "q/b", "q/bB"]]
    assert _case_failure(groups, "classes", "output directory") == (
        "case collision: 5 classes differ only by case (p/A vs p/a, ...); case-insensitive output directory"
    )
    assert _case_failure([["p/A.java", "p/a.java"]], "source files", "temp directory") == (
        "case collision: 2 source files differ only by case (p/A.java vs p/a.java); case-insensitive temp directory"
    )


def test_run_refuses_case_colliding_archive_on_case_insensitive_tree(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    _mixed_jar(make_jar, input_dir)
    make_jar("clean.jar", {"q/C.class": b"c"}, base=input_dir)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    by_rel = {a.rel: a for a in report.artifacts}
    mixed, clean = by_rel["mixed.jar"], by_rel["clean.jar"]
    assert mixed.outcome == "failed"
    assert mixed.failure == (
        "case collision: 2 classes differ only by case (p/A vs p/a); "
        "case-insensitive output and temp directories"
    )
    assert mixed.attempts == [] and mixed.java_files == 0 and mixed.classes == 0
    assert not (out / "mixed.jar").exists()
    assert clean.outcome == "ok" and (out / "clean.jar/q/C.java").is_file()
    assert report.totals["failed"] == 1


def test_run_refuses_case_colliding_sources_jar(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    make_jar(
        "lib-sources.jar",
        {"p/a.java": "class a {}", "p/A.java": "class A {}", "p/B.kt": "fun b() {}"},
        base=input_dir,
    )
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    (art,) = report.artifacts
    assert art.outcome == "failed"
    assert art.failure == (
        "case collision: 2 source files differ only by case (p/A.java vs p/a.java); "
        "case-insensitive output and temp directories"
    )
    assert art.java_files == 0 and not (out / "lib-sources.jar").exists()


def test_run_refuses_case_colliding_class_tree(fake_env, tmp_path: Path, monkeypatch):
    if casefs.is_case_insensitive(tmp_path):  # real probe, before the monkeypatch
        pytest.skip("input tree cannot hold two names that differ only by case")
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    (input_dir / "p").mkdir(parents=True)
    for name in ("a", "A", "B"):
        (input_dir / "p" / f"{name}.class").write_bytes(name.encode())
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    (art,) = report.artifacts
    assert art.rel == "_classes" and art.outcome == "failed"
    assert art.failure.startswith("case collision: 2 classes differ only by case (p/A vs p/a)")
    assert art.attempts == []


@pytest.mark.parametrize(
    "entries, clash",
    [
        ({"WEB-INF/classes/p/a.class": b"a", "WEB-INF/classes/p/A.class": b"A"}, "2 classes differ only by case (p/A vs p/a)"),
        # Review A3: a war with source entries and no class entries is a sources jar.
        ({"x/a.java": "class a {}", "x/A.java": "class A {}"}, "2 source files differ only by case (x/A.java vs x/a.java)"),
    ],
    ids=["classes", "sources"],
)
def test_run_refused_war_still_processes_nested_jars(fake_env, make_jar, tmp_path: Path, monkeypatch, entries, clash):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar("app.war", {**entries, "WEB-INF/lib/dep.jar": inner.read_bytes()}, base=input_dir)
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    by_rel = {a.rel: a for a in report.artifacts}
    assert by_rel["app.war"].outcome == "failed"
    assert by_rel["app.war"].failure.startswith(f"case collision: {clash}")
    assert by_rel["app.war!/WEB-INF/lib/dep.jar"].outcome == "ok"
    assert (out / "app.war/WEB-INF/lib/dep.jar/com/d/D.java").is_file()


def test_run_case_sensitive_tree_leaves_mixed_jar_alone(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: False)
    input_dir = tmp_path / "in"
    _mixed_jar(make_jar, input_dir)
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=perfect_engine)
    (art,) = report.artifacts
    assert art.outcome == "ok" and art.failure is None
    assert art.attempts and art.attempts[0].engine == "vineflower"


def test_run_case_insensitive_tree_never_batches_case_clashing_jars(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    make_jar("x.jar", {"p/A.class": b"A"}, base=input_dir)
    make_jar("y.jar", {"p/a.class": b"a"}, base=input_dir)
    make_jar("z.jar", {"q/Z.class": b"z"}, base=input_dir)
    batches: list[list[str]] = []

    def recording_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None, **kw):
        batches.append(sorted(Path(t).name for t in targets))
        total = 0
        for t in targets:
            total += perfect_engine(spec, jar_path, t, dest, timeout, java=java, cpu_budget=cpu_budget).java_files
        return EngineResult(spec.name, 0, False, total, "")

    out = tmp_path / "out"
    report = run(
        Settings(input=input_dir, output=out, maven=False, mirror=False),
        runner=perfect_engine, batch_runner=recording_batch,
    )
    assert report.totals["failed"] == 0
    assert all(not {"x.jar", "y.jar"} <= set(b) for b in batches), batches
    (clash,) = [c for a in report.artifacts for c in a.collisions]
    assert clash == {"path": "p/A.java", "kept": "x.jar", "dropped": "y.jar", "dropped_path": "p/a.java"}


def test_run_refuses_nested_archives_that_differ_only_by_case(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(casefs, "is_case_insensitive", lambda d: True)
    input_dir = tmp_path / "in"
    upper = make_jar("u/A.jar", {"p/R.class": b"r"})
    lower = make_jar("l/a.jar", {"com/d/D.class": b"d"})
    make_jar(
        "app.war",
        {
            "WEB-INF/classes/q/Q.class": b"q",
            "WEB-INF/lib/A.jar": upper.read_bytes(),
            "WEB-INF/lib/a.jar": lower.read_bytes(),
            "WEB-INF/lib/dep.jar": lower.read_bytes(),
        },
        base=input_dir,
    )
    out = tmp_path / "out"
    report = run(Settings(input=input_dir, output=out, maven=False), runner=perfect_engine)
    by_rel = {a.rel: a for a in report.artifacts}
    assert by_rel["app.war"].outcome == "ok"
    for rel in ("app.war!/WEB-INF/lib/A.jar", "app.war!/WEB-INF/lib/a.jar"):
        assert by_rel[rel].outcome == "failed"
        assert by_rel[rel].failure == (
            "case collision: 2 nested archives differ only by case "
            "(WEB-INF/lib/A.jar vs WEB-INF/lib/a.jar); case-insensitive output and temp directories"
        )
        assert by_rel[rel].attempts == [] and by_rel[rel].kind == "archive"
    assert by_rel["app.war!/WEB-INF/lib/dep.jar"].outcome == "ok"
    assert (out / "app.war/WEB-INF/lib/dep.jar/com/d/D.java").is_file()
    assert not (out / "app.war/WEB-INF/lib/A.jar").exists()
    assert report.totals["failed"] == 2


def _marker_checker(calls):
    def _check(jobs, *, timeout, cpu_budget):
        calls.append(jobs)
        return [
            [check.FileResult("flagged", kinds=("name",), errors=1, first="1: x")
             if "FLAG" in Path(f).read_text() else check.FileResult("clean") for f in job.files]
            for job in jobs
        ]
    return _check


def _flag_then_clean_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
    out = Path(dest) / "com/x/A.java"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("// FLAG" if spec.name == "vineflower" else "class A {}")
    return EngineResult(spec.name, 0, False, 1, "")


@pytest.mark.parametrize("check_on", [False, True])
def test_the_package_roots_of_a_class_folder_are_read_once_and_only_for_the_check(fake_env, tmp_path: Path,
                                                                                  monkeypatch, check_on):
    """Review C10: decaf read each class file of a class folder for its package roots, also with --no-check,
    and again in the check of the copy."""
    import decaf.pipeline as pipeline
    from test_check import _class_file

    input_dir = tmp_path / "in"
    (input_dir / "build/p").mkdir(parents=True)
    (input_dir / "build/p/A.class").write_bytes(_class_file("p/A"))
    calls: list = []
    checks: list = []
    real = pipeline._tree_roots
    monkeypatch.setattr(pipeline, "_tree_roots", lambda source: calls.append(source) or real(source))
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False, check=check_on),
                 runner=perfect_engine, checker=_marker_checker(checks))
    assert report.totals["ok"] == 1
    assert len(calls) == check_on
    if check_on:
        (job,) = checks[0]
        assert job.classpath[0].name == "build" and job.classpath[0] != input_dir / "build"  # the root of the copy
        assert input_dir / "build" in job.classpath  # the input group: the package root of the folder


def test_run_injected_checker_reports_check_and_settings(fake_env, make_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    make_jar("app.jar", {"com/x/A.class": b"a"}, base=input_dir)
    calls: list = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=_flag_then_clean_engine, checker=_marker_checker(calls),
    )
    art = report.artifacts[0]
    assert art.flagged == [] and art.check["checked"] == 1
    assert [(a.engine, a.flagged) for a in art.attempts] == [("vineflower", 1), ("cfr", 0)]
    assert report.totals["checked_files"] == 1
    assert report.settings["check"] is True and report.settings["javac"] is None
    assert calls[0][0].classpath == (input_dir / "app.jar",)  # own archive; the input group adds nothing new


def test_run_no_check_setting_ignores_injected_checker(fake_env, make_jar, tmp_path: Path):
    make_jar("app.jar", {"com/x/A.class": b"a"}, base=tmp_path / "in")
    calls: list = []
    report = run(
        Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False, check=False),
        runner=_flag_then_clean_engine, checker=_marker_checker(calls),
    )
    assert calls == [] and report.artifacts[0].check is None
    assert report.settings["check"] is False


def test_run_injected_runner_without_checker_has_no_check(fake_env, make_jar, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(check, "find_javac", lambda java: pytest.fail("must not look for javac"))
    make_jar("app.jar", {"com/x/A.class": b"a"}, base=tmp_path / "in")
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), runner=perfect_engine)
    assert report.artifacts[0].check is None


def _default_engines(monkeypatch):
    monkeypatch.setattr(
        engines, "run_engine",
        lambda spec, jar, target, dest, timeout, **kw: perfect_engine(spec, jar, target, dest, timeout),
    )
    monkeypatch.setattr(
        engines, "run_engine_batch",
        lambda *a, **kw: pytest.fail("one artifact never batches"),
    )


def test_run_without_javac_warns_once_and_skips_check(fake_env, make_jar, tmp_path: Path, monkeypatch):
    _default_engines(monkeypatch)
    monkeypatch.setattr(check, "find_javac", lambda java: None)
    make_jar("app.jar", {"com/x/A.class": b"a"}, base=tmp_path / "in")
    warns: list[str] = []
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), on_warn=warns.append)
    assert warns == ["compile check skipped: no javac found for java"]
    assert report.artifacts[0].check is None and report.settings["javac"] is None


def test_run_install_failure_warns_and_skips_check(fake_env, make_jar, tmp_path: Path, monkeypatch):
    _default_engines(monkeypatch)
    monkeypatch.setattr(check, "find_javac", lambda java: Path("/jdk/bin/javac"))

    def broken_install(javac, major):
        raise check.CheckError("checker compile failed: boom")

    monkeypatch.setattr(check, "install", broken_install)
    make_jar("app.jar", {"com/x/A.class": b"a"}, base=tmp_path / "in")
    warns: list[str] = []
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), on_warn=warns.append)
    assert warns == ["compile check skipped: checker compile failed: boom"]
    assert report.artifacts[0].check is None


def test_run_installs_and_binds_the_real_checker(fake_env, make_jar, tmp_path: Path, monkeypatch):
    _default_engines(monkeypatch)
    monkeypatch.setattr(check, "find_javac", lambda java: Path("/jdk/bin/javac"))
    monkeypatch.setattr(check, "install", lambda javac, major: tmp_path / "inst")
    seen = {}

    def fake_run_check(jobs, *, timeout, cpu_budget, java, install, tmp_root, cds_dir=None):
        seen.update(java=java, install=install, cpu_budget=cpu_budget)
        return [[check.FileResult("clean") for _ in job.files] for job in jobs]

    monkeypatch.setattr(check, "run_check", fake_run_check)
    make_jar("app.jar", {"com/x/A.class": b"a"}, base=tmp_path / "in")
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False))
    assert seen["install"] == tmp_path / "inst" and seen["java"] == "java" and seen["cpu_budget"] >= 1
    assert report.settings["javac"] == str(Path("/jdk/bin/javac"))
    assert report.artifacts[0].check["checked"] == 1


STUB_TEXT = {
    "vineflower": (
        "class A {\n   void m() {\n      // $VF: Couldn't be decompiled\n"
        "      // Please report this to the Vineflower issue tracker, at https://github.com/Vineflower/vineflower/issues"
        " with a copy of the class file (if you have the rights to distribute it!)\n"
        "      // java.lang.NullPointerException: boom\n   }\n}\n"
    ),
    "cfr": (
        "class A {\n    void m() {\n        /*\n"
        "         * This method has failed to decompile.  When submitting a bug report, please provide this stack trace,"
        " and (if you hold appropriate legal rights) the relevant class file.\n"
        "         * \n"
        "         * org.benf.cfr.reader.util.ConfusedCFRException: Started 2 blocks at once\n"
        "         */\n"
        '        throw new IllegalStateException("Decompilation failed");\n    }\n}\n'
    ),
    "fernflower": "class A {\n   void m() {\n      // $FF: Couldn't be decompiled\n   }\n}\n",
    "procyon": (
        "class A {\n    void m() {\n        // \n        // This method could not be decompiled.\n"
        "        // \n        // The error that occurred was:\n        // \n"
        "        // java.lang.IllegalStateException: Could not infer any expression.\n"
        '        throw new IllegalStateException("An error occurred while decompiling this method.");\n    }\n}\n'
    ),
    "jd": "class A {\n  void m() {\n    // Byte code:\n    //   0: return\n  }\n}\n",
}


def stub_engine(stub: dict[str, set[str]], suffix: dict[str, str] | None = None):
    """Fake engines like perfect_engine; engine NAME writes STUB_TEXT[NAME] for the class stems in stub[NAME]."""

    def run_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        dest, target = Path(dest), Path(target)
        if target.is_dir():
            entries = [p.relative_to(target).as_posix() for p in target.rglob("*.class")]
        else:
            with zipfile.ZipFile(target) as zf:
                entries = [n for n in zf.namelist() if n.endswith(".class")]
        n = 0
        for entry in entries:
            if "$" in entry.rsplit("/", 1)[-1]:
                continue
            stem = entry[: -len(".class")]
            out = dest / (stem + (suffix or {}).get(spec.name, ".java"))
            out.parent.mkdir(parents=True, exist_ok=True)
            if stem in stub.get(spec.name, set()):
                out.write_text(STUB_TEXT[spec.name])
            else:
                out.write_text(f"// {spec.name}\nclass X {{}}\n")
            n += 1
        return EngineResult(spec.name, 0, False, n, "")

    return run_engine


def test_run_retries_a_stubbed_class_with_the_next_engine(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)
    events: list[tuple[str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=stub_engine({"vineflower": {"com/x/A"}}),
        on_event=lambda k, s, d: events.append((k, d)),
    )
    r = report.artifacts[0]
    assert r.outcome == "ok" and r.method == "vineflower"
    assert r.stubs == [] and not r.partial
    assert [(a.engine, a.level, a.stubbed) for a in r.attempts] == [
        ("vineflower", "archive", 1),
        ("cfr", "class", 0),
    ]
    out = tmp_path / "out/a.jar/com/x"
    assert (out / "A.java").read_text() == "// cfr\nclass X {}\n"
    assert (out / "B.java").read_text() == "// vineflower\nclass X {}\n"
    assert r.java_files == 2
    assert ("decompile", "cfr · retry 1 classes") in events
    assert report.totals["stubbed_classes"] == 0


def test_run_keeps_the_first_engine_file_when_every_engine_stubs(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)
    every = {name: {"com/x/A"} for name in ("vineflower", "cfr", "fernflower", "procyon", "jd")}
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=stub_engine(every),
    )
    r = report.artifacts[0]
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == STUB_TEXT["vineflower"]
    assert r.stubs == [{
        "path": "com/x/A.java", "engine": "vineflower", "methods": 1,
        "errors": ["java.lang.NullPointerException"],
    }]
    assert r.outcome == "ok" and r.partial and r.missing_classes == 0
    assert [(a.engine, a.stubbed) for a in r.attempts] == [
        ("vineflower", 1), ("cfr", 1), ("fernflower", 1), ("procyon", 1), ("jd", 1),
    ]
    assert r.java_files == 2
    assert report.totals["stubbed_classes"] == 1 and report.totals["partial"] == 1
    assert report.status == "completed"


@pytest.mark.parametrize("mirror", [True, False])
def test_run_clean_java_retry_replaces_a_stubbed_kotlin_file(fake_env, make_jar, tmp_path, mirror):
    input_dir = tmp_path / "in"
    make_jar("k.jar", {"okio/Buffer.class": b"k", "okio/Okio.class": b"k"}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=mirror),
        runner=stub_engine({"vineflower": {"okio/Buffer"}}, suffix={"vineflower": ".kt"}),
    )
    r = report.artifacts[0]
    root = tmp_path / ("out/k.jar" if mirror else "out/src")
    written = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert written == ["okio/Buffer.java", "okio/Okio.kt"]
    assert r.stubs == [] and r.collisions == []


def test_run_no_fallback_reports_the_stub_without_a_retry(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, fallback=False),
        runner=stub_engine({"vineflower": {"com/x/A"}}),
    )
    r = report.artifacts[0]
    assert [(a.engine, a.level, a.stubbed) for a in r.attempts] == [("vineflower", "archive", 1)]
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["path"] for s in r.stubs] == ["com/x/A.java"] and r.partial


def test_run_retries_missing_and_stubbed_classes_together(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar(
        "a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b", "com/x/C.class": b"c"}, base=input_dir
    )
    base = stub_engine({"vineflower": {"com/x/A"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = base(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower":
            (Path(dest) / "com/x/B.java").unlink()  # B goes missing
            res.java_files -= 1
        return res

    events: list[tuple[str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=engine,
        on_event=lambda k, s, d: events.append((k, d)),
    )
    r = report.artifacts[0]
    assert ("decompile", "cfr · retry 2 classes") in events
    assert r.missing_classes == 0 and r.stubs == [] and not r.partial
    out = tmp_path / "out/a.jar/com/x"
    assert {p.name: p.read_text().splitlines()[0] for p in out.iterdir()} == {
        "A.java": "// cfr", "B.java": "// cfr", "C.java": "// vineflower",
    }


def test_run_without_stubs_reports_none(fake_env, make_jar, tmp_path):
    input_dir = make_inputs(make_jar, tmp_path)
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=perfect_engine)
    assert all(r.stubs == [] for r in report.artifacts)
    assert all(a.stubbed == 0 for r in report.artifacts for a in r.attempts)
    assert report.totals["stubbed_classes"] == 0


def test_run_stub_under_a_container_root_is_released_by_a_package_path_retry(fake_env, make_jar, tmp_path):
    """Vineflower keeps WEB-INF/classes/; real CFR writes the package path. One stem, no leftover stub."""
    input_dir = tmp_path / "in"
    make_jar(
        "app.war",
        {"WEB-INF/classes/com/w/W.class": b"w", "WEB-INF/classes/com/w/V.class": b"v"},
        base=input_dir,
    )
    base = stub_engine({"vineflower": {"WEB-INF/classes/com/w/W"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name != "cfr":
            return base(spec, jar_path, target, dest, timeout, java=java)
        out = Path(dest) / "com/w/W.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("// cfr\nclass W {}\n")
        return EngineResult(spec.name, 0, False, 1, "")

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    out = tmp_path / "out/app.war"
    assert (out / "com/w/W.java").read_text() == "// cfr\nclass W {}\n"
    assert not (out / "WEB-INF/classes/com/w/W.java").exists()
    assert (out / "WEB-INF/classes/com/w/V.java").is_file()
    assert r.stubs == [] and r.missing_classes == 0


def test_run_interrupt_during_a_retry_still_writes_the_held_file(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    base = stub_engine({"vineflower": {"com/x/A"}, "cfr": {"com/x/A"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = base(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "cfr":
            engines.PROCESSES.kill_all()  # Ctrl-C lands while the retry runs
        return res

    try:
        report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    finally:
        engines.PROCESSES.reset()
    r = report.artifacts[0]
    assert [a.engine for a in r.attempts] == ["vineflower", "cfr"]  # the chain stopped
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["engine"] for s in r.stubs] == ["vineflower"]


def test_run_interrupt_during_a_retry_writes_no_retry_file(fake_env, make_jar, tmp_path):
    """A retry that Ctrl-C stops can leave a cut-off file of a missing class: it does not go out (#112)."""
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        name = "A" if spec.name == "vineflower" else "B"
        (Path(dest) / "com/x").mkdir(parents=True, exist_ok=True)
        (Path(dest) / f"com/x/{name}.java").write_text(f"class {name} {{" + ("}" if name == "A" else ""))
        if spec.name == "cfr":
            engines.PROCESSES.kill_all()  # Ctrl-C lands while the retry runs
        return EngineResult(spec.name, 0, False, 1, "")

    try:
        report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    finally:
        engines.PROCESSES.reset()
    r = report.artifacts[0]
    assert [a.engine for a in r.attempts] == ["vineflower", "cfr"]
    assert (tmp_path / "out/a.jar/com/x/A.java").is_file()
    assert not (tmp_path / "out/a.jar/com/x/B.java").exists()
    assert r.missing_classes == 1


def test_run_interrupt_keeps_the_held_file_over_an_unchecked_retry_file(fake_env, make_jar, tmp_path):
    """A retry that Ctrl-C stops can leave a cut-off file with no stub: it replaces nothing (#112)."""
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    base = stub_engine({"vineflower": {"com/x/A"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = base(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "cfr":
            (Path(dest) / "com/x/A.java").write_text("class A {\n    void m() {")  # cut off by the kill
            engines.PROCESSES.kill_all()  # Ctrl-C lands while the retry runs
        return res

    try:
        report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    finally:
        engines.PROCESSES.reset()
    r = report.artifacts[0]
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["engine"] for s in r.stubs] == ["vineflower"]


def test_run_interrupt_keeps_a_flagged_file_over_an_unchecked_retry_file(fake_env, make_jar, tmp_path):
    """After Ctrl-C the check does not run, so a retry file has no result: the flagged file stays (#112)."""
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = _flag_then_clean_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "cfr":
            engines.PROCESSES.kill_all()  # Ctrl-C lands while the retry runs
        return res

    try:
        report = run(
            Settings(input=input_dir, output=tmp_path / "out", maven=False),
            runner=engine, checker=_marker_checker([]),
        )
    finally:
        engines.PROCESSES.reset()
    r = report.artifacts[0]
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == "// FLAG"
    assert [f["engine"] for f in r.flagged] == ["vineflower"]


def test_run_retry_of_one_copy_of_a_class_keeps_the_other_copy(fake_env, make_jar, tmp_path):
    """A class at the root and in WEB-INF/classes: each copy is its own held file (#107)."""
    input_dir = tmp_path / "in"
    make_jar("app.war", {"com/w/W.class": b"r", "WEB-INF/classes/com/w/W.class": b"c"}, base=input_dir)
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=stub_engine({"vineflower": {"com/w/W"}}),
    )
    r = report.artifacts[0]
    out = tmp_path / "out/app.war"
    assert (out / "com/w/W.java").read_text() == "// cfr\nclass X {}\n"
    assert (out / "WEB-INF/classes/com/w/W.java").read_text() == "// vineflower\nclass X {}\n"
    assert r.stubs == [] and not r.partial


def test_run_lists_both_stubbed_copies_of_a_class(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("app.war", {"com/w/W.class": b"r", "WEB-INF/classes/com/w/W.class": b"c"}, base=input_dir)
    both = {"com/w/W", "WEB-INF/classes/com/w/W"}
    every = {name: both for name in ("vineflower", "cfr", "fernflower", "procyon", "jd")}
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=stub_engine(every))
    r = report.artifacts[0]
    out = tmp_path / "out/app.war"
    assert (out / "com/w/W.java").read_text() == STUB_TEXT["vineflower"]
    assert (out / "WEB-INF/classes/com/w/W.java").read_text() == STUB_TEXT["vineflower"]
    assert sorted(s["path"] for s in r.stubs) == ["WEB-INF/classes/com/w/W.java", "com/w/W.java"]


def test_run_held_copies_in_two_container_roots_are_no_missing_class(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    entries = {"WEB-INF/classes/com/w/W.class": b"w", "BOOT-INF/classes/com/w/W.class": b"b"}
    make_jar("app.war", entries, base=input_dir)
    both = {"WEB-INF/classes/com/w/W", "BOOT-INF/classes/com/w/W"}
    every = {name: both for name in ("vineflower", "cfr", "fernflower", "procyon", "jd")}
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=stub_engine(every))
    r = report.artifacts[0]
    assert r.missing_classes == 0 and len(r.stubs) == 2


def test_run_package_path_retry_file_of_a_copied_class_is_the_root_copy(fake_env, make_jar, tmp_path):
    """CFR writes the package path for both copies: that file cannot replace the WEB-INF copy (#107)."""
    input_dir = tmp_path / "in"
    make_jar("app.war", {"com/w/W.class": b"r", "WEB-INF/classes/com/w/W.class": b"c"}, base=input_dir)
    base = stub_engine({"vineflower": {"WEB-INF/classes/com/w/W"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name == "vineflower":
            return base(spec, jar_path, target, dest, timeout, java=java)
        out = Path(dest) / "com/w/W.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"// {spec.name}\nclass W {{}}\n")
        return EngineResult(spec.name, 0, False, 1, "")

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    out = tmp_path / "out/app.war"
    assert (out / "com/w/W.java").read_text() == "// vineflower\nclass X {}\n"
    assert (out / "WEB-INF/classes/com/w/W.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["path"] for s in r.stubs] == ["WEB-INF/classes/com/w/W.java"]


def _damage_entry(archive: Path, name: str) -> None:
    """Make one deflated entry unreadable: its data starts with an invalid block type."""
    with zipfile.ZipFile(archive) as zf:
        offset = zf.getinfo(name).header_offset
    with open(archive, "r+b") as f:
        f.seek(offset + 26)
        name_len, extra_len = struct.unpack("<HH", f.read(4))
        f.seek(offset + 30 + name_len + extra_len)
        f.write(b"\xff")


def test_run_damaged_class_entry_of_a_war_fails_none_of_its_libraries(fake_env, make_jar, tmp_path):
    """The check reads class files of the war; an unreadable entry costs only the check (#109)."""
    input_dir = tmp_path / "in"
    dep = make_jar("dep.jar", {"com/d/D.class": b"d"})
    war = input_dir / "app.war"
    war.parent.mkdir(parents=True)
    with zipfile.ZipFile(war, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("WEB-INF/classes/com/w/W.class", b"\xca\xfe\xba\xbe" + bytes(200))
        zf.writestr("WEB-INF/lib/dep.jar", dep.read_bytes())
    _damage_entry(war, "WEB-INF/classes/com/w/W.class")
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=perfect_engine, checker=_marker_checker([]),
    )
    outcomes = {a.rel: a.outcome for a in report.artifacts}
    assert outcomes == {"app.war": "ok", "app.war!/WEB-INF/lib/dep.jar": "ok"}


def test_run_damaged_nested_jar_fails_only_that_jar(fake_env, make_jar, make_damaged_jar, tmp_path):
    """#109: a war with one damaged library keeps its other libraries."""
    good = make_jar("good.jar", {"com/g/G.class": b"g"})
    bad = make_jar("bad.jar", {"com/b/B.class": b"b"})
    make_damaged_jar("app.war", {"WEB-INF/classes/com/w/W.class": b"w" * 50,
                                 "WEB-INF/lib/good.jar": good.read_bytes(), "WEB-INF/lib/bad.jar": bad.read_bytes()},
                     damaged=("WEB-INF/lib/bad.jar",), base=tmp_path / "in")
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), runner=perfect_engine)
    outcomes = {a.rel: (a.outcome, a.failure) for a in report.artifacts}
    assert outcomes == {
        "app.war": ("ok", None),
        "app.war!/WEB-INF/lib/bad.jar": ("failed", "unreadable archive"),
        "app.war!/WEB-INF/lib/good.jar": ("ok", None),
    }


def _war_with_two_copies(path: Path, first: bytes, last: bytes) -> Path:
    """A war with two entries WEB-INF/lib/a.jar, as Ant duplicate="add" writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("WEB-INF/classes/com/w/W.class", b"w")
            zf.writestr("WEB-INF/lib/a.jar", first)
            zf.writestr("WEB-INF/lib/a.jar", last)
    return path


def test_run_gives_one_row_for_two_same_entries_of_one_name(fake_env, make_jar, tmp_path):
    """Review low item: two entries of one name gave two equal rows for one file."""
    lib = make_jar("a.jar", {"com/a/A.class": b"a"}).read_bytes()
    _war_with_two_copies(tmp_path / "in/app.war", lib, lib)
    warns: list[str] = []
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
                 runner=perfect_engine, on_warn=warns.append)
    assert [a.rel for a in report.artifacts] == ["app.war", "app.war!/WEB-INF/lib/a.jar"]
    assert report.totals["ok"] == 2
    assert not [w for w in warns if "a.jar" in w]  # the copies are the same: nothing is lost


def test_run_warns_when_two_entries_of_one_name_differ(fake_env, make_jar, tmp_path):
    """Review low item, ask-box ruling "One row + warning": decaf decompiles the last copy,
    as Java reads it, and a warning names the entry when the copies differ."""
    first = make_jar("first/a.jar", {"com/a/A.class": b"a"}).read_bytes()
    last = make_jar("last/a.jar", {"com/b/B.class": b"b"}).read_bytes()
    _war_with_two_copies(tmp_path / "in/app.war", first, last)
    warns: list[str] = []
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False),
                 runner=perfect_engine, on_warn=warns.append)
    assert [a.rel for a in report.artifacts] == ["app.war", "app.war!/WEB-INF/lib/a.jar"]
    assert (tmp_path / "out/app.war/WEB-INF/lib/a.jar/com/b/B.java").is_file()
    assert not (tmp_path / "out/app.war/WEB-INF/lib/a.jar/com/a/A.java").exists()
    assert [w for w in warns if "a.jar" in w] == [
        "app.war!/WEB-INF/lib/a.jar: 2 entries with this name are not the same; decaf uses the last one, as Java does"
    ]


def _damage_first_copy(archive: Path, name: str) -> None:
    """An invalid deflate block at the start of the first entry of the name."""
    with zipfile.ZipFile(archive) as zf:
        offset = next(i.header_offset for i in zf.infolist() if i.filename == name)
    with open(archive, "r+b") as f:
        f.seek(offset + 26)
        name_len, extra_len = struct.unpack("<HH", f.read(4))
        f.seek(offset + 30 + name_len + extra_len)
        f.write(b"\xff")


def test_run_uses_the_last_copy_when_an_earlier_copy_is_damaged(fake_env, make_jar, tmp_path):
    """Review low item: a damaged first copy failed the name, also when the last copy was good."""
    lib = make_jar("a.jar", {"com/a/A.class": b"a"}).read_bytes()
    war = _war_with_two_copies(tmp_path / "in/app.war", lib, lib)
    _damage_first_copy(war, "WEB-INF/lib/a.jar")
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), runner=perfect_engine)
    outcomes = {a.rel: a.outcome for a in report.artifacts}
    assert outcomes == {"app.war": "ok", "app.war!/WEB-INF/lib/a.jar": "ok"}
    assert (tmp_path / "out/app.war/WEB-INF/lib/a.jar/com/a/A.java").is_file()


def test_run_uses_the_last_copy_of_a_source_entry_when_an_earlier_copy_is_damaged(fake_env, tmp_path):
    """Review A4: a damaged first copy made the good last copy a missing file, so the sources jar was partial."""
    jar = tmp_path / "in/lib-sources.jar"
    jar.parent.mkdir(parents=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
        with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("x/A.java", "class A { /* first */ }")
            zf.writestr("x/A.java", "class A { /* last */ }")
            zf.writestr("x/B.java", "class B {}")
    _damage_first_copy(jar, "x/A.java")
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), runner=perfect_engine)
    (art,) = report.artifacts
    assert (art.outcome, art.method, art.java_files, art.missing_classes, art.partial) == ("ok", "extracted", 2, 0, False)
    assert (tmp_path / "out/lib-sources.jar/x/A.java").read_text() == "class A { /* last */ }"


def test_run_stubbed_inner_class_file_is_written_without_a_retry(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/A$1.class": b"i"}, base=input_dir)

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        (Path(dest) / "com/x/A$1.java").write_text(STUB_TEXT["vineflower"])  # an inner class on its own
        res.java_files += 1
        return res

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    assert [a.engine for a in r.attempts] == ["vineflower"]  # A$1 is no expected stem: no retry
    assert (tmp_path / "out/a.jar/com/x/A$1.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["path"] for s in r.stubs] == ["com/x/A$1.java"]


def test_run_multi_release_stubs_keep_both_versions(fake_env, make_jar, tmp_path):
    """p/A and META-INF/versions/9/p/A share a stem; each version is its own held file."""
    input_dir = tmp_path / "in"
    make_jar("mr.jar", {"p/A.class": b"a", "META-INF/versions/9/p/A.class": b"a9"}, base=input_dir)
    base = stub_engine({"vineflower": {"p/A", "META-INF/versions/9/p/A"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name != "cfr":
            return base(spec, jar_path, target, dest, timeout, java=java)
        out = Path(dest) / "p/A.java"  # real CFR writes the package path, once
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("// cfr\nclass A {}\n")
        return EngineResult(spec.name, 0, False, 1, "")

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    out = tmp_path / "out/mr.jar"
    assert (out / "p/A.java").read_text() == "// cfr\nclass A {}\n"
    assert (out / "META-INF/versions/9/p/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["path"] for s in r.stubs] == ["META-INF/versions/9/p/A.java"]
    assert [(a.engine, a.stubbed) for a in r.attempts] == [("vineflower", 2), ("cfr", 0)]


def test_run_merge_keeps_the_retried_base_class_over_its_versioned_twin(fake_env, make_jar, tmp_path):
    """Review A1: with --merge, a held base class came from a later tree, and its twin from CFR won."""
    input_dir = tmp_path / "in"
    make_jar("mr.jar", {"p/A.class": b"a", "META-INF/versions/11/p/A.class": b"a11"}, base=input_dir)
    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False, engine="cfr"),
                 runner=stub_engine({"cfr": {"p/A"}}))
    r = report.artifacts[0]
    assert [(a.engine, a.level) for a in r.attempts] == [("cfr", "archive"), ("vineflower", "class")]
    assert (tmp_path / "out/src/p/A.java").read_text() == "// vineflower\nclass X {}\n"
    assert [c["path"] for c in r.collisions] == ["p/A.java"]  # the twin, as for one tree


def test_run_multi_release_versioned_stub_is_not_retried(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("mr.jar", {"p/A.class": b"a", "META-INF/versions/9/p/A.class": b"a9"}, base=input_dir)
    calls: list[str] = []
    base = stub_engine({"vineflower": {"META-INF/versions/9/p/A"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        calls.append(spec.name)
        return base(spec, jar_path, target, dest, timeout, java=java)

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    out = tmp_path / "out/mr.jar"
    assert calls == ["vineflower"]  # a versioned key is no expected stem: no retry
    assert (out / "p/A.java").read_text() == "// vineflower\nclass X {}\n"
    assert (out / "META-INF/versions/9/p/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["path"] for s in r.stubs] == ["META-INF/versions/9/p/A.java"] and r.partial


def test_run_retry_error_still_writes_the_held_file(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)
    base = stub_engine({"vineflower": {"com/x/A"}})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name == "cfr":
            raise OSError(28, "No space left on device")
        return base(spec, jar_path, target, dest, timeout, java=java)

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    assert r.outcome == "failed" and "No space left on device" in (r.failure or "")
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["path"] for s in r.stubs] == ["com/x/A.java"]


def test_run_empty_retry_file_keeps_the_held_stub(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    base = stub_engine({name: {"com/x/A"} for name in ("vineflower", "cfr", "procyon", "jd")})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if spec.name != "fernflower":
            return base(spec, jar_path, target, dest, timeout, java=java)
        out = Path(dest) / "com/x/A.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("")  # Fernflower's whole-class failure: an empty file
        return EngineResult(spec.name, 0, False, 1, "")

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text() == STUB_TEXT["vineflower"]
    assert [s["engine"] for s in r.stubs] == ["vineflower"]


def test_run_empty_first_file_counts_as_missing(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower":
            (Path(dest) / "com/x/A.java").write_text("")  # a whole-class failure
        return res

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    assert [(a.engine, a.level) for a in r.attempts] == [("vineflower", "archive"), ("cfr", "class")]
    assert r.missing_classes == 0 and not r.partial
    assert (tmp_path / "out/a.jar/com/x/A.java").read_text().startswith("// decompiled from")


def test_run_lists_a_stub_that_a_retry_engine_wrote(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)
    base = stub_engine({name: {"com/x/B"} for name in ("cfr", "fernflower", "procyon", "jd")})

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = base(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower":
            (Path(dest) / "com/x/B.java").unlink()  # vineflower misses B
            res.java_files -= 1
        return res

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    r = report.artifacts[0]
    assert [(s["path"], s["engine"]) for s in r.stubs] == [("com/x/B.java", "cfr")]
    assert r.stubs[0]["errors"] == ["org.benf.cfr.reader.util.ConfusedCFRException"]
    assert r.missing_classes == 0 and r.partial


def capped_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
    """perfect_engine for runs with a heap cap: it takes the heap_mb keyword (#98)."""
    return perfect_engine(spec, jar_path, target, dest, timeout, java=java)


def test_run_reports_the_memory_plan(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    events: list[tuple[str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=8, cpus=8, memory_mb=6553),
        runner=capped_engine,
        on_event=lambda k, s, d: events.append((k, d)),
    )
    s = report.settings
    assert (s["jobs"], s["cpu_budget"]) == (4, 2)  # the memory clamp left 4 jobs for 8 cpus
    assert (s["memory_budget_mb"], s["heap_mb"], s["whale_heap_mb"]) == (6553, 1306, 2911)
    assert s["heap_cap_mb"] is None  # an explicit budget keeps the plain shares
    line = "engine memory: up to 6.4 GB (4 jobs, 1.3 GB heap each; a whale gets 2.8 GB)"
    assert ("memory", line) in events
    kinds = [k for k, _ in events]
    assert kinds.index("scan") < kinds.index("memory") < kinds.index("decompile")


def test_run_auto_budget_uses_the_ram_and_caps_each_jvm(fake_env, make_jar, tmp_path, monkeypatch):
    from decaf import memory

    input_dir = tmp_path / "in"
    make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)}, base=input_dir)
    monkeypatch.setattr(memory, "total_ram_mb", lambda: 12500)  # auto: budget 10000, cap 3125
    seen: list[int | None] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        seen.append(heap_mb)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=4, cpus=4, memory_mb=None),
        runner=engine,
    )
    s = report.settings
    assert (s["memory_budget_mb"], s["heap_mb"], s["whale_heap_mb"], s["heap_cap_mb"]) == (10000, 2150, 3125, 3125)
    assert seen == [3125]  # the whale gets the cap, not its 4600 MB share: the JVM default of 1.10.0


def test_run_address_space_limit_caps_each_heap(fake_env, make_jar, tmp_path, monkeypatch):
    """Under ulimit -v, a JVM with an -Xmx above half the limit does not start (#114)."""
    from decaf import memory

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    monkeypatch.setattr(memory, "address_space_cap_mb", lambda: 1000)
    seen: list[int | None] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        seen.append(heap_mb)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1, memory_mb=8000),
        runner=engine,
    )
    assert seen == [1000] and report.settings["heap_cap_mb"] == 1000


def test_run_unknown_ram_means_no_cap(fake_env, make_jar, tmp_path, monkeypatch):
    from decaf import memory

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    monkeypatch.setattr(memory, "total_ram_mb", lambda: None)
    events: list[tuple[str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, memory_mb=None),
        runner=perfect_engine,
        on_event=lambda k, s, d: events.append((k, d)),
    )
    assert (report.settings["memory_budget_mb"], report.settings["heap_mb"]) == (0, None)
    assert report.settings["heap_cap_mb"] is None
    assert ("memory", "engine memory: no cap (RAM size unknown; set --memory)") in events


def test_run_without_a_cap_says_so(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    events: list[tuple[str, str]] = []
    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False),
        runner=perfect_engine,
        on_event=lambda k, s, d: events.append((k, d)),
    )
    assert ("memory", "engine memory: no cap (--memory 0)") in events
    assert (report.settings["memory_budget_mb"], report.settings["whale_heap_mb"]) == (0, None)


def test_run_warns_about_heap_flags_in_java_options(fake_env, make_jar, tmp_path, monkeypatch):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Xmx4g")
    monkeypatch.setenv("_JAVA_OPTIONS", "-Xmx2g")
    monkeypatch.delenv("JDK_JAVA_OPTIONS", raising=False)
    warnings: list[str] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, memory_mb=6553),
        runner=capped_engine,
        on_warn=warnings.append,
    )
    assert (
        "JAVA_TOOL_OPTIONS sets the heap (-Xmx4g); decaf's --memory heap takes precedence"
        " (--memory 0 keeps yours)"
    ) in warnings
    assert any(w.startswith("_JAVA_OPTIONS sets the heap (-Xmx2g)") for w in warnings)
    warnings.clear()
    run(
        Settings(input=input_dir, output=tmp_path / "out2", maven=False, memory_mb=0),
        runner=perfect_engine,
        on_warn=warnings.append,
    )
    assert not any("sets the heap" in w for w in warnings)  # no cap: the user's heap applies


def test_run_gives_each_jvm_its_heap(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("small.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)
    make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)}, base=input_dir)
    seen: list[tuple[str, str, int | None]] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        seen.append((Path(target).name, spec.name, heap_mb))
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower" and Path(target).name == "small.jar":
            (Path(dest) / "com/x/B.java").unlink()  # B goes missing: a cfr class retry follows
            res.java_files -= 1
        return res

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=4, cpus=4, memory_mb=8000),
        runner=engine,
    )
    heaps = {(t, e): h for t, e, h in seen if e == "vineflower"}
    assert heaps == {("whale.jar", "vineflower"): 3620, ("small.jar", "vineflower"): 1660}  # heap_in(4000), heap_in(2000)
    assert [h for _, e, h in seen if e == "cfr"] == [1660]  # the class retry runs in its artifact's slot
    attempts = {r.rel: [(a.engine, a.heap_mb) for a in r.attempts] for r in report.artifacts}
    assert attempts["small.jar"] == [("vineflower", 1660), ("cfr", 1660)]


def test_run_without_a_cap_records_no_heap(fake_env, make_jar, tmp_path):
    report = run(
        Settings(input=make_inputs(make_jar, tmp_path), output=tmp_path / "out", maven=False),
        runner=perfect_engine,  # takes no heap_mb: the pipeline must not pass one
    )
    assert all(a.heap_mb is None for r in report.artifacts for a in r.attempts)


def test_run_gives_each_checker_jvm_the_heap_of_its_engine_jvm(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("small.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"}, base=input_dir)
    make_jar("whale.jar", {f"com/w/C{i}.class": b"x" for i in range(3000)}, base=input_dir)
    seen: list[tuple[str, int | None]] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower" and Path(target).name == "small.jar":
            (Path(dest) / "com/x/B.java").unlink()  # B goes missing: a cfr class retry follows
            res.java_files -= 1
        return res

    def checker(jobs, *, timeout, cpu_budget, heap_mb=None):
        seen.extend((job.classpath[0].name, heap_mb) for job in jobs)
        return [[check.FileResult("clean") for _ in job.files] for job in jobs]

    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=4, cpus=4, memory_mb=8000),
        runner=engine, checker=checker,
    )
    # small.jar: the archive check and the class-retry check; then the whale check
    assert sorted(seen) == [("small.jar", 1660), ("small.jar", 1660), ("whale.jar", 3620)]


def test_run_flags_out_of_memory_attempts(fake_env, make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/x/A.class": b"a"}, base=input_dir)
    make_jar("b.jar", {"com/y/B.class": b"b"}, base=input_dir)
    oom_stub = STUB_TEXT["vineflower"].replace(
        "java.lang.NullPointerException: boom", "java.lang.OutOfMemoryError: Java heap space"
    )

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name == "vineflower" and Path(target).name == "a.jar":
            (Path(dest) / "com/x/A.java").write_text(oom_stub)  # out of heap inside one method
        if spec.name == "vineflower" and Path(target).name == "b.jar":
            res.out_of_memory = True  # out of heap in stderr, output complete
        return res

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine)
    by_rel = {r.rel: [(a.engine, a.out_of_memory) for a in r.attempts] for r in report.artifacts}
    assert by_rel["a.jar"] == [("vineflower", True), ("cfr", False)]
    assert by_rel["b.jar"] == [("vineflower", True)]
    assert report.totals["out_of_memory_attempts"] == 2


def _deflated_jar(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_run_warns_when_an_estimate_exceeds_the_share(fake_env, tmp_path):
    input_dir = tmp_path / "in"
    _deflated_jar(input_dir / "big.jar", {"com/x/A.class": b"\0" * (25 * 2**20)})  # 73 + 48 x 25 = 1273 MB
    warnings: list[str] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1, memory_mb=1400),
        runner=capped_engine,
        on_warn=warnings.append,
    )
    assert "big.jar: vineflower may need about 1.2 GB of heap; its share is 1.0 GB (raise --memory)" in warnings
    warnings.clear()
    run(
        Settings(input=input_dir, output=tmp_path / "out2", maven=False, jobs=1, cpus=1, memory_mb=0),
        runner=perfect_engine,
        on_warn=warnings.append,
    )
    assert not any("may need about" in w for w in warnings)  # no cap, no share warning


def test_run_share_warning_reads_no_class_bytes_without_a_model(fake_env, tmp_path, monkeypatch):
    from decaf import pipeline

    input_dir = tmp_path / "in"
    _deflated_jar(input_dir / "big.jar", {"com/x/A.class": b"\0" * (25 * 2**20)})
    reads: list[Path] = []
    monkeypatch.setattr(pipeline, "_class_bytes", lambda target, largest=False: reads.append(target) or 0)
    for out, engine in (("out", "jd"), ("out2", "vineflower")):
        run(
            Settings(input=input_dir, output=tmp_path / out, maven=False, jobs=1, cpus=1, memory_mb=1400,
                     engine=engine),
            runner=capped_engine,
            on_warn=lambda text: None,
        )
        assert len(reads) == (engine == "vineflower")  # jd has no size model: decaf does not read the jar


def test_run_warns_for_a_batch_eligible_jar_too(fake_env, tmp_path):
    """#114: a jar under 3000 classes got no estimate, but a batch JVM has one share too."""
    input_dir = tmp_path / "in"
    _deflated_jar(input_dir / "big.jar", {"com/x/A.class": b"\0" * (25 * 2**20)})

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    warnings: list[str] = []
    run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1, cpus=1, memory_mb=1400),
        runner=capped_engine,
        batch_runner=batch,
        on_warn=warnings.append,
    )
    assert "big.jar: vineflower may need about 1.2 GB of heap; its share is 1.0 GB (raise --memory)" in warnings


def test_run_estimates_one_class_for_an_engine_that_runs_each_class_alone(fake_env, tmp_path):
    """#114: CFR runs one JVM for each class of a class folder, so the sum of the folder gave a false warning."""
    input_dir = tmp_path / "in"
    for i in range(40):
        (input_dir / "p").mkdir(parents=True, exist_ok=True)
        (input_dir / f"p/C{i}.class").write_bytes(b"\0" * 2**20)  # 85 + 22 x 40 = 965 MB for the folder
    warnings: list[str] = []
    for out, engine in (("out", "cfr"), ("out2", "vineflower")):
        run(
            Settings(input=input_dir, output=tmp_path / out, maven=False, jobs=1, cpus=1, memory_mb=1100,
                     engine=engine),
            runner=capped_engine,
            on_warn=warnings.append,
        )
    estimates = [w for w in warnings if "may need about" in w]
    assert len(estimates) == 1 and "vineflower" in estimates[0]  # Vineflower takes the folder in one JVM


def test_run_counts_one_out_of_heap_batch_jvm_once(fake_env, make_jar, tmp_path: Path):
    """#114: one batch JVM that ran out of heap counted as one attempt for each of its jars."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar(
        "big.jar",
        {**{f"com/big/C{i}.class": b"x" for i in range(900)}, "lib/s0.jar": s0.read_bytes(), "lib/s1.jar": s1.read_bytes()},
        base=input_dir,
    )
    all_queued = threading.Event()
    queued = [0]

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued[0] += 1
            if queued[0] == 3:
                all_queued.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        res.out_of_memory = True  # out of heap in stderr, output complete
        return res

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False, jobs=1, cpus=1),
        runner=gated_solo, batch_runner=batch, on_event=on_event,
    )
    members = [r for r in report.artifacts if r.rel.startswith("big.jar!/")]
    assert [a.out_of_memory for r in members for a in r.attempts] == [True, True]
    assert report.totals["out_of_memory_attempts"] == 1


def test_run_counts_two_out_of_heap_batch_jvms_twice(fake_env, make_jar, tmp_path: Path, monkeypatch):
    import decaf.pipeline as pipeline

    monkeypatch.setattr(pipeline, "_BATCH_MAX_JARS", 2)
    smalls = {f"lib/s{i}.jar": make_jar(f"s{i}.jar", {f"com/s{i}/A.class": b"x"}).read_bytes() for i in range(4)}
    input_dir = tmp_path / "in"
    make_jar("big.jar", {**{f"com/big/C{i}.class": b"x" for i in range(900)}, **smalls}, base=input_dir)
    queued = [0]
    first_two, all_four = threading.Event(), threading.Event()  # with jobs=1, the gate holds two small jars

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued[0] += 1
            if queued[0] == 3:
                first_two.set()
            if queued[0] == 5:
                all_four.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            first_two.wait(timeout=30)
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        all_four.wait(timeout=30)  # the other two small jars wait for the second batch
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        res.out_of_memory = True
        return res

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False, jobs=1, cpus=1),
        runner=gated_solo, batch_runner=batch, on_event=on_event,
    )
    levels = [a.level for r in report.artifacts if r.rel.startswith("big.jar!/") for a in r.attempts]
    assert levels == ["batch"] * 4 and report.totals["out_of_memory_attempts"] == 2


@pytest.mark.skipif(sys.platform != "linux", reason="macOS and Windows file names are Unicode")
def test_run_gives_the_jvms_paths_they_can_open_for_an_input_folder_that_is_not_utf8(
    fake_env, make_jar, tmp_path, monkeypatch
):
    """Java decodes the bytes of a path as UTF-8, so a Latin-1 name reaches it as other characters (#113).

    The input is relative, as the CLI gives it.
    """
    monkeypatch.chdir(tmp_path)
    input_dir = Path(os.fsdecode(b"caf\xe9"))
    jar = make_jar("x.jar", {"com/x/A.class": b"a"}, base=input_dir)
    (input_dir / "p").mkdir()
    (input_dir / "p/C.class").write_bytes(b"c")
    targets: dict[Path, Path] = {}  # path -> the file it opens, read while the run's links exist
    classpath: dict[Path, Path] = {}

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        targets[Path(target)] = Path(target).resolve()
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def checker(jobs, *, timeout, cpu_budget):
        classpath.update((p, p.resolve()) for job in jobs for p in job.classpath)
        return [[check.FileResult("clean") for _ in job.files] for job in jobs]

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False), runner=engine, checker=checker)
    assert [r.outcome for r in report.artifacts] == ["ok", "ok"]
    for p in [*targets, *classpath]:
        str(p).encode("utf-8")  # no lone surrogate: Java reads this name
    assert jar.resolve() in targets.values()
    assert {jar.resolve(), input_dir.resolve()} <= set(classpath.values())


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs extra rights on Windows")
@pytest.mark.parametrize("relative", [False, True], ids=["folder-name", "working-folder"])
def test_run_gives_the_jvms_ascii_paths_for_an_input_that_is_not_ascii(fake_env, make_jar, tmp_path, monkeypatch,
                                                                       relative):
    """Review S7: under LC_ALL=C, Java decodes a path as ASCII, so a UTF-8 name such as café failed.

    Review S6: the link keeps an ASCII file name, because the Maven lookup reads it. A JVM gets the
    absolute path, so a relative ASCII input in a folder such as café needs a link too.
    """
    if relative:
        (tmp_path / "café").mkdir()
        monkeypatch.chdir(tmp_path / "café")
        input_dir = Path("libs")
    else:
        input_dir = tmp_path / "lib.é"  # also the suffix of the folder is no ASCII
    jar = make_jar("mylib-1.2.3.jar", {"com/x/A.class": b"a"}, base=input_dir)
    (input_dir / "p").mkdir()
    (input_dir / "p/C.class").write_bytes(b"c")
    targets: dict[Path, Path] = {}  # path -> the file it opens, read while the run's links exist
    classpath: dict[Path, Path] = {}
    names: list[str] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        targets[Path(target)] = Path(target).resolve()
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def checker(jobs, *, timeout, cpu_budget):
        classpath.update((p, p.resolve()) for job in jobs for p in job.classpath)
        return [[check.FileResult("clean") for _ in job.files] for job in jobs]

    def resolver(jar_path, repos, client, cache_dir, **kw):
        names.append(Path(jar_path).name)
        return Resolution(miss="no pom.properties; 0 candidates")

    report = run(Settings(input=input_dir, output=tmp_path / "out"), runner=engine, checker=checker, resolver=resolver)
    assert [r.outcome for r in report.artifacts] == ["ok", "ok"]
    assert [p for p in [*targets, *classpath] if not str(p.absolute()).isascii()] == []
    assert jar.resolve() in targets.values()
    assert {jar.resolve(), input_dir.resolve()} <= set(classpath.values())
    assert names == ["mylib-1.2.3.jar"]  # the file-name hints of the Maven lookup
    assert jar.is_file()  # the run deletes the links, not the input


@pytest.mark.skipif(sys.platform != "linux", reason="macOS and Windows file names are Unicode")
def test_run_keeps_the_input_path_when_the_link_fails(fake_env, make_jar, tmp_path, monkeypatch):
    jar = make_jar(os.fsdecode(b"caf\xe9.jar"), {"com/x/A.class": b"a"}, base=tmp_path / "in")
    targets: list[Path] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        targets.append(Path(target))
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def no_link(*args, **kwargs):
        raise PermissionError("no links here")

    monkeypatch.setattr(os, "symlink", no_link)
    report = run(Settings(input=tmp_path / "in", output=tmp_path / "out", maven=False), runner=engine)
    assert report.artifacts[0].outcome == "ok" and targets == [jar]


def _other_folders(dest, target) -> set[str]:
    """The folders of the temp root that are not the output folder or the input folder of this engine call (#97)."""
    return {p.name for p in Path(dest).parent.iterdir()} - {Path(dest).name, Path(target).name}


def test_run_drops_each_temp_folder_after_its_last_use(fake_env, make_jar, tmp_path):
    """#97: the temp root grew with the whole run. Now the folders of an engine call go after the call."""
    input_dir = tmp_path / "in"
    for k in range(3):
        make_jar(f"j{k}.jar", {f"p{k}/A.class": b"a", f"p{k}/B.class": b"b"}, base=input_dir)
    left: list[set[str]] = []

    def engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        left.append(_other_folders(dest, target))
        res = perfect_engine(spec, jar_path, target, dest, timeout, java=java)
        if spec.name in ("vineflower", "cfr"):  # B is missing: two class retries
            for b in Path(dest).rglob("B.java"):
                b.unlink()
        return res

    report = run(Settings(input=input_dir, output=tmp_path / "out", maven=False, jobs=1), runner=engine)
    assert report.totals["ok"] == 3 and all(r.missing_classes == 0 for r in report.artifacts)
    assert left == [set()] * 9  # 3 jars, 3 engine calls each


def test_run_leaves_the_final_delete_an_empty_temp_root(fake_env, make_jar, tmp_path, monkeypatch):
    """#97: held files, a class-folder copy, and extracted sources also go after their last use."""
    import shutil

    input_dir = tmp_path / "in"
    make_jar("a.jar", {"p/A.class": b"a", "p/B.class": b"b"}, base=input_dir)
    make_jar("lib-1.2.jar", {"com/x/A.class": b"x"}, base=input_dir)
    make_jar("s-sources.jar", {"com/s/S.java": "class S {}"}, base=input_dir)
    (input_dir / "q").mkdir()
    (input_dir / "q/Q.class").write_bytes(b"q")
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        if Path(jar_path).name != "lib-1.2.jar":
            return Resolution(miss="no sources")
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources,
                          repo="https://r.test/m2", resolved_by="pom-properties")

    real = shutil.rmtree
    left: list[list[str]] = []

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith("decaf-"):  # the temp root, at the end of the run
            left.append(sorted(p.name for p in Path(path).iterdir()))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    report = run(Settings(input=input_dir, output=tmp_path / "out"),
                 runner=stub_engine({"vineflower": {"p/B"}}), resolver=resolver)
    methods = {r.rel: r.method for r in report.artifacts}
    assert methods == {"a.jar": "vineflower", "lib-1.2.jar": "maven", "s-sources.jar": "extracted",
                       "_classes": "vineflower"}
    assert [a.engine for a in report.artifacts if a.rel == "a.jar" for a in a.attempts] == ["vineflower", "cfr"]
    assert left == [[]]
    inputs = sorted(p.relative_to(input_dir).as_posix() for p in input_dir.rglob("*") if p.is_file())
    assert inputs == ["a.jar", "lib-1.2.jar", "q/Q.class", "s-sources.jar"]  # only temp folders go


def test_run_drops_the_batch_folders_after_the_split(fake_env, make_jar, tmp_path):
    """#97: the batch output goes after the split, and the folder of each jar of the batch after its write."""
    s0 = make_jar("s0.jar", {"com/s0/A.class": b"x", "com/s0/B.class": b"x"})
    s1 = make_jar("s1.jar", {"com/s1/A.class": b"x", "com/s1/B.class": b"x"})
    input_dir = tmp_path / "in"
    make_jar("big.jar", {**{f"com/big/C{i}.class": b"x" for i in range(900)},
                         "lib/s0.jar": s0.read_bytes(), "lib/s1.jar": s1.read_bytes()}, base=input_dir)
    all_queued = threading.Event()
    queued = [0]
    left: list[set[str]] = []

    def on_event(kind, subject, detail):
        if kind == "queued":
            queued[0] += 1
            if queued[0] == 3:
                all_queued.set()

    def gated_solo(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        if Path(target).name == "big.jar":
            all_queued.wait(timeout=30)  # the two small jars then form one batch
        if Path(target).is_dir():  # a class retry of a jar of the batch
            left.append(_other_folders(dest, target))
        return perfect_engine(spec, jar_path, target, dest, timeout, java=java)

    def batch_without_b(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        for b in Path(dest).rglob("B.java"):
            b.unlink()
        return res

    report = run(
        Settings(input=input_dir, output=tmp_path / "out", maven=False, mirror=False, jobs=1, cpus=1),
        runner=gated_solo, batch_runner=batch_without_b, on_event=on_event,
    )
    assert report.totals["ok"] == 3 and all(r.missing_classes == 0 for r in report.artifacts)
    # At the retry of the first jar: the nested folder of big.jar (#97 part 2) and the split folder of
    # the second jar, which is not written yet. At the retry of the second jar: only the nested folder.
    assert [len(s) for s in left] == [2, 1]
