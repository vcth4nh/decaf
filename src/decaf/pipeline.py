"""Output writers, report model, artifact processing, and the parallel runner."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
            target = dest / p.relative_to(tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
            if p.suffix == ".java":
                java += 1
            else:
                resources += 1
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
