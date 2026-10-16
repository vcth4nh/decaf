"""Engine memory: the heap budget, its split into JVM shares, and the size model (#98)."""

from __future__ import annotations

import ctypes
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

OVERHEAD_MB = 300  # RSS of one JVM above its -Xmx (probe: 188 MB median, 286 MB max)
FLOOR_MB = 1024  # the smallest heap of one job
SLOT_MIN_MB = FLOOR_MB + OVERHEAD_MB  # 1324: one job's part of the budget, at least
DEFAULT_SHARE = 0.80  # the default budget: 80% of the RAM

_CGROUP_FILES = (
    Path("/sys/fs/cgroup/memory.max"),  # cgroup v2: bytes, or "max"
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # cgroup v1: about 2**63 = no limit
)

# The heap that writes all files (2026-10-16 probe): MB fixed + MB per MiB of class bytes.
_MODELS = {"vineflower": (73, 48), "cfr": (85, 22)}

_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([kmgt])?(?:i?b)?", re.IGNORECASE)
_UNIT_MB = {"k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}

_HEAP_FLAGS = ("-Xmx", "-XX:MaxHeapSize=", "-XX:MaxRAM=", "-XX:MaxRAMPercentage=", "-XX:MaxRAMFraction=")
_OVERRIDE_FLAGS = ("-Xmx", "-XX:MaxHeapSize=")  # in _JAVA_OPTIONS these beat a command-line -Xmx


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
    mb = int(number * _UNIT_MB[unit.lower()])
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


def total_ram_mb() -> int | None:
    """The physical RAM, or a lower cgroup limit (containers), in MB; None when unknown."""
    sizes = [s for s in (_physical_ram(), *map(_cgroup_limit, _CGROUP_FILES)) if s]
    return min(sizes) // 2**20 if sizes else None


def default_budget_mb() -> int | None:
    ram = total_ram_mb()
    return int(ram * DEFAULT_SHARE) if ram else None


@dataclass(frozen=True)
class MemoryPlan:
    budget_mb: int  # 0 = no cap
    jobs: int
    slot_mb: int  # budget_mb // jobs; 0 with no cap

    def heap_mb(self, weight: int = 1) -> int | None:
        """The -Xmx of a JVM with this scheduler weight (a whale has 2); None = no cap."""
        if not self.budget_mb:
            return None
        return max(256, min(weight, self.jobs) * self.slot_mb - OVERHEAD_MB)


NO_CAP = MemoryPlan(0, 1, 0)


def plan(budget_mb: int, jobs: int) -> MemoryPlan:
    """Split the budget as --cpus does: equal slots, and fewer jobs when a slot is below the floor."""
    if budget_mb <= 0:
        return MemoryPlan(0, jobs, 0)
    jobs = max(1, min(jobs, budget_mb // SLOT_MIN_MB))
    return MemoryPlan(budget_mb, jobs, budget_mb // jobs)


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


def heap_env_warnings(env: Mapping[str, str]) -> list[str]:
    """Warnings for heap flags in the JVM option variables while decaf caps the heap."""
    lines = []
    for var in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS"):
        flags = [t for t in env.get(var, "").split() if t.startswith(_HEAP_FLAGS)]
        if flags:
            lines.append(
                f"{var} sets the heap ({' '.join(flags)}); "
                "decaf's --memory heap takes precedence (--memory 0 keeps yours)"
            )
    flags = [t for t in env.get("_JAVA_OPTIONS", "").split() if t.startswith(_OVERRIDE_FLAGS)]
    if flags:
        lines.append(
            f"_JAVA_OPTIONS sets the heap ({' '.join(flags)}) and overrides decaf's "
            "--memory shares: the budget is not enforced"
        )
    return lines
