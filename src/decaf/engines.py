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
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import platformdirs

from . import memory
from .scanner import ARCHIVE_EXTS, SOURCE_SUFFIXES, ZIP_ERRORS, outer_stem, safe_extract_zip

JAVA_MIN = 11
CDS_MIN_JAVA = 19  # -XX:+AutoCreateSharedArchive


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
    "procyon": EngineSpec(
        name="procyon",
        version="0.6.0",
        url="https://github.com/mstrobel/procyon/releases/download/v0.6.0/procyon-decompiler-0.6.0.jar",
        sha256="821da96012fc69244fa1ea298c90455ee4e021434bc796d3b9546ab24601b779",
        min_java=11,
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

ENGINE_ORDER = ["vineflower", "cfr", "fernflower", "procyon", "jd"]


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
    # Temp names of this process only: two runs can download one engine at the same time (#113).
    fd, name = tempfile.mkstemp(dir=cache, prefix=f"{spec.name}-", suffix=".part")
    os.close(fd)
    part = Path(name)
    try:
        fetch_to(client, spec.url, part, spec.name)

        expected = spec.download_sha256 or spec.sha256
        got = _sha256(part)
        if got != expected:
            raise EngineError(
                f"{spec.name}: checksum mismatch for {spec.url}: expected {expected}, got {got}"
            )

        if spec.archive_member:
            extract_dir = Path(tempfile.mkdtemp(dir=cache, prefix=f"{spec.name}-extract-"))
            try:
                found = safe_extract_zip(part, extract_dir, members=[spec.archive_member])
                member = extract_dir / spec.archive_member
                if found != 1 or _sha256(member) != spec.sha256:
                    raise EngineError(
                        f"{spec.name}: member {spec.archive_member!r} missing or checksum mismatch"
                    )
                member.replace(jar)
            finally:
                shutil.rmtree(extract_dir, ignore_errors=True)
        else:
            part.replace(jar)
    finally:
        part.unlink(missing_ok=True)
    return jar


def cds_writable(cds_dir: Path) -> bool:
    """Can a JVM write its CDS archive in cds_dir? One that cannot exits with code 1 after its work (#113)."""
    try:
        cds_dir.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=cds_dir, suffix=".probe")
    except OSError:
        return False
    os.close(fd)
    os.unlink(probe)
    return True


def parse_java_major(text: str) -> int | None:
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)  # also "26-ea" and "17-internal" (#113)
    if not m:
        return None
    if m.group(1) == "1" and m.group(2):  # "1.8.0_392" style
        return int(m.group(2))
    return int(m.group(1))


def find_java() -> tuple[str, int] | None:
    exe = shutil.which("java")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return (exe, 0)  # no answer: the version is unknown (#113)
    major = parse_java_major(proc.stderr or proc.stdout or "")
    return (exe, major or 0)


def required_java(spec: EngineSpec, jar: Path) -> int:
    """The Java that an engine jar needs: its newest class file, and at least spec.min_java (#113).

    A pin can need a newer Java than the built-in spec says. A class of META-INF/versions/N
    loads only on Java N+, and module-info only on Java 9+, so they do not count.
    """
    newest = 0
    try:
        with zipfile.ZipFile(jar) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.endswith(".class") or name.startswith("META-INF/versions/") or name.endswith("module-info.class"):
                    continue
                with zf.open(info) as f:
                    head = f.read(8)
                if head[:4] == b"\xca\xfe\xba\xbe":
                    newest = max(newest, int.from_bytes(head[6:8], "big"))
    except ZIP_ERRORS:
        pass  # an unreadable jar: the spec gives the need
    return max(spec.min_java, newest - 44)


NATIVE_DIR_ENGINES = {"vineflower", "fernflower", "jd"}


BATCH_ENGINES = {"vineflower", "cfr", "fernflower", "procyon", "jd"}  # all five verified multi-source (#74); cfr/procyon positional


