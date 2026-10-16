"""Engine stub markers: the comments an engine writes in place of a lost method body (#96)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .scanner import SOURCE_SUFFIXES

_EXC = r"((?:[A-Za-z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable))\b"


@dataclass(frozen=True)
class _Markers:
    needle: bytes  # every marker of the engine contains it: a cheap test before decoding
    marker: re.Pattern[str]
    error: re.Pattern[str] | None  # captures the exception class name; None = the engine prints none


# A line-comment marker must start its line or follow a lambda arrow, so a string
# literal in decompiled code (a decompiler's own `buffer.append("// $FF: ...")`)
# cannot match. Patterns verified against the engine jars and real output.
_ENGINES: dict[str, _Markers] = {
    "vineflower": _Markers(
        b"VF: ",
        re.compile(
            r"(?:^[ \t]*|->[ \t]*)// \$VF: Couldn't be decompiled"
            r"|(?<!\x22)/\* \$VF: Couldn't be decompiled\. Class "  # an anonymous class never processed
            r"|^\$ ?VF: Unable to decompile class",  # whole class; "$ VF" when it gave no output
            re.MULTILINE,
        ),
        re.compile(r"Please report this to the Vineflower issue tracker[^\n]*\n[ \t]*(?://[ \t]*)?" + _EXC),
    ),
    "fernflower": _Markers(
        b"$FF: ",
        re.compile(  # only the stub forms: "$FF: synthetic method" and the like mark clean code
            r"(?:^[ \t]*|->[ \t]*)// \$FF: (?:Couldn't be decompiled|Limits for .+ are exceeded\.)",
            re.MULTILINE,
        ),
        None,
    ),
    "cfr": _Markers(
        b"failed to decompile",
        re.compile(r"^[ \t]*\* This method has failed to decompile\.", re.MULTILINE),
        re.compile(r"This method has failed to decompile\.[^\n]*\n[^\n]*\n[ \t]*\*[ \t]*" + _EXC),
    ),
    "procyon": _Markers(
        b"could not be decompiled",
        re.compile(r"^[ \t]*// This method could not be decompiled\.", re.MULTILINE),
        re.compile(r"The error that occurred was:[^\n]*\n[^\n]*\n[ \t]*//[ \t]*" + _EXC),
    ),
    "jd": _Markers(
        b"// Byte code:",
        re.compile(r"^[ \t]*// Byte code:[ \t]*\r?$", re.MULTILINE),
        None,
    ),
}


@dataclass(frozen=True)
class Stub:
    path: str  # posix path of the file in the scanned tree
    methods: int  # marker count in the file
    errors: tuple[str, ...]  # sorted, unique exception class names


def scan(tree: Path, engine: str) -> list[Stub]:
    """The files of one engine output that hold a stub marker of that engine, sorted by path."""
    spec = _ENGINES.get(engine)
    if spec is None:
        return []  # a custom runner's engine: no known markers
    found: list[Stub] = []
    for p in sorted(tree.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        data = p.read_bytes()
        if spec.needle not in data:
            continue
        text = data.decode("utf-8", errors="replace")
        methods = sum(1 for _ in spec.marker.finditer(text))
        if not methods:
            continue
        errors = sorted({m.group(1) for m in spec.error.finditer(text)}) if spec.error else []
        found.append(Stub(p.relative_to(tree).as_posix(), methods, tuple(errors)))
    return found
