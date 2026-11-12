import re
import zipfile
from pathlib import Path

import pytest

from decaf import check
from decaf.engines import ENGINES, EngineResult
from decaf.maven import Gav, Resolution
from decaf.pipeline import (
    Ctx,
    MergeWriter,
    MirrorWriter,
    Settings,
    _declared_package,
    _discover_nested,
    _sources_gap,
    chain_for,
    expected_class_stems,
    process_artifact,
)
from decaf.scanner import Artifact, ArtifactKind


def writing_runner(files_per_engine: dict[str, dict[str, str]]):
    """Fake run_engine: each engine 'produces' the given {rel: content} files."""
    calls: list[tuple[str, Path]] = []
    sent: list[list[str] | None] = []  # the class files of a folder target, read in the call: it goes after (#97)

    def _run(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, on_stderr_line=None):
        calls.append((spec.name, Path(target)))
        folder = Path(target)
        sent.append(sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.class")) if folder.is_dir() else None)
        files = files_per_engine.get(spec.name, {})
        for rel, content in files.items():
            p = Path(dest) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return EngineResult(spec.name, 0 if files else 1, False, len(files), "" if files else "boom")

    _run.calls = calls
    _run.sent = sent
    return _run


def make_ctx(tmp_path: Path, runner, *, resolver=None, fallback=True, maven=False, engine="vineflower", writer=None, checker=None):
    settings = Settings(
        input=tmp_path,
        output=tmp_path / "out",
        engine=engine,
        fallback=fallback,
        maven=maven,
        repos=("https://r.test/m2",),
    )
    tmp_root = tmp_path / "work"
    tmp_root.mkdir(exist_ok=True)
    return Ctx(
        settings=settings,
        writer=writer or MergeWriter(tmp_path / "out/src"),
        chain=chain_for(engine, fallback),
        engine_jars={name: Path(f"/fake/{name}.jar") for name in ENGINES},
        java="java",
        tmp_root=tmp_root,
        client=object() if maven else None,
        sources_cache=tmp_path / "cache",
        runner=runner,
        resolver=resolver or (lambda *a, **k: None),
        checker=checker,
        java_major=21,
    )


def test_chain_for():
    assert chain_for("vineflower", True) == ["vineflower", "cfr", "fernflower", "procyon", "jd"]
    assert chain_for("cfr", True) == ["cfr", "vineflower", "fernflower", "procyon", "jd"]
    assert chain_for("cfr", False) == ["cfr"]
    assert chain_for("cfr", True, available={"cfr", "jd"}) == ["cfr", "jd"]


def test_archive_level_fallback(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"x"})
    runner = writing_runner({"cfr": {"com/x/A.java": "class A {}"}})  # vineflower fails
    ctx = make_ctx(tmp_path, runner)
    report, nested = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.method == "cfr"
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]
    assert report.attempts[0].java_files == 0
    assert (tmp_path / "out/src/com/x/A.java").is_file()
    assert nested == []