@dataclass
class EngineResult:
    engine: str
    returncode: int
    timed_out: bool
    java_files: int
    stderr_tail: str
    peak_rss_mb: int | None = None  # the JVM's peak resident memory; None = unknown (#98)
    out_of_memory: bool = False  # java.lang.OutOfMemoryError in stderr (#98)


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


# On a small heap, Procyon prints only the message of the OutOfMemoryError (#113).
_OOM_MARKS = (b"java.lang.OutOfMemoryError", b"Java heap space", b"GC overhead limit exceeded")

if sys.platform == "win32":
    from ctypes import wintypes

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_VM_READ = 0x0010  # GetProcessMemoryInfo needs both rights (Microsoft docs)

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _K32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    _K32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD)
    _K32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _open_query_handle(pid: int):
        return _K32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid) or None

    def _peak_working_set(handle) -> int | None:
        if handle is None:
            return None
        try:
            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not _K32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return counters.PeakWorkingSetSize
        finally:
            _K32.CloseHandle(handle)
else:

    def _open_query_handle(pid: int):
        return None

    def _peak_working_set(handle) -> int | None:
        return None


def _own_rss() -> int:
    """The resident memory of decaf in bytes on Linux, else 0."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


_FORK_SLACK = 16 << 20  # what a forked child can touch before its exec, and what decaf can grow meanwhile


class _PeakWatch:
    """Wait for an engine JVM and keep its peak resident memory (#98).

    POSIX: a waiter thread reaps the child with os.wait4, which returns the
    kernel's peak RSS; decaf then sets returncode itself, so Popen never waits
    on the reaped pid. Windows: a handle opened at the start gives
    PeakWorkingSetSize after the exit.

    On Linux, a forked child starts with the pages of decaf, and ru_maxrss keeps them after
    the exec. A peak that is not above them (floor) is unknown, for example the peak of a JVM
    that stops at start (#113).
    """

    def __init__(self, proc: subprocess.Popen, floor: int = 0) -> None:
        self._proc = proc
        self._floor = floor  # bytes
        self._peak: int | None = None  # bytes
        self._thread: threading.Thread | None = None
        self._handle = None
        if hasattr(os, "wait4"):
            self._thread = threading.Thread(target=self._reap, daemon=True)
            self._thread.start()
        else:
            self._handle = _open_query_handle(proc.pid)

    def _reap(self) -> None:
        try:
            _, status, usage = os.wait4(self._proc.pid, 0)
        except ChildProcessError:
            return  # reaped elsewhere (a Popen.poll during a kill): its returncode stays
        self._proc.returncode = os.waitstatus_to_exitcode(status)
        peak = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        self._peak = None if self._floor and peak <= self._floor + _FORK_SLACK else peak

    def wait(self, timeout: float | None) -> bool:
        """True when the JVM exited within timeout."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self._peak = _peak_working_set(self._handle)
        self._handle = None
        return True

    @property
    def peak_mb(self) -> int | None:
        return None if self._peak is None else self._peak // 2**20


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
    cds_dir: Path | None = None,
    engine_args: Sequence[str] = (),
    heap_mb: int | None = None,
) -> list[str]:
    # Absolute paths: a relative name that starts with "-" would reach the engine as an option (#113).
    t, d, jar = os.path.abspath(target), os.path.abspath(dest), str(jar_path)
    # ActiveProcessorCount caps the JVM's visible cores, which sizes the
    # engine's own thread pools (e.g. vineflower --thread-count), GC, and JIT.
    prefix = [java] if not cpu_budget else [java, f"-XX:ActiveProcessorCount={cpu_budget}"]
    if heap_mb is not None:
        prefix += memory.heap_flags(heap_mb)  # this JVM's share of the memory budget (#98)
    if cds_dir is not None:
        prefix += [
            "-XX:+AutoCreateSharedArchive",
            f"-XX:SharedArchiveFile={cds_dir / f'{spec.name}-{spec.version}.jsa'}",
        ]
    # vineflower/fernflower parse options only BEFORE the positionals; the
    # flag-anywhere dialects (cfr/procyon/jd) get them appended after the
    # built-ins, so a user token wins where the engine takes the last value (#70).
    if spec.name == "vineflower":
        return [*prefix, "-jar", jar, *engine_args, t, d]
    if spec.name == "cfr":
        return [*prefix, "-jar", jar, t, "--outputdir", d, "--silent", "true", *engine_args]
    if spec.name == "procyon":
        if target.suffix.lower() in ARCHIVE_EXTS:
            return [*prefix, "-jar", jar, "-jar", t, "-o", d, *engine_args]
        return [*prefix, "-jar", jar, "-o", d, t, *engine_args]
    if spec.name == "fernflower":
        return [*prefix, "-cp", jar, str(spec.main_class), *engine_args, t, d]
    if spec.name == "jd":
        return [*prefix, "-jar", jar, t, "-od", d, *engine_args]
    raise EngineError(f"unknown engine {spec.name!r}")


def build_batch_command(
    spec: EngineSpec,
    jar_path: Path,
    targets: list[Path],
    dest: Path,
    java: str = "java",
    cpu_budget: int | None = None,
    cds_dir: Path | None = None,
    engine_args: Sequence[str] = (),
    heap_mb: int | None = None,
) -> list[str]:
    if spec.name not in BATCH_ENGINES:
        raise EngineError(f"engine {spec.name!r} cannot batch")
    prefix = [java] if not cpu_budget else [java, f"-XX:ActiveProcessorCount={cpu_budget}"]
    if heap_mb is not None:
        prefix += memory.heap_flags(heap_mb)  # this JVM's share of the memory budget (#98)
    if cds_dir is not None:
        prefix += [
            "-XX:+AutoCreateSharedArchive",
            f"-XX:SharedArchiveFile={cds_dir / f'{spec.name}-{spec.version}.jsa'}",
        ]
    t, d = [os.path.abspath(p) for p in targets], os.path.abspath(dest)  # see build_command (#113)
    if spec.name == "vineflower":
        return [*prefix, "-jar", str(jar_path), *engine_args, *t, d]
    if spec.name == "cfr":
        return [*prefix, "-jar", str(jar_path), *t, "--outputdir", d, "--silent", "true", *engine_args]
    if spec.name == "procyon":
        return [*prefix, "-jar", str(jar_path), *t, "-o", d, *engine_args]
    if spec.name == "fernflower":
        return [*prefix, "-cp", str(jar_path), str(spec.main_class), *engine_args, *t, d]
    if spec.name == "jd":
        return [*prefix, "-jar", str(jar_path), *t, "-od", d, *engine_args]
    raise EngineError(f"unknown engine {spec.name!r}")


def run_engine(
    spec: EngineSpec,
    jar_path: Path,
    target: Path,
    dest: Path,
    timeout: float,
    java: str = "java",
    cpu_budget: int | None = None,
    cds_dir: Path | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    engine_args: Sequence[str] = (),
    heap_mb: int | None = None,
) -> EngineResult:
    dest.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and spec.name not in NATIVE_DIR_ENGINES:
        result = _run_per_class(spec, jar_path, target, dest, timeout, java, cpu_budget, cds_dir, on_stderr_line, engine_args,
                                heap_mb=heap_mb)
    else:
        result = _run_once(spec, jar_path, target, dest, timeout, java, cpu_budget, cds_dir, on_stderr_line, engine_args,
                           heap_mb=heap_mb)
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
    cds_dir: Path | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    engine_args: Sequence[str] = (),
    heap_mb: int | None = None,
) -> EngineResult:
    if PROCESSES.closed:
        return EngineResult(spec.name, -1, False, 0, "interrupted")
    cmd = build_command(
        spec, jar_path, target, dest, java=java, cpu_budget=cpu_budget,
        **({"cds_dir": cds_dir} if cds_dir is not None else {}),
        **({"engine_args": engine_args} if engine_args else {}),
        **({"heap_mb": heap_mb} if heap_mb is not None else {}),
    )
    return _exec_command(spec, cmd, timeout, on_stderr_line)


