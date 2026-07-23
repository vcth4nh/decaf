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
from collections import deque
from collections.abc import Callable, Collection
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from functools import partial
from heapq import heappush, heappop
from itertools import count
from pathlib import Path, PurePosixPath

import httpx

from . import engines, maven
from .engines import ENGINES, SOURCE_SUFFIXES
from .maven import extract_java
from .scanner import (
    ARCHIVE_EXTS,
    Artifact,
    ArtifactKind,
    classify_counted,
    copy_class_tree,
    find_nested_archives,
    safe_extract_zip,
    scan_counted,
)

_CONTAINER_ROOTS = ("WEB-INF/classes/", "BOOT-INF/classes/")

_WHALE_CLASSES = 3000  # artifacts at/above this class count get scheduling headroom

_SMALL_CLASSES = 800  # below this, an archive is batchable
_BATCH_MAX_JARS = 16
_BATCH_MAX_CLASSES = 2000


def _decompile_weight(artifact: Artifact) -> int:
    return 2 if artifact.classes >= _WHALE_CLASSES else 1


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
    level: str  # "archive" | "class" | "batch"
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
    resolved_by: str | None = None  # "pom-properties" | "sha1-index" | "verified-guess"
    sources_miss: str | None = None  # why the maven path yielded nothing
    sources_cached: bool = False  # sources jar came from the on-disk cache
    classes: int = 0
    java_files: int = 0
    resources_skipped: int = 0
    resources_copied: int = 0
    missing_classes: int = 0
    attempts: list[EngineAttempt] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    failure: str | None = None


def _resource_members(archive: Path, include_sources: bool) -> list[str]:
    """Entries of the original archive that mirror mode carries through."""
    excluded = () if include_sources else SOURCE_SUFFIXES
    try:
        with zipfile.ZipFile(archive) as zf:
            return [
                n for n in zf.namelist()
                if not n.endswith("/")
                and not n.endswith(".class")
                and PurePosixPath(n).suffix.lower() not in ARCHIVE_EXTS
                and not n.endswith(excluded)
            ]
    except (zipfile.BadZipFile, OSError):
        return []


class MergeWriter:
    """Merges source files (.java/.kt) from many trees into one package tree.

    Collisions are deterministic: the tree with the lowest sort_key wins,
    regardless of the order in which worker threads deliver results.
    """

    def __init__(self, src_root: Path) -> None:
        self.root = src_root
        self._lock = threading.Lock()
        self._index: dict[str, tuple[str, str]] = {}  # rel -> (sort_key, sha256)

    def add_tree(self, tree: Path, sort_key: str) -> tuple[int, list[dict]]:
        java = 0
        collisions: list[dict] = []
        for p in sorted(tree.rglob("*")):
            if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
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
        return java, collisions

    def add_resources(self, archive: Path, rel: str, *, include_sources: bool = False) -> tuple[int, int]:
        return 0, len(_resource_members(archive, include_sources))

    def add_blob(self, zip_path: Path, member: str, rel: str) -> tuple[int, int]:
        return 0, 1


