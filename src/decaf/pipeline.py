"""Output writers, report model, artifact processing, and the parallel runner."""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import zipfile
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from heapq import heappush, heappop
from itertools import count
from pathlib import Path, PurePosixPath

import httpx

from . import __version__, casefs, check, engines, maven, memory, stubs
from .engines import ENGINES
from .maven import extract_sources
from .scanner import (
    ARCHIVE_EXTS,
    Artifact,
    ArtifactKind,
    SOURCE_SUFFIXES,
    ZIP_ERRORS,
    classify_counted,
    copy_class_tree,
    find_nested_archives,
    outer_stem,
    safe_extract_zip,
    scan_counted,
    stays_inside,
)
from .verdicts import VerdictCache

_CONTAINER_ROOTS = ("WEB-INF/classes/", "BOOT-INF/classes/")

_WHALE_CLASSES = 3000  # artifacts at/above this class count get scheduling headroom

_SMALL_CLASSES = 3000  # below this, an archive is batchable (== _WHALE_CLASSES; #74 sweep)
_BATCH_MAX_JARS = 32
_BATCH_MAX_CLASSES = 8000  # ceilings picked at the measured plateau (#74)

_PROGRESS_INTERVAL = 1.0  # seconds between batch-dest polls (display only)
_WAIT_TICK = 0.5  # seconds: on Windows, Ctrl-C ends only a wait with a timeout (#112)


def _decompile_weight(artifact: Artifact) -> int:
    return 2 if artifact.classes >= _WHALE_CLASSES else 1


def _class_bytes(target: Path) -> int:
    """The uncompressed size of a decompile target's class files (#98)."""
    if target.is_dir():
        return sum(p.stat().st_size for p in target.rglob("*.class") if p.is_file())
    try:
        with zipfile.ZipFile(target) as zf:
            return sum(i.file_size for i in zf.infolist() if i.filename.endswith(".class"))
    except (zipfile.BadZipFile, OSError):
        return 0


