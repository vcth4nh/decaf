"""Artifact discovery, classification, and zip utilities."""

from __future__ import annotations

import shutil
import zipfile
import zlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

ARCHIVE_EXTS = {".jar", ".war", ".ear", ".aar"}
SOURCE_SUFFIXES = (".java", ".kt")  # source files: input classification and engine output

try:
    from lzma import LZMAError
except ImportError:  # a Python without lzma: zipfile raises RuntimeError for an LZMA entry
    LZMAError = RuntimeError

# What zipfile raises for an archive or an entry that it cannot read: damaged data, an unknown
# method, encryption, a UTF-8 entry name that is no UTF-8, or a bzip2 stream (OSError) (#109).
ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, LZMAError, EOFError, NotImplementedError, RuntimeError, OSError, ValueError)


class ScanError(Exception):
    pass


class ArtifactKind(Enum):
    ARCHIVE = "archive"
    SOURCES_JAR = "sources_jar"
    RESOURCE_ONLY = "resource_only"
    CLASS_TREE = "class_tree"
    CORRUPT = "corrupt"
    BEYOND_DEPTH = "beyond_depth"  # nested archive left unextracted (--max-depth)


@dataclass(frozen=True)
class Artifact:
    path: Path
    rel: str
    kind: ArtifactKind
    classes: int = 0
    refusal: str | None = None  # failure text decided on discovery (#93): the artifact is not processed


def _read_names(path: Path) -> list[str] | None:
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    except ZIP_ERRORS:
        return None


def _classify_names(names: list[str]) -> ArtifactKind:
    if any(n.endswith(".class") for n in names):
        return ArtifactKind.ARCHIVE
    if any(n.endswith(SOURCE_SUFFIXES) for n in names):
        return ArtifactKind.SOURCES_JAR
    return ArtifactKind.RESOURCE_ONLY


def _count_classes(names: list[str]) -> int:
    return sum(1 for n in names if n.endswith(".class"))


def classify_counted(path: Path) -> tuple[ArtifactKind, int]:
    """Kind plus .class entry count, from a single namelist read."""
    names = _read_names(path)
    if names is None:
        return ArtifactKind.CORRUPT, 0
    return _classify_names(names), _count_classes(names)


def classify_zip(path: Path) -> ArtifactKind:
    return classify_counted(path)[0]


def outer_stem(stem: str, stems: Collection[str]) -> str | None:
    """The top-level class that an inner class rides with; None for a top-level class (#106).

    A class is inner only when the class file of an outer name exists: p/A$B$C rides with
    p/A, or with p/A$B when p/A is absent. A $ alone does not make a class inner: the
    engines write p/$Util and an orphan p/Gone$In to files of their own.
    """
    i = stem.find("$", stem.rfind("/") + 2)  # a $ at the start of the simple name cuts no outer name
    while i != -1:
        if stem[:i] in stems:
            return stem[:i]
        i = stem.find("$", i + 1)
    return None


def find_nested_archives(names: Iterable[str]) -> list[str]:
    return [
        n
        for n in names
        if not n.endswith("/") and PurePosixPath(n).suffix.lower() in ARCHIVE_EXTS
    ]


def scan_counted(root: Path) -> tuple[list[Artifact], dict[str, int]]:
    """Scan plus, from the same zip read, each archive's nested-archive count.

    Counts exist only for ARCHIVE/SOURCES_JAR/RESOURCE_ONLY artifacts — the kinds
    nested discovery later runs on — so the pipeline can seed display totals upfront.
    """
    counts: dict[str, int] = {}

    def _artifact(path: Path, rel: str) -> Artifact:
        names = _read_names(path)
        if names is None:
            return Artifact(path, rel, ArtifactKind.CORRUPT)
        kind = _classify_names(names)
        if kind in (ArtifactKind.ARCHIVE, ArtifactKind.SOURCES_JAR, ArtifactKind.RESOURCE_ONLY):
            counts[rel] = len(set(find_nested_archives(names)))  # two entries of one name: one artifact
        return Artifact(path, rel, kind, _count_classes(names))

    if root.is_file():
        if root.suffix.lower() not in ARCHIVE_EXTS:
            raise ScanError(
                f"{root}: unsupported file type (expected one of {sorted(ARCHIVE_EXTS)})"
            )
        return [_artifact(root, root.name)], counts

    artifacts: list[Artifact] = []
    loose_classes = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() in ARCHIVE_EXTS:
            artifacts.append(_artifact(p, p.relative_to(root).as_posix()))
        elif p.suffix == ".class":
            loose_classes += 1
    if loose_classes:
        artifacts.append(Artifact(root, "_classes", ArtifactKind.CLASS_TREE, loose_classes))
    return artifacts, counts


def scan_input(root: Path) -> list[Artifact]:
    return scan_counted(root)[0]


def safe_extract_zip(
    zip_path: Path,
    dest: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    members: Collection[str] | None = None,
    failed: list[str] | None = None,
) -> int:
    """Extract files from a zip, refusing paths that escape dest. Returns count.

    An entry that cannot be read is skipped, and its name goes to failed (#109).
    A disk error, for example a full disk, still raises.
    """
    count = 0
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if members is not None and name not in members:
                continue
            if suffixes and not name.lower().endswith(suffixes):
                continue
            target = dest / name
            try:
                ok = target.resolve().is_relative_to(resolved_dest)
            except OSError:
                ok = False
            if not ok:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            except ZIP_ERRORS as exc:
                if isinstance(exc, OSError) and exc.errno is not None:
                    raise  # the disk, not the entry
                target.unlink(missing_ok=True)
                if failed is not None:
                    failed.append(name)
                continue
            count += 1
    return count


def copy_class_tree(root: Path, dest: Path) -> int:
    count = 0
    for p in sorted(root.rglob("*.class")):
        target = dest / p.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        count += 1
    return count
