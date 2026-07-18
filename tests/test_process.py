from pathlib import Path

import pytest

from decaf.engines import ENGINES, EngineResult
from decaf.maven import Gav
from decaf.pipeline import Ctx, MergeWriter, Settings, chain_for, process_artifact
from decaf.scanner import Artifact, ArtifactKind


def writing_runner(files_per_engine: dict[str, dict[str, str]]):
    """Fake run_engine: each engine 'produces' the given {rel: content} files."""
    calls: list[tuple[str, Path]] = []

    def _run(spec, jar_path, target, dest, timeout, java="java"):
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


def test_maven_first_short_circuits_engines(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    sources = make_jar("lib-1.2-sources.jar", {"com/x/A.java": "// real source\nclass A {}"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Gav("com.example", "lib", "1.2"), sources, "https://r.test/m2"

    runner = writing_runner({})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.outcome == "ok"
    assert report.method == "maven"
    assert report.gav == "com.example:lib:1.2"
    assert report.repo == "https://r.test/m2"
    assert runner.calls == []
    assert "real source" in (tmp_path / "out/src/com/x/A.java").read_text()


def test_empty_sources_jar_falls_back_to_engines(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"com/x/A.class": b"x"})
    empty_sources = make_jar("empty-sources.jar", {"README": "no java here"})

    def resolver(jar_path, repos, client, cache_dir, **kw):
        return Gav("com.example", "lib", "1.2"), empty_sources, "https://r.test/m2"

    runner = writing_runner({"vineflower": {"com/x/A.java": "class A {}"}})
    ctx = make_ctx(tmp_path, runner, resolver=resolver, maven=True)
    report, _ = process_artifact(Artifact(jar, "lib-1.2.jar", ArtifactKind.ARCHIVE), ctx)
    assert report.method == "vineflower"


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