def test_no_fallback_fails_fast(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"x"})
    runner = writing_runner({"cfr": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, fallback=False)
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "failed"
    assert [a.engine for a in report.attempts] == ["vineflower"]
    assert report.failure == "all engines failed"


def test_class_level_retry_fills_missing(make_jar, tmp_path: Path):
    jar = make_jar(
        "app.jar",
        {"com/x/A.class": b"a", "com/x/B.class": b"b", "com/x/B$1.class": b"inner"},
    )
    runner = writing_runner(
        {
            "vineflower": {"com/x/A.java": "class A {}"},  # B missing
            "cfr": {"com/x/B.java": "class B {}"},
        }
    )
    ctx = make_ctx(tmp_path, runner)
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.method == "vineflower"
    assert report.classes == 2
    assert report.missing_classes == 0
    assert [(a.engine, a.level) for a in report.attempts] == [
        ("vineflower", "archive"),
        ("cfr", "class"),
    ]
    assert (tmp_path / "out/src/com/x/A.java").is_file()
    assert (tmp_path / "out/src/com/x/B.java").is_file()
    assert runner.sent[1] == ["com/x/B$1.class", "com/x/B.class"]


def test_war_prefix_normalized_for_missing_check(make_jar, tmp_path: Path):
    war = make_jar("app.war", {"WEB-INF/classes/com/y/C.class": b"c"})
    runner = writing_runner({"vineflower": {"WEB-INF/classes/com/y/C.java": "class C {}"}})
    ctx = make_ctx(tmp_path, runner)
    report, _ = process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.missing_classes == 0
    assert (tmp_path / "out/src/com/y/C.java").is_file()


def test_nested_archives_discovered(make_jar, tmp_path: Path):
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    war = make_jar(
        "app.war",
        {
            "WEB-INF/classes/com/y/C.class": b"c",
            "WEB-INF/lib/dep.jar": inner.read_bytes(),
        },
    )
    runner = writing_runner({"vineflower": {"com/y/C.java": "class C {}"}})
    ctx = make_ctx(tmp_path, runner)
    report, nested = process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert len(nested) == 1
    assert nested[0].rel == "app.war!/WEB-INF/lib/dep.jar"
    assert nested[0].kind == ArtifactKind.ARCHIVE
    assert nested[0].path.is_file()


def test_nested_discovery_respects_max_depth(make_jar, tmp_path: Path):
    inner = make_jar("inner.jar", {"com/i/I.class": b"i"})
    dep = make_jar("dep.jar", {"com/d/D.class": b"d", "lib/inner.jar": inner.read_bytes()})
    runner = writing_runner({"vineflower": {"com/d/D.java": "class D {}"}})
    ctx = make_ctx(tmp_path, runner)  # default max_depth=1
    report, nested = process_artifact(
        Artifact(dep, "app.war!/WEB-INF/lib/dep.jar", ArtifactKind.ARCHIVE, parent="app.war", depth=1), ctx
    )
    assert report.outcome == "ok"  # the at-cap artifact itself is still decompiled
    assert len(nested) == 1
    assert nested[0].kind is ArtifactKind.BEYOND_DEPTH
    assert nested[0].rel == "app.war!/WEB-INF/lib/dep.jar!/lib/inner.jar"

    deep_report, deep_nested = process_artifact(nested[0], ctx)
    assert deep_report.outcome == "skipped"
    assert "--max-depth 1" in (deep_report.failure or "")
    assert deep_nested == []
    assert len(runner.calls) == 1  # only the dep itself was ever decompiled


def test_maven_first_short_circuits_engines(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "// real source\nclass A {}"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=sources,
            repo="https://r.test/m2",
            resolved_by="pom-properties",
        )

    runner = writing_runner({})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.method == "maven"
    assert report.gav == "com.example:lib:1.2"
    assert report.repo == "https://r.test/m2"
    assert runner.calls == []
    assert "real source" in (tmp_path / "out/src/com/x/A.java").read_text()
    assert report.resolved_by == "pom-properties"
    assert report.sources_miss is None
    assert (report.classes, report.sources_gap, report.missing_classes) == (1, 0, 0)


def test_empty_sources_jar_falls_back_to_engines(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    empty_sources = make_jar("empty-sources.jar", {"README": "no java here"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=empty_sources,
            repo="https://r.test/m2",
            resolved_by="verified-guess",
        )

    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.method == "vineflower"
    assert report.sources_miss == "sources jar for com.example:lib:1.2 contained no source files"
    assert empty_sources.exists()  # a readable zip with no source file stays in the cache (#180)


def test_resolution_miss_recorded_in_report(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            miss="verified com.example:lib:1.2 via https://r.test/m2 but no -sources.jar in any repo with this build",
        )

    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.method == "vineflower"
    assert report.gav == "com.example:lib:1.2"
    assert report.resolved_by is None
    assert report.sources_miss == (
        "verified com.example:lib:1.2 via https://r.test/m2 but no -sources.jar in any repo with this build"
    )


def test_maven_verbose_line_on_hit_and_miss(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    lines: list[str] = []

    def hit_resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=sources,
            repo="https://r.test/m2",
            resolved_by="verified-guess",
        )

    ctx = make_ctx(tmp_path, writing_runner({}), resolver=hit_resolver, maven=True)
    ctx.on_stderr = lines.append
    process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert lines == [
        "maven lib-1.2.jar: verified-guess com.example:lib:1.2 (https://r.test/m2)"
    ]

    def miss_resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(miss="no pom.properties; 0 candidates")

    lines.clear()
    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=miss_resolver, maven=True)
    ctx.on_stderr = lines.append
    process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert lines == ["maven lib-1.2.jar: no pom.properties; 0 candidates"]


def test_sources_jar_artifact_extracted(make_jar, tmp_path: Path):
    sj = make_jar("lib-sources.jar", {"com/x/A.java": "class A {}"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    report, _ = process_artifact(Artifact(sj, "lib-sources.jar", ArtifactKind.SOURCES_JAR), ctx)
    assert report.outcome == "ok"
    assert report.method == "extracted"
    assert report.java_files == 1


def test_empty_sources_jar_artifact_fails_without_method(make_jar, tmp_path: Path):
    sj = make_jar("empty-sources.jar", {"README": "no java"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    report, _ = process_artifact(Artifact(sj, "empty-sources.jar", ArtifactKind.SOURCES_JAR), ctx)
    assert report.outcome == "failed"
    assert report.method is None


def test_resource_only_skipped_and_corrupt_failed(tmp_path: Path, make_jar):
    r = make_jar("r.jar", {"META-INF/MANIFEST.MF": "m"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    rep, _ = process_artifact(Artifact(r, "r.jar", ArtifactKind.RESOURCE_ONLY), ctx)
    assert rep.outcome == "skipped"
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"junk")
    rep2, _ = process_artifact(Artifact(bad, "bad.jar", ArtifactKind.CORRUPT), ctx)
    assert rep2.outcome == "failed"
    assert rep2.failure == "unreadable archive"


def test_resource_only_container_still_descended(make_jar, tmp_path: Path):
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    war = make_jar("only-libs.war", {"WEB-INF/lib/dep.jar": inner.read_bytes()})
    ctx = make_ctx(tmp_path, writing_runner({}))
    report, nested = process_artifact(
        Artifact(war, "only-libs.war", ArtifactKind.RESOURCE_ONLY), ctx
    )
    assert report.outcome == "skipped"
    assert len(nested) == 1
    assert nested[0].rel == "only-libs.war!/WEB-INF/lib/dep.jar"
    assert nested[0].kind is ArtifactKind.ARCHIVE


def test_class_tree_decompiled_from_copied_tree(tmp_path: Path):
    (tmp_path / "loose/com/x").mkdir(parents=True)
    (tmp_path / "loose/com/x/A.class").write_bytes(b"a")
    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner)
    report, _ = process_artifact(Artifact(tmp_path, "_classes", ArtifactKind.CLASS_TREE), ctx)
    assert report.outcome == "ok"
    assert report.classes == 1
    assert runner.sent[0] == ["loose/com/x/A.class"] and runner.calls[0][1] != tmp_path  # copied tree, not the input root


def test_runner_exception_becomes_failed_report(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"A.class": b"x"})

    def exploding(*a, **k):
        raise RuntimeError("engine exploded")

    ctx = make_ctx(tmp_path, exploding)
    report, nested = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "failed"
    assert "engine exploded" in (report.failure or "")
    assert nested == []


def test_resolver_exception_falls_back_to_the_engines_with_nested(make_jar, tmp_path: Path):
    """A stage-1 (resolution) crash is a sources miss: the engines decompile (#111),
    and nested archives discovered before the crash are still surfaced."""
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    war = make_jar(
        "app.war",
        {
            "WEB-INF/classes/com/w/W.class": b"w",
            "WEB-INF/lib/dep.jar": inner.read_bytes(),
        },
    )

    def exploding_resolver(jar_path, repos, client, cache_dir, **kw):
        raise RuntimeError("index server melted")

    runner = writing_runner({"vineflower": {"com/w/W.java": "class W {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=exploding_resolver, maven=True)
    report, nested = process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method) == ("ok", "vineflower")
    assert report.sources_miss == "sources lookup failed: index server melted"
    assert [n.rel for n in nested] == ["app.war!/WEB-INF/lib/dep.jar"]


def test_discover_nested_carries_class_counts(make_jar, tmp_path):
    inner = make_jar("dep.jar", {"com/d/D.class": b"d", "com/d/E.class": b"e"})
    war = make_jar("app.war", {"WEB-INF/lib/dep.jar": inner.read_bytes()})
    tmp_root = tmp_path / "t"
    tmp_root.mkdir()
    ctx = Ctx(
        settings=Settings(input=war, output=tmp_path / "o"),
        writer=None, chain=[], engine_jars={}, java="java",
        tmp_root=tmp_root, client=None, sources_cache=tmp_path / "s",
        runner=None, resolver=None,
    )
    nested = _discover_nested(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    assert [(n.rel, n.classes) for n in nested] == [("app.war!/WEB-INF/lib/dep.jar", 2)]


def _batch_ctx(tmp_path, batch_runner, chain=None, on_event=None):
    from decaf.pipeline import Ctx, MergeWriter, Settings

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir(exist_ok=True)
    return Ctx(
        settings=Settings(input=tmp_path, output=tmp_path / "out"),
        writer=MergeWriter(tmp_path / "out" / "src"),
        chain=chain or ["vineflower"],
        engine_jars={
            "vineflower": Path("/fake/vf.jar"),
            "cfr": Path("/fake/cfr.jar"),
            "fernflower": Path("/fake/ff.jar"),
        },
        java="java", tmp_root=tmp_root, client=None,
        sources_cache=tmp_path / "s", runner=None, resolver=None,
        batch_runner=batch_runner,
        on_event=on_event,
    )


def _member(make_jar, name, entries):
    from decaf.pipeline import ArtifactReport, _stems_and_claims
    from decaf.scanner import Artifact, ArtifactKind

    jar = make_jar(name, entries)
    a = Artifact(jar, name, ArtifactKind.ARCHIVE, len(entries))
    report = ArtifactReport(rel=name, kind="archive", outcome="ok")
    return (a, jar, report, *_stems_and_claims(jar))


def merged_batch_engine(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
    """Writes one .java per top-level class of every target into one merged dest."""
    import zipfile as _zf

    from decaf.engines import EngineResult

    dest = Path(dest)
    n = 0
    for t in targets:
        with _zf.ZipFile(Path(t)) as zf:
            for entry in zf.namelist():
                if not entry.endswith(".class") or "$" in entry.rsplit("/", 1)[-1]:
                    continue
                out = dest / (entry[: -len(".class")] + ".java")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("class X {}\n")
                n += 1
    return EngineResult(spec.name, 0, False, n, "")


def test_decompile_batch_splits_and_attributes(make_jar, tmp_path):
    from decaf.pipeline import _decompile_batch

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x", "res/a.txt": b"r"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    assert [r.rel for r in done] == ["a.jar", "b.jar"]
    for r in done:
        assert r.outcome == "ok" and r.method == "vineflower"
        assert r.attempts[0].level == "batch" and r.attempts[0].returncode == 0
        assert r.java_files == 1 and r.missing_classes == 0
    out = tmp_path / "out" / "src"
    assert (out / "com/a/A.java").is_file() and (out / "com/b/B.java").is_file()


def test_decompile_batch_failure_requeues_members(make_jar, tmp_path):
    from decaf.engines import EngineResult
    from decaf.pipeline import _decompile_batch

    def broken_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        return EngineResult(spec.name, 1, False, 0, "boom")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, broken_batch)
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert done == []
    assert [a.rel for a, _, _ in requeue] == ["a.jar", "b.jar"]
    for _, _, report in requeue:
        assert report.attempts[0].level == "batch" and report.attempts[0].returncode == 1


def test_decompile_batch_empty_member_requeued(make_jar, tmp_path):
    from decaf.pipeline import _decompile_batch

    def only_first_engine(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        return merged_batch_engine(spec, jar_path, targets[:1], dest, timeout, java=java)

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, only_first_engine)
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert [r.rel for r in done] == ["a.jar"]
    assert [a.rel for a, _, _ in requeue] == ["b.jar"]


def test_decompile_batch_writes_sources_only(make_jar, tmp_path):
    """Resources come from the fetch stage's add_resources, not from the batch."""
    from decaf.pipeline import MirrorWriter, _decompile_batch

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x", "META-INF/spring.factories": b"cfg"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    ctx.writer = MirrorWriter(tmp_path / "out")
    done, requeue = _decompile_batch([m1], ctx)
    assert requeue == [] and done[0].outcome == "ok"
    assert (tmp_path / "out/a.jar/com/a/A.java").is_file()
    assert not (tmp_path / "out/a.jar/META-INF/spring.factories").exists()


def test_decompile_batch_contains_member_postprocessing_errors(make_jar, tmp_path):
    from decaf.pipeline import _decompile_batch

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    real_add_tree = ctx.writer.add_tree
    calls = [0]

    def flaky_add_tree(tree, rel):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError(28, "No space left on device")
        return real_add_tree(tree, rel)

    ctx.writer.add_tree = flaky_add_tree
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    outcomes = {r.rel: r.outcome for r in done}
    assert outcomes == {"a.jar": "failed", "b.jar": "ok"}
    failed = next(r for r in done if r.rel == "a.jar")
    assert "No space left on device" in (failed.failure or "")


def test_decompile_batch_retries_a_stubbed_member_class(make_jar, tmp_path):
    from decaf.engines import EngineResult
    from decaf.pipeline import _decompile_batch

    vf_stub = (
        "class A {\n   void m() {\n      // $VF: Couldn't be decompiled\n"
        "      // Please report this to the Vineflower issue tracker, at https://github.com/Vineflower/vineflower/issues"
        " with a copy of the class file (if you have the rights to distribute it!)\n"
        "      // java.lang.NullPointerException: boom\n   }\n}\n"
    )

    def stubbing_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        (Path(dest) / "com/a/A.java").write_text(vf_stub)
        return res

    def clean_retry(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        out = Path(dest) / "com/a/A.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"// {spec.name}\nclass A {{}}\n")
        return EngineResult(spec.name, 0, False, 1, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, stubbing_batch, chain=["vineflower", "cfr"])
    ctx.runner = clean_retry
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    a, b = done
    assert [(x.engine, x.level, x.stubbed) for x in a.attempts] == [
        ("vineflower", "batch", 1),
        ("cfr", "class", 0),
    ]
    assert a.stubs == [] and not a.partial and a.java_files == 1
    assert [(x.level, x.stubbed) for x in b.attempts] == [("batch", 0)]
    out = tmp_path / "out" / "src"
    assert (out / "com/a/A.java").read_text() == "// cfr\nclass A {}\n"
    assert (out / "com/b/B.java").read_text() == "class X {}\n"


def test_decompile_batch_flags_a_member_with_an_out_of_memory_stub(make_jar, tmp_path):
    from decaf.engines import EngineResult
    from decaf.pipeline import _decompile_batch

    oom_stub = (
        "class A {\n   void m() {\n      // $VF: Couldn't be decompiled\n"
        "      // Please report this to the Vineflower issue tracker, at https://github.com/Vineflower/vineflower/issues"
        " with a copy of the class file (if you have the rights to distribute it!)\n"
        "      // java.lang.OutOfMemoryError: Java heap space\n   }\n}\n"
    )

    def stubbing_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        (Path(dest) / "com/a/A.java").write_text(oom_stub)  # out of heap inside one method of a.jar
        return res

    def clean_retry(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        out = Path(dest) / "com/a/A.java"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"// {spec.name}\nclass A {{}}\n")
        return EngineResult(spec.name, 0, False, 1, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, stubbing_batch, chain=["vineflower", "cfr"])
    ctx.runner = clean_retry
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    a, b = done
    assert [(x.engine, x.level, x.out_of_memory) for x in a.attempts] == [
        ("vineflower", "batch", True),
        ("cfr", "class", False),
    ]
    assert [(x.level, x.out_of_memory) for x in b.attempts] == [("batch", False)]  # its JVM was the same


def test_decompile_batch_passes_one_slot_of_heap(make_jar, tmp_path):
    from decaf import memory
    from decaf.pipeline import _decompile_batch

    seen = {}

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        seen["heap"] = heap_mb
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, batch)
    ctx.memory_plan = memory.plan(8000, 4)
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == [] and seen["heap"] == 1660
    assert [r.attempts[0].heap_mb for r in done] == [1660, 1660]


def test_decompile_batch_gives_the_checker_one_slot_of_heap(make_jar, tmp_path):
    from decaf import memory
    from decaf.pipeline import _decompile_batch

    heaps = []

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None, heap_mb=None):
        return merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)

    def checker(jobs, *, timeout, cpu_budget, heap_mb=None):
        heaps.append(heap_mb)
        return [[check.FileResult("clean") for _ in job.files] for job in jobs]

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, batch)
    ctx.checker = checker
    ctx.memory_plan = memory.plan(8000, 4)
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == [] and heaps == [1660]  # one checker JVM for the batch, with the heap of the batch JVM


def test_decompile_batch_sends_a_check_event_for_each_member_with_files_to_check(make_jar, tmp_path):
    """#101: the display shows the check of a batch; a member with only Kotlin files has nothing to check."""
    from decaf.pipeline import _decompile_batch

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        (Path(dest) / "com/b/B.java").rename(Path(dest) / "com/b/B.kt")
        return res

    def checker(jobs, *, timeout, cpu_budget):
        return [[check.FileResult("clean") for _ in job.files] for job in jobs]

    events: list[tuple[str, str]] = []
    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, batch, on_event=lambda k, s, d: events.append((s, d)) if k == "decompile" else None)
    ctx.checker = checker
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == [] and events == [
        ("a.jar", "vineflower · batch of 2 · 1 classes"), ("b.jar", "vineflower · batch of 2 · 1 classes"),
        ("a.jar", "vineflower · check 1 files"),
    ]


def test_decompile_batch_empty_member_file_requeues(make_jar, tmp_path):
    from decaf.pipeline import _decompile_batch

    def emptying_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        (Path(dest) / "com/a/A.java").write_text("")  # a whole-class failure in the batch
        return res

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, emptying_batch)
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert [r.rel for r in done] == ["b.jar"]
    assert [a.rel for a, _, _ in requeue] == ["a.jar"]  # no output: it redoes solo


def test_decompile_batch_counts_an_empty_file_as_a_class_that_its_engine_wrote_apart(make_jar, tmp_path):
    """#159: Fernflower failed A$1 as a whole and wrote an empty file of it (#96), so its flagged A does not hold
    A$1. Thus the retry file of A$1 fills the gap, as in a run without a batch."""
    from decaf.engines import EngineResult
    from decaf.pipeline import _decompile_batch

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        (Path(dest) / "com/a/A.java").write_text("class A {} // FLAG ff")
        (Path(dest) / "com/a/A$1.java").write_text("")
        return res

    def retry(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        for name, text in (("A", "class A {} // FLAG vf"), ("A$1", "class A$1 { /* vf */ }")):
            out = Path(dest) / f"com/a/{name}.java"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
        return EngineResult(spec.name, 0, False, 2, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": _class_header(52), "com/a/A$1.class": _class_header(52)})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, batch, chain=["fernflower", "vineflower"])
    ctx.runner = retry
    ctx.checker = marker_checker()
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    out = tmp_path / "out" / "src"
    assert (out / "com/a/A.java").read_text() == "class A {} // FLAG ff"
    assert (out / "com/a/A$1.java").read_text() == "class A$1 { /* vf */ }"


def test_decompile_batch_inner_class_rides_with_outer(make_jar, tmp_path):
    from decaf.engines import EngineResult
    from decaf.pipeline import _decompile_batch

    def inner_emitting_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        dest = Path(dest)
        (dest / "com/a").mkdir(parents=True, exist_ok=True)
        (dest / "com/a/A.java").write_text("class A {}")
        (dest / "com/a/A$1.java").write_text("class A$1 {}")  # inner: a claim of a.jar (#106)
        (dest / "com/zz").mkdir(parents=True, exist_ok=True)
        (dest / "com/zz/Stray.java").write_text("class Stray {}")  # no member owns this
        return EngineResult(spec.name, 0, False, 3, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x", "com/a/A$1.class": b"y"})
    ctx = _batch_ctx(tmp_path, inner_emitting_batch)
    done, requeue = _decompile_batch([m1], ctx)
    assert requeue == [] and done[0].outcome == "ok"
    out = tmp_path / "out" / "src"
    assert (out / "com/a/A.java").is_file()
    assert (out / "com/a/A$1.java").is_file()      # the inner class stays with its member
    assert not (out / "com/zz/Stray.java").exists()  # unmatched output dropped


def test_decompile_batch_drops_unmatched_split_output(make_jar, tmp_path):
    """Same split mechanism as the inner-class test, isolated to the
    no-owner-stem case: a stray file with no matching member must never land
    in any output tree, even when it's the only extra file the engine wrote."""
    from decaf.engines import EngineResult
    from decaf.pipeline import _decompile_batch

    def stray_emitting_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        dest = Path(dest)
        (dest / "com/a").mkdir(parents=True, exist_ok=True)
        (dest / "com/a/A.java").write_text("class A {}")
        (dest / "com/zz").mkdir(parents=True, exist_ok=True)
        (dest / "com/zz/Stray.java").write_text("class Stray {}")  # no member owns this
        return EngineResult(spec.name, 0, False, 2, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    ctx = _batch_ctx(tmp_path, stray_emitting_batch)
    done, requeue = _decompile_batch([m1], ctx)
    assert requeue == [] and done[0].outcome == "ok"
    out = tmp_path / "out" / "src"
    assert (out / "com/a/A.java").is_file()
    assert not (out / "com/zz/Stray.java").exists()  # unmatched output dropped


def test_no_engine_output_goes_out_after_ctrl_c(make_jar, tmp_path: Path):
    """#112: a killed engine can leave a cut-off file; after Ctrl-C no engine output reaches the writer."""
    from decaf.engines import PROCESSES

    jar = make_jar("app.jar", {"com/x/A.class": b"a"})

    def killed_engine(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        (Path(dest) / "com/x").mkdir(parents=True)
        (Path(dest) / "com/x/A.java").write_text("class A {")
        PROCESSES.kill_all()  # Ctrl-C lands while the engine runs
        return EngineResult(spec.name, -9, False, 1, "")

    ctx = make_ctx(tmp_path, killed_engine)
    try:
        report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    finally:
        PROCESSES.reset()
    assert not (tmp_path / "out/src/com/x/A.java").exists()
    assert (report.outcome, report.failure) == ("failed", "interrupted")
    assert [a.engine for a in report.attempts] == ["vineflower"]


def test_decompile_batch_writes_nothing_after_ctrl_c(make_jar, tmp_path):
    from decaf.engines import PROCESSES
    from decaf.pipeline import _decompile_batch

    def killed_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout)
        PROCESSES.kill_all()  # Ctrl-C lands while the batch runs
        return res

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, killed_batch)
    try:
        done, requeue = _decompile_batch([m1, m2], ctx)
    finally:
        PROCESSES.reset()
    assert done == [] and [a.rel for a, _, _ in requeue] == ["a.jar", "b.jar"]
    assert not list((tmp_path / "out").rglob("*.java"))


def test_decompile_batch_contains_a_full_disk(make_jar, tmp_path, monkeypatch):
    """#112: an OSError from the batch folder requeues the members; it does not end the run."""
    import errno

    from decaf import pipeline

    def no_space(ctx):
        raise OSError(errno.ENOSPC, "No space left on device")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    monkeypatch.setattr(pipeline, "_tmp_dir", no_space)
    done, requeue = pipeline._decompile_batch([m1, m2], ctx)
    assert done == [] and [a.rel for a, _, _ in requeue] == ["a.jar", "b.jar"]


def test_stems_and_claims_split_metadata_from_classes(make_jar, tmp_path):
    from decaf.pipeline import _stems_and_claims

    jar = make_jar(
        "m.jar",
        {
            "com/a/A.class": b"x",
            "com/a/A$1.class": b"i",
            "com/a/package-info.class": b"p",
            "module-info.class": b"m",
            "META-INF/versions/9/module-info.class": b"m9",
        },
    )
    stems, claims = _stems_and_claims(jar)
    assert stems == {"com/a/A"}
    # The versioned entry normalizes onto the same root claim; the inner class is a claim (#106).
    assert claims == {"com/a/A$1", "com/a/package-info", "module-info"}
    assert expected_class_stems(jar) == stems


def test_stems_and_claims_keep_top_level_dollar_classes(make_jar, tmp_path):
    """#106: a $ in a name does not make an inner class; the outer class file must exist."""
    from decaf.pipeline import _stems_and_claims

    jar = make_jar("d.jar", {
        "com/a/A.class": b"x",
        "com/a/A$1.class": b"i",
        "com/a/$Util.class": b"u",
        "com/a/$Util$In.class": b"ui",
        "com/a/Gone$In.class": b"o",  # an orphan: the jar has no com/a/Gone.class
    })
    stems, claims = _stems_and_claims(jar)
    assert stems == {"com/a/A", "com/a/$Util", "com/a/Gone$In"}  # the engines write a file for each
    assert claims == {"com/a/A$1", "com/a/$Util$In"}


def _named_inner_classes() -> dict[str, bytes]:
    """Classes with an outer class by name. Foo$ does not list itself in its InnerClasses attribute, as the module
    class of a Scala object: it is a top-level class for the JVM. A$1 lists itself, and A names it. S$B lists itself,
    but S does not name it: S was compiled again without it, so it is stale and top-level (#207). K creates the
    Kotlin class K$f$$inlined$g$1, and E lists E$1, which does not list itself: they are inner classes. The file of
    Cut$1 is cut, and Big$1 is above the size limit of a class file (the test sets the limit): decaf cannot read
    them."""
    from test_check import _class_with, _class_with_inner_classes

    inner = _class_with_inner_classes("com/a/A$1", (("com/a/A$1", "com/a/A"),))
    inlined = "com/a/K$f$$inlined$g$1"
    return {
        "com/a/A.class": _class_with("com/a/A", interfaces=("com/a/A$1",)), "com/a/A$1.class": inner,
        "com/a/S.class": _class_with("com/a/S"),
        "com/a/S$B.class": _class_with_inner_classes("com/a/S$B", (("com/a/S$B", "com/a/S"),)),
        "com/a/K.class": _class_with("com/a/K", interfaces=(inlined,)),
        f"{inlined}.class": _class_with_inner_classes(inlined, ((inlined, None),), enclosing="com/a/K"),
        "com/a/E.class": _class_with_inner_classes("com/a/E", (("com/a/E$1", None),)),
        "com/a/E$1.class": _class_with("com/a/E$1"),
        "com/a/Foo.class": _class_with("com/a/Foo"), "com/a/Foo$.class": _class_with("com/a/Foo$"),
        "com/a/Cut.class": _class_with("com/a/Cut"), "com/a/Cut$1.class": _class_with("com/a/Cut$1")[:12],
        "com/a/Big.class": _class_with("com/a/Big"), "com/a/Big$1.class": _class_with("com/a/Big$1") + bytes(100),
        "com/a/package-info.class": _class_with("com/a/package-info"), "module-info.class": _class_with("module-info"),
    }


@pytest.mark.parametrize("folder", [False, True])
def test_stems_and_claims_expect_a_class_that_is_top_level_for_the_jvm(make_jar, tmp_path, monkeypatch, folder):
    """Foo$ does not list itself, so CFR and Procyon write it only from its own class file, and it is expected. Before,
    it was a claim, so a lost Foo$ was never missing, retried, or counted (#206). The same for the stale S$B (#207).
    The classes of the family that hold a class, and a class file that decaf cannot read, keep it a claim, as
    module-info and package-info are."""
    from decaf import pipeline

    entries = _named_inner_classes()
    if folder:
        source = tmp_path / "classes"
        for name, data in entries.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_bytes(data)
    else:
        source = make_jar("t.jar", entries)
    monkeypatch.setattr(pipeline, "_CLASS_FILE_MAX", len(entries["com/a/Big$1.class"]) - 1)
    stems, claims = pipeline._stems_and_claims(source)
    assert stems == {"com/a/A", "com/a/Foo", "com/a/Foo$", "com/a/Cut", "com/a/Big", "com/a/S", "com/a/S$B",
                     "com/a/K", "com/a/E"}
    assert claims == {"com/a/A$1", "com/a/Cut$1", "com/a/Big$1", "com/a/package-info", "module-info",
                      "com/a/K$f$$inlined$g$1", "com/a/E$1"}


def _inner(name: str, outer: str | None, enclosing: str | None = None, lists=(), refs=()) -> bytes:
    """A class that lists itself with this outer class (None: an anonymous or local class); lists: more entries
    (inner class, outer class) of its InnerClasses attribute; refs: more Class entries in its constant pool."""
    from test_check import _class_with_inner_classes

    return _class_with_inner_classes(name, ((name, outer), *lists), enclosing=enclosing, refs=tuple(refs))


def _lister(name: str, lists=(), refs=()) -> bytes:
    """A class that does not list itself: a top-level class that lists the entries (inner class, outer class)."""
    from test_check import _class_with, _class_with_inner_classes

    if not lists:
        return _class_with(name, interfaces=tuple(refs))
    return _class_with_inner_classes(name, tuple(lists), refs=tuple(refs))


def _parents(make_jar, classes: dict[str, bytes], families=None) -> dict[str, str | None]:
    from decaf.pipeline import _class_parents

    return _class_parents(make_jar("t.jar", {f"{n}.class": data for n, data in classes.items()}), families)


def test_class_parents_of_a_class_file_that_cannot_be_read_follow_the_name(make_jar):
    """Rule 1: no fact from the class file, so the parent is the longest $-prefix that is a class, as before."""
    assert _parents(make_jar, {"p/A": _lister("p/A"), "p/A$B": b"x", "p/A$B$C": b"x", "p/A$1": b"x"}) == {
        "p/A$B": "p/A", "p/A$B$C": "p/A$B", "p/A$1": "p/A",
    }


def test_class_parents_of_a_class_that_does_not_list_itself(make_jar):
    """Rule 2: no class of the family lists it: top-level (#206). A class that lists it is its parent (S8), and the
    outer class of that entry comes first; else the longest lister that is a $-prefix, else the first lister."""
    assert _parents(make_jar, {"X": _lister("X"), "X$": _lister("X$")}) == {"X$": None}
    s8 = {"X": _lister("X", lists=(("X$$anon$1", None),)), "X$": _lister("X$"), "X$$anon$1": _lister("X$$anon$1")}
    assert _parents(make_jar, s8)["X$$anon$1"] == "X"
    by_entry = {"p/A": _lister("p/A"), "p/A$C": _inner("p/A$C", "p/A", lists=(("p/A$B", "p/A"),)),
                "p/A$B": _lister("p/A$B")}
    assert _parents(make_jar, by_entry)["p/A$B"] == "p/A"  # the entry in A$C names A as its outer class
    first = {"p/A": _lister("p/A"), "p/A$B": _inner("p/A$B", "p/A", lists=(("p/A$1", None),)),
             "p/A$1": _lister("p/A$1")}
    assert _parents(make_jar, first)["p/A$1"] == "p/A$B"  # A$B is no $-prefix of A$1: the first lister


def test_class_parents_of_a_member_class(make_jar):
    """Rule 3: the outer class of its own entry, when that class names it. Else it is stale: the outer class was
    compiled again without it, and only Vineflower and Fernflower write it (#207)."""
    member = _inner("p/A$B", "p/A")
    assert _parents(make_jar, {"p/A": _lister("p/A", lists=(("p/A$B", "p/A"),)), "p/A$B": member}) == {"p/A$B": "p/A"}
    assert _parents(make_jar, {"p/A": _lister("p/A"), "p/A$B": member}) == {"p/A$B": None}
    other = _inner("p/A$B", "q/Other")  # its outer class is no class of the family
    assert _parents(make_jar, {"p/A": _lister("p/A", refs=("p/A$B",)), "p/A$B": other}) == {"p/A$B": None}


@pytest.mark.parametrize("enclosing, namers, parent", [
    ("X", ("X", "X$"), "X"),  # S4 and S5 with the EnclosingMethod class (#208)
    ("X$", ("X", "X$"), "X$"),
    (None, ("X",), "X"),  # S6: no EnclosingMethod attribute
    (None, ("X", "X$"), "X$"),  # S7: the longest class that names it
    ("X$", ("X",), "X"),  # the EnclosingMethod class does not name it
    (None, (), None),  # stale
])
def test_class_parents_of_an_anonymous_class(make_jar, enclosing, namers, parent):
    """Rule 4: of the classes that name it, the EnclosingMethod class, else the longest $-prefix, else the first."""
    classes = {n: _lister(n, lists=(("X$$anon$1", None),) if n in namers else ()) for n in ("X", "X$")}
    classes["X$$anon$1"] = _inner("X$$anon$1", None, enclosing=enclosing)
    assert _parents(make_jar, classes) == {"X$": None, "X$$anon$1": parent}


def test_class_parents_of_a_kotlin_inlined_class_and_a_class_named_only_below_it(make_jar):
    """A Kotlin K$foo$$inlined$bar$1 lists itself, no class lists it, and K creates it: K names it. A class below
    a class by name is no proof for it."""
    inlined = "p/K$foo$$inlined$bar$1"
    kotlin = {"p/K": _lister("p/K", refs=(inlined,)), inlined: _inner(inlined, None)}
    assert _parents(make_jar, kotlin) == {inlined: "p/K"}
    below = {"p/A": _lister("p/A"), "p/A$1": _inner("p/A$1", None),
             "p/A$1$1": _inner("p/A$1$1", "p/A$1", refs=("p/A$1",))}
    assert _parents(make_jar, below)["p/A$1"] is None
    named = {"p/A": _lister("p/A"), "p/A$B": _inner("p/A$B", "p/A", refs=("p/A$1",)), "p/A$1": _inner("p/A$1", None)}
    assert _parents(make_jar, named)["p/A$1"] == "p/A$B"  # A$B is no $-prefix of A$1: the first class that names it


def test_class_parents_read_only_the_families_asked_for(make_jar, tmp_path):
    """A class folder above its package root (#199): the stems keep the folder, the class files name the classes."""
    from decaf.pipeline import _class_parents

    classes = {"p/A": _lister("p/A"), "p/A$B": _inner("p/A$B", "p/A"), "q/C": _lister("q/C"), "q/C$": _lister("q/C$")}
    jar = make_jar("t.jar", {f"{n}.class": d for n, d in classes.items()})
    assert _class_parents(jar, {"q/C"}) == {"q/C$": None}
    root = tmp_path / "in"
    for n, d in classes.items():
        (root / "target/classes" / f"{n}.class").parent.mkdir(parents=True, exist_ok=True)
        (root / "target/classes" / f"{n}.class").write_bytes(d)
    assert _class_parents(root) == {"target/classes/p/A$B": None, "target/classes/q/C$": None}
    classes["p/A"] = _lister("p/A", lists=(("p/A$B", "p/A"),))
    (root / "target/classes/p/A.class").write_bytes(classes["p/A"])
    assert _class_parents(root)["target/classes/p/A$B"] == "target/classes/p/A"


def test_stems_and_claims_by_name_only_read_no_class_file(make_jar, monkeypatch):
    """The scheduler forms batches with the rule by name only; the union of the sets is the same (#206)."""
    from test_check import _class_with

    from decaf.pipeline import _stems_and_claims

    jar = make_jar("t.jar", {"p/Foo.class": _class_with("p/Foo"), "p/Foo$.class": _class_with("p/Foo$")})
    assert _stems_and_claims(jar) == ({"p/Foo", "p/Foo$"}, set())
    monkeypatch.setattr(check, "lists_itself", lambda data: pytest.fail("a class file was read"))
    assert _stems_and_claims(jar, read=False) == ({"p/Foo"}, {"p/Foo$"})


def test_stems_and_claims_keep_an_inner_class_whose_entry_is_damaged(make_damaged_jar):
    from test_check import _class_with

    from decaf.pipeline import _stems_and_claims

    jar = make_damaged_jar("t.jar", {"p/Foo.class": _class_with("p/Foo"), "p/Foo$.class": _class_with("p/Foo$")},
                           damaged=("p/Foo$.class",))
    assert _stems_and_claims(jar) == ({"p/Foo"}, {"p/Foo$"})


@pytest.mark.parametrize("top", ["p/Foo$.class", "META-INF/versions/9/p/Foo$.class"])
def test_stems_and_claims_expect_a_multi_release_class_when_one_entry_is_top_level_for_the_jvm(make_jar, top):
    """One entry of Foo$ lists itself, and the other entry does not: Foo$ is expected (#206)."""
    from test_check import _class_with, _class_with_inner_classes

    from decaf.pipeline import _stems_and_claims

    inner = _class_with_inner_classes("p/Foo$", (("p/Foo$", "p/Foo"),))
    jar = make_jar("mr.jar", {
        "p/Foo.class": _class_with("p/Foo"),
        **{name: _class_with("p/Foo$") if name == top else inner
           for name in ("p/Foo$.class", "META-INF/versions/9/p/Foo$.class")},
        "p/Bar.class": _class_with("p/Bar", interfaces=("p/Bar$1",)),  # Bar creates Bar$1 (#207)
        "META-INF/versions/9/p/Bar$1.class": _class_with_inner_classes("p/Bar$1", (("p/Bar$1", None),)),
    })
    assert _stems_and_claims(jar) == ({"p/Foo", "p/Foo$", "p/Bar"}, {"p/Bar$1"})


@pytest.mark.parametrize("missing, expected", [
    ({"p/Foo$"}, ["p/Foo$.class", "p/Foo$1.class", "p/Foo.class"]),  # the engines get every file that they can need
    ({"p/Foo"}, ["p/Foo$.class", "p/Foo$1.class", "p/Foo.class"]),
    ({"p/Bar"}, ["p/Bar.class"]),
    ({"p/Foo$", "p/V$"}, ["META-INF/versions/11/p/V$.class", "META-INF/versions/11/p/V.class", "p/Foo$.class",
                          "p/Foo$1.class", "p/Foo.class"]),  # the lowest version of each family by name (#107)
])
def test_a_class_retry_of_a_class_that_is_top_level_for_the_jvm_gets_its_family_by_name(
    make_jar, tmp_path: Path, missing, expected,
):
    """A missing Foo$ brings the class files of the name family of Foo into the retry tree (#206)."""
    from decaf.pipeline import _extract_failed_classes

    jar = make_jar("t.jar", {n: b"c" for n in (
        "p/Foo.class", "p/Foo$.class", "p/Foo$1.class", "p/Bar.class", "META-INF/versions/9/p/Foo$.class",
        "META-INF/versions/11/p/V.class", "META-INF/versions/11/p/V$.class", "META-INF/versions/17/p/V$.class",
    )})
    out = _extract_failed_classes(jar, missing, make_ctx(tmp_path, writing_runner({})))
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.class")) == expected


def _foo_folder(tmp_path: Path) -> Path:
    """A class folder of p/Foo with its inner class Foo$1, the class Foo$ that is top-level for the JVM, and p/Bar."""
    from test_check import _class_with, _class_with_inner_classes

    root = tmp_path / "in"
    (root / "p").mkdir(parents=True)
    for name in ("Foo$", "Bar"):
        (root / f"p/{name}.class").write_bytes(_class_with(f"p/{name}"))
    (root / "p/Foo.class").write_bytes(_class_with("p/Foo", interfaces=("p/Foo$1",)))  # Foo creates Foo$1 (#207)
    (root / "p/Foo$1.class").write_bytes(_class_with_inner_classes("p/Foo$1", (("p/Foo$1", None),)))
    return root


def _cfr_like(monkeypatch, skip: dict[str, set[str]] | None = None) -> list[list[str]]:
    """CFR and Procyon on class files: a file for each class file that they get as an argument, at the path of its
    class name; an inner class rides along. skip: the classes of which an engine writes no file. Returns the
    names of the class files of each JVM."""
    from decaf import engines

    seen: list[list[str]] = []

    def build(spec, jar_path, targets, dest, **kw):
        return [spec.name, str(dest), *map(str, targets)]

    def run(spec, cmd, timeout, on_stderr_line):
        seen.append(sorted(Path(t).name for t in cmd[2:]))
        for t in cmd[2:]:
            name = check.class_name(Path(t).read_bytes())
            if name not in (skip or {}).get(cmd[0], ()):
                (Path(cmd[1]) / f"{name}.java").parent.mkdir(parents=True, exist_ok=True)
                (Path(cmd[1]) / f"{name}.java").write_text(f"class X {{}} // {cmd[0]}")
        return EngineResult(spec.name, 0, False, 0, "")

    monkeypatch.setattr(engines, "build_batch_command", build)
    monkeypatch.setattr(engines, "_exec_command", run)
    return seen


def test_a_class_folder_gives_its_own_file_to_a_class_that_is_top_level_for_the_jvm(tmp_path: Path, monkeypatch):
    """Before, CFR got no class file of Foo$, so no file of it came out, and the row had 2 classes and no missing
    class (#206)."""
    from decaf import engines

    seen = _cfr_like(monkeypatch)
    ctx = make_ctx(tmp_path, engines.run_engine, engine="cfr", fallback=False)
    report, _ = process_artifact(Artifact(_foo_folder(tmp_path), "_classes", ArtifactKind.CLASS_TREE), ctx)
    assert seen == [["Bar.class", "Foo$.class", "Foo.class"]]
    assert sorted(p.name for p in (tmp_path / "out/src/p").iterdir()) == ["Bar.java", "Foo$.java", "Foo.java"]
    assert (report.classes, report.java_files, report.missing_classes) == (3, 3, 0)


@pytest.mark.parametrize("fallback, missing", [(False, 1), (True, 0)])
def test_a_class_that_is_top_level_for_the_jvm_and_has_no_file_is_missing_and_retried(
    tmp_path: Path, monkeypatch, fallback, missing,
):
    """CFR writes no file of Foo$, as JD does for some such classes. The row shows it as missing, and a class retry of
    the next engine gets the class files of the family of Foo. Before, the loss had no sign (#206)."""
    from decaf import engines

    seen = _cfr_like(monkeypatch, skip={"cfr": {"p/Foo$"}})
    ctx = make_ctx(tmp_path, engines.run_engine, engine="cfr", fallback=fallback)
    ctx.chain = ["cfr", "procyon"] if fallback else ["cfr"]
    report, _ = process_artifact(Artifact(_foo_folder(tmp_path), "_classes", ArtifactKind.CLASS_TREE), ctx)
    assert (report.classes, report.java_files, report.missing_classes) == (3, 3 - missing, missing)
    assert [(a.engine, a.level) for a in report.attempts] == [("cfr", "archive"), ("procyon", "class")][: 1 + fallback]
    assert seen[1:] == ([["Foo$.class", "Foo.class"]] if fallback else [])
    if fallback:
        assert (tmp_path / "out/src/p/Foo$.java").read_text() == "class X {} // procyon"


@pytest.mark.parametrize("writes, missing", [(True, 0), (False, 1)])
def test_a_batch_member_counts_its_class_that_is_top_level_for_the_jvm(make_jar, tmp_path, writes, missing):
    """The batch gives the file of Foo$ to its member, and the row counts Foo$ (#206)."""
    from test_check import _class_with, _class_with_inner_classes

    from decaf.pipeline import _decompile_batch

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        for t in targets:
            with zipfile.ZipFile(t) as zf:
                for entry in zf.namelist():
                    if check.lists_itself(zf.read(entry)) or (entry == "p/Foo$.class" and not writes):
                        continue  # an inner class rides with its outer class
                    (Path(dest) / entry).with_suffix(".java").parent.mkdir(parents=True, exist_ok=True)
                    (Path(dest) / entry).with_suffix(".java").write_text("class X {}")
        return EngineResult(spec.name, 0, False, 0, "")

    m1 = _member(make_jar, "a.jar", {
        "p/Foo.class": _class_with("p/Foo", interfaces=("p/Foo$1",)), "p/Foo$.class": _class_with("p/Foo$"),
        "p/Foo$1.class": _class_with_inner_classes("p/Foo$1", (("p/Foo$1", None),)),
    })
    m2 = _member(make_jar, "b.jar", {"q/B.class": _class_with("q/B")})
    done, requeue = _decompile_batch([m1, m2], _batch_ctx(tmp_path, batch))
    assert requeue == []
    assert [(r.rel, r.classes, r.java_files, r.missing_classes) for r in done] == [
        ("a.jar", 2, 2 - missing, missing), ("b.jar", 1, 1, 0),
    ]


def test_decompile_batch_keeps_dollar_classes_with_their_member(make_jar, tmp_path):
    """#106: the split gives a $ class to the member that has its class file, and drops none."""
    from decaf.pipeline import MirrorWriter, _decompile_batch

    def dollar_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        for rel in ("com/a/A.java", "com/a/$Util.java", "q/Foo$Bar.java", "q/Foo.java"):
            p = Path(dest) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("class X {}")
        return EngineResult(spec.name, 0, False, 4, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x", "com/a/$Util.class": b"u", "q/Foo$Bar.class": b"o"})
    m2 = _member(make_jar, "b.jar", {"q/Foo.class": b"f"})
    ctx = _batch_ctx(tmp_path, dollar_batch)
    ctx.writer = MirrorWriter(tmp_path / "out")
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    out = tmp_path / "out"
    assert (out / "a.jar/com/a/$Util.java").is_file()
    assert (out / "a.jar/q/Foo$Bar.java").is_file()  # an orphan: not the class of b.jar's q/Foo
    assert not (out / "b.jar/q/Foo$Bar.java").exists()
    assert (out / "b.jar/q/Foo.java").is_file()
    assert [(r.rel, r.classes, r.java_files, r.missing_classes) for r in done] == [
        ("a.jar", 3, 3, 0), ("b.jar", 1, 1, 0),
    ]


def test_form_batch_defers_dollar_class_overlap(make_jar):
    """#106: two members with the same $ class would share one file in the batch dest."""
    from collections import deque

    from decaf.pipeline import _form_batch

    q = deque([
        _member(make_jar, "a.jar", {"p/$Util.class": b"1"}),
        _member(make_jar, "b.jar", {"p/$Util.class": b"2"}),
        _member(make_jar, "c.jar", {"p/A.class": b"3", "p/A$1.class": b"4"}),
        _member(make_jar, "d.jar", {"p/A$1.class": b"5"}),  # an orphan with the name of c.jar's inner class
    ])
    assert [m[0].rel for m in _form_batch(q)] == ["a.jar", "c.jar"]
    assert [m[0].rel for m in q] == ["b.jar", "d.jar"]


def test_form_batch_defers_an_orphan_inner_class_and_its_outer_class(make_jar):
    """Review A2: one engine JVM put the orphan q/Foo$Bar of a.jar into the file of q/Foo of b.jar."""
    from collections import deque

    from decaf.pipeline import _form_batch

    for outer, fold in (("q/Foo", False), ("q/FOO", True)):
        q = deque([
            _member(make_jar, "a.jar", {"q/Foo$Bar.class": b"1", "q/Other.class": b"2"}),  # no q/Foo in a.jar
            _member(make_jar, "b.jar", {f"{outer}.class": b"3"}),
            _member(make_jar, "c.jar", {"q/Baz.class": b"4"}),
        ])
        assert [m[0].rel for m in _form_batch(q, fold=fold)] == ["a.jar", "c.jar"]
        assert [m[0].rel for m in q] == ["b.jar"]


def test_class_level_retry_takes_dollar_classes(make_jar, tmp_path: Path):
    """#106: a missing $ class is a missing class; its retry tree has its inner classes."""
    jar = make_jar("app.jar", {
        "com/x/A.class": b"a", "com/x/$Util.class": b"u", "com/x/$Util$In.class": b"i",
    })
    runner = writing_runner({
        "vineflower": {"com/x/A.java": "class A {}"},  # $Util missing
        "cfr": {"com/x/$Util.java": "class $Util {}"},
    })
    ctx = make_ctx(tmp_path, runner)
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.classes, report.missing_classes) == (2, 0)
    assert [(a.engine, a.level) for a in report.attempts] == [("vineflower", "archive"), ("cfr", "class")]
    assert [Path(p).name for p in runner.sent[1]] == ["$Util$In.class", "$Util.class"]
    assert (tmp_path / "out/src/com/x/$Util.java").is_file()


def test_decompile_batch_attributes_package_info_to_claiming_member(make_jar, tmp_path):
    """#75: a member's own package-info.class claims the emitted source."""
    from decaf.pipeline import MirrorWriter, _decompile_batch

    m1 = _member(
        make_jar, "a.jar",
        {"org/alpha/Foo.class": b"x", "org/alpha/package-info.class": b"p"},
    )
    m2 = _member(make_jar, "b.jar", {"org/beta/Bar.class": b"y"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    ctx.writer = MirrorWriter(tmp_path / "out")
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    a = next(r for r in done if r.rel == "a.jar")
    assert a.outcome == "ok"
    assert (tmp_path / "out/a.jar/org/alpha/package-info.java").is_file()
    assert not (tmp_path / "out/b.jar/org/alpha/package-info.java").exists()
    assert a.java_files == 2  # Foo.java + package-info.java, same as a solo run
    assert a.classes == 1  # accounting stays class-only: claims never join expectations
    assert a.missing_classes == 0
    assert [at.level for at in a.attempts] == ["batch"]  # no retry fires for metadata


def test_decompile_batch_attributes_module_info_to_sole_claimant(make_jar, tmp_path):
    """#75: root-level module-info.java goes to the one member that has module-info.class."""
    from decaf.pipeline import MirrorWriter, _decompile_batch

    m1 = _member(make_jar, "a.jar", {"module-info.class": b"m", "com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    ctx.writer = MirrorWriter(tmp_path / "out")
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    a = next(r for r in done if r.rel == "a.jar")
    assert a.outcome == "ok"
    assert (tmp_path / "out/a.jar/module-info.java").is_file()
    assert not (tmp_path / "out/b.jar/module-info.java").exists()
    assert a.java_files == 2 and a.classes == 1 and a.missing_classes == 0


def test_damaged_resource_entry_costs_only_that_resource(make_damaged_jar, tmp_path: Path):
    """#109: resources must never taint the decompile outcome."""
    from decaf.pipeline import MirrorWriter

    jar = make_damaged_jar("app.jar", {"com/x/A.class": b"a" * 50, "res/a.txt": "a" * 100, "res/b.txt": "b" * 100},
                           damaged=("res/a.txt",))
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}),
                   writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert (tmp_path / "out/app.jar/res/b.txt").is_file() and not (tmp_path / "out/app.jar/res/a.txt").exists()


def test_damaged_class_entry_in_a_retry_stays_a_missing_class(make_damaged_jar, tmp_path: Path):
    """#109: the archive run wrote A; a damaged B costs only B."""
    jar = make_damaged_jar("app.jar", {"com/x/A.class": b"a" * 50, "com/x/B.class": b"b" * 50},
                           damaged=("com/x/B.class",))
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.missing_classes) == ("ok", 1)
    assert (tmp_path / "out/src/com/x/A.java").is_file()


def test_damaged_entry_of_an_input_sources_jar_is_a_missing_file(make_damaged_jar, tmp_path: Path):
    jar = make_damaged_jar("lib-sources.jar", {"com/x/A.java": "class A {}" * 10, "com/x/B.java": "class B {}" * 10},
                           damaged=("com/x/A.java",))
    ctx = make_ctx(tmp_path, writing_runner({}))
    report, _ = process_artifact(Artifact(jar, "lib-sources.jar", ArtifactKind.SOURCES_JAR), ctx)
    assert (report.outcome, report.method, report.java_files, report.missing_classes) == ("ok", "extracted", 1, 1)
    assert report.partial
    assert (tmp_path / "out/src/com/x/B.java").is_file()


def test_damaged_maven_sources_jar_falls_back_to_the_engines(make_jar, make_damaged_jar, tmp_path: Path):
    """#109: part of the upstream sources is no complete output; the engines decompile."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    sources = make_damaged_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}" * 10, "com/x/B.java": "class B {}" * 10},
                               damaged=("com/x/B.java",))

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources,
                          repo="https://r.test/m2", resolved_by="pom-properties")

    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}), resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method) == ("ok", "vineflower")
    assert report.sources_miss == "sources jar for com.example:lib:1.2 has a damaged entry: com/x/B.java"


def test_resolver_error_falls_back_to_the_engines(make_jar, tmp_path: Path):
    """#111: a read-only or full sources cache must not fail an artifact that the engines can decompile."""
    import errno

    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        raise OSError(errno.EROFS, "Read-only file system", str(cache_dir))

    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}), resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method) == ("ok", "vineflower")
    assert report.sources_miss.startswith("sources lookup failed: [Errno 30] Read-only file system")


def test_damaged_cached_sources_jar_is_dropped(make_jar, make_damaged_jar, tmp_path: Path):
    """#111: the next run downloads the sources jar again."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    cache = tmp_path / "cache"
    sources = make_damaged_jar("com.example_lib_1.2-sources.jar", {"com/x/A.java": "class A {}" * 10},
                               damaged=("com/x/A.java",), base=cache)
    marker = sources.with_suffix(".repo")
    marker.write_text("https://r.test/m2")

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources,
                          repo="https://r.test/m2", resolved_by="pom-properties", cached=True)

    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}), resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method) == ("ok", "vineflower")
    assert not sources.exists() and not marker.exists()


def test_cached_sources_jar_that_is_no_zip_is_named_and_dropped(make_jar, tmp_path: Path):
    """#180: the miss says why, and the next run downloads the sources jar again."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    sources = cache / "com.example_lib_1.2-sources.jar"
    sources.write_bytes(b"this is no zip")
    marker = sources.with_suffix(".repo")
    marker.write_text("https://r.test/m2")

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources,
                          repo="https://r.test/m2", resolved_by="pom-properties", cached=True)

    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}), resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method) == ("ok", "vineflower")
    assert report.sources_miss == "sources jar for com.example:lib:1.2 is not a zip"
    assert not sources.exists() and not marker.exists()


