"""Compile check of decompiled sources (#99): javac on each output file, alone."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("parse", "name", "flow", "type", "other")
RETRY_KINDS = frozenset({"parse", "name", "flow"})
UNCHECKED_REASONS = (
    "absent classes", "kotlin", "newer class version", "old source", "javac crash", "timeout",
)
CLASS_VERSION_OFFSET = 44  # class-file major version = Java version + 44

_OLD_SOURCE = frozenset({
    "compiler.err.enum.as.identifier",
    "compiler.err.assert.as.identifier",
    "compiler.err.underscore.as.identifier",
})
_ENV_CODES = frozenset({
    "compiler.err.doesnt.exist",
    "compiler.err.cant.access",
    "compiler.err.package.not.visible",
    "compiler.err.error.reading.file",
})
_ENV_PREFIXES = ("compiler.err.not.def.access.",)
_RESOLVE = "compiler.err.cant.resolve"
_TYPE_SYMBOLS = frozenset({"class", "interface", "enum", "record", "package"})
_FLOW_CODES = frozenset({
    "compiler.err.missing.ret.stmt",
    "compiler.err.var.might.not.have.been.initialized",
    "compiler.err.var.might.already.be.assigned",
    "compiler.err.except.never.thrown.in.try",
    "compiler.err.unreachable.stmt",
})
_FLOW_PREFIXES = ("compiler.err.unreported.exception.",)
_TYPE_CODES = frozenset({
    "compiler.err.prob.found.req",
    "compiler.err.cant.apply.symbol",
    "compiler.err.cant.apply.symbols",
    "compiler.err.ref.ambiguous",
    "compiler.err.cant.deref",
    "compiler.err.incomparable.types",
})
_TYPE_PREFIXES = ("compiler.err.operator.cant.be.applied",)
_SYMBOL = re.compile(r"symbol:\s+(\w+)\s+(\S+)")


@dataclass(frozen=True)
class Job:
    """One classpath and the files to compile against it."""

    id: str
    classpath: tuple[Path, ...]
    files: tuple[Path, ...]


@dataclass(frozen=True)
class FileResult:
    status: str  # "clean" | "noted" | "flagged" | "unchecked"
    reason: str | None = None  # one of UNCHECKED_REASONS when unchecked
    kinds: tuple[str, ...] = ()  # sorted kinds of the decompiler errors
    errors: int = 0  # number of decompiler errors
    first: str | None = None  # "<line>: <message>" of the first parse/name/flow error
    needs_java: int | None = None  # newer class version: the Java version it needs

    @property
    def passes(self) -> bool:
        return self.status in ("clean", "noted")


def unchecked(reason: str, needs_java: int | None = None) -> FileResult:
    return FileResult("unchecked", reason=reason, needs_java=needs_java)


@dataclass
class _Raw:
    errors: list[tuple[str, str, int, str]] = field(default_factory=list)  # (phase, code, line, message)
    crash: str | None = None
    done: bool = False


def parse_results(text: str) -> dict[tuple[str, int], _Raw]:
    """Result-file lines of Checker.java, by (job id, file index)."""
    out: dict[tuple[str, int], _Raw] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[1].isdigit():
            continue
        raw = out.setdefault((parts[0], int(parts[1])), _Raw())
        tag = parts[2]
        if tag == "E" and len(parts) >= 7:
            number = parts[5]
            line_no = int(number) if number.lstrip("-").isdigit() else 0
            raw.errors.append((parts[3], parts[4], line_no, "\t".join(parts[6:])))
        elif tag == "X":
            raw.crash = "\t".join(parts[3:])
        elif tag == "T":
            raw.done = True
    return out


def _is_env(code: str, message: str) -> bool:
    if code in _ENV_CODES or code.startswith(_ENV_PREFIXES):
        return True
    if code.startswith(_RESOLVE):
        m = _SYMBOL.search(message)
        if m and (m.group(1) in _TYPE_SYMBOLS or (m.group(1) == "variable" and m.group(2)[:1].isupper())):
            return True  # `Absent.call()` reports the absent class as "variable Absent"
    return False


def _kind(phase: str, code: str) -> str:
    if phase == "P":
        return "parse"
    if code.startswith(_RESOLVE) or code.startswith("compiler.err.already.defined"):
        return "name"
    if code in _FLOW_CODES or code.startswith(_FLOW_PREFIXES):
        return "flow"
    if code in _TYPE_CODES or code.startswith(_TYPE_PREFIXES):
        return "type"
    return "other"


def _short(message: str) -> str:
    text = " ".join(message.split())
    m = _SYMBOL.search(text)
    if m and text.startswith("cannot find symbol"):
        return f"cannot find symbol: {m.group(1)} {m.group(2)}"
    return text[:200]


def classify(raw: _Raw | None, timed_out: bool) -> FileResult:
    """Status of one file from its result lines (spec section 3)."""
    if raw is not None and raw.crash is not None:
        return unchecked("javac crash")
    if raw is None or not raw.done:
        return unchecked("timeout" if timed_out else "javac crash")
    if any(phase == "P" and code in _OLD_SOURCE for phase, code, _, _ in raw.errors):
        return unchecked("old source")
    if any(_is_env(code, message) for _, code, _, message in raw.errors):
        return unchecked("absent classes")
    if not raw.errors:
        return FileResult("clean")
    kinds = tuple(sorted({_kind(phase, code) for phase, code, _, _ in raw.errors}))
    retry = sorted(
        (line, message) for phase, code, line, message in raw.errors if _kind(phase, code) in RETRY_KINDS
    )
    if retry:
        line, message = retry[0]
        return FileResult("flagged", kinds=kinds, errors=len(raw.errors), first=f"{line}: {_short(message)}")
    return FileResult("noted", kinds=kinds, errors=len(raw.errors))


def write_jobs(path: Path, jobs: list[Job]) -> None:
    """Jobs file for Checker.java: tokens that end with a NUL byte."""
    tokens: list[str] = []
    for job in jobs:
        tokens += ["job", job.id]
        for p in job.classpath:
            tokens += ["cp", str(p)]
        for f in job.files:
            tokens += ["src", str(f)]
    path.write_bytes(b"".join(t.encode("utf-8", "surrogateescape") + b"\0" for t in tokens))


def summary(results: list[FileResult]) -> dict:
    """The per-artifact `check` counts of the report, over the final output files."""
    out: dict = {
        "checked": 0, "flagged": 0, "noted": 0, "unchecked": 0,
        "unchecked_reasons": dict.fromkeys(UNCHECKED_REASONS, 0),
        "kinds": dict.fromkeys(KINDS, 0),
        "needs_java": None,
    }
    for r in results:
        if r.status == "unchecked":
            out["unchecked"] += 1
            out["unchecked_reasons"][r.reason] += 1
            if r.needs_java is not None:
                out["needs_java"] = max(out["needs_java"] or 0, r.needs_java)
            continue
        out["checked"] += 1
        if r.status in ("flagged", "noted"):
            out[r.status] += 1
        for k in r.kinds:
            out["kinds"][k] += 1
    return out


def class_major(header: bytes) -> int | None:
    """Major version from the first 8 bytes of a class file, or None."""
    if len(header) < 8 or header[:4] != b"\xca\xfe\xba\xbe":
        return None
    return int.from_bytes(header[6:8], "big")
