import re
from pathlib import Path

import pytest

from decaf import check
from decaf.engines import ENGINES, EngineResult
from decaf.maven import Gav, Resolution
from decaf.pipeline import Ctx, MergeWriter, MirrorWriter, Settings, _discover_nested, chain_for, process_artifact
from decaf.scanner import Artifact, ArtifactKind


def writing_runner(files_per_engine: dict[str, dict[str, str]]):
    """Fake run_engine: each engine 'produces' the given {rel: content} files."""
    calls: list[tuple[str, Path]] = []
    sent: list[list[str] | None] = []  # the class files of a folder target, read in the call: it goes after (#97)

    def _run(spec, jar_path, target, dest, timeout, java="java", cpu_budget=None):
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
    assert report.sources_miss == "sources jar for com.example:lib:1.2 contained no .java/.kt files"


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
    from decaf.pipeline import _stems_and_claims, expected_class_stems

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


def _run_family(make_jar, tmp_path: Path, files: dict[str, dict[str, str]], *, check: bool = True):
    """com/x/A, A$1, and A$2 with class headers, so the check knows the outer class of each inner class.

    Returns the report, the written {path: text} in mirror mode, and the runner. check=False runs no check.
    """
    jar = make_jar("app.jar", {f"com/x/{n}.class": _class_header(52) for n in ("A", "A$1", "A$2")})
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
    {_A: "// ENV"},
    {_A: "class A { /* cfr */ }", _A1: "// FLAG cfr"},
    {_A1: "class A$1 { /* cfr */ }"},  # no file of the outer class
    {_A: "class A {} // FLAG cfr", _A1: "class A$1 { /* cfr */ }"},  # the file of A fails, so A$1 goes with it
    {_A: "class A {} // FLAG cfr", _A2: "class A$2 { /* cfr */ }"},  # the file of Vineflower nests A$2
])
def test_a_retry_family_replaces_a_waiting_family_only_as_a_whole(make_jar, tmp_path: Path, cfr):
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG"}, "cfr": cfr,
    })
    assert written == {_A: "class A { /* vf */ }", _A1: "// FLAG"}
    assert [(f["path"], f["engine"]) for f in report.flagged] == [(_A1, "vineflower")]
    assert report.missing_classes == 0 and report.stubs == []


def test_a_retry_family_that_passes_replaces_the_waiting_family(make_jar, tmp_path: Path):
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "class A { /* vf */ }", _A1: "// FLAG"},
        "cfr": {_A: "class A {} // FLAG cfr"},
        "fernflower": {_A: "class A { /* ff */ }", _A1: "class A$1 { /* ff */ }"},
    })
    assert written == {_A: "class A { /* ff */ }", _A1: "class A$1 { /* ff */ }"}
    assert report.flagged == [] and [a.engine for a in report.attempts] == ["vineflower", "cfr", "fernflower"]


def test_a_retry_with_the_missing_outer_class_drops_the_held_file_of_its_inner_class(make_jar, tmp_path: Path):
    """Vineflower wrote A$1 only. The file of A from CFR nests A$1; before, the output had A$1 two times."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: "// FLAG"},
        "cfr": {_A: "class A { /* cfr */ }"},
    })
    assert written == {_A: "class A { /* cfr */ }"}
    assert report.flagged == [] and report.missing_classes == 0


def test_a_family_held_for_stubs_is_replaced_only_as_a_whole(make_jar, tmp_path: Path):
    """The stubs of Vineflower hold A and A$1, so the family waits (#150). No retry passes as a whole, so the
    clean A$1 of Fernflower goes: it cannot replace the held A$1 alone."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: _VF_STUB, _A1: _VF_STUB.replace("class A", "class A$1")},
        "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 {} // FLAG cfr"},
        "fernflower": {_A: "class A {} // FLAG ff", _A1: "class A$1 { /* ff */ }"},
    })
    assert written == {_A: _VF_STUB, _A1: _VF_STUB.replace("class A", "class A$1")}
    assert [s["path"] for s in report.stubs] == [_A1, _A]


