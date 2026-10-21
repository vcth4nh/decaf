"""Engine memory: the heap budget, its split into JVM shares, and the size model (#98)."""

from __future__ import annotations

import ctypes
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

OVERHEAD_MB = 300  # RSS of one JVM above its -Xmx, without the G1 tables (2026-10-17 runs: 232 MB max)
G1_TABLES_DIV = 50  # the G1 tables take about 1/50 of the heap (NMT, Java 21, 1-12 GB heaps)
FLOOR_MB = 1024  # the smallest heap of one job
SLOT_MIN_MB = 1351  # the smallest share whose heap is FLOOR_MB: 1351 - 300 - 27 = 1024
DEFAULT_SHARE = 0.80  # the default budget: 80% of the RAM
JVM_DEFAULT_SHARE = 0.25  # the JVM's own default maximum heap: 25% of the RAM

_CGROUP_V2 = Path("/sys/fs/cgroup")  # cgroup v2 tree: memory.max is bytes, or "max"
_CGROUP_V1 = Path("/sys/fs/cgroup/memory")  # cgroup v1 tree: memory.limit_in_bytes, about 2**63 = no limit
_PROC_CGROUP = Path("/proc/self/cgroup")  # the cgroup of this process in each tree

# The heap that writes all files (2026-10-16 probe): MB fixed + MB per MiB of class bytes.
_MODELS = {"vineflower": (73, 48), "cfr": (85, 22)}