def run_process(
    cmd: list[str],
    timeout: float,
    on_stderr_line: Callable[[str], None] | None = None,
) -> tuple[int, bool, str, int | None, bool]:
    """Run one child JVM under the process registry.

    Returns (returncode, timed_out, stderr tail, peak RSS in MB or None, OutOfMemoryError in stderr).
    """
    if PROCESSES.closed:
        return -1, False, "interrupted", None, False
    floor = _own_rss()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
        **_SPAWN_KWARGS,
    )
    PROCESSES.register(proc)
    watch = _PeakWatch(proc, floor)
    chunks: list[bytes] = []

    def _pump() -> None:  # always a thread: communicate() would reap the JVM before wait4 can
        for raw in proc.stderr:
            chunks.append(raw)
            if on_stderr_line is not None:
                on_stderr_line(raw.decode(errors="replace").rstrip("\r\n"))

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        if not watch.wait(timeout):
            timed_out = True
            _kill_group(proc)
            watch.wait(None)
    finally:
        PROCESSES.unregister(proc)
    reader.join()
    proc.stderr.close()
    err = b"".join(chunks)
    tail = err.decode(errors="replace")[-2000:]
    return proc.returncode or 0, timed_out, tail, watch.peak_mb, any(m in err for m in _OOM_MARKS)


