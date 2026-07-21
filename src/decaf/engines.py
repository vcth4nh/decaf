"""Decompiler engine registry, acquisition, and subprocess adapters."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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

SOURCE_SUFFIXES = (".java", ".kt")  # engine output that counts as decompiled source


def active_specs(overrides: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, EngineSpec]:
    specs = dict(ENGINES)
    for name, fields in (overrides or {}).items():
        specs[name] = replace(ENGINES[name], **fields)
    return specs


def cache_root() -> Path:
    return platformdirs.user_cache_path("decaf")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_status(spec: EngineSpec, cache_dir: Path | None = None) -> bool:
    jar = (cache_dir or cache_root() / "engines") / f"{spec.name}-{spec.version}.jar"
    return jar.is_file() and _sha256(jar) == spec.sha256


def fetch_to(client: httpx.Client, url: str, dest: Path, label: str) -> None:
    try:
        with client.stream("GET", url, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as out:
                for chunk in resp.iter_bytes():
                    out.write(chunk)
    except httpx.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise EngineError(f"{label}: download failed: {exc}") from exc


def ensure_engine(
    spec: EngineSpec,
    client: httpx.Client,
    cache_dir: Path | None = None,
    on_download: Callable[[], None] | None = None,
) -> Path:
    cache = cache_dir or cache_root() / "engines"
    cache.mkdir(parents=True, exist_ok=True)
    jar = cache / f"{spec.name}-{spec.version}.jar"
    if cache_status(spec, cache):
        return jar

    if on_download is not None:
        on_download()
    part = jar.with_suffix(".part")
    fetch_to(client, spec.url, part, spec.name)

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


NATIVE_DIR_ENGINES = {"vineflower", "fernflower", "jd"}


BATCH_ENGINES = {"vineflower", "fernflower", "jd"}  # verified multi-source CLIs


@dataclass
class EngineResult:
    engine: str
    returncode: int
    timed_out: bool
    java_files: int
    stderr_tail: str


class ProcessRegistry:
    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if self._closed:
                kill = True
            else:
                self._procs[proc.pid] = proc
                kill = False
        if kill:
            _kill_group(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.pop(proc.pid, None)

    def kill_all(self) -> int:
        with self._lock:
            self._closed = True
            procs = list(self._procs.values())
            self._procs.clear()
        for proc in procs:
            _kill_group(proc)
        return len(procs)

    def reset(self) -> None:
        with self._lock:
            self._procs.clear()
            self._closed = False


PROCESSES = ProcessRegistry()


if sys.platform == "linux":
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _PR_SET_PDEATHSIG = 1  # linux/prctl.h

    def _set_pdeathsig() -> None:
        # Runs in the child between fork and exec (_LIBC resolved in the parent).
        # If decaf dies uncatchably (kill -9, OOM), the kernel reaps the JVM
        # instead of orphaning it mid-decompile. PDEATHSIG fires on death of the
        # spawning *thread*, which is safe here: the worker thread always blocks
        # until the engine exits, so it cannot die first.
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)

    _SPAWN_KWARGS: dict[str, object] = {"preexec_fn": _set_pdeathsig}
else:
    _SPAWN_KWARGS = {}  # Windows rejects preexec_fn; macOS has no prctl


def _kill_group(proc: subprocess.Popen) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Windows: no process groups; engines spawn no grandchildren, so this suffices.
    proc.kill()


def build_command(
    spec: EngineSpec,
    jar_path: Path,
    target: Path,
    dest: Path,
    java: str = "java",
    cpu_budget: int | None = None,
) -> list[str]:
    t, d, jar = str(target), str(dest), str(jar_path)
    # ActiveProcessorCount caps the JVM's visible cores, which sizes the
    # engine's own thread pools (e.g. vineflower --thread-count), GC, and JIT.
    prefix = [java] if not cpu_budget else [java, f"-XX:ActiveProcessorCount={cpu_budget}"]
    if spec.name == "vineflower":
        return [*prefix, "-jar", jar, t, d]
    if spec.name == "cfr":
        return [*prefix, "-jar", jar, t, "--outputdir", d, "--silent", "true"]
    if spec.name == "procyon":
        if target.suffix.lower() in ARCHIVE_EXTS:
            return [*prefix, "-jar", jar, "-jar", t, "-o", d]
        return [*prefix, "-jar", jar, "-o", d, t]
    if spec.name == "fernflower":
        return [*prefix, "-cp", jar, str(spec.main_class), t, d]
    if spec.name == "jd":
        return [*prefix, "-jar", jar, t, "-od", d]
    raise EngineError(f"unknown engine {spec.name!r}")


def build_batch_command(
    spec: EngineSpec,
    jar_path: Path,
    targets: list[Path],
    dest: Path,
    java: str = "java",
    cpu_budget: int | None = None,
) -> list[str]:
    if spec.name not in BATCH_ENGINES:
        raise EngineError(f"engine {spec.name!r} cannot batch")
    prefix = [java] if not cpu_budget else [java, f"-XX:ActiveProcessorCount={cpu_budget}"]
    t = [str(p) for p in targets]
    if spec.name == "fernflower":
        return [*prefix, "-cp", str(jar_path), str(spec.main_class), *t, str(dest)]
    if spec.name == "jd":
        return [*prefix, "-jar", str(jar_path), *t, "-od", str(dest)]
    return [*prefix, "-jar", str(jar_path), *t, str(dest)]  # vineflower


def run_engine(
    spec: EngineSpec,
    jar_path: Path,
    target: Path,
    dest: Path,
    timeout: float,
    java: str = "java",
    cpu_budget: int | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> EngineResult:
    dest.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and spec.name not in NATIVE_DIR_ENGINES:
        result = _run_per_class(spec, jar_path, target, dest, timeout, java, cpu_budget, on_stderr_line)
    else:
        result = _run_once(spec, jar_path, target, dest, timeout, java, cpu_budget, on_stderr_line)
    _unpack_emitted_archives(dest)
    result.java_files = sum(
        1 for p in dest.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES
    )
    return result


def _run_once(
    spec: EngineSpec,
    jar_path: Path,
    target: Path,
    dest: Path,
    timeout: float,
    java: str,
    cpu_budget: int | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> EngineResult:
    if PROCESSES.closed:
        return EngineResult(spec.name, -1, False, 0, "interrupted")
    cmd = build_command(spec, jar_path, target, dest, java=java, cpu_budget=cpu_budget)
    return _exec_command(spec, cmd, timeout, on_stderr_line)


def _exec_command(
    spec: EngineSpec,
    cmd: list[str],
    timeout: float,
    on_stderr_line: Callable[[str], None] | None,
) -> EngineResult:
    if PROCESSES.closed:
        return EngineResult(spec.name, -1, False, 0, "interrupted")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
        **_SPAWN_KWARGS,
    )
    PROCESSES.register(proc)
    timed_out = False
    try:
        if on_stderr_line is None:
            _, err = proc.communicate(timeout=timeout)
        else:
            chunks: list[bytes] = []

            def _pump() -> None:
                for raw in proc.stderr:
                    chunks.append(raw)
                    on_stderr_line(raw.decode(errors="replace").rstrip("\r\n"))

            reader = threading.Thread(target=_pump, daemon=True)
            reader.start()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                proc.wait()
            reader.join()
            err = b"".join(chunks)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        _, err = proc.communicate()
    finally:
        PROCESSES.unregister(proc)
    tail = (err or b"").decode(errors="replace")[-2000:]
    return EngineResult(spec.name, proc.returncode or 0, timed_out, 0, tail)


def run_engine_batch(
    spec: EngineSpec,
    jar_path: Path,
    targets: list[Path],
    dest: Path,
    timeout: float,
    java: str = "java",
    cpu_budget: int | None = None,
) -> EngineResult:
    """One JVM, many source archives, merged output tree (BATCH_ENGINES only)."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = build_batch_command(spec, jar_path, targets, dest, java=java, cpu_budget=cpu_budget)
    result = _exec_command(spec, cmd, timeout, None)
    _unpack_emitted_archives(dest)
    result.java_files = sum(
        1 for p in dest.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES
    )
    return result


def _run_per_class(
    spec: EngineSpec,
    jar_path: Path,
    root: Path,
    dest: Path,
    timeout: float,
    java: str,
    cpu_budget: int | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> EngineResult:
    returncode = 0
    timed_out = False
    tails: list[str] = []
    for f in sorted(root.rglob("*.class")):
        if "$" in f.name:
            continue  # inner classes ride along with their outer class
        r = _run_once(spec, jar_path, f, dest, timeout, java, cpu_budget, on_stderr_line)
        returncode = returncode or r.returncode
        timed_out = timed_out or r.timed_out
        if r.stderr_tail:
            tails.append(r.stderr_tail)
        if timed_out:
            break
    return EngineResult(spec.name, returncode, timed_out, 0, "\n".join(tails)[-2000:])


def _unpack_emitted_archives(dest: Path) -> None:
    """Fernflower-family engines emit dest/<input>.jar full of sources; flatten it."""
    for arch in list(dest.glob("*.jar")) + list(dest.glob("*.zip")):
        safe_extract_zip(arch, dest)
        arch.unlink()