class MirrorWriter:
    """Copies each artifact's source files under out_root/<rel with '!' removed>."""

    def __init__(self, out_root: Path, resources: bool = True) -> None:
        self.root = out_root
        self.resources = resources

    def dest_for(self, rel: str) -> Path:
        return self.root / rel.replace("!", "")

    def add_tree(self, tree: Path, rel: str) -> tuple[int, list[dict]]:
        dest = self.dest_for(rel)
        java = 0
        for p in sorted(tree.rglob("*")):
            if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
                continue  # engine strays never land; resources come from add_resources
            java += 1
            target = dest / p.relative_to(tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
        return java, []

    def add_resources(self, archive: Path, rel: str, *, include_sources: bool = False) -> tuple[int, int]:
        names = _resource_members(archive, include_sources)
        if not names:
            return 0, 0
        if not self.resources:
            return 0, len(names)
        return safe_extract_zip(archive, self.dest_for(rel), members=names), 0

    def add_blob(self, zip_path: Path, member: str, rel: str) -> tuple[int, int]:
        if not self.resources:
            return 0, 1
        target = self.dest_for(rel)
        try:
            with zipfile.ZipFile(zip_path) as zf, zf.open(member) as src:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        except (KeyError, zipfile.BadZipFile, OSError):
            return 0, 0
        return 1, 0


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
        "resources_copied": sum(r.resources_copied for r in reports),
        "collisions": sum(len(r.collisions) for r in reports),
        "network_misses": sum(
            1 for r in reports if (r.sources_miss or "").startswith("network:")
        ),
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
    engine_overrides: dict[str, dict[str, str]] = field(default_factory=dict)


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
    batch_runner: Callable | None = None
    cpu_budget: int | None = None  # visible cores per engine JVM
    cds_dir: Path | None = None  # CDS archive directory for Java 19+
    on_stderr: Callable[[str], None] | None = None  # live engine-stderr sink (-v)
    on_event: Callable[[str, str, str], None] | None = None  # live progress events


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
        normalize_java_rel(p.relative_to(dest).as_posix())[: -len(p.suffix)]
        for p in dest.rglob("*")
        if p.is_file() and p.suffix in SOURCE_SUFFIXES
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


def _stream_kw_for(ctx: Ctx, name: str, rel: str) -> dict:
    # Only pass the kwarg when streaming, so custom runners without it keep working.
    if ctx.on_stderr is None:
        return {}
    sink = ctx.on_stderr
    return {"on_stderr_line": lambda line: sink(f"{name} {rel}: {line}")}


def _retry_missing_classes(
    artifact: Artifact,
    source: Path,
    expected: set[str],
    produced: set[str],
    ctx: Ctx,
    report: ArtifactReport,
    chain: list[str],
) -> None:
    missing = expected - produced
    for name in chain:
        if engines.PROCESSES.closed:
            break
        if not missing:
            break
        retry_tree = _extract_failed_classes(source, missing, ctx)
        if retry_tree is None:
            break
        dest = _tmp_dir(ctx)
        if ctx.on_event is not None:
            ctx.on_event("decompile", artifact.rel, name)
        res = ctx.runner(
            ENGINES[name], ctx.engine_jars[name], retry_tree, dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
            **_stream_kw_for(ctx, name, artifact.rel),
        )
        report.attempts.append(
            EngineAttempt(name, "class", res.returncode, res.timed_out, res.java_files, res.stderr_tail)
        )
        if res.java_files > 0:
            java, collisions = ctx.writer.add_tree(dest, artifact.rel)
            report.java_files += java
            report.collisions += collisions
            produced |= produced_stems(dest)
            missing = expected - produced
    report.missing_classes = len(missing)


def _decompile(artifact: Artifact, target: Path, ctx: Ctx, report: ArtifactReport) -> None:
    source = target if target.is_dir() else artifact.path
    expected = expected_class_stems(source)
    report.classes = len(expected)
    produced: set[str] = set()

    used_index: int | None = None
    for i, name in enumerate(ctx.chain):
        if engines.PROCESSES.closed:
            break
        dest = _tmp_dir(ctx)
        if ctx.on_event is not None:
            ctx.on_event("decompile", artifact.rel, name)
        res = ctx.runner(
            ENGINES[name], ctx.engine_jars[name], target, dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
            **_stream_kw_for(ctx, name, artifact.rel),
        )
        report.attempts.append(
            EngineAttempt(name, "archive", res.returncode, res.timed_out, res.java_files, res.stderr_tail)
        )
        if res.java_files > 0:
            java, collisions = ctx.writer.add_tree(dest, artifact.rel)
            report.java_files += java
            report.collisions += collisions
            report.method = name
            produced |= produced_stems(dest)
            used_index = i
            break

    if used_index is None:
        report.outcome = "failed"
        report.failure = "all engines failed"
        return

    _retry_missing_classes(
        artifact, source, expected, produced, ctx, report, ctx.chain[used_index + 1 :]
    )
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
            kind, classes = classify_counted(p)
            nested.append(Artifact(p, f"{artifact.rel}!/{name}", kind, classes))
    return nested


def _fetch_stage(
    artifact: Artifact, ctx: Ctx
) -> tuple[ArtifactReport, list[Artifact], Path | None]:
    """Stage 1 (IO): discovery, classification, maven resolution, sources extraction.

    Returns the report, nested artifacts to feed back into stage 1, and the
    decompile target for stage 2 — None when the artifact completed here.
    """
    report = ArtifactReport(rel=artifact.rel, kind=artifact.kind.value, outcome="ok")
    if ctx.on_event is not None:
        ctx.on_event("fetch", artifact.rel, "")
    nested: list[Artifact] = []
    try:
        if artifact.kind is ArtifactKind.CORRUPT:
            report.outcome = "failed"
            report.failure = "unreadable archive"
        elif artifact.kind is ArtifactKind.RESOURCE_ONLY:
            nested = _discover_nested(artifact, ctx)  # e.g. a war bundling only jars
            copied, skipped = ctx.writer.add_resources(
                artifact.path, artifact.rel, include_sources=True
            )
            report.resources_copied += copied
            report.resources_skipped += skipped
            report.outcome = "ok" if copied else "skipped"
        elif artifact.kind is ArtifactKind.BEYOND_DEPTH:
            report.outcome = "skipped"
            report.failure = f"nested deeper than --max-depth {ctx.settings.max_depth}"
        elif artifact.kind is ArtifactKind.SOURCES_JAR:
            tmp = _tmp_dir(ctx)
            extract_java(artifact.path, tmp)
            java, collisions = ctx.writer.add_tree(tmp, artifact.rel)
            report.java_files = java
            report.collisions = collisions
            copied, skipped = ctx.writer.add_resources(artifact.path, artifact.rel)
            report.resources_copied += copied
            report.resources_skipped += skipped
            if java == 0:
                report.outcome = "failed"
                report.failure = "sources jar contained no .java files"
            else:
                report.method = "extracted"
        elif artifact.kind is ArtifactKind.CLASS_TREE:
            tmp = _tmp_dir(ctx)
            copy_class_tree(artifact.path, tmp)
            return report, nested, tmp
        else:  # ARCHIVE
            nested = _discover_nested(artifact, ctx)
            copied, skipped = ctx.writer.add_resources(artifact.path, artifact.rel)
            report.resources_copied += copied
            report.resources_skipped += skipped
            resolution = None
            if ctx.settings.maven and ctx.client is not None:
                resolution = ctx.resolver(
                    artifact.path, list(ctx.settings.repos), ctx.client, ctx.sources_cache
                )
            done = False
            if resolution is not None and resolution.sources_jar is not None:
                tmp = _tmp_dir(ctx)
                if extract_java(resolution.sources_jar, tmp) > 0:
                    java, collisions = ctx.writer.add_tree(tmp, artifact.rel)
                    report.method = "maven"
                    report.repo = resolution.repo
                    report.resolved_by = resolution.resolved_by
                    report.sources_cached = resolution.cached
                    report.java_files = java
                    report.collisions = collisions
                    done = True
                else:
                    resolution.miss = f"sources jar for {resolution.gav} contained no .java files"
            if resolution is not None:
                if resolution.gav is not None:
                    report.gav = str(resolution.gav)
                if not done:
                    report.sources_miss = resolution.miss
                if ctx.on_stderr is not None:
                    msg = (
                        f"{resolution.resolved_by} {resolution.gav} ({resolution.repo})"
                        if done
                        else resolution.miss
                    )
                    ctx.on_stderr(f"maven {artifact.rel}: {msg}")
            if not done:
                return report, nested, artifact.path
    except Exception:
        report.outcome = "failed"
        report.failure = traceback.format_exc()[-2000:]
    return report, nested, None


def _decompile_stage(
    artifact: Artifact, target: Path, ctx: Ctx, report: ArtifactReport
) -> ArtifactReport:
    """Stage 2 (CPU): engine chain + per-class retries on a stage-1 hand-off."""
    try:
        _decompile(artifact, target, ctx, report)
    except Exception:
        report.outcome = "failed"
        report.failure = traceback.format_exc()[-2000:]
    return report


def _decompile_batch(
    members: list[tuple[Artifact, Path, ArtifactReport, set[str]]], ctx: Ctx
) -> tuple[list[ArtifactReport], list[tuple[Artifact, Path, ArtifactReport]]]:
    """Stage 2 for a batch: one primary-engine JVM over several small jars.

    Returns (completed reports, members to requeue for solo processing).
    Members' expected-stem sets are disjoint (enforced at formation), so every
    produced source file belongs to exactly one member.
    """
    if engines.PROCESSES.closed:
        return [], [(a, t, r) for a, t, r, _ in members]
    name = ctx.chain[0]
    dest = _tmp_dir(ctx)
    if ctx.on_event is not None:
        for a, _, _, _ in members:
            ctx.on_event("decompile", a.rel, name)
    try:
        res = ctx.batch_runner(
            ENGINES[name], ctx.engine_jars[name], [t for _, t, _, _ in members], dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
        )
    except Exception:
        res = engines.EngineResult(name, -1, False, 0, traceback.format_exc()[-2000:])

    trees: dict[int, Path] = {}
    produced: dict[int, set[str]] = {i: set() for i in range(len(members))}
    try:
        owners: dict[str, int] = {}
        for i, (_, _, _, stems) in enumerate(members):
            for s in stems:
                owners[s] = i
        if res.returncode == 0 and not res.timed_out:
            for p in sorted(dest.rglob("*")):
                if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
                    continue
                rel = p.relative_to(dest).as_posix()
                stem = normalize_java_rel(rel)[: -len(p.suffix)]
                i = owners.get(stem)
                if i is None:
                    i = owners.get(stem.split("$", 1)[0])  # inner classes ride with their outer
                if i is None:
                    continue  # engine banner/stray file; real resources come from the member jar
                tree = trees.setdefault(i, _tmp_dir(ctx))
                target = tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
                produced[i].add(stem)
    except Exception:
        trees = {}  # split unusable; every member requeues and redoes solo (self-healing)

    done: list[ArtifactReport] = []
    requeue: list[tuple[Artifact, Path, ArtifactReport]] = []
    for i, (a, target, report, stems) in enumerate(members):
        report.attempts.append(
            EngineAttempt(name, "batch", res.returncode, res.timed_out,
                          len(produced[i]), res.stderr_tail)
        )
        if i not in trees:
            requeue.append((a, target, report))
            continue
        try:
            java, collisions = ctx.writer.add_tree(trees[i], a.rel)
            report.java_files += java
            report.collisions += collisions
            report.method = name
            report.classes = len(stems)
            _retry_missing_classes(a, target, stems, produced[i], ctx, report, ctx.chain[1:])
            report.outcome = "ok"
        except Exception:
            report.outcome = "failed"
            report.failure = traceback.format_exc()[-2000:]
        done.append(report)
    return done, requeue


def process_artifact(artifact: Artifact, ctx: Ctx) -> tuple[ArtifactReport, list[Artifact]]:
    """Run both stages synchronously; the parallel runner splits them across pools."""
    report, nested, target = _fetch_stage(artifact, ctx)
    if target is not None:
        _decompile_stage(artifact, target, ctx, report)
    return report, nested


class DecafError(Exception):
    """Environment or usage error; the CLI reports it and exits 2."""


def _preflight_engines(
    settings: Settings,
    java_major: int,
    client: httpx.Client,
    on_event: Callable[[str, str, str], None] | None = None,
) -> tuple[list[str], dict[str, Path]]:
    if on_event is not None:
        on_event("engines", "", "verifying")
    wanted = chain_for(settings.engine, settings.fallback)
    specs = engines.active_specs(settings.engine_overrides)
    jars: dict[str, Path] = {}
    for name in wanted:
        spec = specs[name]
        if spec.min_java > java_major:
            if name == settings.engine:
                raise DecafError(
                    f"engine {name} needs Java {spec.min_java}+, found Java {java_major}"
                )
            continue
        downloaded = False
        kwargs: dict = {}
        if on_event is not None:

            def _hook(name: str = name, version: str = spec.version) -> None:
                nonlocal downloaded
                downloaded = True
                on_event("engines", name, f"downloading {version}")

            kwargs["on_download"] = _hook
        try:
            jars[name] = engines.ensure_engine(spec, client, **kwargs)
        except engines.EngineError as exc:
            if name == settings.engine:
                raise DecafError(str(exc)) from exc
        else:
            if downloaded:
                on_event("engines", name, f"downloaded {spec.version}")
    chain = chain_for(settings.engine, settings.fallback, available=set(jars))
    if not chain:
        raise DecafError(f"primary engine {settings.engine!r} unavailable")
    if on_event is not None:
        on_event("engines", "", "ready")
    return chain, jars


def _form_batch(ready_small: deque) -> list:
    """Greedy prefix of ready smalls with disjoint stems, bounded by the caps.

    Skipped members (stem overlap / class cap) stay queued in order for the
    next batch. Always takes at least one member, so the queue drains.
    """
    batch: list = []
    taken: set[str] = set()
    total_classes = 0
    kept: list = []
    while ready_small and len(batch) < _BATCH_MAX_JARS:
        item = ready_small.popleft()
        a, _, _, stems = item
        if batch and (total_classes + a.classes > _BATCH_MAX_CLASSES or (stems & taken)):
            kept.append(item)
            continue
        batch.append(item)
        taken |= stems
        total_classes += a.classes
    ready_small.extendleft(reversed(kept))
    return batch


def run(
    settings: Settings,
    *,
    on_done: Callable[[ArtifactReport], None] | None = None,
    on_found: Callable[[int], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_warn: Callable[[str], None] | None = None,
    on_event: Callable[[str, str, str], None] | None = None,
    runner: Callable | None = None,
    resolver: Callable | None = None,
    batch_runner: Callable | None = None,
) -> RunReport:
    start = time.monotonic()
    engines.PROCESSES.reset()
    found = engines.find_java()
    if found is None:
        raise DecafError("java not found on PATH (Java 11+ required)")
    java_exe, java_major = found
    if java_major < engines.JAVA_MIN:
        raise DecafError(f"Java {java_major} is too old (Java {engines.JAVA_MIN}+ required)")

    artifacts, nested_counts = scan_counted(settings.input)
    if on_found is not None:
        on_found(len(artifacts) + sum(nested_counts.values()))
    if on_event is not None:
        on_event(
            "scan", "",
            f"{len(artifacts)} top-level + {sum(nested_counts.values())} nested",
        )
    cds_dir: Path | None = None
    if runner is None:
        runner = engines.run_engine
        batch_runner = batch_runner or engines.run_engine_batch
        if java_major >= engines.CDS_MIN_JAVA:
            cds_dir = engines.cache_root() / "engines"
    # An injected runner without an injected batch_runner disables batching:
    # custom runners are per-target and must see every artifact individually.
    net = maven.NetState(warn=on_warn)
    resolver = resolver or partial(maven.resolve_sources, net=net)

    interrupted = False
    reports: list[ArtifactReport] = []
    chain: list[str] = []
    tmp_root = Path(tempfile.mkdtemp(prefix="decaf-"))
    client = httpx.Client(follow_redirects=True, timeout=30)
    total_cpus = settings.cpus or max(1, (os.cpu_count() or 1) - 1)  # auto: leave one core free
    jobs = settings.jobs or min(4, os.cpu_count() or 1)
    jobs = max(1, min(jobs, total_cpus))
    cpu_budget = max(1, total_cpus // jobs)
    fetch_jobs = min(8, 2 * jobs)  # IO-sized: stage 1 waits on the network, not cores
    queue_bound = 2 * jobs  # decompiles queued beyond the running `jobs` before stage 1 stalls
    affinity_base: set[int] | None = None
    if hasattr(os, "sched_setaffinity"):
        # ActiveProcessorCount only sizes JVM pools — JIT/GC warmup still bursts past
        # it. Pinning the process enforces the budget; engine JVMs inherit the mask.
        affinity_base = os.sched_getaffinity(0)
        os.sched_setaffinity(0, set(sorted(affinity_base)[:total_cpus]))
    try:
        chain, engine_jars = _preflight_engines(settings, java_major, client, on_event)
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
            batch_runner=batch_runner,
            cpu_budget=cpu_budget,
            cds_dir=cds_dir,
            on_stderr=on_stderr,
            on_event=on_event,
        )
        try:
            with (
                ThreadPoolExecutor(max_workers=fetch_jobs) as fetch_pool,
                ThreadPoolExecutor(max_workers=jobs) as dec_pool,
            ):
                seq = count()
                todo: list[tuple[int, int, Artifact]] = []
                for a in artifacts:
                    heappush(todo, (-a.classes, next(seq), a))
                fetch_futs: dict = {}  # Future -> Artifact, kept for the stage-2 hand-off
                dec_futs: dict = {}  # Future -> (kind, weight)
                ready: list[tuple] = []  # (-classes, seq, artifact, target, report)
                ready_small: deque = deque()  # batch-eligible: (artifact, target, report, stems)
                dec_weight = 0
                try:
                    while todo or fetch_futs or dec_futs or ready or ready_small:
                        # Admission gate: every fetch future is a potential decompile,
                        # so capping in-flight work (including the held ready buffer)
                        # bounds the stage-2 backlog and stalls downloads when
                        # decompilation falls behind.
                        while (
                            todo
                            and len(fetch_futs) < fetch_jobs
                            and len(fetch_futs) + len(dec_futs) + len(ready) + len(ready_small)
                            < jobs + queue_bound
                        ):
                            _, _, a = heappop(todo)
                            fetch_futs[fetch_pool.submit(_fetch_stage, a, ctx)] = a
                        # Weight-gated drain: whales take 2 slots so they keep
                        # headroom; an idle pool admits anything (no deadlock at
                        # jobs=1). Largest first.
                        while ready and (
                            dec_weight == 0 or dec_weight + _decompile_weight(ready[0][2]) <= jobs
                        ):
                            _, _, a, target, report = heappop(ready)
                            w = _decompile_weight(a)
                            dec_weight += w
                            dec_futs[dec_pool.submit(_decompile_stage, a, target, ctx, report)] = ("solo", w)
                        # ready drains first: a waiting whale gets first claim on freed weight
                        while ready_small and (dec_weight == 0 or dec_weight + 1 <= jobs):
                            batch = _form_batch(ready_small)
                            dec_weight += 1
                            if len(batch) == 1:
                                a, target, report, _ = batch[0]
                                dec_futs[dec_pool.submit(_decompile_stage, a, target, ctx, report)] = ("solo", 1)
                            else:
                                dec_futs[dec_pool.submit(_decompile_batch, batch, ctx)] = ("batch", 1)
                        if not fetch_futs and not dec_futs:
                            continue  # unreachable in practice: drain above always progresses
                        done, _ = wait(
                            fetch_futs.keys() | dec_futs.keys(), return_when=FIRST_COMPLETED
                        )
                        for fut in done:
                            if fut in dec_futs:
                                kind, w = dec_futs.pop(fut)
                                dec_weight -= w
                                if kind == "batch":
                                    done_reports, requeue = fut.result()
                                    for a2, t2, r2 in requeue:
                                        heappush(ready, (-a2.classes, next(seq), a2, t2, r2))
                                    for r2 in done_reports:
                                        reports.append(r2)
                                        if on_done is not None:
                                            on_done(r2)
                                    continue
                                report = fut.result()
                            else:
                                a = fetch_futs.pop(fut)
                                report, nested, target = fut.result()
                                for n in nested:
                                    heappush(todo, (-n.classes, next(seq), n))
                                delta = len(nested) - nested_counts.pop(a.rel, 0)
                                if delta and on_found is not None:
                                    on_found(delta)
                                if target is not None:
                                    if on_event is not None:
                                        on_event("queued", a.rel, "")
                                    if (
                                        batch_runner is not None
                                        and not target.is_dir()
                                        and a.classes < _SMALL_CLASSES
                                        and chain[0] in engines.BATCH_ENGINES
                                    ):
                                        ready_small.append(
                                            (a, target, report, expected_class_stems(target))
                                        )
                                    else:
                                        heappush(ready, (-a.classes, next(seq), a, target, report))
                                    continue
                            reports.append(report)
                            if on_done is not None:
                                on_done(report)
                except KeyboardInterrupt:
                    interrupted = True
                    net.abort.set()  # wake fetch threads sleeping in retry backoff
                    engines.PROCESSES.kill_all()
                    for fut in [*fetch_futs, *dec_futs]:
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
                    "fetch_jobs": fetch_jobs,
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
        if affinity_base is not None:
            os.sched_setaffinity(0, affinity_base)
    return run_report
