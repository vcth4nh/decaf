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
from datetime import datetime, timezone
from functools import partial
from heapq import heappush, heappop
from itertools import count
from pathlib import Path, PurePosixPath

import httpx

from . import __version__, casefs, check, engines, maven
from .engines import ENGINES
from .maven import extract_sources
from .scanner import (
    ARCHIVE_EXTS,
    Artifact,
    ArtifactKind,
    SOURCE_SUFFIXES,
    classify_counted,
    copy_class_tree,
    find_nested_archives,
    safe_extract_zip,
    scan_counted,
)
from .verdicts import VerdictCache

_CONTAINER_ROOTS = ("WEB-INF/classes/", "BOOT-INF/classes/")

_WHALE_CLASSES = 3000  # artifacts at/above this class count get scheduling headroom

_SMALL_CLASSES = 3000  # below this, an archive is batchable (== _WHALE_CLASSES; #74 sweep)
_BATCH_MAX_JARS = 32
_BATCH_MAX_CLASSES = 8000  # ceilings picked at the measured plateau (#74)

_PROGRESS_INTERVAL = 1.0  # seconds between batch-dest polls (display only)


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


def _owner_index(stem: str, owners: dict[str, int]) -> int | None:
    """Batch attribution: which member owns a produced source stem.

    Inner classes ride with their outer class (split on the first ``$``).
    Shared by the post-batch split and the live watcher so they can't diverge.
    """
    i = owners.get(stem)
    if i is None:
        i = owners.get(stem.split("$", 1)[0])
    return i