def _share_warning(artifact: Artifact, target: Path, ctx: Ctx) -> str | None:
    """A warning when the size model says the primary engine needs more heap than the share."""
    heap = ctx.memory_plan.heap_mb(_decompile_weight(artifact))
    if heap is None:
        return None
    engine = ctx.chain[0]
    if memory.estimate_heap_mb(engine, 0) is None:
        return None  # no size model: do not read the jar
    est = memory.estimate_heap_mb(engine, _class_bytes(target))
    if est is None or est <= heap:
        return None
    return (
        f"{artifact.rel}: {engine} may need about {memory.gb(est)} of heap; "
        f"its share is {memory.gb(heap)} (raise --memory)"
    )


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

    Every class stem of a member is a key, inner classes too (#106); a stem that
    is no class stem rides with its part before the first ``$``.
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
    stubbed: int = 0  # files of this attempt's output that hold an engine stub marker (#96)
    peak_rss_mb: int | None = None  # the JVM's peak resident memory (#98)
    heap_mb: int | None = None  # the -Xmx it got; None = no cap
    out_of_memory: bool = False  # java.lang.OutOfMemoryError in stderr or in a stub
    flagged: int = 0  # files that the compile check flagged in this attempt's output (#99)
    batch: int | None = None  # the number of the batch JVM: the jars of one batch share it (#114)


def out_of_memory_runs(attempts: Iterable[EngineAttempt]) -> int:
    """The JVM runs that ran out of heap: the attempts of one batch are one run (#114)."""
    oom = [a for a in attempts if a.out_of_memory]
    return sum(a.batch is None for a in oom) + len({a.batch for a in oom if a.batch is not None})


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
    sources_gap: int = 0  # top-level classes that the Maven sources jar lacks; the engines write them (#180)
    attempts: list[EngineAttempt] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    stubs: list[dict] = field(default_factory=list)  # classes left stubbed in the output (#96)
    failure: str | None = None
    flagged: list[dict] = field(default_factory=list)  # files that stay flagged by the compile check (#99)
    noted: list[dict] = field(default_factory=list)  # checked files with only type or other errors (#99)
    unchecked: list[dict] = field(default_factory=list)  # files that the compile check could not examine (#99)
    check: dict | None = None  # compile-check counts over the final output; None = no check ran

    @property
    def partial(self) -> bool:
        """ok but degraded: engine holes, stubs, flagged files, or decompiled sources-less due to network."""
        return self.outcome == "ok" and (
            self.missing_classes > 0
            or bool(self.stubs)
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
    """Merges source files (.java/.kt by default) from many trees into one package tree.

    Collisions are deterministic: the tree with the lowest sort_key wins, and of one sort_key a base class before
    its versions twin, then the lowest engine path,
    regardless of the order in which worker threads deliver results. On a
    case-insensitive output tree the index is keyed by the case-folded path,
    so two jars with `p/A` and `p/a` collide instead of overwriting (#93).
    The root prefix of each class file of a class folder (target/classes/) is cut before all else (#199).
    """

    def __init__(
        self, src_root: Path, *, case_insensitive: bool = False, roots: dict[str, dict[str, str]] | None = None
    ) -> None:
        self.root = src_root
        self._lock = threading.Lock()
        self._case_insensitive = case_insensitive
        self._index: dict[str, tuple[tuple[str, bool, str], str, str]] = {}  # key -> (rank, sha256, rel)
        # artifact rel -> {the path of a class file without .class: its root prefix}, only non-empty prefixes
        self._roots = roots or {}

    def _cut(self, sort_key: str, raw: str) -> str:
        """raw without the root prefix of the class file with its path; a path of no such class file stays."""
        prefix = self._roots.get(sort_key, {}).get(raw[: len(raw) - len(PurePosixPath(raw).suffix)], "")
        return raw[len(prefix) :]

    def _twin(self, sort_key: str, raw: str) -> bool:
        return _strip_container(self._cut(sort_key, raw)).startswith("META-INF/versions/")

    def _target(self, sort_key: str, raw: str) -> str:
        """The path below src/ of an engine file of the artifact sort_key."""
        return normalize_java_rel(self._cut(sort_key, raw))

    def out_path(self, sort_key: str, raw: str) -> str:
        """The report path of an engine file: its path below src/ (#174), but a versions twin keeps its versions
        folder when the file at that path is the base class of the same artifact."""
        rel = self._target(sort_key, raw)
        if self._twin(sort_key, raw):
            with self._lock:
                existing = self._index.get(casefs.fold(rel) if self._case_insensitive else rel)
            if existing is not None and existing[0][0] == sort_key and not existing[0][1]:
                return _strip_container(self._cut(sort_key, raw))
        return rel

    def add_tree(self, tree: Path, sort_key: str, suffixes: tuple[str, ...] = SOURCE_SUFFIXES) -> tuple[int, list[dict]]:
        java = 0
        collisions: list[dict] = []
        files = sorted(tree.rglob("*"))
        # A META-INF/versions twin has the path of its base class here: the base class wins (#107).
        files.sort(key=lambda p: self._twin(sort_key, p.relative_to(tree).as_posix()))
        for p in files:
            if not p.is_file() or p.suffix.lower() not in suffixes:
                continue
            java += 1
            raw = p.relative_to(tree).as_posix()
            # A held or retried base class can come in a later tree than its twin: of one sort key, the base wins.
            # Of two files of one sort key, the first engine path wins, in any order of the trees.
            rank = (sort_key, self._twin(sort_key, raw), raw)
            rel = self._target(sort_key, raw)
            key = casefs.fold(rel) if self._case_insensitive else rel
            content = p.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            with self._lock:
                existing = self._index.get(key)
                if existing is None:
                    self._index[key] = (rank, digest, rel)
                    target = self.root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                elif existing[1] == digest:
                    pass  # identical duplicate
                elif rank < existing[0]:
                    entry = {"path": rel, "kept": sort_key, "dropped": existing[0][0]}
                    if existing[2] != rel:
                        entry["dropped_path"] = existing[2]
                        (self.root / existing[2]).unlink(missing_ok=True)
                    if sort_key == existing[0][0] and raw != existing[0][2]:
                        entry |= {"kept_file": raw, "dropped_file": existing[0][2]}
                    collisions.append(entry)
                    self._index[key] = (rank, digest, rel)
                    target = self.root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                else:
                    entry = {"path": existing[2], "kept": existing[0][0], "dropped": sort_key}
                    if existing[2] != rel:
                        entry["dropped_path"] = rel
                    if sort_key == existing[0][0] and raw != existing[0][2]:
                        entry |= {"kept_file": existing[0][2], "dropped_file": raw}
                    collisions.append(entry)
        return java, collisions

    def add_resources(self, archive: Path, rel: str, *, include_sources: bool = False) -> tuple[int, int]:
        return 0, len(_resource_members(archive, include_sources))

    def add_blob(self, zip_path: Path, member: str, rel: str) -> tuple[int, int]:
        return 0, 1

    def nest(self, rel: str, parent: str) -> None:
        """The merged tree has no folder for each archive."""


class MirrorWriter:
    """Copies each artifact's source files under out_root/<its folder> (see nest)."""

    def __init__(self, out_root: Path, resources: bool = True) -> None:
        self.root = out_root
        self.resources = resources
        self._folders: dict[str, str] = {}  # nested rel -> its folder under root

    def nest(self, rel: str, parent: str) -> None:
        """A nested archive gets the folder of its container plus its entry name; a top-level one keeps its rel.

        A name keeps its "!": a folder name can end with "!", and an entry name can hold "!/".
        """
        self._folders[rel] = f"{self._folders.get(parent, parent)}/{rel.removeprefix(f'{parent}!/')}"

    def dest_for(self, rel: str) -> Path:
        return self.root / self._folders.get(rel, rel)

    def out_path(self, rel: str, raw: str) -> str:
        """The path below the artifact folder of an engine file: the engine path (#174)."""
        return raw

    def add_tree(self, tree: Path, rel: str, suffixes: tuple[str, ...] = SOURCE_SUFFIXES) -> tuple[int, list[dict]]:
        dest = self.dest_for(rel)
        java = 0
        for p in sorted(tree.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in suffixes:
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
        except ZIP_ERRORS:
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
        except (KeyError, *ZIP_ERRORS):
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
        "stubbed_classes": sum(len(r.stubs) for r in reports),
        "out_of_memory_attempts": out_of_memory_runs(a for r in reports for a in r.attempts),
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
    memory_mb: int | None = 0  # engine heap budget in MB; None = auto (80% of RAM, the CLI default); 0 = no cap (#98)
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
    specs: dict[str, engines.EngineSpec] = field(default_factory=lambda: dict(ENGINES))  # with the pins (#113)
    batch_ids: Iterator[int] = field(default_factory=count)  # numbers for the batch JVMs (#114)
    cpu_budget: int | None = None  # visible cores per engine JVM
    memory_plan: memory.MemoryPlan = memory.NO_CAP  # the heap shares of the engine JVMs (#98)
    cds_dir: Path | None = None  # CDS archive directory for Java 19+
    on_stderr: Callable[[str], None] | None = None  # live engine-stderr sink (-v)
    on_event: Callable[[str, str, str], None] | None = None  # live progress events (scan/engines/fetch/queued/decompile/progress)
    on_warn: Callable[[str], None] | None = None  # run warnings, also from the fetch threads
    case_insensitive: str | None = None  # roots that ignore case ("output directory", ...); None = case-sensitive run (#93)
    checker: Callable | None = None  # compile check (#99): check.run_check with bound args; None = off
    java_major: int = 0  # limits the class versions that the check accepts; 0 = no limit
    input_group: list[Path] = field(default_factory=list)  # top-level classpath entries (#99)
    tree_roots: dict[Path, list[Path]] = field(default_factory=dict)  # the _tree_roots of each class-folder input
    families: dict[str, tuple[Path, list[Path]]] = field(default_factory=dict)  # parent rel -> (path, nested archives)
    family_dirs: dict[str, list[Path]] = field(default_factory=dict)  # container rel -> its temp folders (#97)
    class_roots: dict[str, Path | None] = field(default_factory=dict)  # rel -> extracted WEB-INF/BOOT-INF classes
    entry_names: dict[Path, frozenset[str]] = field(default_factory=dict)  # classpath entry -> its class names (#108)
    family_lock: threading.Lock = field(default_factory=threading.Lock)
    # rel -> the top-level classes that its Maven sources lack; stage 2 decompiles them (#180)
    maven_gaps: dict[str, set[str]] = field(default_factory=dict)


def _tmp_dir(ctx: Ctx) -> Path:
    return Path(tempfile.mkdtemp(dir=ctx.tmp_root))


def _drop(folder: Path | None) -> None:
    """Delete a temp folder after its last use; the end of the run deletes what stays (#97)."""
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def _jvm_input(artifact: Artifact, tmp_root: Path) -> Artifact:
    """The input with a path that a JVM can open (#113).

    Java decodes the bytes of a path with the codec of the locale: UTF-8, or ASCII under LC_ALL=C.
    Thus a path that is not ASCII, for example a Latin-1 name, can reach an engine or javac as
    other characters. Such an input gets a link in the temp root, and Python reads the same file
    through it. The link keeps an ASCII file name: the Maven lookup reads it, and a Maven file name
    is ASCII. Where the link fails, the path stays.
    """
    if artifact.kind not in (ArtifactKind.ARCHIVE, ArtifactKind.CLASS_TREE):
        return artifact  # only Python reads the other kinds
    path = artifact.path.absolute()
    if str(path).isascii():
        return artifact
    name = path.name if path.name.isascii() else "input" + (path.suffix if path.suffix.isascii() else "")
    link = Path(tempfile.mkdtemp(dir=tmp_root)) / name
    try:
        os.symlink(path, link, target_is_directory=artifact.kind is ArtifactKind.CLASS_TREE)
    except OSError:
        return artifact
    return replace(artifact, path=link)


def _class_stem(entry: str) -> str:
    return normalize_java_rel(entry)[: -len(".class")]


def _stems_and_claims(source: Path, read: bool = True) -> tuple[set[str], set[str]]:
    """One pass over the class entries: (top-level class stems, claims).

    Claims are the other source rels this archive could yield: module-info/package-info,
    and the inner classes, which an engine can also write to files of their own (#106).
    They attribute batch output back to its member and keep claimants
    from sharing a batch, but never join expectations (#75): such a file is
    copied when an engine emits it, exactly like a solo run — never retried.

    A class with an outer class by name that is top-level for the JVM (_class_parents) is expected: no file of
    another class holds it. For example the module class Foo$ of a Scala object (#206), or a stale class whose outer
    class was compiled again without it (#207). With read=False, the rule is by name only: no class file is read,
    and the union of the sets is the same.
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
        else:
            stems.add(_class_stem(e))
    inner = {s for s in stems if outer_stem(s, stems) is not None}
    parents = _class_parents(source, names=entries) if read and inner else {}  # the name list of this pass
    tops = {s for s, parent in parents.items() if parent is None} & inner
    return (stems - inner) | tops, claims | (inner - tops)


def expected_class_stems(source: Path) -> set[str]:
    """Top-level class names (no inner class, no module/package-info) a decompile should yield."""
    return _stems_and_claims(source)[0]


def produced_stems(dest: Path) -> set[str]:
    return {
        normalize_java_rel(p.relative_to(dest).as_posix())[: -len(p.suffix)]
        for p in dest.rglob("*")
        if p.is_file() and p.suffix in SOURCE_SUFFIXES
    }


_PACKAGE_LINE = re.compile(r"^[ \t]*package\s+([\w.$]+)", re.MULTILINE)
_PACKAGE_HEAD = 16384  # bytes: room for a license header before the package line


def _declared_package(path: Path) -> str | None:
    """The package of a source file from its package line ("okio", "com.x").

    None when the package is not sure: the file cannot be read, or its head does not have exactly one package
    line. No line: the default package, or a line that the pattern does not see. More than one: chained Scala
    clauses, or a line of a comment or a text block.
    """
    try:
        with path.open("rb") as f:
            head = f.read(_PACKAGE_HEAD)
    except OSError:
        return None
    found = _PACKAGE_LINE.findall(head.decode("utf-8-sig", "replace"))  # -sig: a byte order mark is no text
    return found[0] if len(found) == 1 else None


def _sources_gap(binary: Path, expected: set[str], tree: Path) -> set[str]:
    """The top-level classes of the binary that the extracted Maven sources do not hold (#180)."""
    written = {normalize_java_rel(p.relative_to(tree).as_posix()) for p in tree.rglob("*") if p.is_file()}
    rest = {s for s in expected if not any(s + suffix in written for suffix in maven.MAVEN_SOURCE_SUFFIXES)}
    if rest:
        # A file in a source-set folder (jvmMain/..., src/main/java/...) counts by its package line (#180).
        for p in tree.rglob("*"):
            if not p.is_file() or p.suffix not in maven.MAVEN_SOURCE_SUFFIXES:
                continue
            pkg = _declared_package(p)
            if pkg is not None:  # None: not sure, so the file counts only at its path and its class can stay a gap
                written.add(pkg.replace(".", "/") + "/" + p.name)
        rest = {s for s in rest if not any(s + suffix in written for suffix in maven.MAVEN_SOURCE_SUFFIXES)}
    covered: set[str] = set()
    unnamed: set[str] = set()  # classes with an outer class by name whose class file names no source file (#206)
    try:
        with zipfile.ZipFile(binary) as zf:
            entry: dict[str, str] = {}  # stem -> the entry to read: the base entry, else a META-INF/versions/N entry
            names = [n for n in zf.namelist() if n.endswith(".class")]
            for name in names:
                stem = _class_stem(name)
                if stem in rest and (stem not in entry or not _strip_container(name).startswith("META-INF/versions/")):
                    entry[stem] = name
            for stem, name in entry.items():
                try:
                    with zf.open(name) as f:
                        data = _bounded(f)
                except ZIP_ERRORS:
                    continue  # a damaged entry: a gap
                source = check.source_file(data) if data is not None else None
                package = stem.rpartition("/")[0]
                # The SourceFile of a secondary type or a Kotlin file facade names another file of its package.
                # A set lookup only: the attribute of a crafted class can hold a path (../A.java).
                if source is not None and (package + "/" + source if package else source) in written:
                    covered.add(stem)
                elif source is None and data is not None and outer_stem(stem, expected) is not None:
                    unnamed.add(stem)
            gap = rest - covered
            # A class that is top-level for the JVM, such as a Kotlin X$$Lambda$N, often names no source file. Its
            # source is then in the file of its outer class by name, when a class of that family names it. Generated
            # code that no class names, such as undertow's HttpRequestParser$$generated, stays a gap (#206).
            outer = {s: outer_stem(s, expected) for s in unnamed}
            wanted = {o for o in outer.values() if o not in gap}
            refs: dict[str, dict[str, set[str]]] = {}  # family -> stem of a class of it -> the classes that it names
            for name in names:
                stem = _class_stem(name)
                family = outer_stem(stem, expected) or stem
                if family not in wanted:
                    continue
                try:
                    with zf.open(name) as f:
                        data = _bounded(f)
                except ZIP_ERRORS:
                    continue
                if data is not None:
                    refs.setdefault(family, {}).setdefault(stem, set()).update(check.package_refs(data))
    except ZIP_ERRORS:
        return rest
    named = {  # the class itself and the classes below it are no proof
        s for s, o in outer.items() if o in wanted and any(
            s.rpartition("/")[2] in found
            for m, found in refs.get(o, {}).items() if m != s and not m.startswith(s + "$")
        )
    }
    return gap - named


def _strip_container(rel: str) -> str:
    """rel without its WEB-INF/classes/ or BOOT-INF/classes/ part."""
    for marker in _CONTAINER_ROOTS:
        idx = rel.find(marker)
        if idx != -1:
            return rel[idx + len(marker) :]
    return rel


def _held_key(rel: str) -> str:
    """A held file's key: normalize_java_rel's stem, but META-INF/versions/N/ stays (#96).

    Each version of a multi-release class is its own file, so neither may evict the other.
    """
    rel = _strip_container(rel)
    return rel[: -len(PurePosixPath(rel).suffix)]


def _copied_keys(source: Path) -> set[str]:
    """The _held_keys of the classes that the source has at two places, e.g. the root and WEB-INF/classes."""
    if source.is_dir():
        names = [p.relative_to(source).as_posix() for p in source.rglob("*.class")]
    else:
        try:
            with zipfile.ZipFile(source) as zf:
                names = [n for n in zf.namelist() if n.endswith(".class")]
        except (zipfile.BadZipFile, OSError):
            return set()
    places: dict[str, set[str]] = {}
    for name in names:
        places.setdefault(_held_key(name), set()).add(name)
    return {key for key, found in places.items() if len(found) > 1}


_CLASS_FILE_MAX = 16 << 20  # bytes: decaf reads no larger entry as a class file


def _bounded(f) -> bytes | None:
    """The content of an open class file, or None when it is larger than _CLASS_FILE_MAX."""
    data = f.read(_CLASS_FILE_MAX + 1)
    return data if len(data) <= _CLASS_FILE_MAX else None


def _safe_class(data: bytes | None) -> bool:
    """Can decaf prove that the class name of a class file stays inside the output folder?"""
    name = check.safe_class_name(data) if data is not None else None
    return name is not None and stays_inside(name)


def _refused_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """Does no engine get this entry? Its name does not stay inside, or it is a class entry that decaf cannot read
    or whose class name decaf cannot prove to stay inside. A disk error raises."""
    if not stays_inside(info.filename):
        return True
    if info.is_dir() or not info.filename.endswith(".class"):
        return False
    try:
        with zf.open(info) as f:  # by its ZipInfo: an engine can read each entry of a name
            data = _bounded(f)
    except ZIP_ERRORS as exc:
        if isinstance(exc, OSError) and exc.errno is not None:
            raise  # the disk, not the entry
        return True
    return not _safe_class(data)


def _refused(source: Path) -> list[zipfile.ZipInfo] | list[str]:
    """The items of a source that no engine gets: the refused entries of an archive (_refused_entry), each entry
    of a name apart, or the class files of a folder (paths below it) whose class name fails _safe_class."""
    if not source.is_dir():
        with zipfile.ZipFile(source) as zf:
            return [info for info in zf.infolist() if _refused_entry(zf, info)]
    out: list[str] = []
    for p in sorted(source.rglob("*.class")):
        if not p.is_file():
            continue  # a folder named x.class
        try:
            with open(p, "rb") as f:
                data = _bounded(f)
        except OSError:
            data = None
        if not _safe_class(data):
            out.append(p.relative_to(source).as_posix())
    return out


def _copy_without_refused(archive: Path, folder: Path) -> Path:
    """A copy of the archive in folder, with its file name, without the _refused_entry entries.

    The entries keep their order, and each entry of a name that is not refused stays. An entry that cannot be read
    stays out, as in _root_jar. Each entry goes through a file, not through memory.
    """
    jar = folder / archive.name  # an engine reads the suffix, and one engine names its output after the file
    part = folder / "entry.part"
    with zipfile.ZipFile(archive) as zf, zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as out:
        for info in zf.infolist():
            if _refused_entry(zf, info):
                continue
            try:
                with zf.open(info) as src, open(part, "wb") as f:
                    shutil.copyfileobj(src, f)
            except ZIP_ERRORS as exc:
                if isinstance(exc, OSError) and exc.errno is not None:
                    raise  # the disk, not the entry
                continue
            entry = zipfile.ZipInfo(info.filename, info.date_time)
            entry.external_attr = info.external_attr
            entry.compress_type = zipfile.ZIP_STORED if info.is_dir() else zipfile.ZIP_DEFLATED
            entry.file_size = part.stat().st_size
            out.NameToInfo.pop(info.filename, None)  # a second entry of a name stays, with no "Duplicate name" warning
            with open(part, "rb") as f, out.open(entry, "w") as dst:
                shutil.copyfileobj(f, dst)
    part.unlink(missing_ok=True)  # no part when each entry is refused
    return jar


def _refused_warning(rel: str, count: int, unit: str, first: str) -> str:
    return (f"{rel}: no engine gets {count} of its {unit}, because a name or a class name can leave the output"
            f" folder, or decaf cannot read the class name; the first is {ascii(first)}")


def _class_versions(source: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Lowest class-file major version of each top-level class file, by _held_key (#99),
    and the language of each class from Kotlin, Groovy, or Scala (#108)."""
    out: dict[str, int] = {}
    non_java: dict[str, str] = {}

    def keep(key: str, data: bytes | None) -> None:
        if data is None:
            return
        major = check.class_major(data[:8])
        if major is not None:
            out[key] = min(out.get(key, major), major)
        language = check.language(data)
        if language is not None:
            non_java[key] = language

    if source.is_dir():
        paths = {p.relative_to(source).as_posix(): p for p in source.rglob("*.class")}
        keys = {_held_key(n) for n in paths}
        for name, p in paths.items():
            if outer_stem(_held_key(name), keys) is None:
                with open(p, "rb") as f:
                    keep(_held_key(name), _bounded(f))
        return out, non_java
    try:
        with zipfile.ZipFile(source) as zf:
            names = [n for n in zf.namelist() if n.endswith(".class")]
            keys = {_held_key(n) for n in names}
            for name in names:
                if outer_stem(_held_key(name), keys) is not None:
                    continue
                try:
                    with zf.open(name) as f:
                        keep(_held_key(name), _bounded(f))
                except ZIP_ERRORS:
                    continue  # a damaged entry: only its file gets no version gate (#109)
    except ZIP_ERRORS:
        pass
    return out, non_java


def _family_entries(source: Path) -> dict[str, list[str]]:
    """The class entries of a source by the _held_key of their top-level class: p/A has p/A and p/A$1."""
    if source.is_dir():
        names = [p.relative_to(source).as_posix() for p in source.rglob("*.class")]
    else:
        try:
            with zipfile.ZipFile(source) as zf:
                names = [n for n in zf.namelist() if n.endswith(".class")]
        except ZIP_ERRORS:
            return {}
    keys = {_held_key(n) for n in names}
    out: dict[str, list[str]] = {}
    for name in names:
        key = _held_key(name)
        out.setdefault(outer_stem(key, keys) or key, []).append(name)
    return out


def _class_parents(
    source: Path, families: Collection[str] | None = None, names: Iterable[str] | None = None,
) -> dict[str, str | None]:
    """The parent of each class with an outer class by name, from the class files of its family: the stem of the
    class whose file holds it, or None for a class that is top-level for the JVM (#206, #207, #208).

    The family of a class: the classes with the same top-level class by name. A candidate is a class of the family
    that is not the class and is not below it by name. Q knows X when the InnerClasses attribute of Q lists X, or
    when the simple name of X is in the package_refs of Q. decaf tests the lists first, and reads package_refs only
    when no class lists X, so for most jars it reads none.
    1. The class file cannot be read or does not parse: the longest $-prefix that is a class, as before. The same
       when the path of the class file does not end with its class name, and the class file lists itself.
    2. It does not list itself: top-level (#206), also when a class of the family lists it, and also when its path
       does not end with its class name. Fernflower and JD nest such a class in the class that lists it, and
       Vineflower nests a member class of this kind, but CFR and Procyon write it only from its own class file.
    3. It lists itself with an outer class P, a member class: P when P is a candidate and knows it.
    4. It lists itself with no outer class, an anonymous or local class: of the candidates that know it, the class
       of its EnclosingMethod attribute, else the longest $-prefix, else the first.
    Else it can be stale (its outer class was compiled again without it): top-level, when no other class of its
    package names it, for example a Kotlin class of another family that creates it. A class file that a test needs
    and that decaf cannot read or parse gives no proof: then the class keeps the longest $-prefix that is a class.
    A stem with several entries (a multi-release class, a copy below a container root): one entry is enough for a
    fact. families: the top-level classes by name to read; None reads each family with such a class. names: the
    class entries of the source, when the caller has them. No step makes a list for each class, so the work stays
    about linear in the size of the class files, also for a crafted family.
    """
    if names is None:
        if source.is_dir():
            names = [p.relative_to(source).as_posix() for p in source.rglob("*.class")]
        else:
            try:
                with zipfile.ZipFile(source) as zf:
                    names = [n for n in zf.namelist() if n.endswith(".class")]
            except ZIP_ERRORS:
                return {}
    entries: dict[str, list[str]] = {}
    for name in sorted(names):
        entries.setdefault(_class_stem(name), []).append(name)
    canon = {s: s for s in entries}  # one string for each stem: the facts keep no new string for each entry
    tops = {s: outer_stem(s, entries) for s in entries}
    wanted = {o for o in tops.values() if o is not None and (families is None or o in families)}
    member = {s: tops[s] or s for s in entries if (tops[s] or s) in wanted}  # stem -> its family
    selves: dict[str, tuple[str | None, str | None]] = {}  # stem -> (its outer class, its EnclosingMethod class)
    alone: set[str] = set()  # the stems with an entry that does not list itself
    listers: dict[str, set[str]] = {}  # stem -> the classes of its family that list it
    for name, data in _read_classes(source, [n for s in member for n in entries[s]]):
        found = check.class_links(data)
        if found is None:
            continue
        stem, (own, links, enclosing) = canon[_class_stem(name)], found
        # the names, not the pool indexes: a class writer can name the class again in a second Class entry (#209)
        outer = next((o for i, o in links if i == own), False)
        if outer is False:
            alone.add(stem)
        prefix = stem[: len(stem) - len(own)]  # the folder above the package root
        if not stem.endswith(own) or prefix[-1:] not in ("", "/"):  # the path does not end with the class name
            continue
        if outer is not False:  # outer: None for no outer class, "" for an outer class that the source lacks
            enclosed = canon.get(prefix + enclosing) if enclosing else None
            selves.setdefault(stem, (outer and canon.get(prefix + outer, ""), enclosed))
        for inner, _ in links:
            q = canon.get(prefix + inner)
            if q is not None and q != stem and member.get(q) == member[stem]:
                listers.setdefault(q, set()).add(stem)

    named_by: dict[tuple[str, str], set[str]] = {}  # (package, simple name) -> the classes that name it
    read: set[str] = set()
    broken: set[str] = set()  # a class file that names nothing does not parse: a class file names itself

    def read_refs(stems: set[str]) -> None:
        stems -= read
        read.update(stems)
        broken.update(stems)
        for name, data in _read_classes(source, [n for s in sorted(stems) for n in entries[s]]):
            stem = canon[_class_stem(name)]
            for simple in check.package_refs(data):
                broken.discard(stem)
                named_by.setdefault((stem.rpartition("/")[0], simple), set()).add(stem)

    def below(x: str, q: str) -> bool:
        return q == x or q.startswith(x + "$")

    def candidate(x: str, q: str) -> bool:
        return member.get(q) == member[x] and not below(x, q)

    def namers(x: str) -> set[str]:
        return named_by.get(x.rpartition("/")[::2], set())

    def other_broken(x: str, group: list[str]) -> bool:
        """Has the sorted group of broken stems (of the family or of the package of x) one that is not x and not
        below x?"""
        lo, hi = bisect.bisect_left(group, x + "$"), bisect.bisect_left(group, x + "%")  # "%" follows "$"
        return len(group) - (hi - lo) - (x in broken) > 0

    def by_name(x: str) -> str:
        cut = x.rfind("$")
        while x[:cut] not in entries:
            cut = x.rfind("$", 0, cut)
        return x[:cut]

    def choose(x: str, known: set[str], enclosing: str | None) -> str:
        if enclosing in known:
            return enclosing
        cut = next((i for i in range(len(x) - 1, 0, -1) if x[i] == "$" and x[:i] in known), None)
        return x[:cut] if cut is not None else min(known)

    out: dict[str, str | None] = {}
    tests: list[str] = []
    for x in member:
        if tops[x] is None:
            continue
        if x in alone:  # rule 2: one entry that does not list itself is enough (#206)
            out[x] = None
        elif x not in selves:  # rule 1
            out[x] = by_name(x)
        else:
            outer, enclosing = selves[x]
            known = {q for q in listers.get(x, ()) if not below(x, q) and outer in (None, q)}
            if known:  # rule 3: the outer class lists it; rule 4: classes of the family list it
                out[x] = outer or choose(x, known, enclosing)
            else:
                tests.append(x)
    rule4 = {member[x] for x in tests if selves[x][0] is None}  # these families: each class, for its package_refs
    read_refs({selves[x][0] for x in tests if selves[x][0] and candidate(x, selves[x][0])}
              | {s for s in member if member[s] in rule4})
    fails: dict[str, list[str]] = {}  # family -> its broken stems, sorted
    for s in sorted(broken):
        fails.setdefault(member.get(s, ""), []).append(s)
    stale: list[str] = []
    for x in tests:
        outer, enclosing = selves[x]
        if outer is None:  # rule 4: the classes of the family that name it
            known = {q for q in namers(x) if candidate(x, q)}
            if known:
                out[x] = choose(x, known, enclosing)
            elif other_broken(x, fails.get(member[x], [])):  # no proof that it is stale
                out[x] = by_name(x)
            else:
                stale.append(x)
        elif outer and candidate(x, outer):  # rule 3: the outer class names it
            out[x] = outer if outer in namers(x) else by_name(x) if outer in broken else None
            if out[x] is None:
                stale.append(x)
        else:  # its outer class is no candidate
            stale.append(x)
    if stale:  # a class of the package, also of another family, can name it (#207)
        packages = {x.rpartition("/")[0] for x in stale}
        read_refs({s for s in entries if s.rpartition("/")[0] in packages})
        cut: dict[str, list[str]] = {}  # package -> its broken stems, sorted
        for s in sorted(broken):
            cut.setdefault(s.rpartition("/")[0], []).append(s)
        for x in stale:
            named = any(not below(x, q) for q in namers(x))
            out[x] = by_name(x) if named or other_broken(x, cut.get(x.rpartition("/")[0], [])) else None
    # a cycle of parents comes from crafted class files, or from two anonymous classes of an old javac (no
    # EnclosingMethod attribute) that list each other: _keys_above ends it
    return out


def _read_classes(source: Path, names: Collection[str]) -> Iterator[tuple[str, bytes]]:
    """(name, bytes) of some class entries of a source, with one open of an archive.

    A damaged entry, or one above _CLASS_FILE_MAX, is skipped.
    """
    if source.is_dir():
        for name in names:
            try:
                with open(source / name, "rb") as f:
                    data = _bounded(f)
            except OSError:
                continue
            if data is not None:
                yield name, data
        return
    try:
        with zipfile.ZipFile(source) as zf:
            for name in names:
                try:
                    with zf.open(name) as f:
                        data = _bounded(f)
                except (KeyError, *ZIP_ERRORS):
                    continue
                if data is not None:
                    yield name, data
    except ZIP_ERRORS:
        return


def _package_refs(source: Path, tree: Path) -> Callable[[list[Path]], dict[Path, frozenset[str]]]:
    """For some files of tree: the classes of its package that the class files of each file's class name.

    The check asks only for the files with an absent name (check.Scope).
    """
    families: dict[str, list[str]] | None = None

    def refs(paths: list[Path]) -> dict[Path, frozenset[str]]:
        nonlocal families
        if families is None:
            families = _family_entries(source)
        wanted: dict[Path, list[str]] = {}
        for path in paths:
            key = _held_key(path.relative_to(tree).as_posix())
            wanted[path] = families.get(outer_stem(key, families) or key, [])
        by_name = {name: check.package_refs(data)
                   for name, data in _read_classes(source, {n for names in wanted.values() for n in names})}
        return {path: frozenset().union(*(by_name.get(n, frozenset()) for n in names)) for path, names in wanted.items()}

    return refs


def _class_root(rel: str, path: Path, ctx: Ctx) -> Path | None:
    """WEB-INF/classes or BOOT-INF/classes of an archive, extracted once (#99).

    The extraction holds the family lock: a second worker waits for the first one (#101).
    """
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
                    ctx.family_dirs.setdefault(rel, []).append(dest)  # it goes when the family is done (#97)
                    safe_extract_zip(path, dest, members=members)
                    root = dest / prefix.rstrip("/")
                    break
        except ZIP_ERRORS:
            root = None  # kept as None: the other archives of the family do not try again
        ctx.class_roots[rel] = root
        return root


def _container_group(rel: str, path: Path, ctx: Ctx) -> list[Path]:
    root = _class_root(rel, path, ctx)
    with ctx.family_lock:
        nested = list(ctx.families.get(rel, (path, []))[1])
    return ([root] if root is not None else []) + [path] + nested


def _tree_roots(source: Path) -> list[Path]:
    """The package roots of a class folder, e.g. build/classes/java/main, from the class names (#108)."""
    roots: set[Path] = set()
    for p in source.rglob("*.class"):
        try:
            with open(p, "rb") as f:
                data = _bounded(f)
        except OSError:
            continue
        name = check.class_name(data) if data is not None else None
        rel = p.relative_to(source).as_posix()[: -len(".class")]
        if name and (rel == name or rel.endswith("/" + name)):
            roots.add(source / rel[: len(rel) - len(name)])
    return sorted(roots) or [source]


def _family_classpath(artifact: Artifact, source: Path, ctx: Ctx, roots: list[Path] | None = None) -> list[Path]:
    """Classpath of an artifact's check: its own group, then its parent's or the input group (#99).

    A class folder goes in as its package roots (#108).
    """
    cp = list(roots or [source]) if source.is_dir() else _container_group(artifact.rel, source, ctx)
    if artifact.parent is not None:
        with ctx.family_lock:
            entry = ctx.families.get(artifact.parent)
        if entry is not None:
            cp += _container_group(artifact.parent, entry[0], ctx)
    else:
        cp += ctx.input_group
    out: list[Path] = []
    for p in cp:
        if p not in out:
            out.append(p)
    return out


_METADATA_SOURCES = ("package-info.java", "module-info.java")


@dataclass(frozen=True)
class _HeldFile:
    """One held engine file and its reasons: a stub (#96), a flagged check result (#99), or both."""

    engine: str
    path: str  # posix path in the engine output, and under _Held.root
    stub: stubs.Stub | None = None
    result: check.FileResult | None = None  # None: the file had no check
    peers: frozenset[str] = frozenset()  # the keys of its family in its output, also of empty files: it nests none (#159)


@dataclass
class _Held:
    """Engine files kept out of the output until a clean retry replaces them (#96, #99)."""

    root: Path | None = None  # a _tmp_dir, made at the first held file
    files: dict[str, _HeldFile] = field(default_factory=dict)  # key -> held file
    written: set[str] = field(default_factory=set)  # keys of the files that went to the writer
    checks: dict[str, _HeldFile] = field(default_factory=dict)  # key -> the written file and its check result
    firsts: dict[str, str] = field(default_factory=dict)  # key -> the first error of its newest flagged file
    settled: set[str] = field(default_factory=set)  # keys that get no more retries (#108)
    versions: dict[str, int] | None = None  # key -> class major version, read once
    non_java: dict[str, str] = field(default_factory=dict)  # key -> "kotlin", "groovy", or "scala", read with versions
    copies: set[str] | None = None  # the _copied_keys of the source, read once
    roots: list[Path] | None = None  # the _tree_roots of a class folder, read once
    waiting: set[str] = field(default_factory=set)  # outer classes whose held family waits for a retry (#101, #150)
    nests: dict[str, set[str]] = field(default_factory=dict)  # outer class -> its spares: a chosen file nests them (#159)
    tops: dict[str, str] = field(default_factory=dict)  # key -> the key of its outer class, a cache for _apply_hold
    names: dict[str, str] | None = None  # key -> its class entry in the source, read once (#159)
    parents: dict[str, dict[str, str | None]] = field(default_factory=dict)  # family -> _read_parents of its keys

    def stems(self, settled: bool = True) -> set[str]:
        """The class stems of the held files: a copy's key loses its container root (#107).

        settled=False leaves out the files that get no more retries (#108).
        """
        return {_strip_container(k) for k in self.files if settled or k not in self.settled}


def _check_trees(
    items: list[tuple[Path, Artifact, Path, _Held]], ctx: Ctx, heap_mb: int | None, engine: str
) -> list[dict[str, check.FileResult]] | None:
    """One check call for (tree, artifact, source, held) items; results by path in each tree.

    The checker JVM runs in the slot of the engine JVM, after it, so it gets the same heap (#98).
    """
    if ctx.checker is None or engines.PROCESSES.closed:
        return None
    limit = ctx.java_major + check.CLASS_VERSION_OFFSET if ctx.java_major else None
    results: list[dict[str, check.FileResult]] = []
    sent: list[tuple[dict[str, check.FileResult], list[str]]] = []  # for each job: its item's results, its files
    jobs: list[check.Job] = []
    for tree, artifact, source, held in items:
        if held.versions is None:
            held.versions, held.non_java = _class_versions(source)
        if held.roots is None and source.is_dir():
            roots = ctx.tree_roots.get(artifact.path)  # read in run(): the copy of the folder has the same folders
            held.roots = [source / r.relative_to(artifact.path) for r in roots] if roots else _tree_roots(source)
        by_path: dict[str, check.FileResult] = {}
        by_release: dict[int | None, list[str]] = {}
        non_java: set[str] = set()
        detached: set[str] = set()  # an inner class that the engine wrote apart from its outer class (#101)
        for p in sorted(tree.rglob("*")):
            suffix = p.suffix.lower()
            if not p.is_file() or suffix not in SOURCE_SUFFIXES or p.name.lower() in _METADATA_SOURCES:
                continue
            rel = p.relative_to(tree).as_posix()
            if suffix == ".kt":
                by_path[rel] = check.unchecked("kotlin")
                continue
            key = _held_key(rel)
            top = outer_stem(key, held.versions) or key
            major = held.versions.get(top)
            if limit is not None and major is not None and major > limit:
                by_path[rel] = check.unchecked(
                    "newer class version", needs_java=major - check.CLASS_VERSION_OFFSET
                )
                continue
            # A file with an error that the JDK version can cause gets a second try with the JDK API of the
            # class's own Java version: old code may use removed APIs or names that newer Java restricts (#108).
            release = None if major is None else max(8, major - check.CLASS_VERSION_OFFSET)
            by_release.setdefault(release, []).append(rel)
            if top in held.non_java:
                non_java.add(rel)
            if top != key:
                detached.add(rel)
        results.append(by_path)
        classpath = _family_classpath(artifact, source, ctx, held.roots)
        own = [e for e in classpath if _own_entry(e, artifact, source, held, ctx)]
        twice = _twice(own, [e for e in classpath if e not in own], ctx)
        refs = _package_refs(source, tree)
        for release, paths in by_release.items():
            sent.append((by_path, paths))
            jobs.append(check.Job(
                str(len(jobs)), tuple(classpath), tuple(tree / r for r in paths), release, twice,
                frozenset(tree / r for r in paths if r in non_java), refs, frozenset(tree / r for r in paths if r in detached),
            ))
        if by_release and ctx.on_event is not None:  # the display shows the check, not the engine run (#101)
            ctx.on_event("decompile", artifact.rel, f"{engine} · check {sum(map(len, by_release.values())):,} files")
    if jobs:
        answers = ctx.checker(
            jobs, timeout=ctx.settings.timeout, cpu_budget=ctx.cpu_budget,
            **({"heap_mb": heap_mb} if heap_mb is not None else {}),
        )
        for (by_path, paths), answer in zip(sent, answers):
            by_path.update(zip(paths, answer))
    return results


def _own_entry(entry: Path, artifact: Artifact, source: Path, held: _Held, ctx: Ctx) -> bool:
    """Is a classpath entry the artifact itself: its archive, its class root, or a folder of its class tree?"""
    if entry == source or entry in (held.roots or []):
        return True
    if artifact.kind is ArtifactKind.CLASS_TREE:
        return entry.is_dir() and (entry == artifact.path or artifact.path in entry.parents)
    with ctx.family_lock:
        return entry == ctx.class_roots.get(artifact.rel)


def _entry_names(entry: Path, ctx: Ctx) -> frozenset[str]:
    """The class names (p/A, p/A$B) of a classpath entry, read once (#108)."""
    with ctx.family_lock:
        hit = ctx.entry_names.get(entry)
    if hit is not None:
        return hit
    if entry.is_dir():
        names = frozenset(p.relative_to(entry).as_posix()[: -len(".class")] for p in entry.rglob("*.class"))
    else:
        try:
            with zipfile.ZipFile(entry) as zf:
                names = frozenset(_class_stem(n) for n in zf.namelist() if n.endswith(".class"))
        except ZIP_ERRORS:
            names = frozenset()
    with ctx.family_lock:
        return ctx.entry_names.setdefault(entry, names)


def _twice(own: list[Path], others: list[Path], ctx: Ctx) -> Callable[[str], bool]:
    """Is a dotted class name (p.A, p.A.B) in two or more entries, such as two versions of one library?

    The entries of the artifact itself count as one (#108).
    """

    def twice(dotted: str) -> bool:
        parts = dotted.split(".")
        tops = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
        has = lambda e: any(t in _entry_names(e, ctx) for t in tops)  # noqa: E731
        return any(has(e) for e in own) + sum(1 for e in others if has(e)) >= 2

    return twice


def _parent_key(key: str, parents: Mapping[str, str | None]) -> str | None:
    """The key above a key in the tree of a family plan: its parent (_read_parents), else its name without the last
    $ part. A class that is top-level for the JVM stays below its outer class by name, so the plan reaches it (#208).
    None: the key has no $ part."""
    parent = parents.get(key)
    if parent is not None:
        return parent
    cut = key.rfind("$")
    return key[:cut] if cut > 0 else None


def _keys_above(key: str, parents: Mapping[str, str | None], memo: dict[str, bool]) -> Iterator[str]:
    """The keys above a key in the tree of a family plan, nearest first: _parent_key again and again. The parents can
    give a cycle, also through a key below a second root: crafted class files, or two anonymous classes of an old
    javac (no EnclosingMethod attribute) that list each other. A key whose walk meets a repeat takes only the
    $-prefixes of its name, from long to short. Each step of such a chain is shorter, and no clean chain reaches such
    a key, so no walk loops. memo: key -> its walk meets a repeat, for the walks of one plan."""
    if key not in memo:
        path: list[str] = []
        seen: set[str] = set()
        k: str | None = key
        while k is not None and k not in memo and k not in seen:
            seen.add(k)
            path.append(k)
            k = _parent_key(k, parents)
        memo.update(dict.fromkeys(path, k is not None and (k in seen or memo[k])))
    if memo[key]:
        yield from (key[:i] for i in range(len(key) - 1, 0, -1) if key[i] == "$")
        return
    k = _parent_key(key, parents)
    while k is not None:
        yield k
        k = _parent_key(k, parents)


def _units(
    keys: Collection[str], parents: Mapping[str, str | None], memo: dict[str, bool] | None = None,
) -> dict[str, list[str]]:
    """Each key -> the keys of inner classes one level down: no other key of keys is between them (#159, #208)."""
    memo = {} if memo is None else memo
    out: dict[str, list[str]] = {}
    for k in keys:
        up = next((a for a in _keys_above(k, parents, memo) if a in keys), None)
        if up is not None:
            out.setdefault(up, []).append(k)
    return out


def _read_parents(source: Path, families: set[str], held: _Held, top: Callable[[str], str]) -> None:
    """Note the parent of each class of the families in the source (_class_parents), with all classes, also the
    classes without a file: the key of the class whose file holds it, or None for a class that is top-level for the
    JVM. For example the module class Foo$ of a Scala object, or a stale class (#159, #206, #207, #213). No file of
    another class holds a top-level class. A key of a copy keeps its container root, and so does the key of its
    parent (#107). top: the family of a key."""
    if held.names is None:
        held.names = {}
        for names in _family_entries(source).values():
            for name in names:
                key = _held_key(name)
                held.names.setdefault(name[: -len(".class")] if key in (held.copies or ()) else key, name)
    parents = _class_parents(source, {_class_stem(family + ".class") for family in families})
    for family in families:
        held.parents[family] = {}
    for key, name in held.names.items():
        if top(key) in families and (stem := _class_stem(name)) in parents:
            parent = parents[stem]
            held.parents[top(key)][key] = None if parent is None else key[: len(key) - len(stem)] + parent


def _is_flagged(f: _HeldFile) -> bool:
    return f.result is not None and f.result.status == "flagged"


def _is_unchecked(f: _HeldFile) -> bool:
    return f.result is not None and f.result.status == "unchecked"


def _replaces(came: list[_HeldFile], gone: list[_HeldFile], fresh: set[int], loses: bool, gains: bool) -> bool:
    """Do the files of came replace the files of gone, as for one file? A stubbed or flagged file replaces
    nothing, and a stub is a known loss. A class that no file holds is a known loss too: loses means that the
    change leaves a class without a file, and gains means that it gives a file to a class without one. Else a
    flagged or unchecked file goes only for retry files (their ids are in fresh) that pass the check. A held file
    in came is the file of an earlier engine, so it needs no check result (#159)."""
    if loses or any(f.stub is not None or _is_flagged(f) for f in came):
        return False
    if gains or any(f.stub is not None for f in gone):
        return True
    return any(_is_flagged(f) or _is_unchecked(f) for f in gone) and all(
        f.result is not None and f.result.passes for f in came if id(f) in fresh
    )


@dataclass
class _FamilyPlan:
    """What the files of one output do to a family that waits (#159)."""

    new: dict[str, _HeldFile]  # the files of the output, by key
    chosen: dict[str, _HeldFile]  # key -> the file, held or new, that holds its class
    drop: set[str]  # the held keys whose files a better file replaced
    spares: set[str]  # the other held keys that a chosen file nests: they wait until the family is done
    done: bool  # the files of the family can go out, and its outer class gets no more retries


def _plan_family(
    family: str, olds: dict[str, _HeldFile], new: dict[str, _HeldFile], written: set[str],
    parents: Mapping[str, str | None], replace: bool = True,
) -> _FamilyPlan:
    """Choose the files of a family that waits, from its held files (olds) and the files of this output (#159).

    A file holds its class and each inner class below it that its engine did not write apart; an empty file counts
    as written apart. parents: the parent of each class (_read_parents), so a file of a class holds the classes that
    its engine nests in it (#208). No file of another class holds a class that is top-level for the JVM. From
    the outer class down, the file of the earlier engine holds a class, unless the file of this output and the
    files that it needs replace the files that go, as for one file (_replaces): a class that no file holds is a
    loss, as a stub is. With replace=False, nothing replaces a held file. A class without a held file takes the
    file of this output, also a stubbed or flagged one: it fills the gap.
    """
    fresh = {id(f) for f in new.values()}
    memo: dict[str, dict[str, _HeldFile]] = {}
    lost: dict[str, frozenset[str]] = {}  # key -> the classes at or below key that no file of memo[key] holds
    units: dict[int, dict[str, list[str]]] = {}  # id of a peers set -> its units, made once
    unnested = {k for k, parent in parents.items() if parent is None}
    walks: dict[str, bool] = {}  # the memo of _keys_above

    def holding(key: str, f: _HeldFile) -> tuple[dict[str, _HeldFile], frozenset[str]]:
        """f holds key and the inner classes that it nests; the best files hold the inner classes of its peers.
        Also the classes that no file holds."""
        if id(f.peers) not in units:
            units[id(f.peers)] = _units((f.peers | unnested) - written, parents, walks)
        out, gaps = {key: f}, frozenset()
        for k in units[id(f.peers)].get(key, ()):
            out |= best(k)
            gaps |= lost[k]
        return out, gaps

    def best(key: str) -> dict[str, _HeldFile]:
        """The files that hold key and its inner classes, when no chosen file of an outer class nests key."""
        if key not in memo:
            old, now = olds.get(key), new.get(key)
            if old is None and now is None:  # no file of this class: its inner classes take their own files
                out: dict[str, _HeldFile] = {}
                gaps = frozenset({key})
                for k in _units((olds.keys() | new.keys() | unnested | {key}) - written, parents, walks).get(key, ()):
                    out |= best(k)
                    gaps |= lost[k]
            elif old is None or now is None:
                out, gaps = holding(key, old or now)
            else:
                (keep, kept_gaps), (take, take_gaps) = holding(key, old), holding(key, now)
                came = [f for k, f in take.items() if keep.get(k) is not f]
                gone = [f for k, f in keep.items() if take.get(k) is not f]
                if replace and _replaces(came, gone, fresh, bool(take_gaps - kept_gaps), bool(kept_gaps - take_gaps)):
                    out, gaps = take, take_gaps
                else:
                    out, gaps = keep, kept_gaps
            memo[key], lost[key] = out, gaps
        return memo[key]

    chosen = best(family)

    def holder(k: str) -> str | None:
        """The chosen key whose file holds the class k: the nearest chosen key at k or above it."""
        return next((a for a in (k, *_keys_above(k, parents, walks)) if a in chosen), None)

    drop: set[str] = set()
    spares: set[str] = set()
    for k, f in olds.items():
        if chosen.get(k) is f:
            continue
        h = holder(k)
        if h is not None and id(chosen[h]) in fresh and h in olds:
            drop.add(k)  # a file of this output replaced the held file of its class or of an outer class
        else:
            spares.add(k)
    done = family in chosen and not any(f.stub is not None or _is_flagged(f) for f in chosen.values()) and not any(
        _is_flagged(olds[k]) and olds[k].stub is None and (h := holder(k)) is not None and _is_unchecked(chosen[h])
        for k in spares
    )  # an unchecked file does not replace a flagged one (#99): then the family waits for a file that passes
    return _FamilyPlan(new, chosen, drop, spares, done)


def _apply_hold(
    tree: Path,
    source: Path,
    found: dict[str, stubs.Stub],
    results: dict[str, check.FileResult] | None,
    engine: str,
    held: _Held,
    ctx: Ctx,
    empty: Collection[str] = (),
) -> int:
    """Hold back the stubbed (#96) and flagged (#99) files of one output; returns the flagged count.

    empty: the rels of the empty files that _hold_output deleted; their classes count as written apart (#159).

    Files match by _held_key; each copy of a class that the source has at two places keeps
    its container root in its key, so no copy replaces or deletes the other. A retry file
    replaces a held file only when it has no stub and is not flagged; a held file without a
    stub (flagged only) also needs a retry file that passes the check, and after Ctrl-C no
    retry file replaces a held file. The files of one output never replace or delete each
    other, and a retry drops its copy of a class that an earlier output wrote, for example an
    inner class or a variant of a held class (#107).
    A held file gets no more retries when the next engine's file has the same first error, and
    bytecode can cause this error legally (#108). A file of a Groovy class gets no more retries
    when two engines in a row flag it: no engine writes compilable Java for the call-site and
    meta-class code of groovyc, and each engine gives a different first error for it (#101).
    A flagged or stubbed (#160) file of an inner class that the engine wrote apart from its outer
    class holds its family: the files of the outer class and of its inner classes in the output. A
    file of an inner class also holds its family when the output has no file of its outer class,
    for example when the outer class failed as a whole (#159). The family must be new: no
    earlier output wrote or held a file of it (#101). A held file of an outer class holds its
    family too, from any output: the next engine can nest the inner classes in its file of the
    outer class, so no file of an inner class goes out alone (#150). Then the outer class gets a
    retry. In a family that waits, each class has one file: _plan_family chooses it, as for one
    file, from the held files and the files of the output (#159). A class that is top-level for the
    JVM (_read_parents) has no file of another class that holds it. The held files that a chosen
    file nests wait as spares, unless a file of the output replaced the file of their class or of
    an outer class. The family is done when the outer class has a file, no chosen file has a stub
    or a flag, and no unchecked chosen file nests a flagged spare without a stub. Then the chosen
    files go out, the spares go, and the outer class gets no more retries. Only a held file of an
    inner class whose family does not wait still follows the rule of one file.
    """
    if held.copies is None:
        held.copies = _copied_keys(source)
    if held.versions is None:  # no check ran: the families still need the top-level classes (#150)
        held.versions, held.non_java = _class_versions(source)
    versions = held.versions

    def top(key: str) -> str:
        """The key of the outer class of a key, or the key itself; a copy keeps its container root (#107)."""
        if key not in held.tops:
            stem = _strip_container(key)
            outer = outer_stem(stem, versions)
            held.tops[key] = key[: len(key) - len(stem)] + outer if outer else key
        return held.tops[key]

    def key_of(rel: str) -> str:
        key = _held_key(rel)
        return rel[: -len(PurePosixPath(rel).suffix)] if key in held.copies else key  # one key for each copy (#107)

    files: list[tuple[Path, str, str, stubs.Stub | None, check.FileResult | None]] = []
    for p in sorted(tree.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        rel = p.relative_to(tree).as_posix()
        files.append((p, rel, key_of(rel), found.get(rel), results.get(rel) if results is not None else None))
    keys = {key for _, _, key, _, _ in files}
    waiting = {  # a stubbed or flagged file of an inner class, or one without the file of its outer class (#159, #160)
        top(key) for _, _, key, stub, r in files
        if top(key) != key and (stub is not None or (r is not None and r.status == "flagged") or top(key) not in keys)
    }
    if waiting:  # holds only a new family
        waiting -= {top(k) for k in held.files.keys() | held.written}
    # a clean file with the key of a stubbed or flagged file of the same output is written, so no family waits then
    clean_now = {key for _, _, key, stub, r in files if stub is None and (r is None or r.status != "flagged")}
    waiting |= {  # a stubbed or flagged file of an outer class holds its family, from any output (#150)
        key for _, _, key, stub, r in files
        if top(key) == key and key not in held.written and key not in clean_now
        and (stub is not None or (r is not None and r.status == "flagged"))
    }
    by_family: dict[str, list[tuple[str, str, stubs.Stub | None, check.FileResult | None]]] = {}
    members: dict[str, set[str]] = {}
    for _, rel, key, stub, r in files:
        by_family.setdefault(top(key), []).append((rel, key, stub, r))
        members.setdefault(top(key), set()).add(key)
    for rel in empty:  # the engine wrote the class apart, and the class failed as a whole (#96)
        members.setdefault(top(key_of(rel)), set()).add(key_of(rel))
    peers = {family: frozenset(ks) for family, ks in members.items()}
    planned = {family for family in by_family if family in held.waiting or family in waiting}
    olds: dict[str, dict[str, _HeldFile]] = {family: {} for family in planned}  # their held files, read once
    if planned:
        for k, f in held.files.items():
            if top(k) in olds:
                olds[top(k)][k] = f
        if planned - held.parents.keys():  # all classes of the family, also the classes without a file (#213)
            _read_parents(source, planned - held.parents.keys(), held, top)
    plans: dict[str, _FamilyPlan] = {}
    for family in planned:
        new: dict[str, _HeldFile] = {}
        for rel, key, stub, r in by_family[family]:  # a file of a class that an earlier output wrote goes (#107)
            if key not in held.written:
                new.setdefault(key, _HeldFile(engine, rel, stub, r, peers[family]))
        plans[family] = plan = _plan_family(  # after Ctrl-C the check did not run or end, so replace=False (#112)
            family, olds[family], new, held.written, held.parents[family], replace=not engines.PROCESSES.closed,
        )
        for k in plan.drop | (plan.spares if plan.done else set()):
            (held.root / held.files.pop(k).path).unlink(missing_ok=True)
        if plan.done:  # its held files that stay go out at the end
            held.waiting.discard(family)
            waiting.discard(family)
            held.nests.pop(family, None)
            held.settled.update(k for k, f in plan.chosen.items() if held.files.get(k) is f)
        else:
            held.nests[family] = plan.spares

    def settle(key: str, result: check.FileResult) -> None:
        """Two engines in a row with the same error that bytecode can cause legally (#108)."""
        first = _first_error(result)
        if (result.bytecode_only and held.firsts.get(key) == first and not first.startswith(_CAST_DROPPED)) or (
            key in held.firsts and held.non_java.get(top(_strip_container(key))) == "groovy"
        ):  # a Groovy class: two engines in a row flag it, and no engine writes compilable Java for it (#101)
            held.settled.add(key)
        held.firsts[key] = first

    def hold(p: Path, key: str, f: _HeldFile) -> None:
        if held.root is None:
            held.root = _tmp_dir(ctx)
        target = held.root / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(p, target)
        held.files[key] = f
        held_now.add(key)
        if _is_flagged(f):
            held.firsts[key] = _first_error(f.result)

    def write(key: str, rel: str, result: check.FileResult | None) -> None:
        held.written.add(key)
        written_now.add(key)
        if result is not None:
            held.checks[key] = _HeldFile(engine, rel, None, result)

    flagged = 0
    held_now: set[str] = set()
    written_now: set[str] = set()
    for p, rel, key, stub, result in files:
        is_flagged = result is not None and result.status == "flagged"
        flagged += is_flagged
        if key in held_now:
            continue  # another file of this output with the same key: keep both
        if top(key) in plans and key not in held.written:  # a file of a family that waits (#159)
            plan = plans[top(key)]
            f = plan.new.get(key)
            if f is None or f.path != rel or plan.chosen.get(key) is not f:
                if is_flagged and key in held.files:
                    settle(key, result)
                p.unlink()  # the held file of its class stays, or a chosen file nests its class
            elif plan.done:
                write(key, rel, result)
            else:
                hold(p, key, f)
        elif key in held.files:
            if stub is None and not is_flagged and not engines.PROCESSES.closed and top(key) not in held.waiting and (
                held.files[key].stub is not None or result is None or result.passes
            ):  # an unchecked file can replace a stub (a known loss), not a flagged file; after
                # Ctrl-C a retry file can be cut off, and the check did not run (#112)
                old = held.files.pop(key)
                (held.root / old.path).unlink(missing_ok=True)
                held.written.add(key)
                if result is not None:
                    held.checks[key] = _HeldFile(engine, rel, None, result)
            else:
                if is_flagged:
                    settle(key, result)
                p.unlink()  # the earlier engine's held file stays
        elif key in held.written and key not in written_now:
            p.unlink()  # an earlier engine wrote this class, and its file stays (#107)
        elif stub is not None or is_flagged:
            hold(p, key, _HeldFile(engine, rel, stub, result, peers[top(key)]))
        else:
            write(key, rel, result)
    held.waiting |= waiting
    return flagged


# `throw (T) t` in a generic method: Vineflower and CFR drop the cast and give one of these errors, but JD
# keeps the cast. So two engines in a row with such an error do not show that the bytecode throws it.
_CAST_DROPPED = tuple(
    f"unreported exception {t}" for t in ("java.lang.Throwable;", "java.lang.Exception;", "capture#")
)


def _first_error(result: check.FileResult) -> str:
    """The first error of a flagged file without its line, which differs between engines (#108)."""
    return (result.first or "").partition(": ")[2]


def _hold_output(
    tree: Path, engine: str, artifact: Artifact, source: Path, ctx: Ctx, held: _Held, heap_mb: int | None
) -> tuple[list[stubs.Stub], int]:
    """The hold step of one engine output, before the writer copies it: (its stubs, flagged).

    Empty source files are no output (Fernflower writes one when a whole class fails),
    so they go (#96), but the engine wrote their classes apart (#159). Then the stub scan and
    the compile check decide what to hold.
    """
    empty = []
    for p in list(tree.rglob("*")):
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES and p.stat().st_size == 0:
            empty.append(p.relative_to(tree).as_posix())
            p.unlink()
    found = stubs.scan(tree, engine)
    results = _check_trees([(tree, artifact, source, held)], ctx, heap_mb, engine)
    flagged = _apply_hold(
        tree, source, {s.path: s for s in found}, results[0] if results is not None else None, engine, held, ctx,
        empty,
    )
    return found, flagged


def _finish_held(rel: str, ctx: Ctx, report: ArtifactReport, held: _Held) -> None:
    """End of the chain: write the held files (the first engine's file) and list them (#96, #99)."""
    path = partial(ctx.writer.out_path, rel)  # a list gives the path of the written file (#174), see out_path
    for keys in held.nests.values():  # the spares: a chosen file nests their classes (#159)
        for k in keys:
            f = held.files.pop(k, None)
            if f is not None:
                (held.root / f.path).unlink(missing_ok=True)
    if held.files:
        java, collisions = ctx.writer.add_tree(held.root, rel)
        report.java_files += java
        report.collisions += collisions
        ordered = sorted(held.files.values(), key=lambda f: f.path)
        report.stubs = [
            {"path": path(f.path), "engine": f.engine, "methods": f.stub.methods, "errors": list(f.stub.errors)}
            for f in ordered if f.stub is not None
        ]
        for key, f in held.files.items():
            if f.result is not None:
                held.checks[key] = f
    _drop(held.root)
    if ctx.checker is not None:
        done = sorted(held.checks.values(), key=lambda f: f.path)
        report.flagged = [_check_entry(f, path(f.path)) for f in done if f.result.status == "flagged"]
        report.noted = [_check_entry(f, path(f.path)) for f in done if f.result.status == "noted"]
        report.unchecked = [
            {"path": path(f.path), "engine": f.engine, "reason": f.result.reason,
             "needs_java": f.result.needs_java, "first": f.result.first}
            for f in done if f.result.status == "unchecked"
        ]
        report.check = check.summary([f.result for f in done])


def _check_entry(f: _HeldFile, path: str) -> dict:
    """A flagged or noted file in the report at its output path (#174), with its errors in line order (#99)."""
    r = f.result
    return {"path": path, "engine": f.engine, "kinds": list(r.kinds), "errors": r.errors,
            "first": r.first, "messages": list(r.messages)}


_OOM = "java.lang.OutOfMemoryError"


def _has_oom(found: list[stubs.Stub]) -> bool:
    return any(_OOM in s.errors for s in found)


def _attempt(
    name: str,
    level: str,
    res: engines.EngineResult,
    java_files: int,
    found: list[stubs.Stub],
    heap_mb: int | None,
    flagged: int = 0,
    batch: int | None = None,
) -> EngineAttempt:
    """The report entry of one engine run: its result, its stubs (#96), its memory (#98), its flags (#99)."""
    return EngineAttempt(
        name, level, res.returncode, res.timed_out, java_files, res.stderr_tail,
        stubbed=len(found),
        peak_rss_mb=res.peak_rss_mb,
        heap_mb=heap_mb,
        out_of_memory=res.out_of_memory or _has_oom(found),
        flagged=flagged,
        batch=batch,
    )


def _extract_failed_classes(source: Path, missing: set[str], ctx: Ctx) -> Path | None:
    """Copy the .class files (plus their inner classes) for missing stems into a temp tree.

    A missing stem brings the family of its top-level class by name: a missing Foo$ that is top-level for the
    JVM brings Foo and its inner classes too, so the engines have each class file that they can need (#206).
    Each class goes in once: its base class, or the class of its lowest META-INF/versions folder
    when it has no base class. With two versions of a class in one tree, an engine can write the
    body of one version as the other, or only one of them (#107).
    """
    if source.is_dir():
        names = [p.relative_to(source).as_posix() for p in sorted(source.rglob("*.class"))]
    else:
        with zipfile.ZipFile(source) as zf:
            names = [n for n in zf.namelist() if n.endswith(".class")]
    stems = {_class_stem(n) for n in names}

    def top(entry: str) -> str:
        stem = _class_stem(entry)
        return outer_stem(stem, stems) or stem

    def version(entry: str) -> int:
        """0 for a base class, else the number of its META-INF/versions folder."""
        rel = _strip_container(entry)
        if not rel.startswith("META-INF/versions/"):
            return 0
        number = rel.split("/", 3)[2]
        return int(number) if number.isdigit() else -1  # the JVM reads no other folder

    families = {outer_stem(s, stems) or s for s in missing}
    lowest: dict[str, int] = {}
    for n in names:
        t, v = top(n), version(n)
        if t in families and v >= 0:
            lowest[t] = min(lowest.get(t, v), v)

    def wanted(entry: str) -> bool:
        return lowest.get(top(entry)) == version(entry)

    out = _tmp_dir(ctx)
    if source.is_dir():
        for rel in names:
            if wanted(rel):
                target = out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / rel, target)
    else:
        safe_extract_zip(source, out, members=[n for n in names if wanted(n) and stays_inside(n)])
    # The file on the disk is the file that the engine gets: of two entries with one name, the last one.
    for rel in _refused(out):
        (out / rel).unlink()
    count = sum(1 for p in out.rglob("*.class") if p.is_file())
    return out if count else None


# CFR and Procyon write a class at the path of its name, and JD takes the path below its input as the name (#199,
# #200, #170, #171, #173). Thus they get one package root at a time, and their files go back below its prefix.
_ROOTED_ENGINES = ("cfr", "procyon", "jd")

_VERSIONS_TAIL = re.compile(r"(?:^|(?<=/))META-INF/versions/[^/]+/$")


def _container_prefix(rel: str) -> str:
    """rel up to the end of its WEB-INF/classes/ or BOOT-INF/classes/ part (as _strip_container finds it), else ""."""
    for marker in _CONTAINER_ROOTS:
        idx = rel.find(marker)
        if idx != -1:
            return rel[: idx + len(marker)]
    return ""


def _root_prefix(tree: Path, rel: str) -> str:
    """The part of a class file path above its package root: up to the end of WEB-INF/classes/ or BOOT-INF/classes/,
    else the path before the class name of the file (as _tree_roots reads it), else "".

    A META-INF/versions/N/ folder stays below the root (#202).
    """
    marked = _container_prefix(rel)
    if marked:
        return marked
    try:
        with open(tree / rel, "rb") as f:
            data = _bounded(f)
    except OSError:
        return ""
    name = check.class_name(data) if data is not None else None
    stem = rel[: -len(".class")]
    if not name or not (stem == name or stem.endswith("/" + name)):
        return ""
    prefix = stem[: len(stem) - len(name)]
    tail = _VERSIONS_TAIL.search(prefix)
    return prefix[: tail.start()] if tail else prefix


def _container_classes(archive: Path) -> list[str] | None:
    """The class entries of an archive when one of them is below WEB-INF/classes/ or BOOT-INF/classes/, else None."""
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if n.endswith(".class")]
    except ZIP_ERRORS:
        return None
    return names if any(m in n for n in names for m in _CONTAINER_ROOTS) else None


def _root_jar(archive: Path, prefix: str, jar: Path) -> None:
    """A jar of the class entries of the archive with this container prefix, at their path below it (#173).

    An engine gets the same entries as from a plain jar, so it writes the same text. Of two entries with one name,
    the last one goes in, and an entry that cannot be read stays out, as in safe_extract_zip. An entry whose path
    below the prefix leaves the jar root stays out too. Each entry goes through a file, not through memory.
    The META-INF/MANIFEST.MF of the archive is the first entry: CFR reads its Multi-Release line to keep the
    META-INF/versions/ copy of a class apart from the base class.
    """
    with zipfile.ZipFile(archive) as zf:
        chosen: dict[str, zipfile.ZipInfo] = {}
        try:
            chosen["META-INF/MANIFEST.MF"] = zf.getinfo("META-INF/MANIFEST.MF")
        except KeyError:
            pass
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or not name.endswith(".class") or _container_prefix(name) != prefix:
                continue
            below = name[len(prefix) :]
            if stays_inside(below):
                chosen[below] = info
        part = jar.with_name("entry.part")
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_STORED) as out:
            for below, info in chosen.items():
                try:
                    with zf.open(info) as src, open(part, "wb") as f:
                        shutil.copyfileobj(src, f)
                except ZIP_ERRORS as exc:
                    if isinstance(exc, OSError) and exc.errno is not None:
                        raise  # the disk, not the entry
                    continue
                out.write(part, below)


def _run_engine(ctx: Ctx, spec: engines.EngineSpec, jar: Path, target: Path, dest: Path, timeout: float,
                **kw) -> engines.EngineResult:
    """ctx.runner, but CFR, Procyon, and JD get one package root of the target at a time (#199).

    The other engines, and a target whose classes are all at their package root, go to ctx.runner unchanged. The
    target stays as it is. An archive with WEB-INF/classes/ or BOOT-INF/classes/ gives a temporary jar for each
    root: from a folder, CFR writes other text than from a jar. In a folder, a root is its own folder when each
    class file below it has its prefix, else a copy. The timeout is for the whole target, as in _run_per_class.
    A root whose folder below dest is not inside dest (an entry name that leaves the output folder) gets no run, so
    its classes are missing, as safe_extract_zip skips such an entry.
    """
    if spec.name not in _ROOTED_ENGINES:
        return ctx.runner(spec, jar, target, dest, timeout, **kw)
    resolved_dest = dest.resolve()
    temps: list[Path] = []
    try:
        if target.is_dir():
            prefixes = {rel: _root_prefix(target, rel)
                        for rel in (p.relative_to(target).as_posix() for p in target.rglob("*.class"))}
        else:
            names = _container_classes(target)
            if names is None:
                return ctx.runner(spec, jar, target, dest, timeout, **kw)
            prefixes = {name: _container_prefix(name) for name in names}
        if not any(prefixes.values()):
            return ctx.runner(spec, jar, target, dest, timeout, **kw)
        deadline = time.monotonic() + timeout
        results: list[engines.EngineResult] = []
        timed_out = False
        for prefix in sorted(set(prefixes.values())):
            try:
                inside = (dest / prefix).resolve().is_relative_to(resolved_dest)
            except OSError:
                inside = False
            if not inside:
                continue
            if results and engines.PROCESSES.closed:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            root = target / prefix
            if not target.is_dir():
                temps.append(_tmp_dir(ctx))
                root = temps[-1] / "classes.jar"  # a .jar name: Procyon reads the suffix (build_command)
                _root_jar(target, prefix, root)
            elif any(p != prefix for rel, p in prefixes.items() if rel.startswith(prefix)):
                root = _tmp_dir(ctx)
                temps.append(root)
                for rel, p in prefixes.items():
                    if p == prefix:
                        (root / rel[len(prefix) :]).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(target / rel, root / rel[len(prefix) :])
            out = _tmp_dir(ctx)
            temps.append(out)
            results.append(ctx.runner(spec, jar, root, out, left, **kw))
            for f in [p for p in out.rglob("*") if p.is_file()]:
                moved = dest / prefix / f.relative_to(out)
                moved.parent.mkdir(parents=True, exist_ok=True)
                f.replace(moved)
    finally:
        for folder in temps:
            _drop(folder)
    tails = [r.stderr_tail for r in results if r.stderr_tail]
    peaks = [r.peak_rss_mb for r in results if r.peak_rss_mb is not None]
    return engines.EngineResult(
        spec.name, next((r.returncode for r in results if r.returncode), 0),
        timed_out or any(r.timed_out for r in results),
        sum(1 for p in dest.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES),
        "\n".join(tails)[-2000:], max(peaks, default=None), any(r.out_of_memory for r in results),
    )


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
    held: _Held,
) -> None:
    """Retry missing and held classes down the chain; then write what stays held (#96, #99)."""
    heap = ctx.memory_plan.heap_mb(_decompile_weight(artifact))  # class retries run in their artifact's slot
    missing = expected - produced - held.stems()
    try:
        for name in chain:
            if engines.PROCESSES.closed:
                break
            retry = missing | (held.stems(settled=False) & expected)  # a versioned key is no expected stem (#96)
            if not retry:
                break
            retry_tree = _extract_failed_classes(source, retry, ctx)
            if retry_tree is None:
                break
            dest = _tmp_dir(ctx)
            try:
                if ctx.on_event is not None:
                    ctx.on_event("decompile", artifact.rel, f"{name} · retry {len(retry):,} classes")
                res = _run_engine(
                    ctx, ctx.specs[name], ctx.engine_jars[name], retry_tree, dest,
                    ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
                    **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
                    # the primary engine runs here only for a Maven gap (#180)
                    **({"engine_args": ctx.settings.engine_args} if ctx.settings.engine_args and name == ctx.chain[0] else {}),
                    **({"heap_mb": heap} if heap is not None else {}),
                    **_stream_kw_for(ctx, name, artifact.rel),
                )
                if engines.PROCESSES.closed:  # Ctrl-C: no file of the killed retry goes out (#112)
                    report.attempts.append(_attempt(name, "class", res, res.java_files, [], heap))
                    break
                found, flagged = _hold_output(dest, name, artifact, source, ctx, held, heap) if res.java_files > 0 else ([], 0)
                report.attempts.append(_attempt(name, "class", res, res.java_files, found, heap, flagged))
                if res.java_files > 0:
                    java, collisions = ctx.writer.add_tree(dest, artifact.rel)
                    report.java_files += java
                    report.collisions += collisions
                    produced |= produced_stems(dest)
                    missing = expected - produced - held.stems()
            finally:
                _drop(retry_tree)
                _drop(dest)
    finally:
        _finish_held(artifact.rel, ctx, report, held)  # also when a retry raises: the held files are not lost
    report.missing_classes = len(missing)


def _decompile(artifact: Artifact, target: Path, ctx: Ctx, report: ArtifactReport) -> None:
    gap = ctx.maven_gaps.pop(artifact.rel, None)
    if gap is not None:  # a Maven hit whose sources lack classes: the engines write them (#180)
        # The retry tree of a gap class has the family of its outer class by name (#206), so an engine can also
        # write a class that the sources jar wrote. Such a file goes, as a file of a class that an earlier output
        # wrote (#107): only the gap classes and the classes below them by name get engine files.
        stems, claims = _stems_and_claims(artifact.path, read=False)  # the union is the same without the reads
        published = {s for s in stems | claims
                     if s not in gap and not any(s[:i] in gap for i in range(len(s)) if s[i] == "$")}
        _retry_missing_classes(artifact, artifact.path, gap, set(), ctx, report, ctx.chain, _Held(written=published))
        return
    source = target if target.is_dir() else artifact.path
    # From the input, not from the target: a class that no engine gets (_refused) counts as missing. The copy of a
    # class folder has each class file of the folder (copy_class_tree), so a normal folder gives the same set.
    expected = expected_class_stems(artifact.path)
    report.classes = len(expected)
    produced: set[str] = set()
    held = _Held()
    heap = ctx.memory_plan.heap_mb(_decompile_weight(artifact))

    used_index: int | None = None
    for i, name in enumerate(ctx.chain):
        if engines.PROCESSES.closed:
            break
        dest = _tmp_dir(ctx)
        try:
            if ctx.on_event is not None:
                ctx.on_event("decompile", artifact.rel, f"{name} · {len(expected):,} classes")
            res = _run_engine(
                ctx, ctx.specs[name], ctx.engine_jars[name], target, dest,
                ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
                **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
                **({"engine_args": ctx.settings.engine_args} if ctx.settings.engine_args else {}),
                **({"heap_mb": heap} if heap is not None else {}),
                **_stream_kw_for(ctx, name, artifact.rel),
            )
            if engines.PROCESSES.closed:  # Ctrl-C: the killed engine can leave a cut-off file (#112)
                report.attempts.append(_attempt(name, "archive", res, res.java_files, [], heap))
                break
            found, flagged = _hold_output(dest, name, artifact, source, ctx, held, heap) if res.java_files > 0 else ([], 0)
            report.attempts.append(_attempt(name, "archive", res, res.java_files, found, heap, flagged))
            if res.java_files > 0:
                java, collisions = ctx.writer.add_tree(dest, artifact.rel)
                report.java_files += java
                report.collisions += collisions
                report.method = name
                produced |= produced_stems(dest)
                used_index = i
                break
        finally:
            _drop(dest)

    if used_index is None:
        report.outcome = "failed"
        report.failure = "interrupted" if engines.PROCESSES.closed else "all engines failed"
        return

    _retry_missing_classes(
        artifact, source, expected, produced, ctx, report, ctx.chain[used_index + 1 :], held
    )
    report.outcome = "ok"


def _discover_nested(artifact: Artifact, ctx: Ctx) -> list[Artifact]:
    try:
        with zipfile.ZipFile(artifact.path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError):
        return []
    found = find_nested_archives(i.filename for i in infos)
    names = list(dict.fromkeys(found))  # two entries of one name: one row, and the last one wins, as in Java
    if not names:
        return []
    if len(names) < len(found) and ctx.on_warn is not None:
        for name in names:
            copies = [i for i in infos if i.filename == name]
            if len({(i.CRC, i.file_size) for i in copies}) > 1:  # an earlier copy gets no output
                ctx.on_warn(f"{artifact.rel}!/{name}: {len(copies)} entries with this name are not the same;"
                            " decaf uses the last one, as Java does")

    def _artifact(path: Path, name: str, kind: ArtifactKind, classes: int = 0, refusal: str | None = None) -> Artifact:
        rel = f"{artifact.rel}!/{name}"
        return Artifact(path, rel, kind, classes, refusal, parent=artifact.rel, depth=artifact.depth + 1)

    if artifact.depth >= ctx.settings.max_depth:
        return [_artifact(artifact.path, name, ArtifactKind.BEYOND_DEPTH) for name in names]
    nested: list[Artifact] = []
    if ctx.case_insensitive:
        groups = casefs.case_groups(names)
        if groups:  # e.g. WEB-INF/lib/A.jar + WEB-INF/lib/a.jar would extract onto one file (#93)
            text = _case_failure(groups, "nested archives", ctx.case_insensitive)
            clashing = {n for g in groups for n in g}
            nested = [
                _artifact(artifact.path, name, ArtifactKind.ARCHIVE, refusal=text)
                for name in names
                if name in clashing
            ]
            names = [n for n in names if n not in clashing]
    extract_dir = _tmp_dir(ctx)
    failed: list[str] = []
    safe_extract_zip(artifact.path, extract_dir, members=names, failed=failed)
    unreadable = set(failed)
    for name in names:
        p = extract_dir / name
        if p.is_file():  # also when an earlier entry of the name is damaged: the last one is on disk
            kind, classes = classify_counted(p)
            nested.append(_artifact(p, name, kind, classes))
        elif name in unreadable:  # a damaged entry fails this archive, not its parent (#109)
            nested.append(_artifact(artifact.path, name, ArtifactKind.CORRUPT))
    with ctx.family_lock:
        ctx.families[artifact.rel] = (
            artifact.path,
            [a.path for a in nested if a.kind is ArtifactKind.ARCHIVE and a.refusal is None],
        )
        ctx.family_dirs.setdefault(artifact.rel, []).append(extract_dir)  # it goes when the family is done (#97)
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
                if artifact.kind in (ArtifactKind.ARCHIVE, ArtifactKind.SOURCES_JAR):
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
            member = artifact.rel.removeprefix(f"{artifact.parent}!/")
            copied, skipped = ctx.writer.add_blob(artifact.path, member, artifact.rel)
            report.resources_copied += copied
            report.resources_skipped += skipped
            report.outcome = "skipped"
            report.failure = f"nested deeper than --max-depth {ctx.settings.max_depth}"
        elif artifact.kind is ArtifactKind.SOURCES_JAR:
            state("scanning")
            nested = _discover_nested(artifact, ctx)  # e.g. a war with one stray .java and its jars (#110)
            state("extracting")
            tmp = _tmp_dir(ctx)
            damaged: list[str] = []
            extract_sources(artifact.path, tmp, damaged)
            java, collisions = ctx.writer.add_tree(tmp, artifact.rel)
            _drop(tmp)
            report.java_files = java
            report.collisions = collisions
            report.missing_classes = len(damaged)  # a source entry that cannot be read is a missing file (#109)
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
            refused = _refused(tmp)  # the files of the copy: the bytes that the engines get
            for rel in refused:
                (tmp / rel).unlink()
            if refused and ctx.on_warn is not None:
                ctx.on_warn(_refused_warning(artifact.rel, len(refused), "class files", refused[0]))
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
                try:
                    resolution = ctx.resolver(
                        artifact.path, list(ctx.settings.repos), ctx.client, ctx.sources_cache,
                        **({} if ctx.on_event is None else {"on_state": state, "subject": artifact.rel}),
                    )
                except Exception as exc:  # e.g. a read-only or full sources cache: the engines decompile (#111)
                    resolution = maven.Resolution(miss=f"sources lookup failed: {exc}")
            hit = False  # True when decaf wrote the Maven sources
            gap: set[str] = set()  # the classes of a hit that its sources lack: stage 2 decompiles them (#180)
            # After an abort, the report gets no row for this artifact, so no sources go out.
            if resolution is not None and resolution.sources_jar is not None and not engines.PROCESSES.closed:
                state("extracting")
                tmp = _tmp_dir(ctx)
                damaged = []
                if extract_sources(resolution.sources_jar, tmp, damaged, suffixes=maven.MAVEN_SOURCE_SUFFIXES) > 0 and not damaged:
                    expected = expected_class_stems(artifact.path)
                    gap = _sources_gap(artifact.path, expected, tmp)
                    java, collisions = ctx.writer.add_tree(tmp, artifact.rel, suffixes=maven.MAVEN_SOURCE_SUFFIXES)
                    report.method = "maven"
                    report.repo = resolution.repo
                    report.resolved_by = resolution.resolved_by
                    report.sources_cached = resolution.cached
                    report.java_files = java
                    report.collisions = collisions
                    report.classes = len(expected)
                    report.sources_gap = len(gap)
                    report.missing_classes = len(gap)  # until stage 2: then the classes that no engine writes
                    if gap:
                        ctx.maven_gaps[artifact.rel] = gap
                    hit = True
                elif damaged:  # a part of the sources is no complete output: the engines decompile (#109)
                    maven.drop_cached_sources(resolution.sources_jar)  # the next run downloads it again (#111)
                    resolution.miss = f"sources jar for {resolution.gav} has a damaged entry: {damaged[0]}"
                else:
                    # a fault after the download check can damage a cache file (#180)
                    damage = maven.zip_damage(resolution.sources_jar)
                    if damage is not None:
                        maven.drop_cached_sources(resolution.sources_jar)  # the next run downloads it again
                        resolution.miss = f"sources jar for {resolution.gav} {damage}"
                    else:
                        resolution.miss = f"sources jar for {resolution.gav} contained no source files"
                _drop(tmp)
            if resolution is not None:
                if resolution.gav is not None:
                    report.gav = str(resolution.gav)
                if not hit:
                    report.sources_miss = resolution.miss
                if ctx.on_stderr is not None:
                    msg = (
                        f"{resolution.resolved_by} {resolution.gav} ({resolution.repo})"
                        if hit
                        else resolution.miss
                    )
                    if gap:
                        msg += f"; {len(gap)} classes not in the sources jar"
                    ctx.on_stderr(f"maven {artifact.rel}: {msg}")
            if gap:  # the engines get only the retry trees of the gap classes, without the refused class files
                return report, nested, artifact.path
            if not hit:
                refused = _refused(artifact.path)
                if not refused:
                    return report, nested, artifact.path
                folder = _tmp_dir(ctx)
                with ctx.family_lock:
                    ctx.family_dirs.setdefault(artifact.rel, []).append(folder)  # it goes when the row is final
                target = _copy_without_refused(artifact.path, folder)
                if ctx.on_warn is not None:
                    ctx.on_warn(_refused_warning(artifact.rel, len(refused), "entries", refused[0].filename))
                return report, nested, target
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
    finally:
        if artifact.kind is ArtifactKind.CLASS_TREE:  # the target is the copy of the fetch stage
            _drop(target)
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
    Members' expected-stem and claim sets are disjoint (enforced at
    formation), so every produced source file belongs to exactly one member.
    """
    if engines.PROCESSES.closed:
        return [], [(a, t, r) for a, t, r, _, _ in members]
    # The scheduler formed the batch by name only; the union of the sets is the same with the class files (#206)
    members = [(a, t, r, *_stems_and_claims(t)) for a, t, r, _, _ in members]
    name = ctx.chain[0]
    heap = ctx.memory_plan.heap_mb(1)  # a batch JVM takes one slot
    batch_id = next(ctx.batch_ids)
    try:
        dest = _tmp_dir(ctx)
    except OSError:  # e.g. a full disk: each member gets its own try, and its own failure (#112)
        return [], [(a, t, r) for a, t, r, _, _ in members]
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
            ctx.specs[name], ctx.engine_jars[name], [t for _, t, _, _, _ in members], dest,
            ctx.settings.timeout, java=ctx.java, cpu_budget=ctx.cpu_budget,
            **({"cds_dir": ctx.cds_dir} if ctx.cds_dir is not None else {}),
            **({"engine_args": ctx.settings.engine_args} if ctx.settings.engine_args else {}),
            **({"heap_mb": heap} if heap is not None else {}),
        )
    except Exception:
        res = engines.EngineResult(name, -1, False, 0, traceback.format_exc()[-2000:])
    finally:
        stop.set()
        if watcher is not None:
            watcher.join()
    if engines.PROCESSES.closed:  # Ctrl-C: the killed batch can leave cut-off files (#112)
        return [], [(a, t, r) for a, t, r, _, _ in members]

    trees: dict[int, Path] = {}
    produced: dict[int, set[str]] = {i: set() for i in range(len(members))}
    helds: dict[int, _Held] = {}
    counts: dict[int, tuple[list[stubs.Stub], int]] = {}  # member -> (stubs, flagged) of the batch output
    empty: dict[int, list[str]] = {}  # member -> the rels of its empty files
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
                if p.stat().st_size == 0:  # no output (#96), but the engine wrote the class apart (#159)
                    empty.setdefault(i, []).append(rel)
                    continue
                tree = trees.setdefault(i, _tmp_dir(ctx))
                target = tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
                produced[i].add(stem)
        order = sorted(trees)
        helds = {i: _Held() for i in order}
        found = {i: {s.path: s for s in stubs.scan(trees[i], name)} for i in order}
        results = _check_trees([(trees[i], members[i][0], members[i][1], helds[i]) for i in order], ctx, heap, name)
        for n, i in enumerate(order):
            by_path = results[n] if results is not None else None
            counts[i] = (
                list(found[i].values()),
                _apply_hold(trees[i], members[i][1], found[i], by_path, name, helds[i], ctx, empty.get(i, ())),
            )
    except Exception:
        trees = {}  # split unusable; every member requeues and redoes solo (self-healing)
    _drop(dest)

    done: list[ArtifactReport] = []
    requeue: list[tuple[Artifact, Path, ArtifactReport]] = []
    for i, (a, target, report, stems, _) in enumerate(members):
        member_stubs, flagged = counts.get(i, ([], 0))
        report.attempts.append(_attempt(name, "batch", res, len(produced[i]), member_stubs, heap, flagged, batch_id))
        if i not in trees:
            requeue.append((a, target, report))
            continue
        try:
            java, collisions = ctx.writer.add_tree(trees[i], a.rel)
            _drop(trees[i])
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


def _member_done(rel: str, open_count: dict[str, int], parent_of: dict[str, str], ctx: Ctx) -> None:
    """A row is final. When no row below a container is open, the folders of the container go (#97).

    The check of a member reads the archives of its family, and the row of an archive
    beyond --max-depth reads it from the archive of its parent.
    """
    while True:
        open_count[rel] -= 1
        if open_count[rel]:
            return
        del open_count[rel]
        with ctx.family_lock:
            folders = ctx.family_dirs.pop(rel, [])
        for folder in folders:
            _drop(folder)
        if rel not in parent_of:  # not from the rel: a folder name can end with "!"
            return
        rel = parent_of.pop(rel)


def process_artifact(artifact: Artifact, ctx: Ctx) -> tuple[ArtifactReport, list[Artifact]]:
    """Run both stages synchronously; the parallel runner splits them across pools."""
    report, nested, target = _fetch_stage(artifact, ctx)
    if target is not None:
        _decompile_stage(artifact, target, ctx, report)
    return report, nested


class DecafError(Exception):
    """Environment or usage error; the CLI reports it and exits 2."""


def _engine_failure(name: str, exc: Exception) -> str:
    """Why an engine is not available, without its name (#113)."""
    if isinstance(exc, OSError):
        return f"{exc.strerror or exc}: {exc.filename}" if exc.filename else str(exc.strerror or exc)
    return str(exc).removeprefix(f"{name}: ")


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
        need = spec.min_java
        if need <= java_major:
            downloaded = False
            kwargs: dict = {}
            if on_event is not None:

                def _hook(name: str = name, version: str = spec.version) -> None:
                    nonlocal downloaded
                    downloaded = True
                    on_event("engines", name, f"downloading {version}")

                kwargs["on_download"] = _hook
            try:
                jar = engines.ensure_engine(spec, client, **kwargs)
            except (engines.EngineError, OSError) as exc:  # e.g. a read-only engine cache (#113)
                cause = _engine_failure(name, exc)
                if name == settings.engine:
                    raise DecafError(f"{name}: {cause}") from exc
                skipped.append(f"{name} ({cause})")
                continue
            if downloaded:
                on_event("engines", name, f"downloaded {spec.version}")
            need = engines.required_java(spec, jar)  # a pin can need a newer Java than its spec (#113)
        if need > java_major:
            if name == settings.engine:
                raise DecafError(
                    f"engine {name} needs Java {need}+, found Java {java_major}: use Java {need}+, or choose"
                    " another engine with --engine (`decaf engines list` shows the Java that each engine needs)"
                )
            skipped.append(f"{name} (needs Java {need}+, found {java_major})")
            continue
        jars[name] = jar
    chain = chain_for(settings.engine, settings.fallback, available=set(jars))
    if not chain:
        raise DecafError(f"primary engine {settings.engine!r} unavailable")
    if skipped and on_warn is not None:
        on_warn("fallback chain: skipped " + ", ".join(skipped))
    if on_event is not None:
        on_event("engines", "", "ready")
    return chain, jars


def _form_batch(ready_small: deque, *, fold: bool = False) -> list:
    """Greedy prefix of ready smalls with disjoint footprints, bounded by the caps.

    A footprint has the class stems, the claims, and the outer names of the orphan inner classes:
    an engine puts an orphan into the file of its outer class when one JVM reads both.

    Skipped members (footprint overlap / class cap) stay queued in order for
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
        outers = {s[:i] for s in stems for i in range(s.rfind("/") + 2, len(s)) if s[i] == "$"}
        footprint = stems | claims | outers
        footprint = {casefs.fold(s) for s in footprint} if fold else footprint
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
    if not java_major:  # no answer, or a version text that decaf cannot read (#113)
        raise DecafError(f"cannot read the Java version of {java_exe}")
    if java_major < engines.JAVA_MIN:
        raise DecafError(f"Java {java_major} is too old (Java {engines.JAVA_MIN}+ required)")

    artifacts, nested_counts = scan_counted(settings.input)
    discovered = len(artifacts) + sum(nested_counts.values())
    cds_dir: Path | None = None
    default_runner = runner is None
    if runner is None:
        runner = engines.run_engine
        batch_runner = batch_runner or engines.run_engine_batch
        if java_major >= engines.CDS_MIN_JAVA:
            cds_dir = engines.cache_root() / "engines"
    if not settings.check:
        checker = None
    # An injected runner without an injected batch_runner disables batching:
    # custom runners are per-target and must see every artifact individually.
    net = maven.NetState(warn=on_warn)
    if resolver is None:
        verdict_cache = VerdictCache(
            engines.cache_root() / "verdicts", fresh=settings.fresh_maven
        )
        resolver = partial(maven.resolve_sources, net=net, verdicts=verdict_cache)

    interrupted = False
    aborted = False  # an error other than Ctrl-C ended the run (#112)
    reports: list[ArtifactReport] = []
    chain: list[str] = []
    javac_path: Path | None = None
    tmp_root = Path(tempfile.mkdtemp(prefix="decaf-"))
    client = httpx.Client(follow_redirects=True, timeout=30)
    # The CPUs that decaf may use: its affinity, for example from taskset or the cpuset of a container (#114).
    allowed = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    total_cpus = settings.cpus or max(1, allowed - 1)  # auto: leave one core free
    jobs = settings.jobs or min(4, allowed)
    jobs = max(1, min(jobs, total_cpus))
    budget_mb, cap_mb = settings.memory_mb, 0
    if budget_mb is None:
        budget_mb = memory.default_budget_mb() or 0  # unknown RAM: no cap
        cap_mb = memory.default_cap_mb() or 0  # auto: no JVM gets more heap than the JVM default
    as_cap = memory.address_space_cap_mb()
    if as_cap:  # ulimit -v: a JVM with a larger -Xmx can fail at start (#114)
        cap_mb = min(cap_mb, as_cap) if cap_mb else as_cap
    mem_plan = memory.plan(budget_mb, jobs, cap_mb)
    jobs = mem_plan.jobs  # fewer jobs when a share would fall below the heap floor (#98)
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
        report_due, scheduling = True, False
        try:
            artifacts = [_jvm_input(a, tmp_root) for a in artifacts]
            chain, engine_jars = _preflight_engines(
                settings, java_major, client, on_event, on_warn=on_warn
            )
            if cds_dir is not None and not engines.cds_writable(cds_dir):
                cds_dir = None  # a JVM that cannot write its CDS archive exits with code 1 (#113)
            # The first events come after the last check that can end the run with DecafError,
            # so such an error leaves the stdout of --format ndjson empty (#109).
            if on_found is not None:
                on_found(discovered)
            if on_event is not None:
                on_event(
                    "scan", "",
                    f"{len(artifacts)} top-level + {sum(nested_counts.values())} nested",
                )
                ram_unknown = settings.memory_mb is None and not budget_mb
                on_event("memory", "", memory.budget_line(mem_plan, ram_unknown=ram_unknown))
            if mem_plan.budget_mb and on_warn is not None:
                for line in memory.heap_env_warnings(os.environ, mem_plan.heap_mb(1)):
                    on_warn(line)
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
            # Only the check reads the input group, and the package roots of a class folder read each class file.
            input_group: list[Path] = []
            tree_roots: dict[Path, list[Path]] = {}
            if checker is not None and settings.input.is_dir():
                for a in artifacts:
                    if a.kind is ArtifactKind.CLASS_TREE:
                        tree_roots[a.path] = _tree_roots(a.path)
                        input_group += tree_roots[a.path]
                    elif a.kind is ArtifactKind.ARCHIVE:
                        input_group.append(a.path)
            writer: MergeWriter | MirrorWriter
            if settings.mirror:
                writer = MirrorWriter(settings.output, resources=settings.resources)
            else:
                # a class folder merges by package, not at target/classes/ (#199)
                roots: dict[str, dict[str, str]] = {}
                for a in artifacts:
                    if a.kind is ArtifactKind.CLASS_TREE:
                        rels = (p.relative_to(a.path).as_posix() for p in a.path.rglob("*.class"))
                        prefixes = {rel[: -len(".class")]: _root_prefix(a.path, rel) for rel in rels}
                        roots[a.rel] = {stem: prefix for stem, prefix in prefixes.items() if prefix}
                writer = MergeWriter(settings.output / "src", case_insensitive=case_label is not None, roots=roots)
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
                specs=engines.active_specs(settings.engine_overrides),
                cpu_budget=cpu_budget,
                memory_plan=mem_plan,
                cds_dir=cds_dir,
                on_stderr=on_stderr,
                on_event=on_event,
                on_warn=on_warn,
                case_insensitive=case_label,
                checker=checker,
                java_major=java_major,
                input_group=input_group,
                tree_roots=tree_roots,
            )
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
                open_count: dict[str, int] = {}  # rel -> its own row and the open rows below it (#97)
                parent_of: dict[str, str] = {}  # nested rel -> the rel of its container (#97)
                dec_weight = 0
                scheduling = True  # from here, the handler below takes the first Ctrl-C
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
                        # ready drains first: a waiting whale gets first claim on the weight that a
                        # small job frees (#114). A whale that waits for a running whale cannot use
                        # the free slot, so a small jar may.
                        while ready_small and (dec_weight == 0 or dec_weight + 1 <= jobs) and not (
                            ready and any(w == 1 for _, w in dec_futs.values())
                        ):
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
                            fetch_futs.keys() | dec_futs.keys(), timeout=_WAIT_TICK, return_when=FIRST_COMPLETED
                        )
                        for fut in done:
                            if fut in dec_futs:
                                kind, w = dec_futs.pop(fut)
                                dec_weight -= w
                                if kind == "batch":
                                    done_reports, requeue = fut.result()
                                    for a2, t2, r2 in requeue:
                                        heappush(ready, (-a2.classes, next(seq), a2, t2, r2))
                                    reports.extend(done_reports)  # all rows first: on_done can fail
                                    for r2 in done_reports:
                                        _member_done(r2.rel, open_count, parent_of, ctx)
                                    if on_done is not None:
                                        for r2 in done_reports:
                                            on_done(r2)
                                    continue
                                report = fut.result()
                            else:
                                a = fetch_futs.pop(fut)
                                report, nested, target = fut.result()
                                open_count[a.rel] = 1 + len(nested)
                                for n in nested:
                                    parent_of[n.rel] = a.rel
                                    writer.nest(n.rel, a.rel)  # before any write of the nested archive
                                    heappush(todo, (-n.classes, next(seq), n))
                                delta = len(nested) - nested_counts.pop(a.rel, 0)
                                discovered += delta
                                if delta and on_found is not None:
                                    on_found(delta)
                                if target is not None:
                                    if on_event is not None:
                                        on_event("queued", a.rel, "")
                                    # also for a jar of a batch: its JVM has one share (#114);
                                    # a gap row runs only its gap classes (#180)
                                    if on_warn is not None and report.method != "maven":
                                        text = _share_warning(a, target, ctx)
                                        if text is not None:
                                            on_warn(text)
                                    if (
                                        batch_runner is not None
                                        and not target.is_dir()
                                        # a copy without refused entries: a batch takes the expected classes from
                                        # its target, so the refused classes would not count as missing
                                        and target == a.path
                                        and a.classes < _SMALL_CLASSES
                                        and chain[0] in engines.BATCH_ENGINES
                                        and report.method != "maven"  # a Maven row with a gap: its class retries run alone (#180)
                                        # CFR, Procyon, and JD get the classes of WEB-INF/classes/ apart (#170, #173)
                                        and not (chain[0] in _ROOTED_ENGINES and _container_classes(target) is not None)
                                    ):
                                        # by name only: no class file is read in this thread (#206)
                                        ready_small.append(
                                            (a, target, report, *_stems_and_claims(target, read=False))
                                        )
                                    else:
                                        heappush(ready, (-a.classes, next(seq), a, target, report))
                                    continue
                            reports.append(report)
                            _member_done(report.rel, open_count, parent_of, ctx)
                            if on_done is not None:
                                on_done(report)
                except BaseException as exc:  # Ctrl-C, SIGTERM (see cli), or an error such as a broken pipe
                    interrupted = True
                    aborted = not isinstance(exc, KeyboardInterrupt)
                    ended = [f for f in [*fetch_futs, *dec_futs] if f.done()]  # before the kill
                    net.abort.set()  # wake fetch threads sleeping in retry backoff
                    engines.PROCESSES.kill_all()
                    for fut in [*fetch_futs, *dec_futs]:
                        fut.cancel()
                    for fut in ended:  # a job that ended before the abort has its files out: keep its row
                        if fut.exception() is not None:
                            continue
                        if fut in fetch_futs:
                            report, _, target = fut.result()
                            # a Maven row with a gap has its sources out; the gap stays missing (#180)
                            if target is None or report.method == "maven":
                                reports.append(report)
                        elif dec_futs[fut][0] == "batch":
                            reports.extend(fut.result()[0])
                        else:
                            reports.append(fut.result())
                    for *_, report in ready:  # a Maven row with a gap that waits for its gap run: its sources are out (#180)
                        if report.method == "maven":
                            reports.append(report)
                    if aborted:
                        raise  # after the report below
        except DecafError:
            report_due = False  # a setup error ends the run with exit 2, no events, and no report (#109)
            raise
        except KeyboardInterrupt:
            interrupted = True  # Ctrl-C or SIGTERM before the scheduler, for example in the javac build
            if scheduling:
                raise  # a second Ctrl-C while the scheduler stops
        except BaseException:
            interrupted = aborted = True  # a callback error before the scheduler, for example a broken pipe
            raise
        finally:
            # Write the report even if a second Ctrl-C lands during teardown,
            # so partial results survive.
            reports.sort(key=lambda r: r.rel)
            totals = compute_totals(reports)
            status = (
                "aborted" if aborted
                else "interrupted" if interrupted
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
                    "memory_budget_mb": mem_plan.budget_mb,
                    "heap_mb": mem_plan.heap_mb(1),
                    "whale_heap_mb": mem_plan.heap_mb(2),
                    "heap_cap_mb": mem_plan.cap_mb or None,
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
            if report_due:
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
