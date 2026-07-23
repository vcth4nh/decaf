"""On-disk cache of Maven lookup verdicts (sha1→GAV hits and clean misses).

Per-entry JSON files, written atomically (tmp + os.replace) the moment a
verdict is derived — no locks; safe across fetch threads and concurrent
decaf processes. Positive sha1→GAV verdicts never expire (the mapping is an
immutable fact of the jar's bytes) and are repository-independent. Negative
verdicts expire after NEGATIVE_TTL and are honored only for the exact
repository set they were derived against. Corrupt or unreadable entries
read as absent: cache damage must never fail a run.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

NEGATIVE_TTL = 7 * 86400  # seconds a negative verdict suppresses re-lookups


@dataclass(frozen=True)
class ShaVerdict:
    """A cached resolution outcome for one jar sha1."""

    gav: tuple[str, str, str] | None  # (group, artifact, version); None = negative
    resolved_by: str | None = None  # positive: "sha1-index" | "verified-guess"
    miss: str | None = None  # negative: the clean miss trail to replay


class VerdictCache:
    """Verdict files under root/sha1/ and root/gav/.

    fresh=True disables lookups (records still write): re-derive and overwrite.
    """

    def __init__(self, root: Path, fresh: bool = False):
        self.root = root
        self.fresh = fresh

    def _sha1_path(self, sha1: str) -> Path:
        return self.root / "sha1" / f"{sha1}.json"

    def _gav_path(self, gav: tuple[str, str, str]) -> Path:
        return self.root / "gav" / ("_".join(gav) + ".json")

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        # Best-effort: a cache write failure must never fail resolution.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                "w", dir=path.parent, prefix=path.name + ".", suffix=".part",
                delete=False, encoding="utf-8",
            )
            with tmp:
                json.dump(payload, tmp)
            os.replace(tmp.name, path)
        except OSError:
            if tmp is not None:
                try:
                    Path(tmp.name).unlink()
                except OSError:
                    pass

    def _fresh_negative(self, path: Path, data: dict, repos: Sequence[str]) -> bool:
        ts = data.get("ts")
        if not isinstance(ts, (int, float)) or time.time() - ts >= NEGATIVE_TTL:
            try:
                path.unlink()  # expired (or unreadable ts): prune
            except OSError:
                pass
            return False
        recorded = data.get("repos")
        # Set equality, not subset: the probe budget makes coverage
        # non-monotonic, so a smaller repo set is not "already covered".
        return isinstance(recorded, list) and set(recorded) == set(repos)

    def lookup_sha1(self, sha1: str, repos: Sequence[str]) -> ShaVerdict | None:
        if self.fresh:
            return None
        path = self._sha1_path(sha1)
        data = self._read(path)
        if data is None:
            return None
        gav = data.get("gav")
        if gav is not None:
            if (
                isinstance(gav, list)
                and len(gav) == 3
                and all(isinstance(part, str) for part in gav)
                and isinstance(data.get("resolved_by"), str)
            ):
                return ShaVerdict(
                    gav=(gav[0], gav[1], gav[2]), resolved_by=data["resolved_by"]
                )
            return None
        if not isinstance(data.get("miss"), str) or not self._fresh_negative(path, data, repos):
            return None
        return ShaVerdict(gav=None, miss=data["miss"])

    def record_sha1(self, sha1: str, gav: tuple[str, str, str], resolved_by: str) -> None:
        self._write(
            self._sha1_path(sha1),
            {"gav": list(gav), "resolved_by": resolved_by, "ts": time.time()},
        )

    def record_sha1_miss(self, sha1: str, miss: str, repos: Sequence[str]) -> None:
        self._write(
            self._sha1_path(sha1),
            {"gav": None, "miss": miss, "repos": list(repos), "ts": time.time()},
        )

    def has_no_sources(self, gav: tuple[str, str, str], repos: Sequence[str]) -> bool:
        if self.fresh:
            return False
        path = self._gav_path(gav)
        data = self._read(path)
        return data is not None and self._fresh_negative(path, data, repos)

    def record_no_sources(self, gav: tuple[str, str, str], repos: Sequence[str]) -> None:
        self._write(self._gav_path(gav), {"repos": list(repos), "ts": time.time()})