@dataclass
class EngineAttempt:
    engine: str
    level: str  # "archive" | "class" | "batch"
    returncode: int
    timed_out: bool
    java_files: int
    stderr_tail: str
    flagged: int = 0  # files that the compile check flagged in this attempt's output (#99)


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
    flagged: list[dict] = field(default_factory=list)  # files that stay flagged by the compile check (#99)
    check: dict | None = None  # compile-check counts over the final output; None = no check ran

    @property
    def partial(self) -> bool:
        """ok but degraded: engine holes, flagged files, or decompiled sources-less due to network."""
        return self.outcome == "ok" and (
            self.missing_classes > 0
            or bool(self.flagged)
            or (self.sources_miss or "").startswith("network:")
        )


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
    regardless of the order in which worker threads deliver results. On a
    case-insensitive output tree the index is keyed by the case-folded path,
    so two jars with `p/A` and `p/a` collide instead of overwriting (#93).
    """

    def __init__(self, src_root: Path, *, case_insensitive: bool = False) -> None:
        self.root = src_root
        self._lock = threading.Lock()
        self._case_insensitive = case_insensitive
        self._index: dict[str, tuple[str, str, str]] = {}  # key -> (sort_key, sha256, rel)

    def add_tree(self, tree: Path, sort_key: str) -> tuple[int, list[dict]]:
        java = 0
        collisions: list[dict] = []
        for p in sorted(tree.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            java += 1
            rel = normalize_java_rel(p.relative_to(tree).as_posix())
            key = rel.casefold() if self._case_insensitive else rel
            content = p.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            with self._lock:
                existing = self._index.get(key)
                if existing is None:
                    self._index[key] = (sort_key, digest, rel)
                    target = self.root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                elif existing[1] == digest:
                    pass  # identical duplicate
                elif sort_key < existing[0]:
                    entry = {"path": rel, "kept": sort_key, "dropped": existing[0]}
                    if existing[2] != rel:
                        entry["dropped_path"] = existing[2]
                        (self.root / existing[2]).unlink(missing_ok=True)
                    collisions.append(entry)
                    self._index[key] = (sort_key, digest, rel)
                    target = self.root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                else:
                    entry = {"path": existing[2], "kept": existing[0], "dropped": sort_key}
                    if existing[2] != rel:
                        entry["dropped_path"] = rel
                    collisions.append(entry)
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
            if not p.is_file() or p.suffix.lower() not in SOURCE_SUFFIXES:
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
        try:
            # Partial extractions may leave files behind; refused/duplicate
            # entries make the count approximate. Resources must never taint
            # the artifact's decompile outcome.
            return safe_extract_zip(archive, self.dest_for(rel), members=names), 0
        except (zipfile.BadZipFile, RuntimeError, OSError):
            return 0, 0

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
            target.unlink(missing_ok=True)
            return 0, 0
        return 1, 0


@dataclass
class RunReport:
    settings: dict
    artifacts: list[ArtifactReport]
    totals: dict
    duration_seconds: float
    discovered: int = 0  # scan-found artifacts, incl. never-started ones on interrupt
    interrupted: bool = False
    schema_version: int = 0
    decaf_version: str = ""
    status: str = ""
    started_at: str = ""
    ended_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def compute_totals(reports: list[ArtifactReport]) -> dict:
    return {
        "artifacts": len(reports),
        "ok": sum(r.outcome == "ok" for r in reports),
        "failed": sum(r.outcome == "failed" for r in reports),
        "skipped": sum(r.outcome == "skipped" for r in reports),
        "partial": sum(1 for r in reports if r.partial),
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
        "checked_files": sum((r.check or {}).get("checked", 0) for r in reports),
        "flagged_files": sum((r.check or {}).get("flagged", 0) for r in reports),
        "noted_files": sum((r.check or {}).get("noted", 0) for r in reports),
        "unchecked_files": sum((r.check or {}).get("unchecked", 0) for r in reports),
    }


@dataclass
class Settings:
    input: Path
    output: Path
    engine: str = "vineflower"
    fallback: bool = True
    engine_args: tuple[str, ...] = ()  # raw tokens for the primary engine; requires fallback=False
    mirror: bool = True  # mirror the input layout; False = one merged src/ tree
    resources: bool = True  # mirror mode: also carry the original archives' resources
    maven: bool = True
    fresh_maven: bool = False  # ignore stored lookup verdicts this run (still re-records)
    check: bool = True  # javac compile check of decompiled sources (#99)
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
    on_event: Callable[[str, str, str], None] | None = None  # live progress events (scan/engines/fetch/queued/decompile/progress)
    case_insensitive: str | None = None  # roots that ignore case ("output directory", ...); None = case-sensitive run (#93)
    checker: Callable | None = None  # compile check (#99): check.run_check with bound args; None = off
    java_major: int = 0  # limits the class versions that the check accepts; 0 = no limit
    input_group: list[Path] = field(default_factory=list)  # top-level classpath entries (#99)
    families: dict[str, tuple[Path, list[Path]]] = field(default_factory=dict)  # parent rel -> (path, nested archives)
    class_roots: dict[str, Path | None] = field(default_factory=dict)  # rel -> extracted WEB-INF/BOOT-INF classes
    family_lock: threading.Lock = field(default_factory=threading.Lock)


def _tmp_dir(ctx: Ctx) -> Path:
    return Path(tempfile.mkdtemp(dir=ctx.tmp_root))


def _class_stem(entry: str) -> str:
    return normalize_java_rel(entry)[: -len(".class")]


def _stems_and_claims(source: Path) -> tuple[set[str], set[str]]:
    """One pass over the class entries: (top-level class stems, metadata claims).

    Claims are the module-info/package-info source rels this archive could
    yield. They attribute batch output back to its member and keep claimants
    from sharing a batch, but never join expectations (#75): metadata is
    copied when an engine emits it, exactly like a solo run — never retried.
    """
    if source.is_dir():
        entries = [p.relative_to(source).as_posix() for p in source.rglob("*.class")]
    else:
        try:
            with zipfile.ZipFile(source) as zf:
                entries = [n for n in zf.namelist() if n.endswith(".class")]
        except (zipfile.BadZipFile, OSError):
            return set(), set()
    stems: set[str] = set()
    claims: set[str] = set()
    for e in entries:
        base = e.rsplit("/", 1)[-1]
        if base in ("module-info.class", "package-info.class"):
            claims.add(_class_stem(e))
        elif "$" not in base:
            stems.add(_class_stem(e))
    return stems, claims


def expected_class_stems(source: Path) -> set[str]:
    """Top-level class names (no $-inner, no module/package-info) a decompile should yield."""
    return _stems_and_claims(source)[0]


def produced_stems(dest: Path) -> set[str]:
    return {
        normalize_java_rel(p.relative_to(dest).as_posix())[: -len(p.suffix)]
        for p in dest.rglob("*")
        if p.is_file() and p.suffix in SOURCE_SUFFIXES
    }


def _class_versions(source: Path) -> dict[str, int]:
    """Lowest class-file major version of each top-level class, by stem (#99)."""
    out: dict[str, int] = {}

    def keep(stem: str, major: int | None) -> None:
        if major is not None:
            out[stem] = min(out.get(stem, major), major)

    if source.is_dir():
        for p in source.rglob("*.class"):
            if "$" not in p.name:
                with open(p, "rb") as f:
                    keep(_class_stem(p.relative_to(source).as_posix()), check.class_major(f.read(8)))
        return out
    try:
        with zipfile.ZipFile(source) as zf:
            for name in zf.namelist():
                if not name.endswith(".class") or "$" in name.rsplit("/", 1)[-1]:
                    continue
                with zf.open(name) as f:
                    keep(_class_stem(name), check.class_major(f.read(8)))
    except (zipfile.BadZipFile, OSError):
        pass
    return out


def _class_root(rel: str, path: Path, ctx: Ctx) -> Path | None:
    """WEB-INF/classes or BOOT-INF/classes of an archive, extracted once (#99)."""
    with ctx.family_lock:
        if rel in ctx.class_roots:
            return ctx.class_roots[rel]
    root: Path | None = None
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        for prefix in _CONTAINER_ROOTS:
            members = {n for n in names if n.startswith(prefix) and n.endswith(".class")}
            if members:
                dest = _tmp_dir(ctx)
                safe_extract_zip(path, dest, members=members)
                root = dest / prefix.rstrip("/")
                break
    except (zipfile.BadZipFile, RuntimeError, OSError):
        root = None
    with ctx.family_lock:
        return ctx.class_roots.setdefault(rel, root)


def _container_group(rel: str, path: Path, ctx: Ctx) -> list[Path]:
    root = _class_root(rel, path, ctx)
    with ctx.family_lock:
        nested = list(ctx.families.get(rel, (path, []))[1])
    return ([root] if root is not None else []) + [path] + nested


def _family_classpath(artifact: Artifact, source: Path, ctx: Ctx) -> list[Path]:
    """Classpath of an artifact's check: its own group, then its parent's or the input group (#99)."""
    cp = [source] if source.is_dir() else _container_group(artifact.rel, source, ctx)
    if "!/" in artifact.rel:
        parent = artifact.rel.rsplit("!/", 1)[0]
        with ctx.family_lock:
            entry = ctx.families.get(parent)
        if entry is not None:
            cp += _container_group(parent, entry[0], ctx)
    else:
        cp += ctx.input_group
    out: list[Path] = []
    for p in cp:
        if p not in out:
            out.append(p)
    return out


_METADATA_SOURCES = ("package-info.java", "module-info.java")


@dataclass
class _Held:
    """Per-artifact compile-check state (#99): held flagged files and final results."""

    root: Path | None = None  # a _tmp_dir, made at the first flagged file
    files: dict[str, tuple[str, str, check.FileResult]] = field(default_factory=dict)  # stem -> (engine, path, result)
    checks: dict[str, check.FileResult] = field(default_factory=dict)  # stem -> result of the written file
    versions: dict[str, int] | None = None  # stem -> class major version, read once


def _check_trees(
    items: list[tuple[Path, Artifact, Path, _Held]], ctx: Ctx
) -> list[dict[str, check.FileResult]] | None:
    """One check call for (tree, artifact, source, held) items; results by path in each tree."""
    if ctx.checker is None or engines.PROCESSES.closed:
        return None
    limit = ctx.java_major + check.CLASS_VERSION_OFFSET if ctx.java_major else None
    results: list[dict[str, check.FileResult]] = []
    sent: list[list[str]] = []
    jobs: list[check.Job] = []
    for n, (tree, artifact, source, held) in enumerate(items):
        if held.versions is None:
            held.versions = _class_versions(source)
        by_path: dict[str, check.FileResult] = {}
        paths: list[str] = []
        for p in sorted(tree.rglob("*")):
            suffix = p.suffix.lower()
            if not p.is_file() or suffix not in SOURCE_SUFFIXES or p.name.lower() in _METADATA_SOURCES:
                continue
            rel = p.relative_to(tree).as_posix()
            if suffix == ".kt":
                by_path[rel] = check.unchecked("kotlin")
                continue
            stem = normalize_java_rel(rel)[: -len(p.suffix)]
            major = held.versions.get(stem) or held.versions.get(stem.split("$", 1)[0])
            if limit is not None and major is not None and major > limit:
                by_path[rel] = check.unchecked(
                    "newer class version", needs_java=major - check.CLASS_VERSION_OFFSET
                )
                continue
            paths.append(rel)
        results.append(by_path)
        sent.append(paths)
        jobs.append(check.Job(str(n), tuple(_family_classpath(artifact, source, ctx)), tuple(tree / r for r in paths)))
    if any(job.files for job in jobs):
        answers = ctx.checker(jobs, timeout=ctx.settings.timeout, cpu_budget=ctx.cpu_budget)
        for by_path, paths, answer in zip(results, sent, answers):
            by_path.update(zip(paths, answer))
    return results


def _apply_hold(
    tree: Path, results: dict[str, check.FileResult], engine: str, held: _Held, ctx: Ctx
) -> int:
    """Hold back the flagged files of one output; a passing file replaces a held one (#99)."""
    flagged = 0
    for rel, result in sorted(results.items()):
        p = tree / rel
        if not p.is_file():
            continue
        stem = normalize_java_rel(rel)[: -len(p.suffix)]
        if result.status == "flagged":
            flagged += 1
        if stem in held.files:
            if result.passes:
                _, held_rel, _ = held.files.pop(stem)
                (held.root / held_rel).unlink(missing_ok=True)
                held.checks[stem] = result
            else:
                p.unlink()  # the earlier engine's held file stays
        elif result.status == "flagged":
            if held.root is None:
                held.root = _tmp_dir(ctx)
            target = held.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, target)
            held.files[stem] = (engine, rel, result)
        else:
            held.checks[stem] = result
    return flagged


def _hold_flagged(
    tree: Path, engine: str, artifact: Artifact, source: Path, ctx: Ctx, held: _Held
) -> int:
    """Check one engine output before the writer copies it; returns its flagged count."""
    results = _check_trees([(tree, artifact, source, held)], ctx)
    if results is None:
        return 0
    return _apply_hold(tree, results[0], engine, held, ctx)


def _finish_held(artifact: Artifact, ctx: Ctx, report: ArtifactReport, held: _Held) -> None:
    """End of the chain: write the held files and fill the check report (#99)."""
    if held.files:
        java, collisions = ctx.writer.add_tree(held.root, artifact.rel)
        report.java_files += java
        report.collisions += collisions
        for stem, (engine, rel, result) in held.files.items():
            held.checks[stem] = result
            report.flagged.append({
                "path": rel, "engine": engine, "kinds": list(result.kinds),
                "errors": result.errors, "first": result.first,
            })
        report.flagged.sort(key=lambda e: e["path"])
    if ctx.checker is not None:
        report.check = check.summary(list(held.checks.values()))


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
    held: _Held | None = None,
) -> None:
    held = held if held is not None else _Held()
    missing = expected - produced - set(held.files)
    for name in chain:
        if engines.PROCESSES.closed:
            break
        retry = missing | (set(held.files) & expected)
        if not retry:
            break
        retry_tree = _extract_failed_classes(source, retry, ctx)
        if retry_tree is None:
            break
        dest = _tmp_dir(ctx)
        if ctx.on_event is not None:
            ctx.on_event("decompile", artifact.rel, f"{name} · retry {len(retry):,} classes")
        res = ctx.runner(
            ENGINES[name], ctx.engine_jars[name], retry_tree, dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
            **_stream_kw_for(ctx, name, artifact.rel),
        )
        attempt = EngineAttempt(name, "class", res.returncode, res.timed_out, res.java_files, res.stderr_tail)
        report.attempts.append(attempt)
        if res.java_files > 0:
            attempt.flagged = _hold_flagged(dest, name, artifact, source, ctx, held)
            java, collisions = ctx.writer.add_tree(dest, artifact.rel)
            report.java_files += java
            report.collisions += collisions
            produced |= produced_stems(dest)
            missing = expected - produced - set(held.files)
    report.missing_classes = len(missing)
    _finish_held(artifact, ctx, report, held)


def _decompile(artifact: Artifact, target: Path, ctx: Ctx, report: ArtifactReport) -> None:
    source = target if target.is_dir() else artifact.path
    expected = expected_class_stems(source)
    report.classes = len(expected)
    produced: set[str] = set()
    held = _Held()

    used_index: int | None = None
    for i, name in enumerate(ctx.chain):
        if engines.PROCESSES.closed:
            break
        dest = _tmp_dir(ctx)
        if ctx.on_event is not None:
            ctx.on_event("decompile", artifact.rel, f"{name} · {len(expected):,} classes")
        res = ctx.runner(
            ENGINES[name], ctx.engine_jars[name], target, dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
            **({"engine_args": ctx.settings.engine_args} if ctx.settings.engine_args else {}),
            **_stream_kw_for(ctx, name, artifact.rel),
        )
        attempt = EngineAttempt(name, "archive", res.returncode, res.timed_out, res.java_files, res.stderr_tail)
        report.attempts.append(attempt)
        if res.java_files > 0:
            attempt.flagged = _hold_flagged(dest, name, artifact, source, ctx, held)
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
        artifact, source, expected, produced, ctx, report, ctx.chain[used_index + 1 :], held
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
    nested: list[Artifact] = []
    if ctx.case_insensitive:
        groups = casefs.case_groups(names)
        if groups:  # e.g. WEB-INF/lib/A.jar + WEB-INF/lib/a.jar would extract onto one file (#93)
            text = _case_failure(groups, "nested archives", ctx.case_insensitive)
            clashing = {n for g in groups for n in g}
            nested = [
                Artifact(artifact.path, f"{artifact.rel}!/{name}", ArtifactKind.ARCHIVE, refusal=text)
                for name in names
                if name in clashing
            ]
            names = [n for n in names if n not in clashing]
    extract_dir = _tmp_dir(ctx)
    safe_extract_zip(artifact.path, extract_dir, members=names)
    for name in names:
        p = extract_dir / name
        if p.is_file():
            kind, classes = classify_counted(p)
            nested.append(Artifact(p, f"{artifact.rel}!/{name}", kind, classes))
    with ctx.family_lock:
        ctx.families[artifact.rel] = (
            artifact.path,
            [a.path for a in nested if a.kind is ArtifactKind.ARCHIVE and a.refusal is None],
        )
    return nested


_CASE_CHECKED_KINDS = (ArtifactKind.ARCHIVE, ArtifactKind.SOURCES_JAR, ArtifactKind.CLASS_TREE)


def _output_stems(artifact: Artifact) -> set[str]:
    """Normalized names this artifact would write — the ones that can clash by case (#93).

    Archives and class trees: top-level class stems (an inner class collides iff
    its outer does; module/package-info never do). Sources jars: the source
    entries themselves, suffix included (`A.java` and `a.kt` do not clash).
    """
    if artifact.kind is not ArtifactKind.SOURCES_JAR:
        return expected_class_stems(artifact.path)
    try:
        with zipfile.ZipFile(artifact.path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return set()
    return {
        normalize_java_rel(n)
        for n in names
        if not n.endswith("/") and PurePosixPath(n).suffix.lower() in SOURCE_SUFFIXES
    }


def _case_failure(groups: list[list[str]], unit: str, label: str) -> str:
    total = sum(len(g) for g in groups)
    more = ", ..." if len(groups) > 1 else ""
    return (
        f"case collision: {total} {unit} differ only by case "
        f"({' vs '.join(groups[0])}{more}); case-insensitive {label}"
    )


def _fetch_stage(
    artifact: Artifact, ctx: Ctx
) -> tuple[ArtifactReport, list[Artifact], Path | None]:
    """Stage 1 (IO): discovery, classification, maven resolution, sources extraction.

    Returns the report, nested artifacts to feed back into stage 1, and the
    decompile target for stage 2 — None when the artifact completed here.
    """
    report = ArtifactReport(rel=artifact.rel, kind=artifact.kind.value, outcome="ok")

    def state(sub: str) -> None:
        if ctx.on_event is not None:
            ctx.on_event("fetch", artifact.rel, sub)

    nested: list[Artifact] = []
    try:
        if artifact.refusal:  # decided by the parent's discovery (#93): nothing to extract or run
            report.outcome = "failed"
            report.failure = artifact.refusal
            return report, nested, None
        if ctx.case_insensitive and artifact.kind in _CASE_CHECKED_KINDS:
            groups = casefs.case_groups(_output_stems(artifact))
            if groups:
                if artifact.kind is ArtifactKind.ARCHIVE:
                    nested = _discover_nested(artifact, ctx)  # nested jars get their own check
                unit = "source files" if artifact.kind is ArtifactKind.SOURCES_JAR else "classes"
                report.outcome = "failed"
                report.failure = _case_failure(groups, unit, ctx.case_insensitive)
                return report, nested, None
        if artifact.kind is ArtifactKind.CORRUPT:
            report.outcome = "failed"
            report.failure = "unreadable archive"
        elif artifact.kind is ArtifactKind.RESOURCE_ONLY:
            state("scanning")
            nested = _discover_nested(artifact, ctx)  # e.g. a war bundling only jars
            state("copying resources")
            copied, skipped = ctx.writer.add_resources(
                artifact.path, artifact.rel, include_sources=True
            )
            report.resources_copied += copied
            report.resources_skipped += skipped
            report.outcome = "ok" if copied else "skipped"
        elif artifact.kind is ArtifactKind.BEYOND_DEPTH:
            state("copying resources")
            member = artifact.rel.rsplit("!/", 1)[1]
            copied, skipped = ctx.writer.add_blob(artifact.path, member, artifact.rel)
            report.resources_copied += copied
            report.resources_skipped += skipped
            report.outcome = "skipped"
            report.failure = f"nested deeper than --max-depth {ctx.settings.max_depth}"
        elif artifact.kind is ArtifactKind.SOURCES_JAR:
            state("extracting")
            tmp = _tmp_dir(ctx)
            extract_sources(artifact.path, tmp)
            java, collisions = ctx.writer.add_tree(tmp, artifact.rel)
            report.java_files = java
            report.collisions = collisions
            state("copying resources")
            copied, skipped = ctx.writer.add_resources(artifact.path, artifact.rel)
            report.resources_copied += copied
            report.resources_skipped += skipped
            if java == 0:
                report.outcome = "failed"
                report.failure = "sources jar contained no .java/.kt files"
            else:
                report.method = "extracted"
        elif artifact.kind is ArtifactKind.CLASS_TREE:
            state("copying classes")
            tmp = _tmp_dir(ctx)
            copy_class_tree(artifact.path, tmp)
            return report, nested, tmp
        else:  # ARCHIVE
            state("scanning")
            nested = _discover_nested(artifact, ctx)
            state("copying resources")
            copied, skipped = ctx.writer.add_resources(artifact.path, artifact.rel)
            report.resources_copied += copied
            report.resources_skipped += skipped
            resolution = None
            if ctx.settings.maven and ctx.client is not None:
                state("resolving")
                resolution = ctx.resolver(
                    artifact.path, list(ctx.settings.repos), ctx.client, ctx.sources_cache,
                    **({} if ctx.on_event is None else {"on_state": state, "subject": artifact.rel}),
                )
            done = False
            if resolution is not None and resolution.sources_jar is not None:
                state("extracting")
                tmp = _tmp_dir(ctx)
                if extract_sources(resolution.sources_jar, tmp) > 0:
                    java, collisions = ctx.writer.add_tree(tmp, artifact.rel)
                    report.method = "maven"
                    report.repo = resolution.repo
                    report.resolved_by = resolution.resolved_by
                    report.sources_cached = resolution.cached
                    report.java_files = java
                    report.collisions = collisions
                    done = True
                else:
                    resolution.miss = f"sources jar for {resolution.gav} contained no .java/.kt files"
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


def _watch_batch(
    dest: Path,
    members: list[tuple[Artifact, Path, ArtifactReport, set[str], set[str]]],
    owners: dict[str, int],
    name: str,
    on_event: Callable[[str, str, str], None],
    stop: threading.Event,
    interval: float,
) -> None:
    """Display-only batch progress: poll the dest, attribute output to members.

    Outcomes are decided exclusively by the post-batch split; any exception
    here just ends the watcher. Tree-writing engines get per-member
    ``done/total classes``; fernflower (sequential per-member output archives,
    unreadable while partial) gets a 3-state lifecycle instead.
    """
    k = len(members)
    try:
        if name == "fernflower":
            arch: dict[str, int] = {}
            ambiguous: set[str] = set()
            for i, (_, target, _, _, _) in enumerate(members):
                if target.name in arch:
                    ambiguous.add(target.name)
                arch[target.name] = i
            seen: list[int] = []  # member indices in order of archive appearance
            state: dict[int, str] = {}
            while not stop.wait(interval):
                for p in sorted(dest.glob("*")):
                    i = arch.get(p.name)
                    if i is not None and p.name not in ambiguous and i not in seen:
                        seen.append(i)
                for i, (a, target, _, _, _) in enumerate(members):
                    if target.name in ambiguous:
                        continue  # two members share a basename: attribution is a guess, make none
                    if i not in seen:
                        label = "queued in batch"
                    elif i == seen[-1]:
                        label = "decompiling"
                    else:
                        label = "done, pending split"
                    if state.get(i) != label:
                        state[i] = label
                        on_event("progress", a.rel, f"{name} · batch of {k} · {label}")
        else:
            totals = {i: len(stems) for i, (_, _, _, stems, _) in enumerate(members)}
            last: dict[int, int] = {}
            while not stop.wait(interval):
                produced: dict[int, set[str]] = {i: set() for i in range(k)}
                for p in dest.rglob("*"):
                    if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
                        continue
                    stem = normalize_java_rel(p.relative_to(dest).as_posix())[: -len(p.suffix)]
                    i = _owner_index(stem, owners)
                    if i is not None:
                        produced[i].add(stem)
                for i, (a, _, _, _, _) in enumerate(members):
                    done = min(len(produced[i]), totals[i])
                    if done and done != last.get(i):
                        last[i] = done
                        on_event(
                            "progress", a.rel,
                            f"{name} · batch of {k} · {done:,}/{totals[i]:,} classes",
                        )
    except Exception:
        return  # display-only: a watcher failure must never affect the batch


def _decompile_batch(
    members: list[tuple[Artifact, Path, ArtifactReport, set[str], set[str]]], ctx: Ctx
) -> tuple[list[ArtifactReport], list[tuple[Artifact, Path, ArtifactReport]]]:
    """Stage 2 for a batch: one primary-engine JVM over several small jars.

    Returns (completed reports, members to requeue for solo processing).
    Members' expected-stem and metadata-claim sets are disjoint (enforced at
    formation), so every produced source file belongs to exactly one member.
    """
    if engines.PROCESSES.closed:
        return [], [(a, t, r) for a, t, r, _, _ in members]
    name = ctx.chain[0]
    dest = _tmp_dir(ctx)
    owners: dict[str, int] = {}
    for i, (_, _, _, stems, claims) in enumerate(members):
        for s in stems | claims:
            owners[s] = i
    if ctx.on_event is not None:
        for a, _, _, exp, _ in members:
            ctx.on_event(
                "decompile", a.rel,
                f"{name} · batch of {len(members)} · {len(exp):,} classes",
            )
    stop = threading.Event()
    watcher: threading.Thread | None = None
    if ctx.on_event is not None:
        watcher = threading.Thread(
            target=_watch_batch,
            args=(dest, members, owners, name, ctx.on_event, stop, _PROGRESS_INTERVAL),
            name="decaf-batch-watch",
            daemon=True,
        )
        watcher.start()
    try:
        res = ctx.batch_runner(
            ENGINES[name], ctx.engine_jars[name], [t for _, t, _, _, _ in members], dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
            **({"engine_args": ctx.settings.engine_args} if ctx.settings.engine_args else {}),
        )
    except Exception:
        res = engines.EngineResult(name, -1, False, 0, traceback.format_exc()[-2000:])
    finally:
        stop.set()
        if watcher is not None:
            watcher.join()

    trees: dict[int, Path] = {}
    produced: dict[int, set[str]] = {i: set() for i in range(len(members))}
    helds: dict[int, _Held] = {}
    flagged: dict[int, int] = {}
    try:
        if res.returncode == 0 and not res.timed_out:
            for p in sorted(dest.rglob("*")):
                if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
                    continue
                rel = p.relative_to(dest).as_posix()
                stem = normalize_java_rel(rel)[: -len(p.suffix)]
                i = _owner_index(stem, owners)
                if i is None:
                    continue  # engine banner/stray file; real resources come from the member jar
                tree = trees.setdefault(i, _tmp_dir(ctx))
                target = tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
                produced[i].add(stem)
        order = sorted(trees)
        helds = {i: _Held() for i in order}
        results = _check_trees([(trees[i], members[i][0], members[i][1], helds[i]) for i in order], ctx)
        if results is not None:
            for i, by_path in zip(order, results):
                flagged[i] = _apply_hold(trees[i], by_path, name, helds[i], ctx)
    except Exception:
        trees = {}  # split unusable; every member requeues and redoes solo (self-healing)

    done: list[ArtifactReport] = []
    requeue: list[tuple[Artifact, Path, ArtifactReport]] = []
    for i, (a, target, report, stems, _) in enumerate(members):
        report.attempts.append(
            EngineAttempt(name, "batch", res.returncode, res.timed_out,
                          len(produced[i]), res.stderr_tail, flagged.get(i, 0))
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
            _retry_missing_classes(a, target, stems, produced[i], ctx, report, ctx.chain[1:], helds[i])
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
    *,
    on_warn: Callable[[str], None] | None = None,
) -> tuple[list[str], dict[str, Path]]:
    if on_event is not None:
        on_event("engines", "", "verifying")
    wanted = chain_for(settings.engine, settings.fallback)
    specs = engines.active_specs(settings.engine_overrides)
    jars: dict[str, Path] = {}
    skipped: list[str] = []
    for name in wanted:
        spec = specs[name]
        if spec.min_java > java_major:
            if name == settings.engine:
                raise DecafError(
                    f"engine {name} needs Java {spec.min_java}+, found Java {java_major}"
                )
            skipped.append(f"{name} (needs Java {spec.min_java}+, found {java_major})")
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
    if skipped and on_warn is not None:
        on_warn("fallback chain: skipped " + ", ".join(skipped))
    if on_event is not None:
        on_event("engines", "", "ready")
    return chain, jars


def _form_batch(ready_small: deque, *, fold: bool = False) -> list:
    """Greedy prefix of ready smalls with disjoint stems and claims, bounded by the caps.

    Skipped members (stem/claim overlap / class cap) stay queued in order for
    the next batch. Always takes at least one member, so the queue drains.
    ``fold`` compares footprints case-folded: on a case-insensitive tree, stems
    that differ only by case would share one file in the batch dest (#93).
    """
    batch: list = []
    taken: set[str] = set()
    total_classes = 0
    kept: list = []
    while ready_small and len(batch) < _BATCH_MAX_JARS:
        item = ready_small.popleft()
        a, _, _, stems, claims = item
        footprint = {s.casefold() for s in stems | claims} if fold else stems | claims
        if batch and (total_classes + a.classes > _BATCH_MAX_CLASSES or (footprint & taken)):
            kept.append(item)
            continue
        batch.append(item)
        taken |= footprint
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
    checker: Callable | None = None,
) -> RunReport:
    if settings.engine_args and settings.fallback:
        raise DecafError("engine_args require fallback disabled (engine option dialects don't transfer)")
    start = time.monotonic()
    started_wall = datetime.now(timezone.utc)
    engines.PROCESSES.reset()
    found = engines.find_java()
    if found is None:
        raise DecafError("java not found on PATH (Java 11+ required)")
    java_exe, java_major = found
    if java_major < engines.JAVA_MIN:
        raise DecafError(f"Java {java_major} is too old (Java {engines.JAVA_MIN}+ required)")

    artifacts, nested_counts = scan_counted(settings.input)
    discovered = len(artifacts) + sum(nested_counts.values())
    if on_found is not None:
        on_found(discovered)
    if on_event is not None:
        on_event(
            "scan", "",
            f"{len(artifacts)} top-level + {sum(nested_counts.values())} nested",
        )
    cds_dir: Path | None = None
    default_runner = runner is None
    if runner is None:
        runner = engines.run_engine
        batch_runner = batch_runner or engines.run_engine_batch
        if java_major >= engines.CDS_MIN_JAVA:
            cds_dir = engines.cache_root() / "engines"
    if not settings.check:
        checker = None
    input_group = (
        [a.path for a in artifacts if a.kind in (ArtifactKind.ARCHIVE, ArtifactKind.CLASS_TREE)]
        if settings.input.is_dir() else []
    )
    # An injected runner without an injected batch_runner disables batching:
    # custom runners are per-target and must see every artifact individually.
    net = maven.NetState(warn=on_warn)
    if resolver is None:
        verdict_cache = VerdictCache(
            engines.cache_root() / "verdicts", fresh=settings.fresh_maven
        )
        resolver = partial(maven.resolve_sources, net=net, verdicts=verdict_cache)

    interrupted = False
    reports: list[ArtifactReport] = []
    chain: list[str] = []
    javac_path: Path | None = None
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
        try:
            case_label = casefs.prepare_roots(settings.output, tmp_root)
        except OSError as exc:
            where = getattr(exc, "filename", None) or settings.output
            raise DecafError(f"cannot write to {where}: {exc.strerror or exc}") from exc
        chain, engine_jars = _preflight_engines(
            settings, java_major, client, on_event, on_warn=on_warn
        )
        if settings.check and checker is None and default_runner:
            # An injected runner without an injected checker has no check: fake engines write fake Java.
            javac_path = check.find_javac(java_exe)
            if javac_path is None:
                if on_warn is not None:
                    on_warn(f"compile check skipped: no javac found for {java_exe}")
            else:
                try:
                    install_dir = check.install(javac_path, java_major)
                except check.CheckError as exc:
                    javac_path = None
                    if on_warn is not None:
                        on_warn(f"compile check skipped: {exc}")
                else:
                    checker = partial(
                        check.run_check, java=java_exe, install=install_dir,
                        tmp_root=tmp_root, cds_dir=cds_dir,
                    )
        writer: MergeWriter | MirrorWriter
        if settings.mirror:
            writer = MirrorWriter(settings.output, resources=settings.resources)
        else:
            writer = MergeWriter(settings.output / "src", case_insensitive=case_label is not None)
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
            case_insensitive=case_label,
            checker=checker,
            java_major=java_major,
            input_group=input_group,
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
                ready_small: deque = deque()  # batch-eligible: (artifact, target, report, stems, claims)
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
                            batch = _form_batch(ready_small, fold=case_label is not None)
                            dec_weight += 1
                            if len(batch) == 1:
                                a, target, report, _, _ = batch[0]
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
                                discovered += delta
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
                                            (a, target, report, *_stems_and_claims(target))
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
            totals = compute_totals(reports)
            status = (
                "interrupted" if interrupted
                else "completed_with_failures" if totals["failed"] else "completed"
            )
            run_report = RunReport(
                settings={
                    "input": str(settings.input),
                    "output": str(settings.output),
                    "engine": settings.engine,
                    "fallback": settings.fallback,
                    "engine_args": list(settings.engine_args),
                    "mirror": settings.mirror,
                    "resources": settings.resources,
                    "maven": settings.maven,
                    "fresh_maven": settings.fresh_maven,
                    "check": settings.check,
                    "javac": str(javac_path) if javac_path is not None else None,
                    "max_depth": settings.max_depth,
                    "jobs": jobs,
                    "fetch_jobs": fetch_jobs,
                    "cpus": total_cpus,
                    "cpu_budget": cpu_budget,
                    "timeout": settings.timeout,
                    "repos": maven._redacted(settings.repos),
                    "chain": chain,
                    "java": java_exe,
                    "java_major": java_major,
                },
                artifacts=reports,
                totals=totals,
                duration_seconds=round(time.monotonic() - start, 2),
                discovered=discovered,
                interrupted=interrupted,
                schema_version=1, decaf_version=__version__, status=status,
                started_at=started_wall.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ended_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            settings.output.mkdir(parents=True, exist_ok=True)
            report_path = settings.output / "decaf-report.json"
            tmp_path_ = report_path.with_suffix(".json.tmp")
            tmp_path_.write_text(run_report.to_json() + "\n")
            os.replace(tmp_path_, report_path)
    finally:
        client.close()
        shutil.rmtree(tmp_root, ignore_errors=True)
        if affinity_base is not None:
            os.sched_setaffinity(0, affinity_base)
    return run_report