def test_a_retry_replaces_a_family_with_a_held_outer_file_only_as_a_whole(make_jar, tmp_path: Path):
    """The flagged A of Vineflower holds its family, with the clean A$1 (#150). The flagged A$1 of CFR fails
    the whole retry family, so the clean A of CFR replaces nothing."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: "// FLAG", _A1: "class A$1 { /* vf */ }"},
        "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 {} // FLAG cfr"},
    })
    assert written == {_A: "// FLAG", _A1: "class A$1 { /* vf */ }"}
    assert [(f["path"], f["engine"]) for f in report.flagged] == [(_A, "vineflower")]


def test_a_family_with_a_written_file_of_an_earlier_output_does_not_wait(make_jar, tmp_path: Path):
    """Vineflower wrote A$1 only, and it passed. The retry writes A, and the flagged A$1 of CFR goes (#107)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: "class A$1 { /* vf */ }"},
        "cfr": {_A: "class A { /* cfr */ }", _A1: "class A$1 {} // FLAG cfr"},
    })
    assert written == {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* vf */ }"}
    assert [a.engine for a in report.attempts] == ["vineflower", "cfr"]


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
     {_A: "class A { /* cfr */ }", _A1: "class A$1 { /* cfr */ }"}),
    ({"vineflower": {_A: "class A {} // FLAG"}, "cfr": {_A: "class A {} // FLAG cfr", _A1: "class A$1 {} // FLAG cfr"},
      "fernflower": {_A: "class A { /* ff */ }"}}, {_A: "class A { /* ff */ }"}),  # xstream-1.4.21 with --engine cfr
])
def test_a_held_file_of_an_outer_class_makes_its_family_wait(make_jar, tmp_path: Path, files, expected):
    """A is held for a stub or a flag, and an engine writes A$1 apart from A, in the same output or in a retry.
    A later retry nests A$1, so the file of A$1 must not go out on its own: the family of A waits, and only a
    whole family replaces it (#150)."""
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
    """Vineflower wrote A$1 alone, and CFR held A, so the family waits. The flagged A$1 of Fernflower goes
    (#107), so it must not fail the retry family: the clean A of Fernflower replaces the held A."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A1: _A1_VF},
        "cfr": {_A: "class A {} // FLAG cfr"},
        "fernflower": {_A: "class A { /* ff */ }", _A1: "class A$1 {} // FLAG ff"},
    })
    assert written == {_A: "class A { /* ff */ }", _A1: _A1_VF}
    assert report.flagged == []


def test_a_held_file_of_an_outer_class_holds_the_family_of_its_copy(make_jar, tmp_path: Path):
    """A war can have a class at the root and under WEB-INF/classes (#107). The file of an inner class rides
    with the file of the outer class of its own copy."""
    web = "WEB-INF/classes/"
    jar = make_jar("app.jar", {
        f"{root}com/x/{n}.class": _class_header(52) for root in ("", web) for n in ("A", "A$1")
    })
    runner = writing_runner({
        "vineflower": {_A: _VF_STUB, _A1: _A1_VF, web + _A: _VF_STUB, web + _A1: "class A$1 { /* vf web */ }"},
        "cfr": {_A: "class A { /* cfr */ }", web + _A: "class A { /* cfr web */ }"},
    })
    ctx = make_ctx(tmp_path, runner, checker=marker_checker(), writer=MirrorWriter(tmp_path / "out"))
    report, _ = process_artifact(Artifact(jar, "app.jar", ArtifactKind.ARCHIVE), ctx)
    out = tmp_path / "out/app.jar"
    written = {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.java")}
    assert written == {_A: "class A { /* cfr */ }", web + _A: "class A { /* cfr web */ }"}
    assert report.stubs == [] and report.missing_classes == 0


def test_an_unchecked_retry_family_replaces_a_family_held_for_a_stub_in_its_outer_file(make_jar, tmp_path: Path):
    """A stub in the file of the outer class is a known loss for the whole family: the unchecked A and A$1 of
    CFR replace the stub A and the clean A$1 of Vineflower, as an unchecked file replaces a stub (#96)."""
    report, written, _ = _run_family(make_jar, tmp_path, {
        "vineflower": {_A: _VF_STUB, _A1: _A1_VF},
        "cfr": {_A: "// ENV", _A1: "// ENV 1"},
    })
    assert written == {_A: "// ENV", _A1: "// ENV 1"}
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