def test_maven_hit_mirror_carries_original_resources(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "META-INF/spring.factories": b"cfg"})
    sources = make_jar(
        "lib-1.2-sources.jar",
        {"com/x/A.java": "class A {}", "sql/schema.sql": "-- upstream copy"},
    )

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=sources,
            repo="https://r.test/m2",
            resolved_by="pom-properties",
        )

    ctx = make_ctx(tmp_path, writing_runner({}), resolver=resolver, maven=True,
                   writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok" and report.method == "maven"
    dest = tmp_path / "out/lib-1.2.jar"
    assert (dest / "META-INF/spring.factories").read_bytes() == b"cfg"
    assert (dest / "com/x/A.java").is_file()
    assert not (dest / "sql/schema.sql").exists()  # sources jar's resource copies are ignored
    assert report.resources_copied == 1
    assert report.resources_skipped == 0


def test_failed_decompile_still_writes_resources_in_mirror(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    jar = make_jar("app.jar", {"com/x/A.class": b"x", "cfg.properties": "k=v"})
    ctx = make_ctx(tmp_path, writing_runner({}), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "failed"
    assert (tmp_path / "out/app.jar/cfg.properties").is_file()
    assert report.resources_copied == 1


def test_resource_only_mirrored_ok(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    r = make_jar("r.jar", {"META-INF/MANIFEST.MF": "m"})
    ctx = make_ctx(tmp_path, writing_runner({}), writer=MirrorWriter(tmp_path / "out"))
    rep, _ = process_artifact(Artifact(r, "r.jar", ArtifactKind.RESOURCE_ONLY), ctx)
    assert rep.outcome == "ok"
    assert rep.resources_copied == 1
    assert (tmp_path / "out/r.jar/META-INF/MANIFEST.MF").is_file()


def test_kotlin_sources_jar_extracted(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    sj = make_jar("lib-sources.jar", {"com/x/A.kt": "fun a() {}", "META-INF/MANIFEST.MF": "m"})
    ctx = make_ctx(tmp_path, writing_runner({}), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(sj, "lib-sources.jar", ArtifactKind.SOURCES_JAR), ctx)
    assert report.outcome == "ok"
    assert report.method == "extracted"
    assert report.java_files == 1
    assert report.resources_copied == 1
    assert (tmp_path / "out/lib-sources.jar/com/x/A.kt").is_file()
    assert (tmp_path / "out/lib-sources.jar/META-INF/MANIFEST.MF").is_file()


def test_mixed_sources_jar_keeps_java_and_kt(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    sj = make_jar("lib-sources.jar", {"com/x/A.java": "class A {}", "com/x/B.kt": "fun b() {}"})
    ctx = make_ctx(tmp_path, writing_runner({}), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(sj, "lib-sources.jar", ArtifactKind.SOURCES_JAR), ctx)
    assert report.outcome == "ok"
    assert report.java_files == 2
    assert (tmp_path / "out/lib-sources.jar/com/x/A.java").is_file()
    assert (tmp_path / "out/lib-sources.jar/com/x/B.kt").is_file()


def test_maven_kotlin_sources_jar_used(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.kt": "// real source\nfun a() {}"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            sources_jar=sources,
            repo="https://r.test/m2",
            resolved_by="pom-properties",
        )

    runner = writing_runner({})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.method == "maven"
    assert runner.calls == []
    assert report.sources_miss is None
    assert "real source" in (tmp_path / "out/src/com/x/A.kt").read_text()


def _files(base: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return base


def test_sources_gap_pairs_classes_with_their_source_files(make_jar, class_file, tmp_path: Path):
    """#180: a class has a source when a file of its name, or the file of its SourceFile attribute, is there."""
    jar = make_jar("lib.jar", {
        "com/x/A.class": class_file("com/x/A", "A.java"),  # its own file
        "META-INF/versions/11/com/x/B.class": class_file("com/x/B"),  # a variant without the attribute
        "com/x/B.class": class_file("com/x/B", "A.java"),  # a secondary type of A.java: the base entry is read
        "com/x/UtilKt.class": class_file("com/x/UtilKt", "Util.kt"),  # a Kotlin file facade
        "g/G.class": class_file("g/G", "G.groovy"),  # written in Groovy
        "Top.class": class_file("Top", "Top.java"),  # the default package
        "com/x/Gen.class": class_file("com/x/Gen"),  # no attribute, no file: a gap
        "com/x/Lost.class": class_file("com/x/Lost", "Lost.java"),  # the attribute names no file: a gap
        "META-INF/versions/11/com/x/V.class": class_file("com/x/V", "V.java"),  # only a variant, no file: a gap
        "com/x/A$1.class": b"x",  # an inner class is no top-level class
        "module-info.class": b"x",
    })
    tree = _files(tmp_path / "tree", {
        "com/x/A.java": "class A {} class B {}", "com/x/Util.kt": "fun u() {}", "g/G.groovy": "class G {}",
        "Top.java": "class Top {}",
    })
    expected = expected_class_stems(jar)
    assert expected == {"com/x/A", "com/x/B", "com/x/UtilKt", "g/G", "Top", "com/x/Gen", "com/x/Lost", "com/x/V"}
    assert _sources_gap(jar, expected, tree) == {"com/x/Gen", "com/x/Lost", "com/x/V"}


def test_sources_gap_counts_a_class_entry_that_cannot_be_read(make_damaged_jar, class_file, tmp_path: Path):
    jar = make_damaged_jar("lib.jar", {"com/x/A.class": class_file("com/x/A", "A.java") * 4,
                                       "com/x/D.class": class_file("com/x/D", "A.java") * 4},
                           damaged=("com/x/D.class",))
    tree = _files(tmp_path / "tree", {"com/x/A.java": "class A {} class D {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"com/x/D"}


def test_sources_gap_needs_no_attribute_for_a_file_of_the_class_name(make_jar, class_file, tmp_path: Path):
    jar = make_jar("lib.jar", {"com/x/A.class": class_file("com/x/A")})  # no SourceFile attribute (javac -g:none)
    tree = _files(tmp_path / "tree", {"com/x/A.java": "class A {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_takes_groovy_and_scala_files_of_the_class_name(make_jar, class_file, tmp_path: Path):
    jar = make_jar("lib.jar", {"g/G.class": class_file("g/G"), "s/S.class": class_file("s/S")})
    tree = _files(tmp_path / "tree", {"g/G.groovy": "class G {}", "s/S.scala": "class S"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_reads_the_base_entry_when_a_variant_comes_after_it(make_jar, class_file, tmp_path: Path):
    jar = make_jar("lib.jar", {
        "com/x/B.class": class_file("com/x/B", "A.java"),  # a secondary type of A.java
        "META-INF/versions/11/com/x/B.class": class_file("com/x/B"),  # a variant without the attribute
    })
    tree = _files(tmp_path / "tree", {"com/x/A.java": "class A {} class B {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_takes_the_attribute_file_in_the_default_package(make_jar, class_file, tmp_path: Path):
    jar = make_jar("lib.jar", {"TopKt.class": class_file("TopKt", "Top.kt")})  # a Kotlin file facade
    tree = _files(tmp_path / "tree", {"Top.kt": "fun top() {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_reads_the_other_classes_after_a_damaged_entry(make_damaged_jar, class_file, tmp_path: Path):
    jar = make_damaged_jar("lib.jar", {"com/x/D.class": class_file("com/x/D", "A.java") * 4,
                                       "com/x/E.class": class_file("com/x/E", "A.java") * 4},
                           damaged=("com/x/D.class",))
    tree = _files(tmp_path / "tree", {"com/x/A.java": "class A {} class D {} class E {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"com/x/D"}


def test_sources_gap_counts_the_uncovered_classes_of_an_archive_that_cannot_be_read(tmp_path: Path):
    binary = tmp_path / "lib.jar"
    binary.write_bytes(b"not a zip")
    tree = _files(tmp_path / "tree", {"com/x/A.java": "class A {}"})
    assert _sources_gap(binary, {"com/x/A", "com/x/B"}, tree) == {"com/x/B"}


def test_sources_gap_takes_a_file_at_its_declared_package(make_jar, tmp_path: Path):
    """#180: okio-jvm and kotlin-stdlib keep their files under jvmMain/..., not at the package path."""
    jar = make_jar("lib.jar", {"okio/Buffer.class": b"x"})
    tree = _files(tmp_path / "tree", {"jvmMain/okio/Buffer.kt": "package okio\n\nclass Buffer"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_takes_a_java_file_under_a_source_root(make_jar, tmp_path: Path):
    jar = make_jar("lib.jar", {"com/x/A.class": b"x", "Main.class": b"x"})
    tree = _files(tmp_path / "tree", {
        "src/main/java/com/x/A.java": "/*\n * license\n */\npackage com.x;\n\nclass A {}",
        "src/Main.java": "class Main {}",  # no package line: its package is not sure, so Main stays a gap
    })
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"Main"}


def test_sources_gap_takes_the_attribute_file_at_its_declared_package(make_jar, class_file, tmp_path: Path):
    jar = make_jar("lib.jar", {"demo/Lazy.class": b"x", "demo/LazyKt.class": class_file("demo/LazyKt", "Lazy.kt")})
    tree = _files(tmp_path / "tree", {"commonMain/kotlin/util/Lazy.kt": '@file:JvmName("LazyKt")\npackage demo\n\nclass Lazy'})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_ignores_a_package_word_in_a_comment(make_jar, tmp_path: Path):
    jar = make_jar("lib.jar", {"com/x/A.class": b"x"})
    tree = _files(tmp_path / "tree", {"other/A.java": "// package com.x\nclass A {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"com/x/A"}


def test_sources_gap_reads_the_package_line_after_a_byte_order_mark(make_jar, tmp_path: Path):
    jar = make_jar("lib.jar", {"com/x/A.class": b"x"})
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src/A.java").write_bytes(b"\xef\xbb\xbfpackage com.x;\nclass A {}")
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_takes_no_package_from_chained_scala_clauses(make_jar, tmp_path: Path):
    """`package scala` then `package collection` is scala.collection: the first clause alone names a wrong package."""
    jar = make_jar("lib.jar", {"scala/X.class": b"x", "scala/collection/X.class": b"x"})
    tree = _files(tmp_path / "tree", {"scala/collection/X.scala": "package scala\npackage collection\n\nclass X"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"scala/X"}


def test_sources_gap_takes_no_package_from_a_file_with_two_package_lines(make_jar, tmp_path: Path):
    jar = make_jar("lib.jar", {"com/old/A.class": b"x", "com/x/A.class": b"x"})
    tree = _files(tmp_path / "tree", {"src/A.java": "/*\npackage com.old;\n*/\npackage com.x;\nclass A {}"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"com/old/A", "com/x/A"}  # not sure: both are gaps


def test_sources_gap_takes_no_package_from_a_cut_head(make_jar, tmp_path: Path):
    jar = make_jar("lib.jar", {"Buffer.class": b"x", "okio/Buffer.class": b"x"})
    tree = _files(tmp_path / "tree", {"jvmMain/okio/Buffer.kt": "// " + "x" * 17000 + "\npackage okio\n\nclass Buffer"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == {"Buffer", "okio/Buffer"}


_K, _KL = "com/x/K", "com/x/K$$Lambda$0"


def _k_classes(class_file, lambda_file: str | None, namer: str | None) -> dict[str, bytes]:
    """K and its lambda K$$Lambda$0, a class that is top-level for the JVM. namer: the class whose constant pool has
    a Class entry of the lambda: K, the inner class K$1, or the class K$$Lambda$0$1 below the lambda. K names K$1,
    and the lambda names K$$Lambda$0$1: they are inner classes (#207)."""
    from test_check import _class_with, _class_with_inner_classes

    classes = {
        f"{_K}.class": _class_with(_K, interfaces={"K": (_KL,), "K$1": (f"{_K}$1",)}.get(namer, ())),
        f"{_KL}.class": _class_with(_KL, interfaces=(f"{_KL}$1",)) if namer == "K$$Lambda$0$1"
        else class_file(_KL, lambda_file),
    }
    if namer == "K$1":
        # a Class entry, no entry in its InnerClasses attribute: a class that lists the lambda nests it (#207)
        classes[f"{_K}$1.class"] = _class_with_inner_classes(f"{_K}$1", ((f"{_K}$1", _K),), refs=(_KL,))
    if namer == "K$$Lambda$0$1":
        classes[f"{_KL}$1.class"] = _class_with_inner_classes(f"{_KL}$1", ((f"{_KL}$1", _KL),))
    return classes


@pytest.mark.parametrize("sources, lambda_file, namer, gap", [
    ({"com/x/K.kt": "class K"}, None, "K", set()),  # the file of the outer class K holds the lambda
    ({"com/x/K.kt": "class K"}, None, "K$1", set()),  # an inner class of K names it
    ({"com/x/K.kt": "class K"}, None, None, {_KL}),  # no class of the family of K names it, for example generated code
    ({"com/x/K.kt": "class K"}, None, "K$$Lambda$0$1", {_KL}),  # a class below the lambda is no proof
    ({}, None, "K", {_K, _KL}),  # the outer class is a gap: the lambda too
    ({"com/x/K.kt": "class K"}, "Other.kt", "K", {_KL}),  # its attribute names an absent file: a gap
    ({"com/x/K.kt": "class K", "com/x/Other.kt": "fun o() {}"}, "Other.kt", None, set()),
])
def test_sources_gap_covers_a_class_without_a_source_file_attribute_by_its_outer_class(
    make_jar, class_file, tmp_path: Path, sources, lambda_file, namer, gap,
):
    """The class K$$Lambda$0 is top-level for the JVM, so it is expected (#206). Its class file names no source file,
    so the file of its outer class by name covers it, when the sources have that file and a class of the family of
    K names the lambda. A class with a SourceFile attribute keeps the rules of #180."""
    jar = make_jar("lib.jar", _k_classes(class_file, lambda_file, namer))
    tree = _files(tmp_path / "tree", sources)
    expected = expected_class_stems(jar)
    assert expected == {_K, _KL}
    assert _sources_gap(jar, expected, tree) == gap


def test_sources_gap_covers_a_scala_module_class_by_its_source_file(make_jar, class_file, tmp_path: Path):
    """The module class Foo$ of a Scala object names Foo.scala, as Foo does (#206, the rule of #180)."""
    jar = make_jar("lib.jar", {
        "s/Foo.class": class_file("s/Foo", "Foo.scala"), "s/Foo$.class": class_file("s/Foo$", "Foo.scala"),
    })
    tree = _files(tmp_path / "tree", {"s/Foo.scala": "object Foo"})
    assert _sources_gap(jar, expected_class_stems(jar), tree) == set()


def test_sources_gap_keeps_a_class_without_an_outer_file_rule_when_its_entry_cannot_be_read(
    make_damaged_jar, class_file, tmp_path: Path,
):
    """The entry of K$$Lambda$0 is damaged, so decaf does not know that it names no source file: a gap (#206)."""
    jar = make_damaged_jar("lib.jar", {"com/x/K.class": class_file("com/x/K", "K.kt") * 4,
                                       "com/x/K$$Lambda$0.class": class_file("com/x/K$$Lambda$0") * 4},
                           damaged=("com/x/K$$Lambda$0.class",))
    tree = _files(tmp_path / "tree", {"com/x/K.kt": "class K"})
    assert _sources_gap(jar, {"com/x/K", "com/x/K$$Lambda$0"}, tree) == {"com/x/K$$Lambda$0"}


def test_declared_package_reads_one_package_line(tmp_path: Path):
    p = tmp_path / "A.java"
    p.write_text("package com.x$y;\nclass A {}")
    assert _declared_package(p) == "com.x$y"  # a Java name can hold a dollar sign
    p.write_text("/* (c) */ package com.x;\nclass A {}")
    assert _declared_package(p) is None  # no line that starts with the package word: not sure
    p.write_text("class A {}")
    assert _declared_package(p) is None
    assert _declared_package(tmp_path / "absent.java") is None


def _hit(sources: Path):
    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(gav=Gav("com.example", "lib", "1.2"), sources_jar=sources, repo="https://r.test/m2",
                          resolved_by="pom-properties")
    return resolver


def test_maven_sources_that_lack_a_class_get_it_from_the_engines(make_jar, tmp_path: Path):
    """#180: a published sources jar can lack classes (generated, shaded, bundled); the engines write them."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x", "com/x/B$1.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "// upstream\nclass A {}"})
    runner = writing_runner({"vineflower": {"com/x/B.java": "// engine\nclass B {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method, report.gav, report.repo) == ("ok", "maven", "com.example:lib:1.2", "https://r.test/m2")
    assert (report.classes, report.sources_gap, report.missing_classes, report.java_files) == (2, 1, 0, 2)
    assert [(a.engine, a.level) for a in report.attempts] == [("vineflower", "class")]
    assert runner.sent == [["com/x/B$1.class", "com/x/B.class"]]  # the gap with its inner class; never A
    assert not report.partial and report.sources_miss is None
    assert "upstream" in (tmp_path / "out/src/com/x/A.java").read_text()
    assert "engine" in (tmp_path / "out/src/com/x/B.java").read_text()


def _foo_gen_jar(make_jar, class_file) -> tuple[Path, Path]:
    """p/Foo and p/Foo$$Gen, a class that is top-level for the JVM; the sources jar has p/Foo.java only."""
    jar = make_jar("lib-1.2.jar", {
        "p/Foo.class": class_file("p/Foo", "Foo.java"), "p/Foo$$Gen.class": class_file("p/Foo$$Gen", "Foo$$Gen.java"),
    })
    return jar, make_jar("lib-1.2-sources.jar", {"p/Foo.java": "// upstream\nclass Foo {}"})


def every_class_runner(text: str = "// engine"):
    """Fake engine: one file for each class file of a folder target, at the path of its class name."""
    sent: list[list[str]] = []

    def _run(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        names = sorted(check.class_name(p.read_bytes()) for p in Path(target).rglob("*.class"))
        sent.append(names)
        for name in names:
            (Path(dest) / f"{name}.java").parent.mkdir(parents=True, exist_ok=True)
            (Path(dest) / f"{name}.java").write_text(f"{text} {spec.name}\nclass X {{}}\n")
        return EngineResult(spec.name, 0, False, len(names), "")

    _run.sent = sent
    return _run


@pytest.mark.parametrize("mirror", [False, True], ids=["merge", "mirror"])
def test_the_gap_run_keeps_the_published_file_of_the_outer_class(make_jar, class_file, tmp_path: Path, mirror):
    """The gap class Foo$$Gen brings the family of Foo into its retry tree (#206), and the engine also writes Foo.
    The sources jar wrote Foo, so the file of the engine goes, as a file of a class that an earlier output wrote
    (#107). Before, it replaced the published file in the mirror layout, and with --merge the row had a collision."""
    jar, sources = _foo_gen_jar(make_jar, class_file)
    runner = every_class_runner()
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True,
                   writer=MirrorWriter(tmp_path / "out") if mirror else None)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert runner.sent == [["p/Foo", "p/Foo$$Gen"]]
    out = tmp_path / ("out/lib-1.2.jar" if mirror else "out/src")
    assert (out / "p/Foo.java").read_text() == "// upstream\nclass Foo {}"
    assert (out / "p/Foo$$Gen.java").read_text().startswith("// engine vineflower")
    assert (report.method, report.classes, report.sources_gap, report.missing_classes) == ("maven", 2, 1, 0)
    assert (report.java_files, report.collisions) == (2, [])


def test_a_flagged_gap_file_is_held_and_retried(make_jar, class_file, tmp_path: Path):
    jar, sources = _foo_gen_jar(make_jar, class_file)
    runner = writing_runner({
        "vineflower": {"p/Foo.java": "class Foo {} // vf", "p/Foo$$Gen.java": "// FLAG vf"},
        "cfr": {"p/Foo.java": "class Foo {} // cfr", "p/Foo$$Gen.java": "class Foo$$Gen {} // cfr"},
    })
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert [(a.engine, a.level) for a in report.attempts] == [("vineflower", "class"), ("cfr", "class")]
    assert (tmp_path / "out/src/p/Foo$$Gen.java").read_text() == "class Foo$$Gen {} // cfr"
    assert (tmp_path / "out/src/p/Foo.java").read_text() == "// upstream\nclass Foo {}"
    assert (report.flagged, report.missing_classes, report.java_files, report.collisions) == ([], 0, 2, [])


def test_maven_sources_in_a_source_set_folder_are_no_gap(make_jar, class_file, tmp_path: Path):
    """#180: the row is complete, no engine runs, and the .kt file keeps its path."""
    jar = make_jar("okio-3.4.jar", {"okio/Buffer.class": b"x", "okio/BufferKt.class": class_file("okio/BufferKt", "Buffer.kt")})
    sources = make_jar("okio-3.4-sources.jar", {"jvmMain/okio/Buffer.kt": "package okio\n\nclass Buffer"})
    runner = writing_runner({"vineflower": {"okio/Buffer.java": "class Buffer {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "okio-3.4.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method, report.classes, report.sources_gap, report.missing_classes) == ("ok", "maven", 2, 0, 0)
    assert runner.calls == [] and not report.partial
    assert (tmp_path / "out/src/jvmMain/okio/Buffer.kt").exists()
    assert not (tmp_path / "out/src/okio/Buffer.java").exists()


def test_a_gap_class_that_no_engine_writes_stays_missing(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    ctx = make_ctx(tmp_path, writing_runner({}), resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method, report.classes, report.sources_gap, report.missing_classes) == ("ok", "maven", 2, 1, 1)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower", "procyon", "jd"]
    assert report.partial
    assert (tmp_path / "out/src/com/x/A.java").is_file()


def test_a_damaged_gap_entry_keeps_the_row(make_damaged_jar, make_jar, class_file, tmp_path: Path):
    """#180: a class entry that cannot be read is a gap that stays missing; the row is ok and partial."""
    jar = make_damaged_jar("lib-1.2.jar", {"com/x/A.class": class_file("com/x/A", "A.java") * 4,
                                           "com/x/D.class": class_file("com/x/D", "D.java") * 4},
                           damaged=("com/x/D.class",))
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    ctx = make_ctx(tmp_path, writing_runner({}), resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.method, report.classes, report.sources_gap, report.missing_classes) == ("ok", "maven", 2, 1, 1)
    assert report.partial


def test_a_class_with_a_source_under_another_name_is_no_gap(make_jar, class_file, tmp_path: Path):
    """A secondary type and a Kotlin facade have their source in a file of another name."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": class_file("com/x/A", "A.java"),
                                   "com/x/B.class": class_file("com/x/B", "A.java"),
                                   "com/x/UtilKt.class": class_file("com/x/UtilKt", "Util.kt")})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {} class B {}", "com/x/Util.kt": "fun u() {}"})
    runner = writing_runner({})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.method, report.classes, report.sources_gap, report.missing_classes, report.attempts) == ("maven", 3, 0, 0, [])
    assert runner.calls == []


@pytest.mark.parametrize("mirror", [False, True], ids=["merge", "mirror"])
def test_groovy_sources_of_a_maven_jar_are_written(make_jar, class_file, tmp_path: Path, mirror):
    from decaf.pipeline import MirrorWriter

    jar = make_jar("lib-1.2.jar", {"g/G.class": class_file("g/G", "G.groovy")})
    sources = make_jar("lib-1.2-sources.jar", {"g/G.groovy": "class G {}"})
    runner = writing_runner({})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True, writer=MirrorWriter(tmp_path / "out") if mirror else None)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.method, report.classes, report.sources_gap, report.java_files) == ("maven", 1, 0, 1)
    assert runner.calls == []
    assert (tmp_path / ("out/lib-1.2.jar/g/G.groovy" if mirror else "out/src/g/G.groovy")).read_text() == "class G {}"


def test_a_class_only_under_a_versions_folder_is_a_gap(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "META-INF/versions/11/com/x/V.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    runner = writing_runner({"vineflower": {"com/x/V.java": "class V {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.sources_gap, report.missing_classes) == (1, 0)
    assert runner.sent == [["META-INF/versions/11/com/x/V.class"]]


def test_maven_verbose_line_names_the_gap(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    lines: list[str] = []
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/B.java": "class B {}"}}), resolver=_hit(sources), maven=True)
    ctx.on_stderr = lines.append
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert lines[0] == "maven lib-1.2.jar: pom-properties com.example:lib:1.2 (https://r.test/m2); 1 classes not in the sources jar"
    assert report.outcome == "ok"


def test_a_nested_maven_hit_with_a_gap_writes_the_engine_file_in_its_container_folder(make_jar, tmp_path: Path):
    """#180: the gap retries of a nested jar use the writer paths of the nested rel."""
    from decaf.pipeline import MirrorWriter

    lib = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    writer = MirrorWriter(tmp_path / "out")
    writer.nest("app.war!/WEB-INF/lib/lib-1.2.jar", "app.war")
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/B.java": "class B {}"}}), resolver=_hit(sources), maven=True,
                   writer=writer)
    report, _ = process_artifact(
        Artifact(lib, "app.war!/WEB-INF/lib/lib-1.2.jar", ArtifactKind.ARCHIVE, parent="app.war", depth=1), ctx
    )
    assert (report.method, report.sources_gap, report.missing_classes) == ("maven", 1, 0)
    assert (tmp_path / "out/app.war/WEB-INF/lib/lib-1.2.jar/com/x/A.java").is_file()
    assert (tmp_path / "out/app.war/WEB-INF/lib/lib-1.2.jar/com/x/B.java").is_file()


def test_a_gap_row_after_ctrl_c_keeps_its_maven_files_and_counts_the_gap(make_jar, tmp_path: Path):
    """#180: Ctrl-C lands while an engine runs for the gap: no file of it goes out (#112)."""
    from decaf.engines import PROCESSES

    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    inner = writing_runner({"vineflower": {"com/x/B.java": "class B {}"}})

    def killed(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        res = inner(spec, jar_path, target, dest, timeout, java=java, cpu_budget=cpu_budget)
        PROCESSES.kill_all()  # Ctrl-C lands while the engine runs
        return res

    ctx = make_ctx(tmp_path, killed, resolver=_hit(sources), maven=True)
    try:
        report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    finally:
        PROCESSES.reset()
    assert (report.outcome, report.method, report.sources_gap, report.missing_classes) == ("ok", "maven", 1, 1)
    assert [a.engine for a in report.attempts] == ["vineflower"]
    assert (tmp_path / "out/src/com/x/A.java").is_file()
    assert not (tmp_path / "out/src/com/x/B.java").exists()


def test_a_gap_row_with_a_checker_checks_the_engine_files_only(make_jar, tmp_path: Path):
    """#180: `check` and `flagged` of a Maven row come from the gap files; the checker does not read the Maven files."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    calls: list = []
    runner = writing_runner({"vineflower": {"com/x/B.java": "class B { FLAG }"}})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True, checker=marker_checker(calls))
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.method, report.sources_gap, report.missing_classes) == ("maven", 1, 0)
    assert [f["path"] for f in report.flagged] == ["com/x/B.java"] and report.partial
    assert (report.check["checked"], report.check["flagged"]) == (1, 1)
    assert calls and all(Path(f).name == "B.java" for jobs in calls for job in jobs for f in job.files)


def test_a_gap_run_of_the_primary_engine_gets_the_engine_args(make_jar, tmp_path: Path):
    """#180: the tokens after `--` go to the primary engine, also for the gap classes."""
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x", "com/x/B.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "class A {}"})
    seen: list = []

    def runner(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None, engine_args=()):
        seen.append(tuple(engine_args))
        (Path(dest) / "com/x").mkdir(parents=True)
        (Path(dest) / "com/x/B.java").write_text("class B {}")
        return EngineResult(spec.name, 0, False, 1, "")

    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True, fallback=False)
    ctx.settings.engine_args = ("-dgs=1",)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert seen == [("-dgs=1",)]
    assert (report.method, report.sources_gap, report.missing_classes) == ("maven", 1, 0)


def test_a_groovy_only_sources_jar_is_a_hit(make_jar, class_file, tmp_path: Path):
    """A Groovy or Scala library publishes .groovy or .scala sources; they are the sources of its classes."""
    jar = make_jar("lib-1.2.jar", {"g/G.class": class_file("g/G", "G.groovy"), "g/H.class": class_file("g/H", "G.groovy")})
    sources = make_jar("lib-1.2-sources.jar", {"g/G.groovy": "class G {}\nclass H {}"})
    runner = writing_runner({})
    ctx = make_ctx(tmp_path, runner, resolver=_hit(sources), maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert (report.method, report.classes, report.sources_gap, report.missing_classes, report.java_files) == ("maven", 2, 0, 0, 1)
    assert runner.calls == []


def test_sources_jar_artifact_carries_resources(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    sj = make_jar("lib-sources.jar", {"com/x/A.java": "class A {}", "LICENSE": "MIT"})
    ctx = make_ctx(tmp_path, writing_runner({}), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(sj, "lib-sources.jar", ArtifactKind.SOURCES_JAR), ctx)
    assert report.outcome == "ok" and report.method == "extracted"
    assert (tmp_path / "out/lib-sources.jar/LICENSE").is_file()
    assert (tmp_path / "out/lib-sources.jar/com/x/A.java").is_file()
    assert report.resources_copied == 1


def test_solo_mirror_drops_strays_and_takes_original_resources(make_jar, tmp_path: Path):
    from decaf.pipeline import MirrorWriter

    jar = make_jar("app.jar", {"com/x/A.class": b"x", "cfg.properties": "k=v"})
    runner = writing_runner(
        {"vineflower": {"com/x/A.java": "class A {}", "summary.txt": "engine banner"}}
    )
    ctx = make_ctx(tmp_path, runner, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    dest = tmp_path / "out/app.jar"
    assert (dest / "com/x/A.java").is_file()
    assert (dest / "cfg.properties").is_file()   # from the original jar
    assert not (dest / "summary.txt").exists()   # engine stray dropped
    assert report.resources_copied == 1
    assert report.resources_skipped == 0


def test_corrupt_resource_member_does_not_block_decompile(make_jar, tmp_path: Path):
    """A damaged resource member must never stop the engines from running."""
    from decaf.pipeline import MirrorWriter

    jar = make_jar("app.jar", {"com/x/A.class": b"x", "big.properties": b"A" * 1024})
    jar.write_bytes(jar.read_bytes().replace(b"A" * 64, b"B" * 64, 1))
    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.resources_copied == 0
    assert (tmp_path / "out/app.jar/com/x/A.java").is_file()


def test_owner_index_direct_inner_and_miss():
    from decaf.pipeline import _owner_index

    owners = {"com/a/A": 0, "com/b/B": 1}
    assert _owner_index("com/a/A", owners) == 0
    assert _owner_index("com/a/A$Inner", owners) == 0  # inner rides with outer
    assert _owner_index("com/b/B$1$2", owners) == 1  # only the first $ splits
    assert _owner_index("com/zz/Nope", owners) is None
    assert _owner_index("com/a/A2", owners) is None  # no prefix guessing


def test_batch_watcher_reports_tree_progress(make_jar, tmp_path, monkeypatch):
    import threading
    import time

    from decaf import pipeline
    from decaf.engines import EngineResult

    monkeypatch.setattr(pipeline, "_PROGRESS_INTERVAL", 0.01)
    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x", "com/a/B.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    events: list[tuple[str, str, str]] = []
    waits: dict[tuple[str, str], threading.Event] = {}

    def _ev(subject, detail):
        return waits.setdefault((subject, detail), threading.Event())

    def on_event(kind, subject, detail):
        events.append((kind, subject, detail))
        if kind == "progress":
            _ev(subject, detail).set()

    def stepped_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        dest = Path(dest)
        (dest / "com/a").mkdir(parents=True)
        (dest / "com/a/A.java").write_text("class A {}\n")
        assert _ev("a.jar", "vineflower · batch of 2 · 1/2 classes").wait(10)
        (dest / "com/a/B.java").write_text("class B {}\n")
        (dest / "com/b").mkdir(parents=True)
        (dest / "com/b/B.java").write_text("class B {}\n")
        assert _ev("a.jar", "vineflower · batch of 2 · 2/2 classes").wait(10)
        assert _ev("b.jar", "vineflower · batch of 2 · 1/1 classes").wait(10)
        return EngineResult(spec.name, 0, False, 3, "")

    ctx = _batch_ctx(tmp_path, stepped_batch, on_event=on_event)
    done, requeue = pipeline._decompile_batch([m1, m2], ctx)
    assert requeue == [] and sorted(r.rel for r in done) == ["a.jar", "b.jar"]
    a_details = [d for k, s, d in events if k == "progress" and s == "a.jar"]
    assert a_details == [
        "vineflower · batch of 2 · 1/2 classes",
        "vineflower · batch of 2 · 2/2 classes",
    ]
    assert ("progress", "b.jar", "vineflower · batch of 2 · 1/1 classes") in events
    n = len(events)
    time.sleep(0.05)  # ~5 poll intervals: the watcher must be gone after the runner returns
    assert len(events) == n


def test_batch_watcher_fernflower_states(make_jar, tmp_path, monkeypatch):
    import threading

    from decaf import pipeline
    from decaf.engines import EngineResult

    monkeypatch.setattr(pipeline, "_PROGRESS_INTERVAL", 0.01)
    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    events: list[tuple[str, str, str]] = []
    waits: dict[tuple[str, str], threading.Event] = {}

    def _ev(subject, detail):
        return waits.setdefault((subject, detail), threading.Event())

    def on_event(kind, subject, detail):
        events.append((kind, subject, detail))
        if kind == "progress":
            _ev(subject, detail).set()

    def ff_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        dest = Path(dest)
        (dest / "a.jar").write_bytes(b"partial zip")
        assert _ev("a.jar", "fernflower · batch of 2 · decompiling").wait(10)
        assert _ev("b.jar", "fernflower · batch of 2 · queued in batch").wait(10)
        (dest / "b.jar").write_bytes(b"partial zip")
        assert _ev("a.jar", "fernflower · batch of 2 · done, pending split").wait(10)
        assert _ev("b.jar", "fernflower · batch of 2 · decompiling").wait(10)
        return EngineResult(spec.name, 1, False, 0, "boom")

    ctx = _batch_ctx(tmp_path, ff_batch, chain=["fernflower"], on_event=on_event)
    done, requeue = pipeline._decompile_batch([m1, m2], ctx)
    assert done == [] and len(requeue) == 2  # rc=1: normal requeue flow, watcher already proven


def test_batch_watcher_skips_colliding_basenames(make_jar, tmp_path, monkeypatch):
    import time

    from decaf import pipeline
    from decaf.engines import EngineResult
    from decaf.pipeline import ArtifactReport, _stems_and_claims
    from decaf.scanner import Artifact, ArtifactKind

    monkeypatch.setattr(pipeline, "_PROGRESS_INTERVAL", 0.01)
    j1 = make_jar("dep.jar", {"com/x/A.class": b"x"}, base=tmp_path / "p1")
    j2 = make_jar("dep.jar", {"com/y/B.class": b"y"}, base=tmp_path / "p2")
    m1 = (Artifact(j1, "app1!/dep.jar", ArtifactKind.ARCHIVE, 1),
          j1, ArtifactReport(rel="app1!/dep.jar", kind="archive", outcome="ok"),
          *_stems_and_claims(j1))
    m2 = (Artifact(j2, "app2!/dep.jar", ArtifactKind.ARCHIVE, 1),
          j2, ArtifactReport(rel="app2!/dep.jar", kind="archive", outcome="ok"),
          *_stems_and_claims(j2))
    events: list[tuple[str, str, str]] = []

    def ff_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        (Path(dest) / "dep.jar").write_bytes(b"partial zip")
        time.sleep(0.1)  # ~10 poll intervals: ambiguous name must never be attributed
        return EngineResult(spec.name, 1, False, 0, "boom")

    ctx = _batch_ctx(tmp_path, ff_batch, chain=["fernflower"],
                     on_event=lambda k, s, d: events.append((k, s, d)))
    done, requeue = pipeline._decompile_batch([m1, m2], ctx)
    assert len(requeue) == 2
    assert not [e for e in events if e[0] == "progress"]


def test_batch_watcher_not_started_without_on_event(make_jar, tmp_path, monkeypatch):
    from decaf import pipeline

    calls: list[int] = []
    monkeypatch.setattr(pipeline, "_watch_batch", lambda *a, **k: calls.append(1))
    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)  # on_event defaults to None
    done, requeue = pipeline._decompile_batch([m1], ctx)
    assert [r.outcome for r in done] == ["ok"] and requeue == []
    assert calls == []


def test_batch_watcher_exception_never_taints_outcome(make_jar, tmp_path, monkeypatch):
    import threading
    import time

    from decaf import pipeline
    from decaf.engines import EngineResult

    monkeypatch.setattr(pipeline, "_PROGRESS_INTERVAL", 0.01)
    real = pipeline._owner_index

    def boom(stem, owners):
        if threading.current_thread().name.startswith("decaf-batch-watch"):
            raise RuntimeError("watcher tick blew up")
        return real(stem, owners)

    monkeypatch.setattr(pipeline, "_owner_index", boom)
    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    events: list[tuple[str, str, str]] = []

    def slow_batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        res = merged_batch_engine(spec, jar_path, targets, dest, timeout, java=java)
        time.sleep(0.1)  # give the watcher ticks a chance to hit the poisoned helper
        return res

    ctx = _batch_ctx(tmp_path, slow_batch, on_event=lambda k, s, d: events.append((k, s, d)))
    done, requeue = pipeline._decompile_batch([m1], ctx)
    assert [r.outcome for r in done] == ["ok"] and requeue == []  # split ran on this thread, unpoisoned
    assert not [e for e in events if e[0] == "progress"]


def test_form_batch_respects_raised_caps():
    from collections import deque
    from types import SimpleNamespace

    from decaf.pipeline import _BATCH_MAX_CLASSES, _BATCH_MAX_JARS, _form_batch

    def member(i, classes):
        return (SimpleNamespace(classes=classes), None, None, {f"s{i}"}, set())

    # Jar cap: 33 one-class members -> exactly 32 taken, 1 left queued.
    q = deque(member(i, 1) for i in range(33))
    batch = _form_batch(q)
    assert len(batch) == _BATCH_MAX_JARS == 32
    assert len(q) == 1

    # Class cap: 500-class members -> 16 fill 8,000; the rest stay queued in order.
    q = deque(member(i, 500) for i in range(20))
    batch = _form_batch(q)
    assert len(batch) == 16
    assert sum(a.classes for a, *_ in batch) == _BATCH_MAX_CLASSES == 8000
    assert len(q) == 4


def test_form_batch_defers_metadata_claim_overlap():
    """#75: members claiming the same metadata rel never co-batch, exactly like
    overlapping class stems — the second claimant stays queued for the next batch."""
    from collections import deque
    from types import SimpleNamespace

    from decaf.pipeline import _form_batch

    def member(stems, claims):
        return (SimpleNamespace(classes=1), None, None, stems, claims)

    # Two modular jars both claim "module-info".
    q = deque([
        member({"com/a/A"}, {"module-info"}),
        member({"com/b/B"}, {"module-info"}),
        member({"com/c/C"}, set()),
    ])
    batch = _form_batch(q)
    assert [stems for _, _, _, stems, _ in batch] == [{"com/a/A"}, {"com/c/C"}]
    assert [stems for _, _, _, stems, _ in q] == [{"com/b/B"}]

    # Two jars sharing a package both claim its package-info.
    q = deque([
        member({"com/x/A"}, {"com/x/package-info"}),
        member({"com/x/B"}, {"com/x/package-info"}),
    ])
    batch = _form_batch(q)
    assert [stems for _, _, _, stems, _ in batch] == [{"com/x/A"}]
    assert [stems for _, _, _, stems, _ in q] == [{"com/x/B"}]


def test_form_batch_folds_case_when_tree_ignores_case():
    """#93: on a case-insensitive tree, stems that differ only by case would share one
    file in the batch dest — with fold=True they never co-batch; exact matching otherwise."""
    from collections import deque
    from types import SimpleNamespace

    from decaf.pipeline import _form_batch

    def member(stems):
        return (SimpleNamespace(classes=1), None, None, stems, set())

    q = deque([member({"p/A"}), member({"p/a"}), member({"q/Z"})])
    batch = _form_batch(q, fold=True)
    assert [stems for _, _, _, stems, _ in batch] == [{"p/A"}, {"q/Z"}]
    assert [stems for _, _, _, stems, _ in q] == [{"p/a"}]  # deferred, in order

    q = deque([member({"p/A"}), member({"p/a"}), member({"q/Z"})])
    batch = _form_batch(q)  # case-sensitive tree: exact footprints, all three co-batch
    assert [stems for _, _, _, stems, _ in batch] == [{"p/A"}, {"p/a"}, {"q/Z"}]


def test_form_batch_folds_two_normal_forms_when_tree_ignores_case():
    """Review low item: on APFS, stems that differ only by the normal form share one file."""
    from collections import deque
    from types import SimpleNamespace

    from decaf.pipeline import _form_batch

    def member(stems):
        return (SimpleNamespace(classes=1), None, None, stems, set())

    q = deque([member({"p/Caf\u00e9"}), member({"p/Cafe\u0301"})])
    batch = _form_batch(q, fold=True)
    assert [stems for _, _, _, stems, _ in batch] == [{"p/Caf\u00e9"}]
    assert [stems for _, _, _, stems, _ in q] == [{"p/Cafe\u0301"}]


def test_partial_predicate_and_totals():
    from dataclasses import replace

    from decaf.pipeline import ArtifactReport, compute_totals

    ok = ArtifactReport(rel="a.jar", kind="archive", outcome="ok")
    assert not ok.partial
    assert replace(ok, missing_classes=3).partial
    assert replace(ok, stubs=[{"path": "p/A.java", "engine": "vineflower", "methods": 1, "errors": []}]).partial
    assert replace(ok, sources_miss="network: index timeout; trail").partial
    assert not replace(ok, sources_miss="no pom.properties").partial
    assert not replace(ok, outcome="failed", missing_classes=3).partial
    totals = compute_totals([ok, replace(ok, rel="b.jar", missing_classes=1)])
    assert totals["partial"] == 1
    assert replace(ok, flagged=[{"path": "a/B.java"}]).partial
    assert not replace(ok, outcome="failed", flagged=[{"path": "a/B.java"}]).partial
    with_check = replace(ok, rel="c.jar", check={"checked": 5, "flagged": 1, "noted": 2, "unchecked": 3})
    t = compute_totals([with_check, ok])
    assert (t["checked_files"], t["flagged_files"], t["noted_files"], t["unchecked_files"]) == (5, 1, 2, 3)


def test_new_report_fields_have_backward_compatible_defaults():
    from decaf.pipeline import ArtifactReport, EngineAttempt

    assert EngineAttempt("cfr", "archive", 0, False, 1, "").flagged == 0
    r = ArtifactReport(rel="a.jar", kind="archive", outcome="ok")
    assert r.flagged == [] and r.check is None
    assert Settings(input=Path("in"), output=Path("out")).check is True


def _class_header(major: int) -> bytes:
    return b"\xca\xfe\xba\xbe\x00\x00" + major.to_bytes(2, "big") + b"rest"


def test_class_versions_reads_top_level_majors(make_jar, tmp_path: Path):
    from decaf.pipeline import _class_versions
    from test_check import _class_with

    entries = {
        "WEB-INF/classes/com/a/A.class": _class_header(69),
        "WEB-INF/classes/com/a/A$1.class": _class_header(70),
        "com/b/B.class": _class_header(52),
        "META-INF/versions/9/com/b/B.class": _class_header(53),
        "com/c/$Top.class": _class_header(61),  # a top-level class (#106)
        "com/c/$Top$In.class": _class_header(62),
        "com/k/K.class": _class_with("com/k/K", utf8=("Lkotlin/Metadata;",)),  # #108
        "x.txt": b"no",
    }
    folder = tmp_path / "classes"
    for rel, data in entries.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
    expected = ({
        "com/a/A": 69, "com/b/B": 52, "META-INF/versions/9/com/b/B": 53, "com/c/$Top": 61, "com/k/K": 52,
    }, {"com/k/K": "kotlin"})
    assert _class_versions(make_jar("v.jar", entries)) == expected
    assert _class_versions(folder) == expected


@pytest.mark.parametrize("as_folder", [False, True])
def test_class_versions_skip_a_class_entry_above_the_size_limit(make_jar, tmp_path: Path, monkeypatch, as_folder):
    """Review C5: decaf reads no larger class entry into its memory."""
    import decaf.pipeline as pipeline

    small, big = _class_header(52), _class_header(61) + bytes(100)
    source, _ = _classes(make_jar, tmp_path, {"p/A.class": small, "p/B.class": big}, as_folder)
    monkeypatch.setattr(pipeline, "_CLASS_FILE_MAX", len(small))
    assert pipeline._class_versions(source) == ({"p/A": 52}, {})


def test_tree_roots_skip_a_class_file_above_the_size_limit(tmp_path: Path, monkeypatch):
    import decaf.pipeline as pipeline
    from test_check import _class_file

    source = tmp_path / "copy"
    for rel, data in (("out/p/A.class", _class_file("p/A")), ("other/q/B.class", _class_file("q/B") + bytes(100))):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    monkeypatch.setattr(pipeline, "_CLASS_FILE_MAX", len(_class_file("p/A")))
    assert pipeline._tree_roots(source) == [source / "out"]


def test_family_classpath_war_and_nested_jar(make_jar, tmp_path: Path):
    from decaf.pipeline import _family_classpath

    dep = make_jar("dep.jar", {"com/d/D.class": b"d"})
    other = make_jar("other.jar", {"com/o/O.class": b"o"})
    war = make_jar("app.war", {
        "WEB-INF/classes/com/w/W.class": b"w",
        "WEB-INF/lib/dep.jar": dep.read_bytes(),
        "WEB-INF/lib/other.jar": other.read_bytes(),
    })
    ctx = make_ctx(tmp_path, writing_runner({}))
    war_a = Artifact(war, "app.war", ArtifactKind.ARCHIVE)
    nested = _discover_nested(war_a, ctx)
    cp = _family_classpath(war_a, war, ctx)
    assert (cp[0] / "com/w/W.class").is_file()  # the class root comes first
    assert cp[1] == war
    assert [p.name for p in cp[2:]] == ["dep.jar", "other.jar"]
    dep_a = next(a for a in nested if a.rel.endswith("dep.jar"))
    cp_dep = _family_classpath(dep_a, dep_a.path, ctx)
    assert cp_dep[0] == dep_a.path
    assert cp_dep[1:] == [cp[0], war, cp[3]]  # parent group without the duplicate dep.jar


def test_family_classpath_war_without_lib_keeps_its_class_root(make_jar, tmp_path: Path):
    from decaf.pipeline import _family_classpath

    war = make_jar("only.war", {"WEB-INF/classes/com/w/W.class": b"w"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    a = Artifact(war, "only.war", ArtifactKind.ARCHIVE)
    _discover_nested(a, ctx)  # no nested archives
    cp = _family_classpath(a, war, ctx)
    assert len(cp) == 2 and (cp[0] / "com/w/W.class").is_file() and cp[1] == war


def test_class_root_extracts_once_for_two_workers(make_jar, tmp_path: Path, monkeypatch):
    """#101: two workers that asked for one class root at the same time both extracted it."""
    import threading
    import time

    from decaf import pipeline

    war = make_jar("app.war", {"WEB-INF/classes/com/w/W.class": b"w"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    real = pipeline.safe_extract_zip
    calls: list[Path] = []
    started = threading.Event()

    def slow(*args, **kwargs):
        calls.append(args[1])
        started.set()
        time.sleep(0.3)  # the second worker asks during the extraction
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "safe_extract_zip", slow)
    roots: list = []
    first = threading.Thread(target=lambda: roots.append(pipeline._class_root("app.war", war, ctx)))
    first.start()
    assert started.wait(5)
    roots.append(pipeline._class_root("app.war", war, ctx))
    first.join()
    assert len(calls) == 1 and roots[0] == roots[1] and (roots[0] / "com/w/W.class").is_file()


def test_family_classpath_input_group_and_class_tree(make_jar, tmp_path: Path):
    from decaf.pipeline import _family_classpath

    a_jar = make_jar("in/a.jar", {"com/a/A.class": b"a"})
    b_jar = make_jar("in/b.jar", {"com/b/B.class": b"b"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    ctx.input_group = [a_jar, b_jar, tmp_path / "in"]
    assert _family_classpath(Artifact(a_jar, "a.jar", ArtifactKind.ARCHIVE), a_jar, ctx) == [
        a_jar, b_jar, tmp_path / "in",
    ]
    tree = tmp_path / "copy"
    tree.mkdir()
    t = Artifact(tmp_path / "in", "_classes", ArtifactKind.CLASS_TREE)
    assert _family_classpath(t, tree, ctx) == [tree, a_jar, b_jar, tmp_path / "in"]


def test_check_of_a_class_folder_uses_its_package_roots(tmp_path: Path):
    """#108: a folder such as build/classes/java/main/p needs its root on the classpath, not the folder above."""
    from decaf.pipeline import _check_trees, _Held, _tree_roots
    from test_check import _class_file

    source = tmp_path / "copy"
    for rel, name in (("build/classes/java/main/p/A.class", "p/A"), ("q/B.class", "q/B")):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(_class_file(name))
    assert _tree_roots(source) == [source, source / "build/classes/java/main"]
    tree = tmp_path / "out-tree"
    (tree / "build/classes/java/main/p").mkdir(parents=True)
    (tree / "build/classes/java/main/p/A.java").write_text("package p; class A {}")
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({}), checker=marker_checker(calls))
    _check_trees([(tree, Artifact(tmp_path / "in", "_classes", ArtifactKind.CLASS_TREE), source, _Held())], ctx, None, "vineflower")
    assert calls[0][0].classpath[:2] == (source, source / "build/classes/java/main")


def test_check_jobs_carry_the_release_of_their_classes(make_jar, tmp_path: Path):
    """#108: javac checks old code against the JDK API of its own Java version."""
    from decaf.pipeline import _check_trees, _Held

    jar = make_jar("app.jar", {"p/A.class": _class_header(52), "p/B.class": _class_header(61), "p/C.class": b"no header"})
    tree = tmp_path / "out-tree"
    (tree / "p").mkdir(parents=True)
    for name in "ABC":
        (tree / f"p/{name}.java").write_text(f"package p; class {name} {{}}")
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({}), checker=marker_checker(calls))
    _check_trees([(tree, Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), jar, _Held())], ctx, None, "vineflower")
    assert sorted((job.release or 0, [f.name for f in job.files]) for job in calls[0]) == [
        (0, ["C.java"]), (8, ["A.java"]), (17, ["B.java"]),
    ]


def test_check_jobs_name_the_files_of_kotlin_groovy_and_scala_classes(make_jar, tmp_path: Path):
    """#108: the marker is in the class file of the top-level class."""
    from decaf.pipeline import _check_trees, _Held
    from test_check import _class_with

    jar = make_jar("app.jar", {
        "p/K.class": _class_with("p/K", utf8=("Lkotlin/Metadata;",)),
        "p/G.class": _class_with("p/G", interfaces=("groovy/lang/GroovyObject",)),
        "p/G$_run_closure1.class": _class_with("p/G$_run_closure1"),
        "p/J.class": _class_with("p/J", utf8=("groovy/lang/GroovyObject",)),
    })
    tree = tmp_path / "out-tree"
    (tree / "p").mkdir(parents=True)
    for name in ("K", "G", "G$_run_closure1", "J"):
        (tree / f"p/{name}.java").write_text("package p; class X {}")
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({}), checker=marker_checker(calls))
    _check_trees([(tree, Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), jar, _Held())], ctx, None, "vineflower")
    (job,) = calls[0]
    assert sorted(f.name for f in job.non_java) == ["G$_run_closure1.java", "G.java", "K.java"]


def test_check_jobs_name_the_files_of_detached_inner_classes(make_jar, tmp_path: Path):
    """#101: a file of an inner class that the engine wrote apart from its outer class; Gone$In has no outer class."""
    from decaf.pipeline import _check_trees, _Held
    from test_check import _class_with

    names = ("A", "A$1", "A$1$1", "Gone$In", "$Util")
    jar = make_jar("app.jar", {f"p/{n}.class": _class_with(f"p/{n}") for n in names})
    tree = tmp_path / "out-tree"
    (tree / "p").mkdir(parents=True)
    for name in names:
        (tree / f"p/{name}.java").write_text("package p; class X {}")
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({}), checker=marker_checker(calls))
    _check_trees([(tree, Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), jar, _Held())], ctx, None, "vineflower")
    (job,) = calls[0]
    assert sorted(f.name for f in job.detached) == ["A$1$1.java", "A$1.java"]



def _classes(make_jar, tmp_path: Path, entries: dict[str, bytes], as_folder: bool) -> tuple[Path, Artifact]:
    """The entries as a jar, or as a class folder."""
    if not as_folder:
        jar = make_jar("app.jar", entries)
        return jar, Artifact(jar, "app.jar", ArtifactKind.ARCHIVE)
    folder = tmp_path / "classes"
    for name, data in entries.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    return folder, Artifact(folder, "classes", ArtifactKind.CLASS_TREE)


@pytest.mark.parametrize("as_folder", [False, True])
def test_check_jobs_read_the_package_refs_of_the_class_files_of_a_file(make_jar, tmp_path: Path, as_folder):
    """Review C3: a same-package class that the class files name is an absent class, not an engine error."""
    from decaf.pipeline import _check_trees, _Held
    from test_check import _class_with

    source, artifact = _classes(make_jar, tmp_path, {
        "p/A.class": _class_with("p/A", interfaces=("p/B",)),
        "p/A$1.class": _class_with("p/A$1", interfaces=("p/C",)),
        "p/AB.class": _class_with("p/AB", interfaces=("p/D",)),
        "p/E.class": _class_with("p/E", interfaces=("p/F",)),
    }, as_folder)
    tree = tmp_path / "out-tree"
    (tree / "p").mkdir(parents=True)
    for name in ("A", "A$1", "E"):
        (tree / f"p/{name}.java").write_text("package p; class X {}")
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({}), checker=marker_checker(calls))
    _check_trees([(tree, artifact, source, _Held())], ctx, None, "vineflower")
    (job,) = calls[0]
    files = [tree / "p/A.java", tree / "p/A$1.java", tree / "p/E.java"]
    assert job.refs(files) == {
        files[0]: frozenset({"A", "A$1", "B", "C"}),  # not D of p/AB
        files[1]: frozenset({"A", "A$1", "B", "C"}),  # an engine can write p/A$1 to a file of its own
        files[2]: frozenset({"E", "F"}),
    }


@pytest.mark.parametrize("as_folder", [False, True])
def test_package_refs_skip_a_class_entry_above_the_size_limit(make_jar, tmp_path: Path, monkeypatch, as_folder):
    import decaf.pipeline as pipeline
    from test_check import _class_with

    small, big = _class_with("p/A", interfaces=("p/B",)), _class_with("p/A$1", interfaces=("p/C",), utf8=("x" * 200,))
    source, _ = _classes(make_jar, tmp_path, {"p/A.class": small, "p/A$1.class": big}, as_folder)
    monkeypatch.setattr(pipeline, "_CLASS_FILE_MAX", len(small))
    tree = tmp_path / "tree"
    assert pipeline._package_refs(source, tree)([tree / "p/A.java"]) == {tree / "p/A.java": frozenset({"A", "B"})}


def test_package_refs_open_the_archive_once_for_all_files_of_a_job(make_jar, tmp_path: Path, monkeypatch):
    """A big jar takes some milliseconds to open, and a jar without its dependencies has many absent names."""
    import zipfile

    import decaf.pipeline as pipeline
    from test_check import _class_with

    jar = make_jar("app.jar", {f"p/C{i}.class": _class_with(f"p/C{i}", interfaces=("p/B",)) for i in range(5)})
    opened: list = []
    real = zipfile.ZipFile

    def counting(*a, **kw):
        opened.append(a[0])
        return real(*a, **kw)

    monkeypatch.setattr(pipeline.zipfile, "ZipFile", counting)
    tree = tmp_path / "tree"
    refs = pipeline._package_refs(jar, tree)
    files = [tree / f"p/C{i}.java" for i in range(5)]
    assert refs(files) == {f: frozenset({f.stem, "B"}) for f in files}
    assert len(opened) == 2  # the entry names, then the class files
    refs(files[:2])
    assert len(opened) == 3


def test_twice_finds_two_versions_of_one_library(make_jar, tmp_path: Path):
    """#108: a member that one version of a class lacks is no engine error."""
    from decaf.pipeline import _twice

    own = make_jar("app.jar", {"p/A.class": b"a", "lib/Own.class": b"o"})
    v1 = make_jar("lib-1.jar", {"lib/Foo.class": b"f", "lib/Foo$In.class": b"i", "lib/Own.class": b"o"})
    v2 = make_jar("lib-2.jar", {"lib/Foo.class": b"g", "lib/Bar.class": b"b"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    twice = _twice([own], [v1, v2], ctx)
    assert twice("lib.Foo") and twice("lib.Foo.In") and twice("lib.Own")
    assert not twice("lib.Bar") and not twice("p.A") and not twice("java.lang.Object")


def test_family_classpath_single_file_input_has_no_input_group(make_jar, tmp_path: Path):
    from decaf.pipeline import _family_classpath

    jar = make_jar("solo.jar", {"com/s/S.class": b"s"})
    ctx = make_ctx(tmp_path, writing_runner({}))
    assert _family_classpath(Artifact(jar, "solo.jar", ArtifactKind.ARCHIVE), jar, ctx) == [jar]


def marker_checker(calls=None):
    """Fake check: FLAG = flagged (name), NOTE = noted (type), ENV = unchecked, else clean.

    SNEAKY:name@line = flagged (flow) with the first error "line: unreported exception name; ...",
    an error that bytecode can cause legally (line 1 when not given) (#108). SNEAKY:[a type] takes a
    type with dots or spaces, e.g. SNEAKY:[java.lang.Throwable].
    """

    def _check(jobs, *, timeout, cpu_budget):
        if calls is not None:
            calls.append(jobs)
        out = []
        for job in jobs:
            results = []
            for f in job.files:
                text = Path(f).read_text()
                sneaky = re.search(r"SNEAKY:(\[[^]]*\]|\w+)(?:@(\d+))?", text)
                if sneaky:
                    first = (f"{sneaky.group(2) or 1}: unreported exception {sneaky.group(1).strip('[]')};"
                             " must be caught or declared to be thrown")
                    results.append(check.FileResult(
                        "flagged", kinds=("flow",), errors=1, first=first, messages=(first,), bytecode_only=True,
                    ))
                elif "FLAG" in text:
                    results.append(check.FileResult(
                        "flagged", kinds=("name",), errors=1, first="1: cannot find symbol: variable xx",
                        messages=("1: cannot find symbol: variable xx",),
                    ))
                elif "NOTE" in text:
                    results.append(check.FileResult(
                        "noted", kinds=("type",), errors=1, first="1: incompatible types: a",
                        messages=("1: incompatible types: a",),
                    ))
                elif "ENV" in text:
                    results.append(check.unchecked("absent classes", first="1: package org.absent does not exist"))
                else:
                    results.append(check.FileResult("clean"))
            out.append(results)
        return out

    return _check


def _app(make_jar):
    return make_jar("app.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"})


def test_flagged_file_replaced_by_clean_retry(make_jar, tmp_path: Path):
    jar = _app(make_jar)
    runner = writing_runner({
        "vineflower": {"com/x/A.java": "class A {} // FLAG", "com/x/B.java": "class B {}"},
        "cfr": {"com/x/A.java": "class A { /* cfr */ }"},
    })
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok" and not report.partial
    assert report.flagged == [] and report.missing_classes == 0
    assert [(a.engine, a.level, a.flagged) for a in report.attempts] == [
        ("vineflower", "archive", 1), ("cfr", "class", 0),
    ]
    assert (tmp_path / "out/src/com/x/A.java").read_text() == "class A { /* cfr */ }"
    assert report.check["checked"] == 2 and report.check["flagged"] == 0
    assert runner.sent[1] == ["com/x/A.class"]


def test_all_engines_flagged_keeps_first_file_and_is_partial(make_jar, tmp_path: Path):
    """The same engine error from all engines: each engine gets its try (#108)."""
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    runner = writing_runner({name: {"com/x/A.java": f"class A {{}} // FLAG {name}"} for name in ENGINES})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.partial and report.missing_classes == 0
    assert report.flagged == [{
        "path": "com/x/A.java", "engine": "vineflower", "kinds": ["name"],
        "errors": 1, "first": "1: cannot find symbol: variable xx",
        "messages": ["1: cannot find symbol: variable xx"],
    }]
    assert "FLAG vineflower" in (tmp_path / "out/src/com/x/A.java").read_text()
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower", "procyon", "jd"]
    assert report.check["flagged"] == 1


def test_retries_of_a_class_stop_when_the_next_engine_gives_the_same_first_error(make_jar, tmp_path: Path):
    """#108: bytecode can throw an undeclared checked exception, for example after Lombok's @SneakyThrows.

    The same such error from two engines in a row is in the bytecode, and the next engines
    give it too. The line of the error can differ between engines; only its message counts.
    """
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    runner = writing_runner({
        name: {"com/x/A.java": f"// SNEAKY:IOException@{i + 3} {name}"} for i, name in enumerate(ENGINES)
    })
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [(a.engine, a.flagged) for a in report.attempts] == [("vineflower", 1), ("cfr", 1)]
    assert (tmp_path / "out/src/com/x/A.java").read_text() == "// SNEAKY:IOException@3 vineflower"
    assert report.partial and [(f["engine"], f["first"]) for f in report.flagged] == [
        ("vineflower", "3: unreported exception IOException; must be caught or declared to be thrown"),
    ]


def test_retries_stop_at_the_same_first_error_as_the_engine_before(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    files = {"vineflower": "SNEAKY:A", "cfr": "FLAG", "fernflower": "SNEAKY:A", "procyon": "SNEAKY:A", "jd": "x"}
    runner = writing_runner({name: {"com/x/A.java": f"// {text}"} for name, text in files.items()})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower", "procyon"]
    assert report.flagged[0]["first"].startswith("1: unreported exception A;")



@pytest.mark.parametrize("exception", [
    "java.lang.Throwable", "java.lang.Exception", "capture#1 of ? extends java.lang.Exception",
])
def test_retries_go_on_after_the_same_error_for_a_cast_that_an_engine_can_drop(make_jar, tmp_path: Path, exception):
    """Review C2: for `throw (T) t`, Vineflower and CFR drop the cast and give the same error, but JD keeps it."""
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    files = {name: f"// SNEAKY:[{exception}] {name}" for name in list(ENGINES)[:-1]} | {"jd": "// jd keeps the cast"}
    runner = writing_runner({name: {"com/x/A.java": text} for name, text in files.items()})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == list(ENGINES)
    assert (tmp_path / "out/src/com/x/A.java").read_text() == "// jd keeps the cast"
    assert not report.partial


def test_retries_stop_only_for_the_class_with_the_same_first_error(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b"})
    runner = writing_runner({
        name: {"com/x/A.java": "// SNEAKY:A", "com/x/B.java": f"// SNEAKY:{name}"} for name in ENGINES
    })
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower", "procyon", "jd"]
    sent = [[Path(p).name for p in s] for s in runner.sent[1:]]
    assert sent == [["A.class", "B.class"], ["B.class"], ["B.class"], ["B.class"]]
    assert [(f["path"], f["engine"]) for f in report.flagged] == [
        ("com/x/A.java", "vineflower"), ("com/x/B.java", "vineflower"),
    ]


def _marked_jar(make_jar, language: str):
    """com/g/G with the class-file marker of a Groovy or a Kotlin class (#108)."""
    from test_check import _class_with

    marker = {"groovy": {"interfaces": ("groovy/lang/GroovyObject",)}, "kotlin": {"utf8": ("Lkotlin/Metadata;",)}}
    return make_jar("app.jar", {"com/g/G.class": _class_with("com/g/G", **marker[language])})


_G = "com/g/G.java"


def test_a_groovy_class_gets_no_more_retries_when_two_engines_in_a_row_flag_it(make_jar, tmp_path: Path):
    """#101: no engine writes compilable Java for the call-site and meta-class code of groovyc, and each
    engine gives a different first error, so the #108 rule (the same bytecode-only error two times) never
    stops the retries. A FLAG file never settles under #108: its error is no bytecode-only kind."""
    files = {"vineflower": "// FLAG vf", "cfr": "// FLAG cfr", "fernflower": "// ff passes"}
    runner = writing_runner({name: {_G: text} for name, text in files.items()})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(_marked_jar(make_jar, "groovy"), "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [(a.engine, a.flagged) for a in report.attempts] == [("vineflower", 1), ("cfr", 1)]
    assert (tmp_path / "out/src" / _G).read_text() == "// FLAG vf"
    assert report.partial and [(f["path"], f["engine"]) for f in report.flagged] == [(_G, "vineflower")]


def test_a_kotlin_class_keeps_its_retries_when_two_engines_in_a_row_flag_it(make_jar, tmp_path: Path):
    """The rule is for Groovy only: a later engine can write compilable Java for a Kotlin or Scala class."""
    files = {"vineflower": "// FLAG vf", "cfr": "// FLAG cfr", "fernflower": "// ff passes"}
    runner = writing_runner({name: {_G: text} for name, text in files.items()})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(_marked_jar(make_jar, "kotlin"), "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower"]
    assert (tmp_path / "out/src" / _G).read_text() == "// ff passes"
    assert report.flagged == [] and not report.partial


def test_a_groovy_class_with_a_stub_but_no_flag_keeps_its_retries(make_jar, tmp_path: Path):
    """A stub is no flag: the rule needs two flagged files in a row."""
    files = {"vineflower": _VF_STUB, "cfr": "// FLAG cfr", "fernflower": "// ff passes"}
    runner = writing_runner({name: {_G: text} for name, text in files.items()})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(_marked_jar(make_jar, "groovy"), "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower"]
    assert (tmp_path / "out/src" / _G).read_text() == "// ff passes"
    assert report.stubs == [] and report.flagged == []


def test_a_groovy_class_whose_retry_passes_gets_the_retry_file(make_jar, tmp_path: Path):
    files = {"vineflower": "// FLAG vf", "cfr": "// cfr passes"}
    runner = writing_runner({name: {_G: text} for name, text in files.items()})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(_marked_jar(make_jar, "groovy"), "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]
    assert (tmp_path / "out/src" / _G).read_text() == "// cfr passes"
    assert report.flagged == []


def test_unchecked_retry_file_does_not_replace_held_file(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    runner = writing_runner({"vineflower": {"com/x/A.java": "// FLAG"}, "cfr": {"com/x/A.java": "// ENV"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    ctx.chain = ["vineflower", "cfr"]
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert (tmp_path / "out/src/com/x/A.java").read_text() == "// FLAG"
    assert report.flagged[0]["engine"] == "vineflower"


def test_kotlin_retry_file_does_not_replace_held_java(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    runner = writing_runner({"vineflower": {"com/x/A.java": "// FLAG"}, "cfr": {"com/x/A.kt": "class A"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    ctx.chain = ["vineflower", "cfr"]
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]  # the held class was retried
    assert (tmp_path / "out/src/com/x/A.java").read_text() == "// FLAG"
    assert not (tmp_path / "out/src/com/x/A.kt").exists()


def test_noted_files_are_written_and_not_partial(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "// NOTE"}}), checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert not report.partial and [a.engine for a in report.attempts] == ["vineflower"]
    assert report.check["noted"] == 1 and report.check["kinds"]["type"] == 1


def test_report_lists_noted_and_unchecked_files(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a", "com/x/B.class": b"b", "com/x/C.class": b"c"})
    runner = writing_runner({"vineflower": {"com/x/A.java": "// NOTE", "com/x/B.java": "// ENV", "com/x/C.java": "class C {}"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.noted == [{
        "path": "com/x/A.java", "engine": "vineflower", "kinds": ["type"], "errors": 1,
        "first": "1: incompatible types: a", "messages": ["1: incompatible types: a"],
    }]
    assert report.unchecked == [{
        "path": "com/x/B.java", "engine": "vineflower", "reason": "absent classes", "needs_java": None,
        "first": "1: package org.absent does not exist",
    }]
    assert not report.partial and report.flagged == []


def test_noted_retry_file_is_listed_with_its_engine(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    runner = writing_runner({"vineflower": {"com/x/A.java": "// FLAG"}, "cfr": {"com/x/A.java": "// NOTE cfr"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.flagged == [] and [(n["path"], n["engine"]) for n in report.noted] == [("com/x/A.java", "cfr")]


def test_no_fallback_writes_and_reports_flagged(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    runner = writing_runner({"vineflower": {"com/x/A.java": "// FLAG"}})
    ctx = make_ctx(tmp_path, runner, fallback=False, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.partial and report.flagged[0]["path"] == "com/x/A.java"
    assert [a.engine for a in report.attempts] == ["vineflower"]
    assert (tmp_path / "out/src/com/x/A.java").read_text() == "// FLAG"


def test_without_checker_the_flow_is_unchanged(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "// FLAG"}}))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.check is None and report.flagged == [] and not report.partial
    assert report.noted == report.unchecked == []
    assert [(a.engine, a.flagged) for a in report.attempts] == [("vineflower", 0)]


def test_newer_class_version_is_not_sent(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": _class_header(69)})  # Java 25 class, java_major 21
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({"vineflower": {"com/x/A.java": "class A {}"}}), checker=marker_checker(calls))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert calls == []
    assert report.check["unchecked_reasons"]["newer class version"] == 1
    assert report.check["needs_java"] == 25
    assert report.unchecked == [{
        "path": "com/x/A.java", "engine": "vineflower", "reason": "newer class version", "needs_java": 25,
        "first": None,
    }]


def test_package_info_is_not_checked_or_counted(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    calls: list = []
    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}", "com/x/package-info.java": "// FLAG"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker(calls))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert [Path(f).name for f in calls[0][0].files] == ["A.java"]
    assert (tmp_path / "out/src/com/x/package-info.java").is_file()
    assert report.check["checked"] == 1 and report.flagged == []


def test_check_trees_returns_none_after_interrupt(make_jar, tmp_path: Path):
    from decaf.engines import PROCESSES
    from decaf.pipeline import _check_trees, _Held

    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    tree = tmp_path / "tree"
    (tree / "com/x").mkdir(parents=True)
    (tree / "com/x/A.java").write_text("// FLAG")
    calls: list = []
    ctx = make_ctx(tmp_path, writing_runner({}), checker=marker_checker(calls))
    PROCESSES.kill_all()
    try:
        assert _check_trees([(tree, Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), jar, _Held())], ctx, None, "vineflower") is None
    finally:
        PROCESSES.reset()
    assert calls == []


def test_clean_check_writes_the_same_tree(make_jar, tmp_path: Path):
    jar = _app(make_jar)
    files = {"vineflower": {"com/x/A.java": "class A {}", "com/x/B.java": "class B {}"}}
    plain, checked = tmp_path / "plain", tmp_path / "checked"
    plain.mkdir()
    checked.mkdir()
    r1, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), make_ctx(plain, writing_runner(files)))
    r2, _ = process_artifact(
        Artifact(jar, "app.jar", ArtifactKind.ARCHIVE),
        make_ctx(checked, writing_runner(files), checker=marker_checker()),
    )

    def tree(root: Path) -> dict[str, str]:
        return {p.relative_to(root).as_posix(): p.read_text() for p in (root / "out").rglob("*.java")}

    assert tree(plain) == tree(checked)
    assert [(a.engine, a.level) for a in r1.attempts] == [(a.engine, a.level) for a in r2.attempts]
    assert r1.check is None and r2.check["checked"] == 2


_MR9 = "META-INF/versions/9/com/x/A.java"


def _run_mr(make_jar, tmp_path: Path, files: dict[str, dict[str, str]], *, fallback: bool, classes=None, calls=None):
    """A multi-release jar in mirror mode; returns the report and the written {path: text}."""
    jar = make_jar("mr.jar", classes or {"com/x/A.class": b"a", "META-INF/versions/9/com/x/A.class": b"a9"})
    ctx = make_ctx(tmp_path, writing_runner(files), fallback=fallback, checker=marker_checker(calls),
                   writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "mr.jar", ArtifactKind.ARCHIVE), ctx)
    out = tmp_path / "out/mr.jar"
    return report, {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.java")}


def test_two_files_with_one_stem_in_one_output(make_jar, tmp_path: Path):
    # The variants of a multi-release class are separate files: both are written, the flagged one is reported.
    report, written = _run_mr(
        make_jar, tmp_path, {"vineflower": {_MR9: "// FLAG", "com/x/A.java": "class A {}"}}, fallback=False,
    )
    assert written == {_MR9: "// FLAG", "com/x/A.java": "class A {}"}
    assert [f["path"] for f in report.flagged] == [_MR9] and report.partial
    assert report.check["checked"] == 2 and report.check["flagged"] == 1


def test_sibling_that_does_not_pass_is_not_deleted(make_jar, tmp_path: Path):
    report, written = _run_mr(
        make_jar, tmp_path, {"vineflower": {_MR9: "// FLAG", "com/x/A.java": "// ENV"}}, fallback=False,
    )
    assert written == {_MR9: "// FLAG", "com/x/A.java": "// ENV"}
    assert [f["path"] for f in report.flagged] == [_MR9]
    assert report.check["unchecked_reasons"]["absent classes"] == 1


def test_both_flagged_variants_are_written_and_reported(make_jar, tmp_path: Path):
    report, written = _run_mr(
        make_jar, tmp_path, {"vineflower": {_MR9: "// FLAG 9", "com/x/A.java": "// FLAG"}}, fallback=False,
    )
    assert written == {_MR9: "// FLAG 9", "com/x/A.java": "// FLAG"}
    assert [f["path"] for f in report.flagged] == [_MR9, "com/x/A.java"]


def test_same_key_sibling_in_one_output_is_not_deleted(make_jar, tmp_path: Path):
    files = {"vineflower": {"WEB-INF/classes/com/x/A.java": "// FLAG", "com/x/A.java": "// ENV"}}
    report, written = _run_mr(make_jar, tmp_path, files, fallback=False)
    assert written == {"WEB-INF/classes/com/x/A.java": "// FLAG", "com/x/A.java": "// ENV"}
    assert [f["path"] for f in report.flagged] == ["WEB-INF/classes/com/x/A.java"]


def test_same_key_sibling_in_one_output_is_not_held_for_the_flag_of_its_sibling(make_jar, tmp_path: Path):
    """Here the clean sibling sorts first (Windows sorts the test above this way). The flag of the other file
    must not hold it, and the flagged file stays reported (#150)."""
    files = {"vineflower": {"WEB-INF/classes/com/x/A.java": "// ENV", "com/x/A.java": "// FLAG"}}
    report, written = _run_mr(make_jar, tmp_path, files, fallback=False)
    assert written == {"WEB-INF/classes/com/x/A.java": "// ENV", "com/x/A.java": "// FLAG"}
    assert [f["path"] for f in report.flagged] == ["com/x/A.java"]


def test_same_key_files_of_one_output_are_both_written(make_jar, tmp_path: Path):
    """Only a file of an earlier output makes a file with the same key go (#107)."""
    files = {"vineflower": {"WEB-INF/classes/com/x/A.java": "class A { /* w */ }", "com/x/A.java": "class A {}"}}
    report, written = _run_mr(make_jar, tmp_path, files, fallback=False)
    assert written == {"WEB-INF/classes/com/x/A.java": "class A { /* w */ }", "com/x/A.java": "class A {}"}


def test_retry_of_a_held_class_keeps_the_written_variant(make_jar, tmp_path: Path):
    # The retry re-decompiles both variants; the writer keeps its first copy of the clean one.
    report, written = _run_mr(make_jar, tmp_path, {
        "vineflower": {_MR9: "class A { /* vf 9 */ }", "com/x/A.java": "// FLAG"},
        "cfr": {_MR9: "class A { /* cfr 9 */ }", "com/x/A.java": "class A { /* cfr */ }"},
    }, fallback=True)
    assert written == {_MR9: "class A { /* vf 9 */ }", "com/x/A.java": "class A { /* cfr */ }"}
    assert report.flagged == [] and report.collisions == [] and not report.partial
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]


def test_class_retry_sends_no_versioned_class(make_jar, tmp_path: Path):
    """#107: with a base class and its versioned twin in one retry tree, Vineflower writes no
    base class, and Fernflower writes the versioned body as the base class."""
    jar = make_jar("mr.jar", {"com/x/A.class": b"a", "META-INF/versions/9/com/x/A.class": b"a9"})
    runner = writing_runner({"vineflower": {"com/x/A.java": "// FLAG"}, "cfr": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    process_artifact(Artifact(jar, "mr.jar", ArtifactKind.ARCHIVE), ctx)
    assert runner.sent[1] == ["com/x/A.class"]


def test_class_retry_sends_the_lowest_version_of_a_class_with_no_base(make_jar, tmp_path: Path):
    """A class can exist only in META-INF/versions folders; Vineflower writes none of them (#107)."""
    jar = make_jar("mr.jar", {
        "com/x/A.class": b"a",
        "META-INF/versions/17/com/x/V.class": b"v17", "META-INF/versions/17/com/x/V$1.class": b"i17",
        "META-INF/versions/21/com/x/V.class": b"v21", "META-INF/versions/21/com/x/V$1.class": b"i21",
    })
    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}, "cfr": {"com/x/V.java": "class V {}"}})
    ctx = make_ctx(tmp_path, runner, checker=marker_checker())
    report, _ = process_artifact(Artifact(jar, "mr.jar", ArtifactKind.ARCHIVE), ctx)
    assert runner.sent[1] == ["META-INF/versions/17/com/x/V$1.class", "META-INF/versions/17/com/x/V.class"]
    assert report.missing_classes == 0


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("retry_copy", ["class A$B { /* cfr */ }", "class A$B { /* cfr */ } // FLAG"])
def test_class_retry_keeps_the_written_file_of_an_inner_class(make_jar, tmp_path: Path, mirror, retry_copy):
    """#107: a retry of A extracts A$B again, and its copy of A$B must not replace the written file."""
    jar = make_jar("app.jar", {"com/x/A.class": b"a", "com/x/A$B.class": b"b"})
    runner = writing_runner({
        "vineflower": {"com/x/A.java": "// FLAG", "com/x/A$B.java": "class A$B { /* vf */ }"},
        "cfr": {"com/x/A.java": "class A { /* cfr */ }", "com/x/A$B.java": retry_copy},
    })
    ctx = make_ctx(tmp_path, runner, checker=marker_checker(), writer=MirrorWriter(tmp_path / "out") if mirror else None)
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    out = tmp_path / ("out/app.jar" if mirror else "out/src")
    assert (out / "com/x/A$B.java").read_text() == "class A$B { /* vf */ }"
    assert (out / "com/x/A.java").read_text() == "class A { /* cfr */ }"
    assert report.flagged == [] and report.collisions == [] and report.java_files == 2


def test_flagged_versioned_variant_is_not_retried(make_jar, tmp_path: Path):
    # As for stubs (#96): a versioned key is no expected stem, so no retry.
    report, written = _run_mr(make_jar, tmp_path, {
        "vineflower": {_MR9: "// FLAG", "com/x/A.java": "class A {}"},
        "cfr": {_MR9: "class A { /* cfr 9 */ }", "com/x/A.java": "class A { /* cfr */ }"},
    }, fallback=True)
    assert [a.engine for a in report.attempts] == ["vineflower"]
    assert written == {_MR9: "// FLAG", "com/x/A.java": "class A {}"}
    assert [f["path"] for f in report.flagged] == [_MR9] and report.partial


def test_newer_multi_release_variant_is_not_sent(make_jar, tmp_path: Path):
    calls: list = []
    report, _ = _run_mr(
        make_jar, tmp_path,
        {"vineflower": {"META-INF/versions/25/com/x/A.java": "class A {}", "com/x/A.java": "class A {}"}},
        fallback=False, calls=calls,
        classes={"com/x/A.class": _class_header(52), "META-INF/versions/25/com/x/A.class": _class_header(69)},
    )
    sent = [Path(f).as_posix() for f in calls[0][0].files]
    assert len(sent) == 1 and sent[0].endswith("/com/x/A.java") and "META-INF" not in sent[0]
    assert report.check["unchecked_reasons"]["newer class version"] == 1 and report.check["needs_java"] == 25


def test_decompile_batch_checks_once_and_retries_flagged_member(make_jar, tmp_path: Path):
    from decaf.pipeline import _decompile_batch

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        for stem, text in (("com/a/A", "// FLAG"), ("com/b/B", "// NOTE")):
            out = Path(dest) / f"{stem}.java"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
        return EngineResult(spec.name, 0, False, 2, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    calls: list = []
    ctx = _batch_ctx(tmp_path, batch, chain=["vineflower", "cfr"])
    ctx.runner = writing_runner({"cfr": {"com/a/A.java": "class A { /* cfr */ }"}})
    ctx.checker = marker_checker(calls)
    ctx.java_major = 21
    done, requeue = _decompile_batch([m1, m2], ctx)
    assert requeue == []
    assert len(calls) == 2 and len(calls[0]) == 2  # one call for the batch, one for the a.jar retry
    a, b = done
    assert (a.attempts[0].flagged, b.attempts[0].flagged) == (1, 0)
    assert a.flagged == [] and not a.partial and a.attempts[-1].engine == "cfr"
    assert b.check["checked"] == 1 and b.flagged == []
    assert (a.noted, [(n["path"], n["engine"]) for n in b.noted]) == ([], [("com/b/B.java", "vineflower")])
    assert (tmp_path / "out/src/com/a/A.java").read_text() == "class A { /* cfr */ }"


# One hold step for #96 stubs and #99 flags (spec "Interfaces", merge rule).
_VF_STUB = "class A {\n   void m() {\n      // $VF: Couldn't be decompiled\n   }\n}\n"
_CFR_STUB = "class A {\n    void m() {\n        /*\n         * This method has failed to decompile.\n         */\n    }\n}\n"


def _run_one(make_jar, tmp_path: Path, files: dict[str, str], *, fallback: bool = True):
    jar = make_jar("app.jar", {"com/x/A.class": b"a"})
    ctx = make_ctx(tmp_path, writing_runner({k: {"com/x/A.java": v} for k, v in files.items()}),
                   fallback=fallback, checker=marker_checker())
    ctx.chain = ctx.chain[: len(files)] if fallback else ctx.chain
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    return report, (tmp_path / "out/src/com/x/A.java").read_text()


def test_stubbed_and_flagged_file_is_listed_under_both_keys(make_jar, tmp_path: Path):
    report, text = _run_one(make_jar, tmp_path, {"vineflower": _VF_STUB + "// FLAG\n"}, fallback=False)
    assert [(a.stubbed, a.flagged) for a in report.attempts] == [(1, 1)]
    assert [s["path"] for s in report.stubs] == ["com/x/A.java"]
    assert [f["path"] for f in report.flagged] == ["com/x/A.java"]
    assert text == _VF_STUB + "// FLAG\n" and report.partial and report.check["flagged"] == 1


def test_stubbed_file_with_a_noted_result_is_listed_as_noted(make_jar, tmp_path: Path):
    report, text = _run_one(make_jar, tmp_path, {"vineflower": _VF_STUB + "// NOTE\n"}, fallback=False)
    assert text == _VF_STUB + "// NOTE\n" and [s["path"] for s in report.stubs] == ["com/x/A.java"]
    assert [(n["path"], n["engine"]) for n in report.noted] == [("com/x/A.java", "vineflower")]


def test_flagged_retry_file_does_not_replace_a_stubbed_file(make_jar, tmp_path: Path):
    report, text = _run_one(make_jar, tmp_path, {"vineflower": _VF_STUB, "cfr": "// FLAG cfr"})
    assert text == _VF_STUB
    assert [s["engine"] for s in report.stubs] == ["vineflower"] and report.flagged == []
    assert [(a.engine, a.stubbed, a.flagged) for a in report.attempts] == [("vineflower", 1, 0), ("cfr", 0, 1)]


def test_stubbed_retry_file_does_not_replace_a_flagged_file(make_jar, tmp_path: Path):
    report, text = _run_one(make_jar, tmp_path, {"vineflower": "// FLAG", "cfr": _CFR_STUB})
    assert text == "// FLAG"
    assert [f["engine"] for f in report.flagged] == ["vineflower"] and report.stubs == []
    assert [(a.engine, a.stubbed, a.flagged) for a in report.attempts] == [("vineflower", 0, 1), ("cfr", 1, 0)]


def test_unchecked_retry_file_replaces_a_stubbed_file(make_jar, tmp_path: Path):
    # A stub is a known loss; a stub-free file that the check cannot examine (absent classes) is better.
    report, text = _run_one(make_jar, tmp_path, {"vineflower": _VF_STUB, "cfr": "// ENV"})
    assert text == "// ENV"
    assert report.stubs == [] and report.flagged == [] and not report.partial
    assert report.check["unchecked_reasons"]["absent classes"] == 1


def test_clean_retry_file_replaces_a_stubbed_and_flagged_file(make_jar, tmp_path: Path):
    report, text = _run_one(make_jar, tmp_path, {"vineflower": _VF_STUB + "// FLAG\n", "cfr": "class A {}"})
    assert text == "class A {}"
    assert report.stubs == [] and report.flagged == [] and not report.partial
    assert report.check["checked"] == 1 and report.check["flagged"] == 0


def test_decompile_batch_holds_stubs_and_flags_in_one_step(make_jar, tmp_path: Path):
    from decaf.pipeline import _decompile_batch

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        for stem, text in (("com/a/A", _VF_STUB), ("com/b/B", "// FLAG")):
            out = Path(dest) / f"{stem}.java"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
        return EngineResult(spec.name, 0, False, 2, "")

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x"})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": b"y"})
    calls: list = []
    ctx = _batch_ctx(tmp_path, batch, chain=["vineflower", "cfr"])
    ctx.runner = writing_runner({"cfr": {"com/a/A.java": "class A {}", "com/b/B.java": "class B {}"}})
    ctx.checker = marker_checker(calls)
    done, _ = _decompile_batch([m1, m2], ctx)
    a, b = done
    assert [(m.attempts[0].stubbed, m.attempts[0].flagged) for m in (a, b)] == [(1, 0), (0, 1)]
    assert len(calls[0]) == 2  # one check call for the batch, one job for each member
    assert a.stubs == b.stubs == [] and a.flagged == b.flagged == [] and not (a.partial or b.partial)
    assert (tmp_path / "out/src/com/a/A.java").read_text() == "class A {}"
    assert (tmp_path / "out/src/com/b/B.java").read_text() == "class B {}"


# A flagged file of a detached inner class: a retry of its outer class (#101).
_A, _A1, _A2 = "com/x/A.java", "com/x/A$1.java", "com/x/A$2.java"


def _run_family(make_jar, tmp_path: Path, files: dict[str, dict[str, str]], *, check: bool = True, classes=None):
    """com/x/A, A$1, and A$2 with class headers, so the check knows the outer class of each inner class.

    Returns the report, the written {path: text} in mirror mode, and the runner. check=False runs no check.
    classes replaces some class files, for example {"com/x/A$1.class": <bytes>}.
    """
    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1", "A$2")} | (classes or {}))
    runner = writing_runner(files)
    ctx = make_ctx(tmp_path, runner, checker=marker_checker() if check else None, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    out = tmp_path / "out/app.jar"
    return report, {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.java")}, runner


def test_a_flagged_detached_file_gets_a_retry_of_its_outer_class(make_jar, tmp_path: Path):
    """Vineflower wrote A$1 apart from A, and the check flags A$1. The family of A waits for a retry of A,
    and the file of CFR nests A$1."""
    report, written, runner = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG"},
        "cfr": {_A: "class A { /* cfr */ }"},
    })
    assert written == {_A: "class A { /* cfr */ }"}
    assert [(a.engine, a.level) for a in report.attempts] == [("vineflower", "archive"), ("cfr", "class")]
    assert runner.sent[1] == ["com/x/A$1.class", "com/x/A$2.class", "com/x/A.class"]
    assert report.flagged == [] and not report.partial and report.java_files == 1


def test_a_waiting_family_holds_its_clean_file_of_an_inner_class(make_jar, tmp_path: Path):
    """The file of CFR nests A$2 too, so the clean A$2 of Vineflower must not go out first."""
    _, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG", _A2: "class A$2 { /* vf */ }"},
        "cfr": {_A: "class A { /* cfr */ }"},
    })
    assert written == {_A: "class A { /* cfr */ }"}


@pytest.mark.parametrize("cfr", [
    {_A: "class A {} // FLAG cfr"},
    {_A: _CFR_STUB},
    {_A: "// ENV"},  # an unchecked file does not replace the flagged A$1 that it nests
    {_A: "class A { /* cfr */ }", _A1: "// FLAG cfr"},  # the clean A of Vineflower stays: the A of CFR is not better
    {_A: "class A {} // FLAG cfr", _A2: "class A$2 { /* cfr */ }"},  # the file of Vineflower nests A$2
])
def test_a_retry_family_that_is_not_better_leaves_the_waiting_family(make_jar, tmp_path: Path, cfr):
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG"}, "cfr": cfr,
    })
    assert written == {_A: "class A { /* vf */ }", _A1: "// FLAG"}
    assert [(f["path"], f["engine"]) for f in report.flagged] == [(_A1, "vineflower")]
    assert report.missing_classes == 0 and report.stubs == []


@pytest.mark.parametrize("cfr", [
    {_A1: "class A$1 { /* cfr */ }"},  # no file of the outer class
    {_A: "class A {} // FLAG cfr", _A1: "class A$1 { /* cfr */ }"},
])
def test_a_clean_retry_file_replaces_the_flagged_held_file_of_its_class_alone(make_jar, tmp_path: Path, cfr):
    """Vineflower wrote A$1 apart, so its file of A does not nest A$1. The clean A$1 of CFR replaces the flagged A$1
    alone, as for one file, and the clean A of Vineflower stays (#159). Then no file of the family has a flag, so A
    gets no more retries. Before, only a whole retry family replaced a held family."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG"}, "cfr": cfr,
    })
    assert written == {_A: "class A { /* vf */ }", _A1: "class A$1 { /* cfr */ }"}
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]
    assert report.flagged == [] and not report.partial and report.java_files == 2


def test_a_retry_replaces_only_the_held_files_that_it_makes_better(make_jar, tmp_path: Path):
    """The clean A of Vineflower stays: the clean A of Fernflower is not better. The clean A$1 of Fernflower replaces
    the flagged A$1 of Vineflower (#159). Before, the whole family of Fernflower replaced the held family."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG"},
        "cfr": {_A: "class A {} // FLAG cfr"},
        "fernflower": {_A: "class A { /* ff */ }", _A1: "class A$1 { /* ff */ }"},
    })
    assert written == {_A: "class A { /* vf */ }", _A1: "class A$1 { /* ff */ }"}
    assert report.flagged == [] and [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower"]


def test_a_retry_with_the_missing_outer_class_drops_the_held_file_of_its_inner_class(make_jar, tmp_path: Path):
    """Vineflower wrote A$1 only. The file of A from CFR nests A$1; before, the output had A$1 two times."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: "// FLAG"},
        "cfr": {_A: "class A { /* cfr */ }"},
    })
    assert written == {_A: "class A { /* cfr */ }"}
    assert report.flagged == [] and report.missing_classes == 0
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]  # the A of CFR passes, so the family is done


def test_a_clean_retry_file_replaces_a_stubbed_held_file_of_an_inner_class_alone(make_jar, tmp_path: Path):
    """The stubs of Vineflower hold A and A$1, so the family waits (#150). CFR and Fernflower flag A, so the stubbed
    A of Vineflower stays. Vineflower wrote A$1 apart, so the clean A$1 of Fernflower replaces the stubbed A$1 alone
    (#159). Before, only a whole retry family replaced a held family, and both stubs went out."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: _VF_STUB, _A1: _VF_STUB.replace("class A", "class A$1")},
        "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 {} // FLAG cfr"},
        "fernflower": {_A: "class A {} // FLAG ff", _A1: "class A$1 { /* ff */ }"},
    })
    assert written == {_A: _VF_STUB, _A1: "class A$1 { /* ff */ }"}
    assert [s["path"] for s in report.stubs] == [_A]


def test_a_retry_file_that_needs_a_flagged_file_of_an_inner_class_replaces_nothing(make_jar, tmp_path: Path):
    """The flagged A of Vineflower holds its family, with the clean A$1 (#150), and it nests A$2. CFR wrote A$2
    apart, so its clean A needs its own flagged A$2. A flagged file replaces nothing, so the held files stay
    (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "// FLAG", _A1: "class A$1 { /* vf */ }"},
        "cfr": {_A: "class A { /* cfr */ }", _A2: "class A$2 {} // FLAG cfr"},
    })
    assert written == {_A: "// FLAG", _A1: "class A$1 { /* vf */ }"}
    assert [(f["path"], f["engine"]) for f in report.flagged] == [(_A, "vineflower")]


@pytest.mark.parametrize("first", [{_A1: "class A$1 { /* vf */ }"}, {_A: "// FLAG", _A1: "class A$1 { /* vf */ }"}])
def test_a_held_file_of_an_inner_class_stays_when_the_retry_flags_its_own_file_of_that_class(
    make_jar, tmp_path: Path, first,
):
    """The family of A waits with the clean A$1 of Vineflower (#159, #150). CFR wrote A$1 apart too, so its file
    of A does not nest A$1: the clean A$1 of Vineflower stays, as for one file, because a flagged file does not
    replace it. Before, the flagged A$1 of CFR failed the whole retry family."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": first,
        "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 {} // FLAG cfr"},
    })
    assert written == {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* vf */ }"}
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]
    assert report.flagged == [] and not report.partial


def test_a_retry_output_without_a_check_replaces_no_waiting_family(make_jar, tmp_path: Path):
    """Ctrl-C can stop the check of a retry output (#112). Then its files replace no held family."""
    from decaf.pipeline import _apply_hold, _Held

    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1")})
    ctx = make_ctx(tmp_path, writing_runner({}))
    held = _Held(versions={"com/x/A": 52})
    first, retry = tmp_path / "vf", tmp_path / "cfr"
    for tree, files in (
        (first, {_A: "class A {}", _A1: "// FLAG"}), (retry, {_A: "class A { /* cfr */ }", _A1: "class A$1 {}"}),
    ):
        for rel, text in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)
    flag = check.FileResult("flagged", kinds=("name",), errors=1, first="1: cannot find symbol: variable this$0")
    _apply_hold(first, jar, {}, {_A: check.FileResult("clean"), _A1: flag}, "vineflower", held, ctx)
    assert sorted(held.files) == ["com/x/A", "com/x/A$1"]
    _apply_hold(retry, jar, {}, None, "cfr", held, ctx)
    assert sorted(held.files) == ["com/x/A", "com/x/A$1"] and list(retry.rglob("*.java")) == []


# A held file of an outer class makes its family wait, from any output (#150).
_A1_VF = "class A$1 { /* vf */ }"


@pytest.mark.parametrize("files, expected", [
    ({"vineflower": {_A: _VF_STUB, _A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }"}}, {_A: "class A { /* cfr */ }"}),
    ({"vineflower": {_A: "class A {} // FLAG", _A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }"}},
     {_A: "class A { /* cfr */ }"}),
    ({"vineflower": {_A: _VF_STUB, _A1: _A1_VF}, "cfr": {_A: _CFR_STUB}, "fernflower": {_A: "class A { /* ff */ }"}},
     {_A: "class A { /* ff */ }"}),
    ({"vineflower": {_A: _VF_STUB, _A1: _A1_VF}, "cfr": {_A: "// ENV"}}, {_A: "// ENV"}),  # a stub is a known loss
    ({"vineflower": {_A: "class A {} // FLAG", _A1: _A1_VF},
      "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* cfr */ }"}},
     {_A: "class A { /* cfr */ }", _A1: _A1_VF}),  # the clean A$1 of Vineflower stays: the A$1 of CFR is not better
    ({"vineflower": {_A: "class A {} // FLAG"}, "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 {} // FLAG cfr"},
      "fernflower": {_A: "class A { /* ff */ }"}}, {_A: "class A { /* ff */ }"}),  # xstream-1.4.21 with --engine cfr
])
def test_a_held_file_of_an_outer_class_makes_its_family_wait(make_jar, tmp_path: Path, files, expected):
    """A is held for a stub or a flag, and an engine writes A$1 apart from A, in the same output or in a retry.
    A later retry nests A$1, so the file of A$1 must not go out on its own: the family of A waits (#150), and
    decaf chooses the file of each class (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, files)
    assert written == expected
    assert report.flagged == [] and report.stubs == [] and report.missing_classes == 0
    assert report.java_files == len(expected)


def test_a_held_file_of_an_outer_class_makes_its_family_wait_without_the_check(make_jar, tmp_path: Path):
    """With --no-check, a stub still holds A, and the family of A still waits (#150)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: _VF_STUB, _A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }"},
    }, check=False)
    assert written == {_A: "class A { /* cfr */ }"}
    assert report.stubs == [] and report.missing_classes == 0 and report.java_files == 1


def test_a_file_that_an_earlier_output_wrote_fails_no_retry_family(make_jar, tmp_path: Path):
    """A retry drops its copy of a class that an earlier output wrote (#107), so that copy must not fail the retry
    family: the clean A of the retry replaces the held A (#150)."""
    from decaf.pipeline import _apply_hold, _Held

    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1")})
    ctx = make_ctx(tmp_path, writing_runner({}))
    held = _Held(versions={"com/x/A": 52}, written={"com/x/A$1"})
    first, retry = tmp_path / "vf", tmp_path / "cfr"
    for tree, files in ((first, {_A: "// FLAG"}), (retry, {_A: "class A { /* cfr */ }", _A1: "// FLAG cfr"})):
        for rel, text in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)
    flag = check.FileResult("flagged", kinds=("name",), errors=1, first="1: cannot find symbol: variable xx")
    _apply_hold(first, jar, {}, {_A: flag}, "vineflower", held, ctx)
    assert sorted(held.files) == ["com/x/A"] and held.waiting == {"com/x/A"}
    _apply_hold(retry, jar, {}, {_A: check.FileResult("clean"), _A1: flag}, "cfr", held, ctx)
    assert held.files == {} and held.waiting == set()
    assert sorted(p.relative_to(retry).as_posix() for p in retry.rglob("*.java")) == [_A]


def test_a_gap_fill_keeps_the_held_files_of_its_family_until_the_end_of_the_chain(make_jar, tmp_path: Path):
    """The flagged A of CFR fills the gap of the family (#159), and that file nests A$1. Fernflower wrote a clean
    A and a flagged A$1 apart: its A replaces the flagged A, and the clean A$1 of Vineflower comes back, because
    the held files that a gap fill nests wait until the files of the family are done."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_VF},
        "cfr": {_A: "class A {} // FLAG cfr"},
        "fernflower": {_A: "class A { /* ff */ }", _A1: "class A$1 {} // FLAG ff"},
    })
    assert written == {_A: "class A { /* ff */ }", _A1: _A1_VF}
    assert report.flagged == []


def test_a_held_file_of_an_outer_class_holds_the_family_of_its_copy(make_jar, tmp_path: Path):
    """A war can have a class at the root and under WEB-INF/classes (#107). The file of an inner class rides
    with the file of the outer class of its own copy. CFR gets each copy at its package root (#173)."""
    web = "WEB-INF/classes/"
    jar = make_jar("app.jar", {
        f"{root}com/x/{n}.class": _class_header(52) for root in ("", web) for n in ("A", "A$1")
    })
    vineflower = writing_runner({
        "vineflower": {_A: _VF_STUB, _A1: _A1_VF, web + _A: _VF_STUB, web + _A1: "class A$1 { /* vf web */ }"},
    })
    cfr_root = writing_runner({"cfr": {_A: "class A { /* cfr */ }"}})
    cfr_web = writing_runner({"cfr": {_A: "class A { /* cfr web */ }"}})

    def runner(spec, jar_path, target, dest, timeout, **kw):
        if spec.name != "cfr":
            return vineflower(spec, jar_path, target, dest, timeout, **kw)
        run = cfr_web if Path(target).as_posix().endswith("/WEB-INF/classes") else cfr_root
        return run(spec, jar_path, target, dest, timeout, **kw)

    ctx = make_ctx(tmp_path, runner, checker=marker_checker(), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    out = tmp_path / "out/app.jar"
    written = {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.java")}
    assert written == {_A: "class A { /* cfr */ }", web + _A: "class A { /* cfr web */ }"}
    assert report.stubs == [] and report.missing_classes == 0


def test_an_unchecked_retry_file_replaces_a_stubbed_held_file_but_not_a_clean_one(make_jar, tmp_path: Path):
    """An unchecked file replaces a stub, a known loss (#96): the unchecked A of CFR replaces the stubbed A of
    Vineflower. The held A$1 is clean, so the unchecked A$1 of CFR does not replace it (#159). Before, the stub of A
    let the whole unchecked family of CFR replace the held family."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: _VF_STUB, _A1: _A1_VF},
        "cfr": {_A: "// ENV", _A1: "// ENV 1"},
    })
    assert written == {_A: "// ENV", _A1: _A1_VF}
    assert report.stubs == []


def test_a_waiting_family_settles_like_one_file(make_jar, tmp_path: Path):
    """Two engines in a row flag A with an error that bytecode can cause legally (#108): A gets no more
    retries, and its held family goes out at the end."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A {} // SNEAKY:java.io.IOException", _A1: _A1_VF},
        "cfr": {_A: "class A {} // SNEAKY:java.io.IOException cfr"},
        "fernflower": {_A: "class A { /* ff */ }"},
    })
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]
    assert written == {_A: "class A {} // SNEAKY:java.io.IOException", _A1: _A1_VF}


def test_after_ctrl_c_no_retry_file_replaces_a_family_held_for_a_stub(make_jar, tmp_path: Path):
    """A file without a result replaces a stub, but not after Ctrl-C: then the check did not run (#112)."""
    from decaf import engines, stubs
    from decaf.pipeline import _apply_hold, _Held

    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1")})
    ctx = make_ctx(tmp_path, writing_runner({}))
    held = _Held()
    first, retry = tmp_path / "vf", tmp_path / "cfr"
    for tree, files in ((first, {_A: _VF_STUB, _A1: _A1_VF}), (retry, {_A: "class A { /* cfr */ }"})):
        for rel, text in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)
    _apply_hold(first, jar, {s.path: s for s in stubs.scan(first, "vineflower")}, None, "vineflower", held, ctx)
    assert sorted(held.files) == ["com/x/A", "com/x/A$1"] and held.waiting == {"com/x/A"}
    engines.PROCESSES.kill_all()
    try:
        _apply_hold(retry, jar, {}, None, "cfr", held, ctx)
    finally:
        engines.PROCESSES.reset()
    assert sorted(held.files) == ["com/x/A", "com/x/A$1"] and list(retry.rglob("*.java")) == []


def test_after_ctrl_c_a_retry_file_still_goes_in_for_a_class_without_a_held_file(make_jar, tmp_path: Path):
    """The stubbed A$1 of Vineflower makes its family wait, and no engine wrote A yet (#159, #160). After Ctrl-C,
    the clean A$1 of CFR does not replace the stub (#112), but the A of CFR goes in for the missing A, as for one
    file. Before, the A of CFR went too, and A was missing."""
    from decaf import engines, stubs
    from decaf.pipeline import _apply_hold, _Held

    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1")})
    ctx = make_ctx(tmp_path, writing_runner({}))
    held = _Held()
    first, retry = tmp_path / "vf", tmp_path / "cfr"
    for tree, files in ((first, {_A1: _A1_STUB}), (retry, {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* cfr */ }"})):
        for rel, text in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)
    _apply_hold(first, jar, {s.path: s for s in stubs.scan(first, "vineflower")}, None, "vineflower", held, ctx)
    assert sorted(held.files) == ["com/x/A$1"] and held.waiting == {"com/x/A"}
    engines.PROCESSES.kill_all()
    try:
        _apply_hold(retry, jar, {}, None, "cfr", held, ctx)
    finally:
        engines.PROCESSES.reset()
    assert {k: f.engine for k, f in held.files.items()} == {"com/x/A": "cfr", "com/x/A$1": "vineflower"}
    assert list(retry.rglob("*.java")) == []


# A file of an inner class without the file of its outer class (#159), and a stubbed file of an inner class (#160).
_A1_STUB = _VF_STUB.replace("class A", "class A$1")


@pytest.mark.parametrize("files, expected", [
    ({"vineflower": {_A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }"}}, {_A: "class A { /* cfr */ }"}),
    ({"vineflower": {_A1: _A1_VF, _A2: "class A$2 { /* vf */ }"}, "cfr": {_A: "class A { /* cfr */ }"}},
     {_A: "class A { /* cfr */ }"}),
    ({"vineflower": {_A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }", _A2: "class A$2 { /* cfr */ }"}},
     {_A: "class A { /* cfr */ }", _A2: "class A$2 { /* cfr */ }"}),  # CFR nests A$1 and writes A$2 apart
])
def test_a_file_of_an_inner_class_waits_when_its_output_has_no_file_of_the_outer_class(
    make_jar, tmp_path: Path, files, expected,
):
    """Vineflower wrote no file of A, for example when A failed as a whole (#96), but it wrote A$1 apart. The
    retry of A nests A$1, so the file of A$1 must not go out first: its family waits (#159). Before, the output
    had A$1 two times."""
    report, written, _ = _run_family(make_jar, tmp_path, files)
    assert written == expected
    assert report.java_files == len(expected) and report.missing_classes == 0
    assert report.flagged == [] and not report.partial


def test_a_file_of_an_inner_class_waits_without_the_check_too(make_jar, tmp_path: Path):
    """With --no-check, the retry file of A has no check result. It fills the gap of A and nests A$1, and it has no
    stub, so the files of the family are done, and no more retries follow (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }"},
    }, check=False)
    assert written == {_A: "class A { /* cfr */ }"}
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]


@pytest.mark.parametrize("fernflower, expected, engines", [
    ({}, {_A: "// ENV"}, ["vineflower", "cfr", "fernflower", "procyon", "jd"]),
    ({_A: "class A { /* ff */ }"}, {_A: "class A { /* ff */ }"}, ["vineflower", "cfr", "fernflower"]),
])
def test_an_unchecked_retry_file_that_nests_a_flagged_held_file_keeps_its_family_waiting(
    make_jar, tmp_path: Path, fernflower, expected, engines,
):
    """The unchecked A of CFR fills the gap of A, and it nests the flagged A$1 of Vineflower. An unchecked file does
    not replace a flagged file (#99), so A gets more retries, and a clean A replaces the A of CFR. Else the A of CFR
    goes out at the end (#159). Before, A was missing at the end."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: "class A$1 {} // FLAG"}, "cfr": {_A: "// ENV"}, "fernflower": fernflower,
    })
    assert written == expected
    assert [a.engine for a in report.attempts] == engines
    assert report.flagged == [] and report.missing_classes == 0


@pytest.mark.parametrize("files, expected", [
    ({"vineflower": {_A1: _A1_VF}, "cfr": {_A: "class A {} // FLAG cfr"}}, {_A: "class A {} // FLAG cfr"}),
    ({"vineflower": {_A1: _A1_VF}, "cfr": {_A: _CFR_STUB}}, {_A: _CFR_STUB}),
    ({"vineflower": {_A1: "class A$1 {} // FLAG"}, "cfr": {_A: "class A {} // FLAG cfr"}},
     {_A: "class A {} // FLAG cfr"}),
    ({"vineflower": {_A1: _A1_VF}, "cfr": {_A: "class A {} // FLAG cfr", _A2: "class A$2 { /* cfr */ }"}},
     {_A: "class A {} // FLAG cfr", _A2: "class A$2 { /* cfr */ }"}),  # CFR wrote A$2 apart: it stays with A
    ({"vineflower": {_A1: _A1_VF, _A2: "class A$2 { /* vf */ }"},
      "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 {} // FLAG cfr"}},
     {_A: "class A {} // FLAG cfr", _A1: _A1_VF}),  # CFR wrote A$1 apart and nests A$2
])
def test_the_first_retry_file_of_a_missing_outer_class_fills_the_gap_of_its_family(
    make_jar, tmp_path: Path, files, expected,
):
    """No engine wrote a good file of A. The first retry file of A fills the gap of its family, also a stubbed or
    flagged one, so A is not missing at the end. It nests the held files of the inner classes that its engine
    did not write apart, so these go at the end of the chain (#159). Before, the output had A$1 two times, or A
    was missing."""
    report, written, _ = _run_family(make_jar, tmp_path, files)
    assert written == expected
    assert report.missing_classes == 0 and report.java_files == len(expected)


@pytest.mark.parametrize("cfr", [
    {_A: "class A {} // FLAG cfr"},
    {_A: "class A { /* cfr */ }", _A2: "class A$2 {} // FLAG cfr"},  # the flagged A$2 fails the family of CFR
])
def test_a_later_good_retry_replaces_a_gap_fill_as_a_whole(make_jar, tmp_path: Path, cfr):
    """The files of CFR fill the gap of the family of A, and they wait for a better retry (#159). The family of
    Fernflower is good, so it replaces all of them, also the clean A of CFR."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_VF}, "cfr": cfr, "fernflower": {_A: "class A { /* ff */ }"},
    })
    assert written == {_A: "class A { /* ff */ }"}
    assert report.flagged == [] and report.java_files == 1


def test_after_ctrl_c_a_retry_file_still_fills_the_gap_of_a_family(make_jar, tmp_path: Path):
    """After Ctrl-C, the check of a retry output can be missing, but no file of a killed engine comes here (#112).
    So the A of CFR fills the gap of A, as for one file, and it holds A$1: the family is done, and the held A$1 of
    Vineflower goes. Before, the A of CFR went, and A was missing."""
    from decaf import engines
    from decaf.pipeline import _apply_hold, _Held

    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1")})
    ctx = make_ctx(tmp_path, writing_runner({}))
    held = _Held()
    first, retry = tmp_path / "vf", tmp_path / "cfr"
    for tree, files in ((first, {_A1: _A1_VF}), (retry, {_A: "class A { /* cfr */ }"})):
        for rel, text in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)
    _apply_hold(first, jar, {}, None, "vineflower", held, ctx)
    assert sorted(held.files) == ["com/x/A$1"] and held.waiting == {"com/x/A"}
    engines.PROCESSES.kill_all()
    try:
        _apply_hold(retry, jar, {}, None, "cfr", held, ctx)
    finally:
        engines.PROCESSES.reset()
    assert held.files == {} and held.waiting == set()
    assert [p.relative_to(retry).as_posix() for p in retry.rglob("*.java")] == [_A]


@pytest.mark.parametrize("cfr, expected", [
    ({_A: "class A { /* cfr */ }"}, {_A: "class A { /* cfr */ }"}),
    ({_A: "// ENV"}, {_A: "// ENV"}),  # the stub of A$1 is a known loss for the whole family
    ({_A: "class A { /* cfr */ }", _A1: "// ENV 1"}, {_A: "class A { /* vf */ }", _A1: "// ENV 1"}),  # A of VF stays
])
def test_a_stubbed_file_of_an_inner_class_holds_its_family(make_jar, tmp_path: Path, cfr, expected):
    """Vineflower wrote A$1 apart with a stub (#96). Its family waits, so A gets a retry, and the file of A from
    CFR can nest A$1 without the stub (#160). Before, A got no retry, and the stub of A$1 went out."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: _A1_STUB}, "cfr": cfr,
    })
    assert written == expected
    assert [(a.engine, a.level, a.stubbed) for a in report.attempts] == [
        ("vineflower", "archive", 1), ("cfr", "class", 0),
    ]
    assert report.stubs == [] and not report.partial


@pytest.mark.parametrize("first, with_check", [
    ({_A: "class A { /* vf */ }", _A1: _A1_STUB}, False),
    ({_A1: _A1_STUB}, True),
    ({_A1: _A1_STUB}, False),
])
def test_a_stubbed_file_of_an_inner_class_holds_its_family_without_the_check_or_the_outer_file(
    make_jar, tmp_path: Path, first, with_check,
):
    """With --no-check, a stub is the only known loss, and it holds the family too (#160). Without a file of A,
    the stubbed A$1 must not go out next to the A of CFR, which nests A$1 (#159)."""
    report, written, _ = _run_family(
        make_jar, tmp_path, {"vineflower": first, "cfr": {_A: "class A { /* cfr */ }"}}, check=with_check,
    )
    assert written == {_A: "class A { /* cfr */ }"}
    assert report.stubs == [] and report.missing_classes == 0 and not report.partial


def test_a_family_held_for_a_stubbed_file_of_an_inner_class_goes_out_as_it_was_when_no_retry_helps(
    make_jar, tmp_path: Path,
):
    """The flagged A of CFR replaces nothing, so the held files of Vineflower go out at the end of the chain, and
    the report lists the stub of A$1 (#160)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: _A1_STUB}, "cfr": {_A: "class A {} // FLAG cfr"},
    })
    assert written == {_A: "class A { /* vf */ }", _A1: _A1_STUB}
    assert [(a.engine, a.level) for a in report.attempts][:2] == [("vineflower", "archive"), ("cfr", "class")]
    assert [(s["path"], s["engine"]) for s in report.stubs] == [(_A1, "vineflower")] and report.partial
    assert report.flagged == []


def test_decompile_batch_holds_the_family_of_a_stubbed_file_of_an_inner_class(make_jar, tmp_path: Path):
    """The hold step of a batch member follows the same rules (#160)."""
    from decaf.pipeline import _decompile_batch

    def batch(spec, jar_path, targets, dest, timeout, java="java", cpu_budget=None):
        for rel, text in ((_A, "class A { /* vf */ }"), (_A1, _A1_STUB), ("com/b/B.java", "class B {}")):
            out = Path(dest) / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
        return EngineResult(spec.name, 0, False, 3, "")

    m1 = _member(make_jar, "a.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1")})
    m2 = _member(make_jar, "b.jar", {"com/b/B.class": _class_header(52)})
    ctx = _batch_ctx(tmp_path, batch, chain=["vineflower", "cfr"])
    ctx.runner = writing_runner({"cfr": {_A: "class A { /* cfr */ }"}})
    ctx.checker = marker_checker()
    done, requeue = _decompile_batch([m1, m2], ctx)
    a, b = done
    assert requeue == [] and a.stubs == [] and not a.partial
    assert [x.engine for x in a.attempts] == ["vineflower", "cfr"] and [x.engine for x in b.attempts] == ["vineflower"]
    out = tmp_path / "out/src"
    assert {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.java")} == {
        _A: "class A { /* cfr */ }", "com/b/B.java": "class B {}",
    }


# For each class of a family that waits, the file of the earliest engine stays, unless a later file is better as
# for one file. A file holds its class and the inner classes that its engine did not write apart (#159).
@pytest.mark.parametrize("fernflower, expected", [
    ({}, {_A1: "class A$1 { /* cfr */ }", _A2: "class A$2 { /* vf */ }"}),
    ({_A: "class A { /* ff */ }"}, {_A: "class A { /* ff */ }"}),  # the A of Fernflower nests A$1 and A$2
])
def test_retry_files_of_inner_classes_fill_the_gaps_of_a_family_without_a_file_of_the_outer_class(
    make_jar, tmp_path: Path, fernflower, expected,
):
    """No engine wrote a file of A yet. The A$1 of CFR fills a gap next to the A$2 of Vineflower, because neither
    file nests the class of the other. The family waits for a file of A (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A2: "class A$2 { /* vf */ }"}, "cfr": {_A1: "class A$1 { /* cfr */ }"}, "fernflower": fernflower,
    })
    assert written == expected
    assert report.missing_classes == (0 if fernflower else 1)


def test_an_unchecked_retry_file_that_nests_a_stubbed_held_file_ends_the_wait(make_jar, tmp_path: Path):
    """A stub is a known loss, also in a flagged file (#96): the unchecked A of CFR nests the stubbed and flagged
    A$1 of Vineflower, so the family is done, and A gets no more retries (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_STUB + "// FLAG\n"}, "cfr": {_A: "// ENV"},
    })
    assert written == {_A: "// ENV"}
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]
    assert report.stubs == [] and report.flagged == []


_A11 = "com/x/A$1$1.java"


def _run_deep(make_jar, tmp_path: Path, files: dict[str, dict[str, str]]):
    """com/x/A, A$1, and A$1$1, an inner class of the inner class A$1. Returns the report and the written files."""
    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1", "A$1$1")})
    ctx = make_ctx(tmp_path, writing_runner(files), checker=marker_checker(), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    out = tmp_path / "out/app.jar"
    return report, {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.java")}


@pytest.mark.parametrize("files, expected", [
    ({"vineflower": {_A1: _A1_VF, _A11: "class A$1$1 { /* vf */ }"},  # the flagged A$1 of CFR nests A$1$1
      "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 {} // FLAG cfr"}},
     {_A: "class A { /* cfr */ }", _A1: _A1_VF, _A11: "class A$1$1 { /* vf */ }"}),
    ({"vineflower": {_A: "class A {} // FLAG", _A1: "class A$1 { /* vf, nests A$1$1 */ }"},
      "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 {} // FLAG cfr", _A11: "class A$1$1 { /* cfr */ }"}},
     {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* vf, nests A$1$1 */ }"}),
    ({"vineflower": {_A1: _A1_VF, _A11: "class A$1$1 { /* vf */ }"},
      "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 {} // FLAG cfr"}},
     {_A: "class A {} // FLAG cfr", _A1: _A1_VF, _A11: "class A$1$1 { /* vf */ }"}),
    ({"vineflower": {_A1: "class A$1 {} // FLAG vf, nests A$1$1"},
      "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* cfr */ }", _A11: "class A$1$1 {} // FLAG cfr"}},
     {_A: "class A { /* cfr */ }", _A1: "class A$1 {} // FLAG vf, nests A$1$1"}),
    ({"vineflower": {_A: "class A {} // FLAG vf, nests A$1", _A11: "class A$1$1 { /* vf */ }"},  # A$1$1 apart
      "cfr": {_A: "class A {} // FLAG cfr"}},
     {_A: "class A {} // FLAG vf, nests A$1", _A11: "class A$1$1 { /* vf */ }"}),
])
def test_a_file_that_nests_an_inner_class_of_an_inner_class_keeps_it(make_jar, tmp_path: Path, files, expected):
    """A$1$1 is an inner class of A$1. A file of A$1 that stays keeps the A$1$1 that it nests, and a file of A$1$1
    goes out only next to a file of A$1 that does not nest it. So no class goes out two times, and no class gets
    lost (#159)."""
    _, written = _run_deep(make_jar, tmp_path, files)
    assert written == expected


def test_a_clean_retry_file_replaces_a_stubbed_held_file_next_to_a_gap_fill(make_jar, tmp_path: Path):
    """The flagged A of CFR fills the gap of A. CFR wrote A$1 apart, so its clean A$1 replaces the stubbed A$1 of
    Vineflower, as for one file (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_STUB}, "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 { /* cfr */ }"},
    })
    assert written == {_A: "class A {} // FLAG cfr", _A1: "class A$1 { /* cfr */ }"}
    assert report.stubs == [] and [f["path"] for f in report.flagged] == [_A]


@pytest.mark.parametrize("with_check", [True, False])
def test_a_held_file_that_a_gap_fill_nests_comes_back_when_a_later_engine_writes_its_class_apart(
    make_jar, tmp_path: Path, with_check,
):
    """The stubbed A of CFR fills the gap of A and nests A$1, so the clean A$1 of Vineflower waits. Fernflower wrote
    a clean A and a stubbed A$1 apart: its A replaces the stub, and the A$1 of Vineflower comes back (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_VF},
        "cfr": {_A: _CFR_STUB},
        "fernflower": {_A: "class A { /* ff */ }", _A1: _A1_STUB.replace("$VF:", "$FF:")},
    }, check=with_check)
    assert written == {_A: "class A { /* ff */ }", _A1: _A1_VF}
    assert report.stubs == []


def test_an_unchecked_retry_file_does_not_replace_a_clean_held_file(make_jar, tmp_path: Path):
    """The clean A$1 of Vineflower waits for a file of A (#159). CFR wrote its A$1 apart, and the check could not
    examine that file, so the clean A$1 of Vineflower stays, as for one file."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_VF}, "cfr": {_A: "class A { /* cfr */ }", _A1: "// ENV cfr"},
    })
    assert written == {_A: "class A { /* cfr */ }", _A1: _A1_VF}
    assert report.unchecked == [] and [a.engine for a in report.attempts] == ["vineflower", "cfr"]


def test_an_unchecked_held_file_comes_back_when_a_clean_retry_file_replaces_a_gap_fill(make_jar, tmp_path: Path):
    """The flagged A of CFR fills the gap of A and nests the unchecked A$1 of Vineflower. The clean A of Fernflower
    replaces the flagged A, and Fernflower wrote a stubbed A$1 apart. So the A$1 of Vineflower comes back: it is the
    file of an earlier engine, and it needs no check result for that (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: "// ENV vf"},
        "cfr": {_A: "class A {} // FLAG cfr"},
        "fernflower": {_A: "class A { /* ff */ }", _A1: _A1_STUB.replace("$VF:", "$FF:")},
    })
    assert written == {_A: "class A { /* ff */ }", _A1: "// ENV vf"}
    assert report.flagged == [] and report.stubs == []


def test_two_retry_files_with_the_key_of_the_outer_class_are_both_written(make_jar, tmp_path: Path):
    """The flagged A of Vineflower makes its family wait (#150). The source has one class file of A, so both files
    of CFR get the key of A. One file replaces the flagged A, and the other goes out too, as for two files with one
    key in an output without a family (#107)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A {} // FLAG"},
        "cfr": {_A: "class A { /* cfr */ }", "WEB-INF/classes/com/x/A.java": "class A { /* cfr, other */ }"},
    })
    assert written == {_A: "class A { /* cfr */ }", "WEB-INF/classes/com/x/A.java": "class A { /* cfr, other */ }"}
    assert report.flagged == []


def _a1_class(shape: str) -> dict[str, bytes]:
    """A class file of A$1. An inner class of the JVM lists itself in its InnerClasses attribute ("inner"). A class
    that does not is a top-level class for the JVM: it has no InnerClasses attribute ("top"), or a tool removed the
    attribute and kept its name in the constant pool ("stripped"). The A of "inner" names A$1 (#207)."""
    from test_check import _class_with, _class_with_inner_classes

    data = {
        "inner": _class_with_inner_classes("com/x/A$1", (("com/x/A$1", "com/x/A"),)),
        "top": _class_with("com/x/A$1"),
        "stripped": _class_with("com/x/A$1", utf8=("InnerClasses",)),
    }[shape]
    outer = {"com/x/A.class": _class_with("com/x/A", interfaces=("com/x/A$1",))} if shape == "inner" else {}
    return {"com/x/A$1.class": data, **outer}


@pytest.mark.parametrize("shape, expected", [
    ("top", {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* vf */ }"}),
    ("stripped", {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* vf */ }"}),
    ("inner", {_A: "class A { /* cfr */ }"}),  # the A of CFR nests A$1
])
def test_a_retry_file_of_the_outer_class_does_not_nest_a_class_that_is_top_level_for_the_jvm(
    make_jar, tmp_path: Path, shape, expected,
):
    """CFR and Procyon get only the files of the outer classes in a class retry (#106). No InnerClasses attribute
    links A$1 to A, so CFR writes no file of A$1, and its file of A does not hold A$1. Thus the held A$1 of
    Vineflower stays. Before, the class was lost, and the report did not show it (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: "class A$1 { /* vf */ }"}, "cfr": {_A: "class A { /* cfr */ }"},
    }, classes=_a1_class(shape))
    assert written == expected
    assert report.java_files == len(expected) and report.missing_classes == 0


@pytest.mark.parametrize("fernflower, expected", [
    ({}, {_A: "class A { /* vf */ }", _A1: _A1_STUB}),
    ({_A: "class A { /* ff */ }", _A1: "class A$1 { /* ff */ }"}, {_A: "class A { /* vf */ }", _A1: "class A$1 { /* ff */ }"}),
])
def test_a_stubbed_file_of_a_class_that_is_top_level_for_the_jvm_stays_until_a_better_file_comes(
    make_jar, tmp_path: Path, fernflower, expected,
):
    """The stub of A$1 holds its family (#160), and the A of CFR does not hold A$1: no InnerClasses attribute links
    A$1 to A. So the stub stays, and the report lists it, until Fernflower gets the class file of A$1 in its retry
    and writes a better file of it. Before, the class was lost, and the report did not show it (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: _A1_STUB},
        "cfr": {_A: "class A { /* cfr */ }"},
        "fernflower": fernflower,
    }, classes=_a1_class("top"))
    assert written == expected
    assert [s["path"] for s in report.stubs] == ([_A1] if expected[_A1] == _A1_STUB else [])


@pytest.mark.parametrize("engine", ["cfr", "procyon"])
def test_a_retry_file_of_the_outer_class_does_not_nest_an_inner_class_of_a_top_level_class(
    make_jar, tmp_path: Path, engine,
):
    """A$1 does not list itself, so it is a top-level class for the JVM, and A$1$1 is an inner class of A$1. CFR and
    Procyon get only the outer classes in a class retry (#106), so their file of A holds neither A$1 nor A$1$1, also
    when no engine wrote a file of A$1. Thus the held A$1$1 of Vineflower stays. Before, it was lost (#159)."""
    from test_check import _class_with, _class_with_inner_classes

    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A11: "class A$1$1 { /* vf */ }"}, engine: {_A: f"class A {{ /* {engine} */ }}"},
    }, classes={
        "com/x/A$1.class": _class_with("com/x/A$1"),
        "com/x/A$1$1.class": _class_with_inner_classes("com/x/A$1$1", (("com/x/A$1$1", "com/x/A$1"),)),
    })
    assert written == {_A: f"class A {{ /* {engine} */ }}", _A11: "class A$1$1 { /* vf */ }"}


def test_a_retry_file_that_loses_an_inner_class_does_not_replace_the_file_that_nests_it(make_jar, tmp_path: Path):
    """Fernflower failed A$1 as a whole and wrote an empty file of it (#96), so its clean A does not hold A$1. The
    flagged A of Vineflower nests A$1, so it stays, and the report lists it: a class that no file holds is a known
    loss, as a stub is. Before, A$1 was lost, and the report did not show it (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A {} // FLAG vf"}, "fernflower": {_A: "class A { /* ff */ }", _A1: ""},
    })
    assert written == {_A: "class A {} // FLAG vf"}
    assert [f["path"] for f in report.flagged] == [_A]


def test_a_retry_file_replaces_a_held_file_whose_engine_lost_an_inner_class(make_jar, tmp_path: Path):
    """Vineflower failed A$1$2 as a whole and wrote an empty file of it (#96), so its clean A$1 does not hold A$1$2.
    The A$1 of Fernflower nests A$1$2, so it replaces the held A$1, as for a stub. Before, A$1$2 was lost (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A {} // FLAG vf", _A1: "class A$1 { /* vf */ }", "com/x/A$1$2.java": ""},
        "fernflower": {_A: "class A { /* ff */ }", _A1: "class A$1 { /* ff, nests A$1$2 */ }"},
    }, classes={"com/x/A$1$2.class": _class_header(52)})
    assert written == {_A: "class A { /* ff */ }", _A1: "class A$1 { /* ff, nests A$1$2 */ }"}
    assert report.flagged == []


@pytest.mark.parametrize("files, expected", [
    ({"vineflower": {_A: "", _A1: _A1_VF}, "fernflower": {_A: "class A { /* ff */ }", _A1: ""}},
     {_A: "class A { /* ff */ }", _A1: _A1_VF}),
    ({"vineflower": {_A: "class A { /* vf */ }", _A1: _A1_STUB}, "cfr": {_A: "class A {} // FLAG cfr"},
      "fernflower": {_A: "class A { /* ff */ }", _A1: ""}},
     {_A: "class A { /* vf */ }", _A1: _A1_STUB}),
])
def test_an_empty_file_counts_as_a_class_that_its_engine_wrote_apart(make_jar, tmp_path: Path, files, expected):
    """Fernflower writes an empty file for a class that fails as a whole, and decaf deletes it (#96). Fernflower
    wrote A$1 apart, so its file of A does not hold A$1, and the held A$1 of Vineflower stays. Before, the class
    was lost (#159)."""
    report, written, _ = _run_family(make_jar, tmp_path, files)
    assert written == expected
    assert report.java_files == len(expected) and report.missing_classes == 0


# --- CFR, Procyon, and JD get one package root at a time (#199, #200, #170, #171, #173) ---


def _tree(base: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return base


def by_name_runner(results=None, on_call=None):
    """Fake CFR: one file for each top-level class of the target, at the path of its class name (#199)."""
    calls: list[dict] = []

    def _run(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        target = Path(target)
        if target.is_dir():
            entries = {p.relative_to(target).as_posix(): p.read_bytes() for p in sorted(target.rglob("*.class"))}
        else:
            with zipfile.ZipFile(target) as zf:
                entries = {n: zf.read(n) for n in sorted(zf.namelist()) if n.endswith(".class")}
        calls.append({"engine": spec.name, "target": target, "classes": sorted(entries), "timeout": timeout,
                      "data": entries})
        n = 0
        for rel, data in entries.items():
            name = check.class_name(data) or rel[: -len(".class")]
            if "$" in name.rsplit("/", 1)[-1]:
                continue
            out = Path(dest) / (name + ".java")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"class X {{}} // {spec.name}\n")
            n += 1
        if on_call is not None:
            on_call(len(calls))
        if results is not None:
            r = results[len(calls) - 1]
            return EngineResult(spec.name, r.returncode, r.timed_out, n, r.stderr_tail, r.peak_rss_mb, r.out_of_memory)
        return EngineResult(spec.name, 0, False, n, "")

    _run.calls = calls
    return _run


def _sources(dest: Path) -> list[str]:
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())


def test_root_prefix_of_a_container_entry(tmp_path: Path):
    from decaf.pipeline import _root_prefix

    assert _root_prefix(tmp_path, "WEB-INF/classes/p/A.class") == "WEB-INF/classes/"
    assert _root_prefix(tmp_path, "app/WEB-INF/classes/p/A.class") == "app/WEB-INF/classes/"
    assert _root_prefix(tmp_path, "BOOT-INF/classes/META-INF/versions/11/p/A.class") == "BOOT-INF/classes/"


def test_root_prefix_from_the_class_name(tmp_path: Path, class_file):
    from decaf.pipeline import _root_prefix

    _tree(tmp_path, {
        "target/classes/p/A.class": class_file("p/A"),
        "target/classes/q/B$Nested.class": class_file("q/B$Nested"),
        "p/A.class": class_file("p/A"),
        "Top.class": class_file("Top"),
    })
    assert _root_prefix(tmp_path, "target/classes/p/A.class") == "target/classes/"
    assert _root_prefix(tmp_path, "target/classes/q/B$Nested.class") == "target/classes/"
    assert _root_prefix(tmp_path, "p/A.class") == ""
    assert _root_prefix(tmp_path, "Top.class") == ""


def test_root_prefix_cuts_a_versions_folder(tmp_path: Path, class_file):
    """A versions folder stays inside its root, as before (#202 is the multi-release fault)."""
    from decaf.pipeline import _root_prefix

    _tree(tmp_path, {
        "target/classes/META-INF/versions/11/p/A.class": class_file("p/A"),
        "META-INF/versions/11/p/A.class": class_file("p/A"),
    })
    assert _root_prefix(tmp_path, "target/classes/META-INF/versions/11/p/A.class") == "target/classes/"
    assert _root_prefix(tmp_path, "META-INF/versions/11/p/A.class") == ""


def test_root_prefix_is_empty_when_the_name_does_not_match(tmp_path: Path, class_file):
    from decaf.pipeline import _root_prefix

    _tree(tmp_path, {
        "target/classes/p/A.class": class_file("other/Z"),
        "target/classes/q/B.class": class_file("ses/q/B"),  # the end of the path, but not at a "/"
        "target/classes/r/C.class": b"x",
    })
    assert _root_prefix(tmp_path, "target/classes/p/A.class") == ""
    assert _root_prefix(tmp_path, "target/classes/q/B.class") == ""
    assert _root_prefix(tmp_path, "target/classes/r/C.class") == ""
    assert _root_prefix(tmp_path, "target/classes/s/Gone.class") == ""  # cannot be read


def _inner_class_file(name: str) -> bytes:
    """A class file of an inner class: it lists itself in its InnerClasses attribute, and its outer class names it
    (_outer_class_file), so it is no expected class (#206, #207)."""
    from test_check import _class_with_inner_classes

    return _class_with_inner_classes(name, ((name, name.rsplit("$", 1)[0]),))


def _outer_class_file(name: str, inner: str) -> bytes:
    """A class file whose constant pool names the inner class, as the code of a class that creates it."""
    from test_check import _class_with

    return _class_with(name, interfaces=(inner,))


def _class_folder(tmp_path: Path, class_file) -> Path:
    root = tmp_path / "in"
    _tree(root, {
        "target/classes/p/A.class": class_file("p/A"),
        "target/classes/q/B.class": _outer_class_file("q/B", "q/B$Nested"),
        "target/classes/q/B$Nested.class": _inner_class_file("q/B$Nested"),
    })
    return root


@pytest.mark.parametrize("engine", ["cfr", "procyon", "jd"])
def test_a_rooted_engine_gets_the_package_root_of_a_class_folder(tmp_path: Path, class_file, engine):
    """Before, CFR wrote p/A.java for target/classes/p/A.class, and the row had 2 missing classes (#199)."""
    root = _class_folder(tmp_path, class_file)
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine=engine, fallback=False, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(root, "_classes", ArtifactKind.CLASS_TREE), ctx)
    assert [c["classes"] for c in runner.calls] == [["p/A.class", "q/B$Nested.class", "q/B.class"]]
    assert (report.outcome, report.classes, report.missing_classes, report.java_files) == ("ok", 2, 0, 2)
    assert _sources(tmp_path / "out") == ["_classes/target/classes/p/A.java", "_classes/target/classes/q/B.java"]


def test_the_fallback_after_a_rooted_engine_writes_no_second_file(tmp_path: Path, class_file):
    """Before, Vineflower wrote target/classes/p/A.java next to the p/A.java of CFR, and java_files was 4 (#199)."""
    root = _class_folder(tmp_path, class_file)
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr", writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(root, "_classes", ArtifactKind.CLASS_TREE), ctx)
    assert [a.engine for a in report.attempts] == ["cfr"]
    assert (report.java_files, report.missing_classes) == (2, 0)


@pytest.mark.parametrize("engine", ["vineflower", "fernflower"])
def test_vineflower_and_fernflower_get_the_target_unchanged(make_jar, tmp_path: Path, class_file, engine):
    root = _class_folder(tmp_path, class_file)
    war = make_jar("app.war", {"WEB-INF/classes/com/x/A.class": class_file("com/x/A")})
    runner = writing_runner({engine: {"WEB-INF/classes/com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, engine=engine, fallback=False)
    process_artifact(Artifact(root, "_classes", ArtifactKind.CLASS_TREE), ctx)
    process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    assert runner.sent[0] == ["target/classes/p/A.class", "target/classes/q/B$Nested.class", "target/classes/q/B.class"]
    assert runner.calls[1] == (engine, war)


@pytest.mark.parametrize("engine", ["cfr", "procyon", "jd"])
def test_a_rooted_engine_gets_an_archive_without_a_container_entry_unchanged(make_jar, tmp_path: Path, class_file, engine):
    jar = make_jar("app.jar", {"com/x/A.class": class_file("com/x/A")})
    runner = writing_runner({engine: {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, engine=engine, fallback=False)
    process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert runner.calls == [(engine, jar)]


@pytest.mark.parametrize("engine", ["cfr", "procyon", "jd"])
def test_a_rooted_engine_gets_the_classes_of_a_war_without_their_container(make_jar, tmp_path: Path, class_file, engine):
    """Before, CFR and Procyon wrote com/x/A.java, and JD took WEB-INF.classes.com.x as the package (#170, #173)."""
    war = make_jar("app.war", {
        "WEB-INF/web.xml": "<web-app/>",
        "WEB-INF/classes/com/x/A.class": _outer_class_file("com/x/A", "com/x/A$1"),
        "WEB-INF/classes/com/x/A$1.class": _inner_class_file("com/x/A$1"),
    })
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine=engine, fallback=False, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    (call,) = runner.calls
    assert call["classes"] == ["com/x/A$1.class", "com/x/A.class"]
    assert call["target"].name == "classes.jar"  # a jar, as a plain jar: CFR writes other text from a folder
    assert not call["target"].exists()  # the temp jar goes after the run
    assert _sources(tmp_path / "out") == ["app.war/WEB-INF/classes/com/x/A.java", "app.war/WEB-INF/web.xml"]
    assert (report.java_files, report.missing_classes) == (1, 0)


def test_a_class_folder_target_is_not_modified(tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    files = {
        "target/classes/p/A.class": class_file("p/A"),
        "x/Z.class": class_file("x/Z"),
        "target/classes/r/C.class": b"no class file",
    }
    tree = _tree(tmp_path / "tree", files)
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    dest = tmp_path / "dest"
    dest.mkdir()
    res = _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), tree, dest, 100, java="java")
    assert {p.relative_to(tree).as_posix(): p.read_bytes() for p in tree.rglob("*") if p.is_file()} == files
    assert [c["classes"] for c in runner.calls] == [["target/classes/r/C.class", "x/Z.class"], ["p/A.class"]]
    assert runner.calls[1]["target"] != tree / "target/classes"  # a copy: r/C.class below it has no prefix
    assert _sources(dest) == ["target/classes/p/A.java", "target/classes/r/C.java", "x/Z.java"]
    assert res.java_files == 3


def test_a_retry_tree_with_two_roots_gives_two_runs(make_jar, tmp_path: Path, class_file):
    """A Spring Boot jar has its loader classes at the root and its own classes under BOOT-INF/classes/."""
    jar = make_jar("boot.jar", {
        "BOOT-INF/classes/com/x/Done.class": class_file("com/x/Done"),
        "BOOT-INF/classes/com/x/A.class": _outer_class_file("com/x/A", "com/x/A$1"),
        "BOOT-INF/classes/com/x/A$1.class": _inner_class_file("com/x/A$1"),
        "org/boot/L.class": class_file("org/boot/L"),
    })
    vineflower = writing_runner({"vineflower": {"BOOT-INF/classes/com/x/Done.java": "class Done {}"}})
    cfr = by_name_runner()

    def runner(spec, *a, **k):
        return (vineflower if spec.name == "vineflower" else cfr)(spec, *a, **k)

    ctx = make_ctx(tmp_path, runner, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "boot.jar", ArtifactKind.ARCHIVE), ctx)
    assert [c["classes"] for c in cfr.calls] == [["org/boot/L.class"], ["com/x/A$1.class", "com/x/A.class"]]
    assert [(a.engine, a.level, a.java_files) for a in report.attempts] == [("vineflower", "archive", 1), ("cfr", "class", 2)]
    assert _sources(tmp_path / "out") == [
        "boot.jar/BOOT-INF/classes/com/x/A.java", "boot.jar/BOOT-INF/classes/com/x/Done.java", "boot.jar/org/boot/L.java",
    ]
    assert report.missing_classes == 0


def _two_roots(tmp_path: Path, class_file) -> Path:
    return _tree(tmp_path / "tree", {"p/A.class": class_file("p/A"), "WEB-INF/classes/q/B.class": class_file("q/B")})


def test_the_roots_of_a_target_share_one_deadline(tmp_path: Path, class_file, monkeypatch):
    import decaf.pipeline as pipeline
    from decaf.pipeline import _run_engine

    now = [0.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: now[0])
    runner = by_name_runner(on_call=lambda n: now.__setitem__(0, now[0] + 60))
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    tree = _two_roots(tmp_path, class_file)
    res = _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), tree, tmp_path / "d1", 100, java="java")
    assert [c["timeout"] for c in runner.calls] == [100, 40]
    assert not res.timed_out
    runner.calls.clear()
    res = _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), tree, tmp_path / "d2", 50, java="java")
    assert [c["timeout"] for c in runner.calls] == [50]  # no time is left for the second root
    assert res.timed_out


def test_the_runs_of_the_roots_give_one_result(tmp_path: Path, class_file):
    """The first non-zero return code, the highest peak, and a timeout or an out of memory error of any run."""
    from decaf.pipeline import _run_engine

    runner = by_name_runner(results=[
        EngineResult("cfr", 0, False, 0, "a" * 1500, 100, False),
        EngineResult("cfr", 3, True, 0, "b" * 1500, 300, True),
        EngineResult("cfr", 5, False, 0, "c" * 1500, 200, False),
    ])
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    tree = _tree(tmp_path / "tree", {
        "p/A.class": class_file("p/A"), "BOOT-INF/classes/r/C.class": class_file("r/C"),
        "WEB-INF/classes/q/B.class": class_file("q/B"),
    })
    res = _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), tree, tmp_path / "d", 100, java="java")
    assert runner.calls[0]["target"] != tree  # a copy of the classes of the root ""
    assert runner.calls[2]["target"] == tree / "WEB-INF/classes"  # no copy: each class file below it has its prefix
    assert (res.engine, res.returncode, res.timed_out, res.java_files, res.peak_rss_mb, res.out_of_memory) == (
        "cfr", 3, True, 3, 300, True,
    )
    assert res.stderr_tail == ("a" * 1500 + "\n" + "b" * 1500 + "\n" + "c" * 1500)[-2000:]


def test_no_root_runs_after_ctrl_c(tmp_path: Path, class_file):
    from decaf.engines import PROCESSES
    from decaf.pipeline import _run_engine

    runner = by_name_runner(on_call=lambda n: PROCESSES.kill_all())
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    try:
        _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), _two_roots(tmp_path, class_file), tmp_path / "d", 100,
                    java="java")
    finally:
        PROCESSES.reset()
    assert len(runner.calls) == 1


def test_the_temp_folders_of_the_roots_go_when_the_engine_raises(tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    def exploding(*a, **k):
        raise RuntimeError("engine exploded")

    ctx = make_ctx(tmp_path, exploding, engine="cfr")
    with pytest.raises(RuntimeError):
        _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), _two_roots(tmp_path, class_file), tmp_path / "d", 100,
                    java="java")
    assert list(ctx.tmp_root.iterdir()) == []


# --- the report path of a held file is the path of the written file (#174) ---


@pytest.mark.parametrize("mirror", [False, True], ids=["merge", "mirror"])
def test_report_paths_of_a_war_are_the_paths_of_the_written_files(make_jar, tmp_path: Path, mirror):
    """Before, --merge wrote src/com/x/A.java, and the report gave WEB-INF/classes/com/x/A.java (#174)."""
    war = make_jar("app.war", {f"WEB-INF/classes/com/x/{n}.class": b"x" for n in "ABCD"})
    web = "WEB-INF/classes/com/x/"
    runner = writing_runner({"vineflower": {
        web + "A.java": "// FLAG", web + "B.java": "// NOTE", web + "C.java": "// ENV", web + "D.java": _VF_STUB,
    }})
    writer = MirrorWriter(tmp_path / "out") if mirror else MergeWriter(tmp_path / "out/src")
    ctx = make_ctx(tmp_path, runner, fallback=False, checker=marker_checker(), writer=writer)
    report, _ = process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    paths = [[e["path"] for e in entries] for entries in (report.flagged, report.noted, report.unchecked, report.stubs)]
    prefix = "WEB-INF/classes/com/x/" if mirror else "com/x/"
    assert paths == [[prefix + "A.java"], [prefix + "B.java"], [prefix + "C.java"], [prefix + "D.java"]]
    base = tmp_path / ("out/app.war" if mirror else "out/src")
    assert all((base / p).is_file() for entries in paths for p in entries)


@pytest.mark.parametrize("mirror", [False, True], ids=["merge", "mirror"])
def test_the_report_path_of_a_flagged_versions_twin_is_not_the_path_of_its_base_class(make_jar, tmp_path: Path, mirror):
    """Before, --merge gave flagged com/y/S.java, and src/com/y/S.java was the clean base class."""
    jar = make_jar("mr.jar", {"com/y/S.class": b"x", "META-INF/versions/11/com/y/S.class": b"y"})
    runner = writing_runner({"cfr": {"com/y/S.java": "class S {}", "META-INF/versions/11/com/y/S.java": "// FLAG"}})
    writer = MirrorWriter(tmp_path / "out") if mirror else MergeWriter(tmp_path / "out/src")
    ctx = make_ctx(tmp_path, runner, engine="cfr", fallback=False, checker=marker_checker(), writer=writer)
    report, _ = process_artifact(Artifact(jar, "mr.jar", ArtifactKind.ARCHIVE), ctx)
    assert [f["path"] for f in report.flagged] == ["META-INF/versions/11/com/y/S.java"]
    assert (tmp_path / ("out/mr.jar/com/y/S.java" if mirror else "out/src/com/y/S.java")).read_text() == "class S {}"


def test_report_paths_of_a_class_folder_with_merge_are_below_src(tmp_path: Path, class_file):
    root = _tree(tmp_path / "in", {
        "target/classes/com/x/A.class": class_file("com/x/A"), "target/classes/com/x/B.class": class_file("com/x/B"),
    })
    runner = writing_runner({"vineflower": {
        "target/classes/com/x/A.java": "// FLAG", "target/classes/com/x/B.java": _VF_STUB + "// ENV\n",
    }})
    stems = ("target/classes/com/x/A", "target/classes/com/x/B")
    writer = MergeWriter(tmp_path / "out/src", roots={"_classes": dict.fromkeys(stems, "target/classes/")})
    ctx = make_ctx(tmp_path, runner, fallback=False, checker=marker_checker(), writer=writer)
    report, _ = process_artifact(Artifact(root, "_classes", ArtifactKind.CLASS_TREE), ctx)
    assert [f["path"] for f in report.flagged] == ["com/x/A.java"]
    assert [f["path"] for f in report.unchecked] == ["com/x/B.java"]
    assert [s["path"] for s in report.stubs] == ["com/x/B.java"]
    assert sorted(p.relative_to(tmp_path / "out/src").as_posix() for p in (tmp_path / "out/src").rglob("*.java")) == [
        "com/x/A.java", "com/x/B.java",
    ]


# --- an archive with a container root gives a temporary jar for each root (#173) ---


def _zip(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "Duplicate name"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return path


def test_the_temp_jar_of_a_root_holds_its_classes_at_their_package_path(tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    jar = _zip(tmp_path / "boot.jar", [
        ("org/boot/L.class", class_file("org/boot/L")),
        ("BOOT-INF/classes/com/x/A.class", class_file("com/x/A")),
        ("BOOT-INF/classes/com/x/A$1.class", class_file("com/x/A$1")),
        ("BOOT-INF/classes/application.properties", b"x=1"),
    ])
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    dest = tmp_path / "dest"
    res = _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), jar, dest, 100, java="java")
    assert [c["target"].name for c in runner.calls] == ["classes.jar", "classes.jar"]
    assert [sorted(c["data"]) for c in runner.calls] == [["org/boot/L.class"], ["com/x/A$1.class", "com/x/A.class"]]
    with zipfile.ZipFile(jar) as zf:
        assert runner.calls[1]["data"]["com/x/A.class"] == zf.read("BOOT-INF/classes/com/x/A.class")
    assert _sources(dest) == ["BOOT-INF/classes/com/x/A.java", "org/boot/L.java"] and res.java_files == 2
    assert all(not c["target"].exists() for c in runner.calls)
    assert list(ctx.tmp_root.iterdir()) == []


def test_the_temp_jar_takes_the_last_of_two_entries_with_one_name(tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    war = _zip(tmp_path / "app.war", [
        ("WEB-INF/classes/com/x/A.class", class_file("com/x/A") + b"first"),
        ("WEB-INF/classes/com/x/A.class", class_file("com/x/A") + b"last"),
    ])
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="jd")
    _run_engine(ctx, ENGINES["jd"], Path("/fake/jd.jar"), war, tmp_path / "dest", 100, java="java")
    (call,) = runner.calls
    assert call["data"] == {"com/x/A.class": class_file("com/x/A") + b"last"}


def test_the_temp_jar_leaves_out_an_entry_that_leaves_its_root(tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    war = _zip(tmp_path / "app.war", [
        ("WEB-INF/classes/com/x/A.class", class_file("com/x/A")),
        ("WEB-INF/classes/../evil/E.class", class_file("evil/E")),
        ("WEB-INF/classes//abs/F.class", class_file("abs/F")),
        ("../up/U.class", class_file("up/U")),
        ("org/x/R.class", class_file("org/x/R")),
    ])
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="procyon")
    _run_engine(ctx, ENGINES["procyon"], Path("/fake/procyon.jar"), war, tmp_path / "dest", 100, java="java")
    assert [sorted(c["data"]) for c in runner.calls] == [["org/x/R.class"], ["com/x/A.class"]]


def _jar_entries_runner():
    """Fake CFR that records the entry names and the manifest of each jar that it gets."""
    seen: list[tuple[list[str], bytes | None]] = []

    def _run(spec, jar_path, target, dest, timeout, **kw):
        with zipfile.ZipFile(target) as zf:
            names = zf.namelist()
            seen.append((names, zf.read("META-INF/MANIFEST.MF") if "META-INF/MANIFEST.MF" in names else None))
        return EngineResult(spec.name, 0, False, 0, "")

    _run.seen = seen
    return _run


def test_each_temp_jar_starts_with_the_manifest_of_the_archive(tmp_path: Path, class_file):
    """Without the Multi-Release line, CFR wrote the body of META-INF/versions/9/p/A.class as p/A.java."""
    from decaf.pipeline import _run_engine

    manifest = b"Manifest-Version: 1.0\r\nMulti-Release: true\r\n\r\n"
    jar = _zip(tmp_path / "boot.jar", [
        ("p/A.class", class_file("p/A")),
        ("META-INF/MANIFEST.MF", manifest),
        ("META-INF/versions/9/p/A.class", class_file("p/A")),
        ("BOOT-INF/classes/com/x/A.class", class_file("com/x/A")),
    ])
    runner = _jar_entries_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), jar, tmp_path / "dest", 100, java="java")
    assert runner.seen == [
        (["META-INF/MANIFEST.MF", "p/A.class", "META-INF/versions/9/p/A.class"], manifest),
        (["META-INF/MANIFEST.MF", "com/x/A.class"], manifest),
    ]
    runner.seen.clear()
    war = _zip(tmp_path / "app.war", [("WEB-INF/classes/com/x/A.class", class_file("com/x/A"))])
    _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), war, tmp_path / "dest2", 100, java="java")
    assert runner.seen == [(["com/x/A.class"], None)]  # no manifest in the archive, none in the jar


def test_the_temp_jar_leaves_out_a_manifest_that_cannot_be_read(make_damaged_jar, tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    war = make_damaged_jar("app.war", {
        "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\n\r\n", "WEB-INF/classes/com/x/A.class": class_file("com/x/A"),
    }, damaged=("META-INF/MANIFEST.MF",))
    runner = _jar_entries_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), war, tmp_path / "dest", 100, java="java")
    assert runner.seen == [(["com/x/A.class"], None)]


def _files_outside(base: Path, dest: Path, inputs: set[Path]) -> list[Path]:
    return [p for p in base.rglob("*") if p.is_file() and not p.is_relative_to(dest) and p not in inputs]


@pytest.mark.parametrize("name", ["relative", "absolute"])
def test_a_root_whose_entry_name_leaves_the_output_folder_gets_no_run(tmp_path: Path, class_file, name):
    from decaf.pipeline import _run_engine

    above = "../../elsewhere/" if name == "relative" else (tmp_path / "elsewhere").as_posix() + "/"
    war = _zip(tmp_path / "app.war", [
        ("WEB-INF/classes/q/B.class", class_file("q/B")),
        (above + "WEB-INF/classes/p/A.class", class_file("p/A")),
        ("org/x/R.class", class_file("org/x/R")),
    ])
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    dest = tmp_path / "out/d"
    res = _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), war, dest, 100, java="java")
    assert [sorted(c["data"]) for c in runner.calls] == [["org/x/R.class"], ["q/B.class"]]
    assert _sources(dest) == ["WEB-INF/classes/q/B.java", "org/x/R.java"] and res.java_files == 2
    assert _files_outside(tmp_path, dest, {war}) == []


def test_the_row_counts_a_class_whose_entry_name_leaves_the_output_folder_as_missing(tmp_path: Path, class_file):
    war = _zip(tmp_path / "app.war", [
        ("WEB-INF/classes/q/B.class", class_file("q/B")),
        ("../../elsewhere/WEB-INF/classes/p/A.class", class_file("p/A")),
    ])
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr", fallback=False, writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(war, "app.war", ArtifactKind.ARCHIVE), ctx)
    assert (report.outcome, report.classes, report.missing_classes, report.java_files) == ("ok", 2, 1, 1)
    assert _sources(tmp_path / "out") == ["app.war/WEB-INF/classes/q/B.java"]
    assert _files_outside(tmp_path, tmp_path / "out", {war}) == []


def test_the_temp_jar_goes_when_the_engine_raises(tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    war = _zip(tmp_path / "app.war", [("WEB-INF/classes/com/x/A.class", class_file("com/x/A"))])
    seen: list[Path] = []

    def exploding(spec, jar_path, target, dest, timeout, **kw):
        seen.append(Path(target))
        raise RuntimeError("engine exploded")

    ctx = make_ctx(tmp_path, exploding, engine="cfr")
    with pytest.raises(RuntimeError):
        _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), war, tmp_path / "d", 100, java="java")
    assert [p.name for p in seen] == ["classes.jar"] and not seen[0].exists()
    assert list(ctx.tmp_root.iterdir()) == []


def test_the_temp_jar_raises_a_disk_error(tmp_path: Path, class_file, monkeypatch):
    """A full disk is no damaged entry: the run stops, and the temp folders go."""
    import errno

    import decaf.pipeline as pipeline
    from decaf.pipeline import _run_engine

    def full(src, dst, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    war = _zip(tmp_path / "app.war", [("WEB-INF/classes/com/x/A.class", class_file("com/x/A"))])
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    monkeypatch.setattr(pipeline.shutil, "copyfileobj", full)
    with pytest.raises(OSError) as exc:
        _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), war, tmp_path / "dest", 100, java="java")
    assert exc.value.errno == errno.ENOSPC
    assert runner.calls == [] and list(ctx.tmp_root.iterdir()) == []


def test_the_temp_jar_leaves_out_an_entry_that_cannot_be_read(make_damaged_jar, tmp_path: Path, class_file):
    from decaf.pipeline import _run_engine

    war = make_damaged_jar("app.war", {
        "WEB-INF/classes/com/x/A.class": class_file("com/x/A"), "WEB-INF/classes/com/x/B.class": class_file("com/x/B"),
    }, damaged=("WEB-INF/classes/com/x/B.class",))
    runner = by_name_runner()
    ctx = make_ctx(tmp_path, runner, engine="cfr")
    _run_engine(ctx, ENGINES["cfr"], Path("/fake/cfr.jar"), war, tmp_path / "dest", 100, java="java")
    assert [sorted(c["data"]) for c in runner.calls] == [["com/x/A.class"]]
