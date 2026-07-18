"""Output writers, report model, artifact processing, and the parallel runner."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import traceback
import zipfile
from collections.abc import Callable, Collection
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from . import engines, maven
from .engines import ENGINES
from .maven import extract_java
from .scanner import (
    ARCHIVE_EXTS,
    Artifact,
    ArtifactKind,
    classify_zip,
    copy_class_tree,
    find_nested_archives,
    safe_extract_zip,
    scan_input,
)

_CONTAINER_ROOTS = ("WEB-INF/classes/", "BOOT-INF/classes/")


def normalize_java_rel(rel: str) -> str:
    for marker in _CONTAINER_ROOTS:
        idx = rel.find(marker)
        if idx != -1:
            rel = rel[idx + len(marker) :]
            break
    if rel.startswith("META-INF/versions/"):
        parts = rel.split("/", 3)
        if len(parts) == 4:
            rel = parts[3]
    return rel


@dataclass
class EngineAttempt:
    engine: str
    level: str  # "archive" | "class"
    returncode: int
    timed_out: bool
    java_files: int
    stderr_tail: str


@dataclass
class ArtifactReport:
    rel: str
    kind: str
    outcome: str  # "ok" | "failed" | "skipped"
    method: str | None = None  # "maven" | "extracted" | engine name | None
    gav: str | None = None
    repo: str | None = None
    classes: int = 0
    java_files: int = 0
    resources_skipped: int = 0
    missing_classes: int = 0
    attempts: list[EngineAttempt] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    failure: str | None = None


class MergeWriter:
    """Merges .java files from many trees into one package tree.

    Collisions are deterministic: the tree with the lowest sort_key wins,
    regardless of the order in which worker threads deliver results.
    """

    def __init__(self, src_root: Path) -> None:
        self.root = src_root
        self._lock = threading.Lock()
        self._index: dict[str, tuple[str, str]] = {}  # rel -> (sort_key, sha256)

    def add_tree(self, tree: Path, sort_key: str) -> tuple[int, int, list[dict]]:
        java = resources = 0
        collisions: list[dict] = []
        for p in sorted(tree.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix != ".java":
                resources += 1
                continue
            java += 1
            rel = normalize_java_rel(p.relative_to(tree).as_posix())
            content = p.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            with self._lock:
                existing = self._index.get(rel)
                if existing is None:
                    self._index[rel] = (sort_key, digest)
                    target = self.root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                elif existing[1] == digest:
                    pass  # identical duplicate
                elif sort_key < existing[0]:
                    collisions.append({"path": rel, "kept": sort_key, "dropped": existing[0]})
                    self._index[rel] = (sort_key, digest)
                    (self.root / rel).write_bytes(content)
                else:
                    collisions.append({"path": rel, "kept": existing[0], "dropped": sort_key})
        return java, resources, collisions


class MirrorWriter:
    """Copies each artifact's full output tree under out_root/<rel with '!' removed>."""

    def __init__(self, out_root: Path) -> None:
        self.root = out_root

    def dest_for(self, rel: str) -> Path:
        return self.root / rel.replace("!", "")

    def add_tree(self, tree: Path, rel: str) -> tuple[int, int, list[dict]]:
        dest = self.dest_for(rel)
        java = resources = 0
        for p in sorted(tree.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix == ".java":
                java += 1
            else:
                resources += 1
                if p.suffix.lower() in ARCHIVE_EXTS:
                    continue  # a nested archive's decompiled directory takes this path; blob and directory cannot coexist
            target = dest / p.relative_to(tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
        return java, resources, []


@dataclass
class RunReport:
    settings: dict
    artifacts: list[ArtifactReport]
    totals: dict
    duration_seconds: float
    interrupted: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def compute_totals(reports: list[ArtifactReport]) -> dict:
    return {
        "artifacts": len(reports),
        "ok": sum(r.outcome == "ok" for r in reports),
        "failed": sum(r.outcome == "failed" for r in reports),
        "skipped": sum(r.outcome == "skipped" for r in reports),
        "maven_sources": sum(r.method == "maven" for r in reports),
        "extracted": sum(r.method == "extracted" for r in reports),
        "decompiled": sum(
            1 for r in reports if r.method not in (None, "maven", "extracted")
        ),
        "java_files": sum(r.java_files for r in reports),
        "collisions": sum(len(r.collisions) for r in reports),
    }


@dataclass
class Settings:
    input: Path
    output: Path
    engine: str = "vineflower"
    fallback: bool = True
    mirror: bool = True  # mirror the input layout; False = one merged src/ tree
    maven: bool = True
    max_depth: int = 1  # archive-in-archive levels to extract; 0 = top-level only
    jobs: int = 0  # 0 = auto: min(4, cpu count)
    cpus: int = 0  # total CPU budget across all workers; 0 = all cores
    timeout: float = 600.0
    repos: tuple[str, ...] = ()
    verbose: bool = False
    quiet: bool = False


def chain_for(
    primary: str, fallback: bool, available: Collection[str] | None = None
) -> list[str]:
    from .engines import ENGINE_ORDER

    chain = [primary] + [e for e in ENGINE_ORDER if e != primary]
    if not fallback:
        chain = chain[:1]
    if available is not None:
        chain = [e for e in chain if e in available]
    return chain


@dataclass
class Ctx:
    settings: Settings
    writer: MergeWriter | MirrorWriter
    chain: list[str]
    engine_jars: dict[str, Path]
    java: str
    tmp_root: Path
    client: httpx.Client | None
    sources_cache: Path
    runner: Callable
    resolver: Callable
    cpu_budget: int | None = None  # visible cores per engine JVM
    on_stderr: Callable[[str], None] | None = None  # live engine-stderr sink (-v)


def _tmp_dir(ctx: Ctx) -> Path:
    return Path(tempfile.mkdtemp(dir=ctx.tmp_root))


def _class_stem(entry: str) -> str:
    return normalize_java_rel(entry)[: -len(".class")]


def expected_class_stems(source: Path) -> set[str]:
    """Top-level class names (no $-inner, no module/package-info) a decompile should yield."""
    if source.is_dir():
        entries = [p.relative_to(source).as_posix() for p in source.rglob("*.class")]
    else:
        try:
            with zipfile.ZipFile(source) as zf:
                entries = [n for n in zf.namelist() if n.endswith(".class")]
        except (zipfile.BadZipFile, OSError):
            return set()
    stems = set()
    for e in entries:
        base = e.rsplit("/", 1)[-1]
        if "$" in base or base in ("module-info.class", "package-info.class"):
            continue
        stems.add(_class_stem(e))
    return stems


def produced_stems(dest: Path) -> set[str]:
    return {
        normalize_java_rel(p.relative_to(dest).as_posix())[: -len(".java")]
        for p in dest.rglob("*.java")
    }


def _extract_failed_classes(source: Path, missing: set[str], ctx: Ctx) -> Path | None:
    """Copy the .class files (plus their $-inners) for missing stems into a temp tree."""

    def wanted(entry: str) -> bool:
        stem = _class_stem(entry)
        return stem in missing or stem.split("$", 1)[0] in missing

    out = _tmp_dir(ctx)
    if source.is_dir():
        count = 0
        for p in sorted(source.rglob("*.class")):
            rel = p.relative_to(source).as_posix()
            if wanted(rel):
                target = out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
                count += 1
    else:
        with zipfile.ZipFile(source) as zf:
            names = [n for n in zf.namelist() if n.endswith(".class") and wanted(n)]
        count = safe_extract_zip(source, out, members=names)
    return out if count else None


def _decompile(artifact: Artifact, target: Path, ctx: Ctx, report: ArtifactReport) -> None:
    source = target if target.is_dir() else artifact.path
    expected = expected_class_stems(source)
    report.classes = len(expected)
    produced: set[str] = set()

    def _stream_kw(name: str) -> dict:
        # Only pass the kwarg when streaming, so custom runners without it keep working.
        if ctx.on_stderr is None:
            return {}
        sink, rel = ctx.on_stderr, artifact.rel
        return {"on_stderr_line": lambda line: sink(f"{name} {rel}: {line}")}

    used_index: int | None = None
    for i, name in enumerate(ctx.chain):
        if engines.PROCESSES.closed:
            break
        dest = _tmp_dir(ctx)
        res = ctx.runner(
            ENGINES[name], ctx.engine_jars[name], target, dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **_stream_kw(name),
        )
        report.attempts.append(
            EngineAttempt(name, "archive", res.returncode, res.timed_out, res.java_files, res.stderr_tail)
        )
        if res.java_files > 0:
            java, resources, collisions = ctx.writer.add_tree(dest, artifact.rel)
            report.java_files += java
            report.resources_skipped += resources
            report.collisions += collisions
            report.method = name
            produced |= produced_stems(dest)
            used_index = i
            break

    if used_index is None:
        report.outcome = "failed"
        report.failure = "all engines failed"
        return

    missing = expected - produced
    for name in ctx.chain[used_index + 1 :]:
        if engines.PROCESSES.closed:
            break
        if not missing:
            break
        retry_tree = _extract_failed_classes(source, missing, ctx)
        if retry_tree is None:
            break
        dest = _tmp_dir(ctx)
        res = ctx.runner(
            ENGINES[name], ctx.engine_jars[name], retry_tree, dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **_stream_kw(name),
        )
        report.attempts.append(
            EngineAttempt(name, "class", res.returncode, res.timed_out, res.java_files, res.stderr_tail)
        )
        if res.java_files > 0:
            java, _, collisions = ctx.writer.add_tree(dest, artifact.rel)
            report.java_files += java
            report.collisions += collisions
            produced |= produced_stems(dest)
            missing = expected - produced
    report.missing_classes = len(missing)
    report.outcome = "ok"


def _discover_nested(artifact: Artifact, ctx: Ctx) -> list[Artifact]:
    try:
        with zipfile.ZipFile(artifact.path) as zf:
            names = find_nested_archives(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return []
    if not names:
        return []
    if artifact.rel.count("!/") >= ctx.settings.max_depth:
        return [
            Artifact(artifact.path, f"{artifact.rel}!/{name}", ArtifactKind.BEYOND_DEPTH)
            for name in names
        ]
    extract_dir = _tmp_dir(ctx)
    safe_extract_zip(artifact.path, extract_dir, members=names)
    nested = []
    for name in names:
        p = extract_dir / name
        if p.is_file():
            nested.append(Artifact(p, f"{artifact.rel}!/{name}", classify_zip(p)))
    return nested


def process_artifact(artifact: Artifact, ctx: Ctx) -> tuple[ArtifactReport, list[Artifact]]:
    report = ArtifactReport(rel=artifact.rel, kind=artifact.kind.value, outcome="ok")
    nested: list[Artifact] = []
    try:
        if artifact.kind is ArtifactKind.CORRUPT:
            report.outcome = "failed"
            report.failure = "unreadable archive"
        elif artifact.kind is ArtifactKind.RESOURCE_ONLY:
            nested = _discover_nested(artifact, ctx)  # e.g. a war bundling only jars
            report.outcome = "skipped"
        elif artifact.kind is ArtifactKind.BEYOND_DEPTH:
            report.outcome = "skipped"
            report.failure = f"nested deeper than --max-depth {ctx.settings.max_depth}"
        elif artifact.kind is ArtifactKind.SOURCES_JAR:
            tmp = _tmp_dir(ctx)
            extract_java(artifact.path, tmp)
            java, resources, collisions = ctx.writer.add_tree(tmp, artifact.rel)
            report.java_files = java
            report.resources_skipped = resources
            report.collisions = collisions
            if java == 0:
                report.outcome = "failed"
                report.failure = "sources jar contained no .java files"
            else:
                report.method = "extracted"
        elif artifact.kind is ArtifactKind.CLASS_TREE:
            tmp = _tmp_dir(ctx)
            copy_class_tree(artifact.path, tmp)
            _decompile(artifact, tmp, ctx, report)
        else:  # ARCHIVE
            nested = _discover_nested(artifact, ctx)
            resolved = None
            if ctx.settings.maven and ctx.client is not None:
                resolved = ctx.resolver(
                    artifact.path, list(ctx.settings.repos), ctx.client, ctx.sources_cache
                )
            if resolved:
                gav, sources_jar, repo = resolved
                tmp = _tmp_dir(ctx)
                if extract_java(sources_jar, tmp) > 0:
                    java, resources, collisions = ctx.writer.add_tree(tmp, artifact.rel)
                    report.method = "maven"
                    report.gav = str(gav)
                    report.repo = repo
                    report.java_files = java
                    report.resources_skipped = resources
                    report.collisions = collisions
                else:
                    resolved = None
            if not resolved:
                _decompile(artifact, artifact.path, ctx, report)
    except Exception:
        report.outcome = "failed"
        report.failure = traceback.format_exc()[-2000:]
    return report, nested


class DecafError(Exception):
    """Environment or usage error; the CLI reports it and exits 2."""


def _preflight_engines(
    settings: Settings, java_major: int, client: httpx.Client
) -> tuple[list[str], dict[str, Path]]:
    wanted = chain_for(settings.engine, settings.fallback)
    jars: dict[str, Path] = {}
    for name in wanted:
        spec = ENGINES[name]
        if spec.min_java > java_major:
            if name == settings.engine:
                raise DecafError(
                    f"engine {name} needs Java {spec.min_java}+, found Java {java_major}"
                )
            continue
        try:
            jars[name] = engines.ensure_engine(spec, client)
        except engines.EngineError as exc:
            if name == settings.engine:
                raise DecafError(str(exc)) from exc
    chain = chain_for(settings.engine, settings.fallback, available=set(jars))
    if not chain:
        raise DecafError(f"primary engine {settings.engine!r} unavailable")
    return chain, jars


def run(
    settings: Settings,
    *,
    on_done: Callable[[ArtifactReport], None] | None = None,
    on_found: Callable[[int], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    runner: Callable | None = None,
    resolver: Callable | None = None,
) -> RunReport:
    start = time.monotonic()
    engines.PROCESSES.reset()
    found = engines.find_java()
    if found is None:
        raise DecafError("java not found on PATH (Java 11+ required)")
    java_exe, java_major = found
    if java_major < engines.JAVA_MIN:
        raise DecafError(f"Java {java_major} is too old (Java {engines.JAVA_MIN}+ required)")

    artifacts = scan_input(settings.input)
    if on_found is not None:
        on_found(len(artifacts))
    runner = runner or engines.run_engine
    resolver = resolver or maven.resolve_sources

    interrupted = False
    reports: list[ArtifactReport] = []
    chain: list[str] = []
    tmp_root = Path(tempfile.mkdtemp(prefix="decaf-"))
    client = httpx.Client(follow_redirects=True, timeout=30)
    total_cpus = settings.cpus or (os.cpu_count() or 1)
    jobs = settings.jobs or min(4, os.cpu_count() or 1)
    jobs = max(1, min(jobs, total_cpus))
    cpu_budget = max(1, total_cpus // jobs)
    try:
        chain, engine_jars = _preflight_engines(settings, java_major, client)
        writer: MergeWriter | MirrorWriter
        if settings.mirror:
            writer = MirrorWriter(settings.output)
        else:
            writer = MergeWriter(settings.output / "src")
        ctx = Ctx(
            settings=settings,
            writer=writer,
            chain=chain,
            engine_jars=engine_jars,
            java=java_exe,
            tmp_root=tmp_root,
            client=client if settings.maven else None,
            sources_cache=engines.cache_root() / "sources",
            runner=runner,
            resolver=resolver,
            cpu_budget=cpu_budget,
            on_stderr=on_stderr,
        )
        try:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                pending: set = set()
                try:
                    for a in artifacts:
                        pending.add(pool.submit(process_artifact, a, ctx))
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for fut in done:
                            report, nested = fut.result()
                            reports.append(report)
                            if nested and on_found is not None:
                                on_found(len(nested))
                            if on_done is not None:
                                on_done(report)
                            for n in nested:
                                pending.add(pool.submit(process_artifact, n, ctx))
                except KeyboardInterrupt:
                    interrupted = True
                    engines.PROCESSES.kill_all()
                    for fut in pending:
                        fut.cancel()
        finally:
            # Write the report even if a second Ctrl-C lands during teardown,
            # so partial results survive.
            reports.sort(key=lambda r: r.rel)
            run_report = RunReport(
                settings={
                    "input": str(settings.input),
                    "output": str(settings.output),
                    "engine": settings.engine,
                    "fallback": settings.fallback,
                    "mirror": settings.mirror,
                    "maven": settings.maven,
                    "max_depth": settings.max_depth,
                    "jobs": jobs,
                    "cpus": total_cpus,
                    "cpu_budget": cpu_budget,
                    "timeout": settings.timeout,
                    "repos": list(settings.repos),
                    "chain": chain,
                    "java": java_exe,
                    "java_major": java_major,
                },
                artifacts=reports,
                totals=compute_totals(reports),
                duration_seconds=round(time.monotonic() - start, 2),
                interrupted=interrupted,
            )
            settings.output.mkdir(parents=True, exist_ok=True)
            (settings.output / "decaf-report.json").write_text(run_report.to_json())
    finally:
        client.close()
        shutil.rmtree(tmp_root, ignore_errors=True)
    return run_report
