"""Decompiler engine registry, acquisition, and subprocess adapters."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx
import platformdirs

from .scanner import ARCHIVE_EXTS, safe_extract_zip

JAVA_MIN = 11


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class EngineSpec:
    name: str
    version: str
    url: str
    sha256: str  # hash of the cached jar
    min_java: int
    download_sha256: str | None = None  # hash of the downloaded file, when it differs (zip dists)
    archive_member: str | None = None  # member to extract when the download is a zip
    main_class: str | None = None  # run via -cp when set; else java -jar


ENGINES: dict[str, EngineSpec] = {
    "vineflower": EngineSpec(
        name="vineflower",
        version="1.12.0",
        url="https://repo1.maven.org/maven2/org/vineflower/vineflower/1.12.0/vineflower-1.12.0.jar",
        sha256="1dfcfe974395734fa467ce620661c7623d05ba83670de0529b1fbd63ff548b9d",
        min_java=17,
    ),
    "cfr": EngineSpec(
        name="cfr",
        version="0.152",
        url="https://repo1.maven.org/maven2/org/benf/cfr/0.152/cfr-0.152.jar",
        sha256="f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2",
        min_java=11,
    ),
    "procyon": EngineSpec(
        name="procyon",
        version="0.6.0",
        url="https://github.com/mstrobel/procyon/releases/download/v0.6.0/procyon-decompiler-0.6.0.jar",
        sha256="821da96012fc69244fa1ea298c90455ee4e021434bc796d3b9546ab24601b779",
        min_java=11,
    ),
    "fernflower": EngineSpec(
        name="fernflower",
        version="253.33813.25",
        url=(
            "https://www.jetbrains.com/intellij-repository/releases/com/jetbrains/intellij/java/"
            "java-decompiler-engine/253.33813.25/java-decompiler-engine-253.33813.25.jar"
        ),
        sha256="c87d45b0ead73cc058bb176fd8a396a7fa3e8445daa3a12e866df5d2ad6fe2a5",
        min_java=21,
        main_class="org.jetbrains.java.decompiler.main.decompiler.ConsoleDecompiler",
    ),
    "jd": EngineSpec(
        name="jd",
        version="1.2.0",
        url="https://github.com/intoolswetrust/jd-cli/releases/download/jd-cli-1.2.0/jd-cli-1.2.0-dist.zip",
        sha256="d520acfa775f97f93599d04b90fc6f7d6fd5c7a525c711fbff439e03accfe61b",
        min_java=11,
        download_sha256="ae589be342b8ea2ccfa48f9da09c78e1c54f263d6695c7a4385a9f748c22bb25",
        archive_member="jd-cli.jar",
    ),
}

ENGINE_ORDER = ["vineflower", "cfr", "procyon", "fernflower", "jd"]


def cache_root() -> Path:
    return platformdirs.user_cache_path("decaf")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_engine(spec: EngineSpec, client: httpx.Client, cache_dir: Path | None = None) -> Path:
    cache = cache_dir or cache_root() / "engines"
    cache.mkdir(parents=True, exist_ok=True)
    jar = cache / f"{spec.name}-{spec.version}.jar"
    if jar.is_file() and _sha256(jar) == spec.sha256:
        return jar

    part = jar.with_suffix(".part")
    try:
        with client.stream("GET", spec.url, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(part, "wb") as out:
                for chunk in resp.iter_bytes():
                    out.write(chunk)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise EngineError(f"{spec.name}: download failed: {exc}") from exc

    expected = spec.download_sha256 or spec.sha256
    got = _sha256(part)
    if got != expected:
        part.unlink()
        raise EngineError(
            f"{spec.name}: checksum mismatch for {spec.url}: expected {expected}, got {got}"
        )

    if spec.archive_member:
        extract_dir = cache / f"{spec.name}-extract"
        shutil.rmtree(extract_dir, ignore_errors=True)
        found = safe_extract_zip(part, extract_dir, members=[spec.archive_member])
        part.unlink()
        member = extract_dir / spec.archive_member
        if found != 1 or _sha256(member) != spec.sha256:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise EngineError(
                f"{spec.name}: member {spec.archive_member!r} missing or checksum mismatch"
            )
        member.replace(jar)
        shutil.rmtree(extract_dir, ignore_errors=True)
    else:
        part.replace(jar)
    return jar


def parse_java_major(text: str) -> int | None:
    m = re.search(r'version "([0-9][0-9._]*)"', text)
    if not m:
        return None
    parts = m.group(1).split(".")
    if parts[0] == "1" and len(parts) > 1:  # "1.8.0_392" style
        return int(parts[1])
    return int(parts[0])


def find_java() -> tuple[str, int] | None:
    exe = shutil.which("java")
    if not exe:
        return None
    proc = subprocess.run([exe, "-version"], capture_output=True, text=True)
    major = parse_java_major(proc.stderr or proc.stdout or "")
    return (exe, major or 0)
