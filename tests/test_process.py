from pathlib import Path

import pytest

from decaf.engines import ENGINES, EngineResult
from decaf.maven import Gav, Resolution
from decaf.pipeline import Ctx, MergeWriter, Settings, _discover_nested, chain_for, process_artifact
from decaf.scanner import Artifact, ArtifactKind


def writing_runner(files_per_engine: dict[str, dict[str, str]]):
    """Fake run_engine: each engine 'produces' the given {rel: content} files."""
    calls: list[tuple[str, Path]] = []

    def _run(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
        calls.append((spec.name, Path(target)))
        files = files_per_engine.get(spec.name, {})
        for rel, content in files.items():
            p = Path(dest) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return EngineResult(spec.name, 0 if files else 1, False, len(files), "" if files else "boom")

    _run.calls = calls
    return _run


def make_ctx(tmp_path: Path, runner, *, resolver=None, fallback=True, maven=False, engine="vineflower"):
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
        writer=MergeWriter(tmp_path / "out/src"),
        chain=chain_for(engine, fallback),
        engine_jars={name: Path(f"/fake/{name}.jar") for name in ENGINES},
        java="java",
        tmp_root=tmp_root,
        client=object() if maven else None,
        sources_cache=tmp_path / "cache",
        runner=runner,
        resolver=resolver or (lambda *a, **k: None),
    )


def test_chain_for():
    assert chain_for("vineflower", True) == ["vineflower", "cfr", "procyon", "fernflower", "jd"]
    assert chain_for("cfr", True) == ["cfr", "vineflower", "procyon", "fernflower", "jd"]
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
    retry_target = runner.calls[1][1]
    assert retry_target.is_dir()
    assert sorted(p.name for p in retry_target.rglob("*.class")) == ["B$1.class", "B.class"]


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
        Artifact(dep, "app.war!/WEB-INF/lib/dep.jar", ArtifactKind.ARCHIVE), ctx
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
    assert report.sources_miss == "sources jar for com.example:lib:1.2 contained no .java files"


def test_resolution_miss_recorded_in_report(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Resolution(
            gav=Gav("com.example", "lib", "1.2"),
            miss="verified com.example:lib:1.2 via https://r.test/m2 but no -sources.jar published",
        )

    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.method == "vineflower"
    assert report.gav == "com.example:lib:1.2"
    assert report.resolved_by is None
    assert report.sources_miss == (
        "verified com.example:lib:1.2 via https://r.test/m2 but no -sources.jar published"
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
    target = runner.calls[0][1]
    assert target.is_dir() and target != tmp_path  # copied tree, not the input root


def test_runner_exception_becomes_failed_report(make_jar, tmp_path: Path):
    jar = make_jar("app.jar", {"A.class": b"x"})

    def exploding(*a, **k):
        raise RuntimeError("engine exploded")

    ctx = make_ctx(tmp_path, exploding)
    report, nested = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "failed"
    assert "engine exploded" in (report.failure or "")
    assert nested == []


def test_resolver_exception_becomes_failed_report_with_nested(make_jar, tmp_path: Path):
    """A stage-1 (resolution) crash fails the artifact without running engines,
    but nested archives discovered before the crash are still surfaced."""
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
    assert report.outcome == "failed"
    assert "index server melted" in (report.failure or "")
    assert runner.calls == []  # engines never run after a resolution crash
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


def _batch_ctx(tmp_path, batch_runner, chain=None):
    from decaf.pipeline import Ctx, MergeWriter, Settings

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir(exist_ok=True)
    return Ctx(
        settings=Settings(input=tmp_path, output=tmp_path / "out"),
        writer=MergeWriter(tmp_path / "out" / "src"),
        chain=chain or ["vineflower"],
        engine_jars={"vineflower": Path("/fake/vf.jar"), "cfr": Path("/fake/cfr.jar")},
        java="java", tmp_root=tmp_root, client=None,
        sources_cache=tmp_path / "s", runner=None, resolver=None,
        batch_runner=batch_runner,
    )


def _member(make_jar, name, entries):
    from decaf.pipeline import ArtifactReport, expected_class_stems
    from decaf.scanner import Artifact, ArtifactKind

    jar = make_jar(name, entries)
    a = Artifact(jar, name, ArtifactKind.ARCHIVE, len(entries))
    report = ArtifactReport(rel=name, kind="archive", outcome="ok")
    return (a, jar, report, expected_class_stems(jar))


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


def test_decompile_batch_extracts_member_resources(make_jar, tmp_path):
    from decaf.pipeline import MirrorWriter, _decompile_batch

    m1 = _member(make_jar, "a.jar", {"com/a/A.class": b"x", "META-INF/spring.factories": b"cfg"})
    ctx = _batch_ctx(tmp_path, merged_batch_engine)
    ctx.writer = MirrorWriter(tmp_path / "out")
    done, requeue = _decompile_batch([m1], ctx)
    assert requeue == [] and done[0].outcome == "ok"
    assert (tmp_path / "out/a.jar/META-INF/spring.factories").read_bytes() == b"cfg"
    assert (tmp_path / "out/a.jar/com/a/A.java").is_file()
