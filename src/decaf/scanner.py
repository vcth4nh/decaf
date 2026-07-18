"""Artifact discovery, classification, and zip utilities."""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

ARCHIVE_EXTS = {".jar", ".war", ".ear", ".aar"}


class ScanError(Exception):
    pass


class ArtifactKind(Enum):
    ARCHIVE = "archive"
    SOURCES_JAR = "sources_jar"
    RESOURCE_ONLY = "resource_only"
    CLASS_TREE = "class_tree"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class Artifact:
    path: Path
    rel: str
    kind: ArtifactKind


def classify_zip(path: Path) -> ArtifactKind:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return ArtifactKind.CORRUPT
    if any(n.endswith(".class") for n in names):
        return ArtifactKind.ARCHIVE
    if any(n.endswith(".java") for n in names):
        return ArtifactKind.SOURCES_JAR
    return ArtifactKind.RESOURCE_ONLY


def scan_input(root: Path) -> list[Artifact]:
    if root.is_file():
        if root.suffix.lower() not in ARCHIVE_EXTS:
            raise ScanError(
                f"{root}: unsupported file type (expected one of {sorted(ARCHIVE_EXTS)})"
            )
        return [Artifact(root, root.name, classify_zip(root))]

    artifacts: list[Artifact] = []
    has_loose_classes = False
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() in ARCHIVE_EXTS:
            artifacts.append(Artifact(p, p.relative_to(root).as_posix(), classify_zip(p)))
        elif p.suffix == ".class":
            has_loose_classes = True
    if has_loose_classes:
        artifacts.append(Artifact(root, "_classes", ArtifactKind.CLASS_TREE))
    return artifacts


def find_nested_archives(names: Iterable[str]) -> list[str]:
    return [
        n
        for n in names
        if not n.endswith("/") and PurePosixPath(n).suffix.lower() in ARCHIVE_EXTS
    ]


def safe_extract_zip(
    zip_path: Path,
    dest: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    members: Collection[str] | None = None,
) -> int:
    """Extract files from a zip, refusing paths that escape dest. Returns count."""
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
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
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