def _exec_command(
    spec: EngineSpec,
    cmd: list[str],
    timeout: float,
    on_stderr_line: Callable[[str], None] | None,
) -> EngineResult:
    returncode, timed_out, tail, peak_mb, out_of_memory = run_process(cmd, timeout, on_stderr_line)
    return EngineResult(
        spec.name, returncode, timed_out, 0, tail,
        peak_rss_mb=peak_mb, out_of_memory=out_of_memory,
    )


def run_engine_batch(
    spec: EngineSpec,
    jar_path: Path,
    targets: list[Path],
    dest: Path,
    timeout: float,
    java: str = "java",
    cpu_budget: int | None = None,
    cds_dir: Path | None = None,
    engine_args: Sequence[str] = (),
    heap_mb: int | None = None,
) -> EngineResult:
    """One JVM, many source archives, merged output tree (BATCH_ENGINES only)."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = build_batch_command(
        spec, jar_path, targets, dest, java=java, cpu_budget=cpu_budget,
        **({"cds_dir": cds_dir} if cds_dir is not None else {}),
        **({"engine_args": engine_args} if engine_args else {}),
        **({"heap_mb": heap_mb} if heap_mb is not None else {}),
    )
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
    cds_dir: Path | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    engine_args: Sequence[str] = (),
    heap_mb: int | None = None,
) -> EngineResult:
    returncode = 0
    timed_out = False
    tails: list[str] = []
    peak: int | None = None
    out_of_memory = False
    files = {f.relative_to(root).as_posix()[: -len(".class")]: f for f in sorted(root.rglob("*.class"))}
    deadline = time.monotonic() + timeout  # the timeout is for the whole folder (#113)
    for stem, f in files.items():
        if outer_stem(stem, files) is not None:
            continue  # inner classes ride along with their outer class (#106)
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        r = _run_once(spec, jar_path, f, dest, left, java, cpu_budget, cds_dir, on_stderr_line, engine_args,
                      heap_mb=heap_mb)
        returncode = returncode or r.returncode
        timed_out = timed_out or r.timed_out
        if r.stderr_tail:
            tails.append(r.stderr_tail)
        if r.peak_rss_mb is not None:
            peak = max(peak or 0, r.peak_rss_mb)
        out_of_memory = out_of_memory or r.out_of_memory
        if timed_out:
            break
    return EngineResult(spec.name, returncode, timed_out, 0, "\n".join(tails)[-2000:], peak, out_of_memory)


def _unpack_emitted_archives(dest: Path) -> None:
    """Fernflower-family engines emit dest/<input>.jar full of sources; flatten it."""
    for arch in list(dest.glob("*.jar")) + list(dest.glob("*.zip")):
        try:
            safe_extract_zip(arch, dest)
        except ZIP_ERRORS:
            pass  # cut off by a timeout: the next engine of the chain gets its try (#113)
        arch.unlink()