_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([kmgt])?(?:i?b)?", re.IGNORECASE)
_UNIT_MB = {"k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}

_HEAP_FLAGS = ("-Xmx", "-XX:MaxHeapSize=", "-XX:MaxRAM=", "-XX:MaxRAMPercentage=", "-XX:MaxRAMFraction=")
_OVERRIDE_FLAGS = ("-Xmx", "-XX:MaxHeapSize=")  # in _JAVA_OPTIONS these beat a command-line -Xmx
_INITIAL_FLAGS = ("-Xms", "-XX:InitialHeapSize=", "-XX:MinHeapSize=")  # above the -Xmx, the JVM does not start
_JVM_SIZE = re.compile(r"(\d+)([kmgt]?)", re.IGNORECASE)  # a JVM size: a bare number is bytes
_JVM_UNIT_MB = {"": 1 / 2**20, "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}
_EARLY_VARS = ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS")  # the JVM reads these before the command line
_VM_ERRORS_TO_STDERR = "-XX:+DisplayVMOutputToStderr"  # start errors go to stdout, which decaf discards


def parse_size(text: str) -> int:
    """A size like 8g, 8192m, or 1.5G, in MB (MiB); 0 means no cap."""
    m = _SIZE.fullmatch(text.strip())
    if m is None:
        raise ValueError(f"not a size: {text!r}")
    number = float(m.group(1))
    if number == 0:
        return 0
    unit = m.group(2)
    if unit is None:
        raise ValueError(f"a size needs a unit (k, m, g, t): {text!r}")
    size = number * _UNIT_MB[unit.lower()]
    if not math.isfinite(size):  # a number with hundreds of digits (#114)
        raise ValueError(f"a size too large: {text!r}")
    mb = int(size)
    if mb == 0:
        raise ValueError(f"a size below 1 MB: {text!r}")
    return mb


def _windows_total_ram() -> int | None:
    class _Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _Status()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.ullTotalPhys


def _physical_ram() -> int | None:
    try:
        if sys.platform == "win32":
            return _windows_total_ram()
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None


def _cgroup_limit(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text.isdigit():
        return None  # "max" (cgroup v2) or junk
    value = int(text)
    return value if value < 1 << 60 else None


def _cgroup_files() -> list[Path]:
    """The limit files of this process's cgroup and of each parent, then the root files (#104).

    The root files alone miss a limit on a child cgroup: a systemd scope, a batch job, or a
    container in the host's cgroup namespace. A container with its own namespace has the path "/".
    """
    try:
        lines = _PROC_CGROUP.read_text().splitlines()
    except OSError:
        lines = []  # no /proc: not Linux
    files = []
    for line in lines:
        _, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")  # "0::/a/b" (v2) or "4:memory:/a/b" (v1)
        if not controllers:
            tree, name = _CGROUP_V2, "memory.max"
        elif "memory" in controllers.split(","):
            tree, name = _CGROUP_V1, "memory.limit_in_bytes"
        else:
            continue
        parts = PurePosixPath(path).parts[1:]
        files += [tree.joinpath(*parts[:i], name) for i in range(len(parts), 0, -1)]
    return files + [_CGROUP_V2 / "memory.max", _CGROUP_V1 / "memory.limit_in_bytes"]


def total_ram_mb() -> int | None:
    """The physical RAM, or a lower limit of this process's cgroup or a parent, in MB; None when unknown."""
    sizes = [s for s in (_physical_ram(), *map(_cgroup_limit, _cgroup_files())) if s]
    return min(sizes) // 2**20 if sizes else None


def default_budget_mb() -> int | None:
    ram = total_ram_mb()
    return int(ram * DEFAULT_SHARE) if ram else None


def default_cap_mb() -> int | None:
    """The cap on one JVM's heap with the auto budget: the JVM default, so no JVM gets more than in 1.10.0."""
    ram = total_ram_mb()
    return int(ram * JVM_DEFAULT_SHARE) if ram else None


def address_space_cap_mb() -> int | None:
    """Half the address-space limit of this process (ulimit -v), as the JVM's own default; None = no limit.

    A JVM with a larger -Xmx can fail at start; without an -Xmx, the JVM takes this heap itself.
    """
    if sys.platform == "win32":
        return None
    import resource

    soft, _ = resource.getrlimit(resource.RLIMIT_AS)
    return soft // 2 // 2**20 if soft > 0 and soft != resource.RLIM_INFINITY else None


def heap_in(share_mb: int) -> int:
    """The -Xmx that keeps a JVM's RSS inside its share: the share less the overhead and the G1 tables."""
    return share_mb - OVERHEAD_MB - share_mb // G1_TABLES_DIV


@dataclass(frozen=True)
class MemoryPlan:
    budget_mb: int  # 0 = no cap
    jobs: int
    slot_mb: int  # budget_mb // jobs; 0 with no cap
    cap_mb: int = 0  # the largest heap of one JVM; 0 = none (the auto budget sets it)

    def heap_mb(self, weight: int = 1) -> int | None:
        """The -Xmx of a JVM with this scheduler weight (a whale has 2); None = no cap."""
        if not self.budget_mb:
            return None
        heap = max(256, heap_in(min(weight, self.jobs) * self.slot_mb))
        return min(heap, self.cap_mb) if self.cap_mb else heap


NO_CAP = MemoryPlan(0, 1, 0)


def plan(budget_mb: int, jobs: int, cap_mb: int = 0) -> MemoryPlan:
    """Split the budget as --cpus does: equal slots, and fewer jobs when a slot is below the floor."""
    if budget_mb <= 0:
        return MemoryPlan(0, jobs, 0)
    jobs = max(1, min(jobs, budget_mb // SLOT_MIN_MB))
    return MemoryPlan(budget_mb, jobs, budget_mb // jobs, cap_mb)


def estimate_heap_mb(engine: str, class_bytes: int) -> int | None:
    """The heap that the engine needs to write all files; None for an engine without a model."""
    model = _MODELS.get(engine)
    if model is None:
        return None
    base, per_mb = model
    return math.ceil(base + per_mb * class_bytes / 2**20)


def gb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB"


def budget_line(mem: MemoryPlan, ram_unknown: bool = False) -> str:
    """The line that the CLI shows before the decompile starts."""
    if not mem.budget_mb:
        if ram_unknown:
            return "engine memory: no cap (RAM size unknown; set --memory)"
        return "engine memory: no cap (--memory 0)"
    heap, whale = mem.heap_mb(1), mem.heap_mb(2)
    if mem.jobs == 1:
        text = f"engine memory: up to {gb(mem.budget_mb)} (1 job, {gb(heap)} heap"
    else:
        text = f"engine memory: up to {gb(mem.budget_mb)} ({mem.jobs} jobs, {gb(heap)} heap each"
    if whale != heap:
        text += f"; a whale gets {gb(whale)}"
    return text + ")"


def _initial_heap_mb(options: str) -> int:
    """The largest initial heap that JVM options set, in MB; 0 when they set none."""
    best = 0
    for token in options.split():
        for flag in _INITIAL_FLAGS:
            m = _JVM_SIZE.fullmatch(token[len(flag) :]) if token.startswith(flag) else None
            if m:
                best = max(best, math.ceil(int(m.group(1)) * _JVM_UNIT_MB[m.group(2).lower()]))
    return best


def heap_flags(heap_mb: int, env: Mapping[str, str] | None = None) -> list[str]:
    """The flags that cap one JVM's heap at heap_mb (#98) and still let it start.

    An initial heap above heap_mb in JAVA_TOOL_OPTIONS or JDK_JAVA_OPTIONS loses to an -Xms of
    heap_mb on the command line. One in _JAVA_OPTIONS beats the command line, so this JVM gets no
    cap. The JVM writes its start errors to stdout, which decaf discards: the last flag sends
    them to the stderr tail.
    """
    env = os.environ if env is None else env
    if _initial_heap_mb(env.get("_JAVA_OPTIONS", "")) > heap_mb:
        return []
    early = max(_initial_heap_mb(env.get(var, "")) for var in _EARLY_VARS)
    return [f"-Xmx{heap_mb}m", *([f"-Xms{heap_mb}m"] if early > heap_mb else []), _VM_ERRORS_TO_STDERR]


def heap_env_warnings(env: Mapping[str, str], heap_mb: int) -> list[str]:
    """Warnings for heap flags in the JVM option variables while decaf caps the heap at heap_mb or more."""

    def heap_tokens(var: str, flags: tuple[str, ...]) -> list[str]:
        return [t for t in env.get(var, "").split() if t.startswith(flags) or _initial_heap_mb(t) > heap_mb]

    lines = []
    for var in _EARLY_VARS:
        flags = heap_tokens(var, _HEAP_FLAGS)
        if flags:
            lines.append(
                f"{var} sets the heap ({' '.join(flags)}); "
                "decaf's --memory heap takes precedence (--memory 0 keeps yours)"
            )
    flags = heap_tokens("_JAVA_OPTIONS", _OVERRIDE_FLAGS)
    if flags:
        lines.append(
            f"_JAVA_OPTIONS sets the heap ({' '.join(flags)}) and overrides decaf's "
            "--memory shares: the budget is not enforced"
        )
    return lines
