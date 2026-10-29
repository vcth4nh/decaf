"""Compile check of decompiled sources (#99): javac on each output file, alone."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import engines, memory

KINDS = ("parse", "name", "flow", "type", "other")
RETRY_KINDS = frozenset({"parse", "name", "flow"})
UNCHECKED_REASONS = (
    "absent classes", "kotlin", "newer class version", "old source", "javac crash", "timeout", "interrupted",
)
CLASS_VERSION_OFFSET = 44  # class-file major version = Java version + 44
MESSAGES_PER_FILE = 100  # javac's own default -Xmaxerrs

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
    "compiler.err.package.in.other.module",  # a jar with a package that a JDK module has (#108)
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
    "compiler.err.cant.ref.non.effectively.final.var",  # four more Flow-phase codes (#108)
    "compiler.err.except.already.caught",
    "compiler.err.var.might.be.assigned.in.loop",
    "compiler.err.final.parameter.may.not.be.assigned",
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
_UNREPORTED = "compiler.err.unreported.exception."
_SYMBOL = re.compile(r"symbol:\s+(\w+)\s+(\S+)")
_LOCATION = re.compile(
    r"location:\s+(?:(?:class|interface|enum|record)\s+([\w.$]+)"
    r"|variable\s+\S+\s+of\s+type\s+([\w.$]+)|(package)\s)"
)
_IMPORT = re.compile(r"^[ \t]*import\s+(static\s+)?([\w.$]+?)(\.\*)?\s*;", re.MULTILINE)
_PACKAGE = re.compile(r"^[ \t]*package\s+([\w.$]+)\s*;", re.MULTILINE)
_SUPERTYPES = re.compile(r"\b(?:extends|implements)\s+([^{;()]+)")
_GENERIC = re.compile(r"<[^<>]*>")
_NEW = re.compile(r"\bnew\s+([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)")
_SPACES = re.compile(r"\s*")
_ABSENT_PACKAGE = re.compile(r"package\s+([\w.$]+)\s+does not exist")
_SYNTHETIC = re.compile(r"(?:this|val|access|class)\$[\w$]*")  # the names that javac makes for a nested class


@dataclass(frozen=True)
class Scope:
    """What can supply a name that a file does not define (#108).

    this: the dotted name of the file's own class; imports: the simple names of its
    single imports; on_demand: an import with *; supertypes: the names after extends and
    implements and of the anonymous classes, as written, e.g. org.absent.Base (an absent
    supertype can supply any name); twice: is a dotted class name in two or more classpath
    entries (two versions of one library)? same_package: the classes of the file's package
    that its class files name; the classpath can lack them, e.g. in a split package.
    """

    this: str = ""
    imports: frozenset[str] = frozenset()
    on_demand: bool = False
    supertypes: frozenset[str] = frozenset()
    twice: Callable[[str], bool] = field(default=lambda name: False, compare=False)
    same_package: frozenset[str] = frozenset()


def scope_of(
    text: str, stem: str, twice: Callable[[str], bool] | None = None, same_package: frozenset[str] = frozenset()
) -> Scope:
    """The Scope of a source file from its package and import declarations."""
    package = _PACKAGE.search(text)
    names: set[str] = set()
    on_demand = False
    for m in _IMPORT.finditer(text):
        if m.group(3):
            on_demand = True
        else:
            names.add(m.group(2).rsplit(".", 1)[-1])
    this = f"{package.group(1)}.{stem}" if package else stem
    supers: set[str] = set()
    for m in _SUPERTYPES.finditer(text):
        types = m.group(1)
        while _GENERIC.search(types):
            types = _GENERIC.sub("", types)
        supers |= {"".join(t.split()) for t in re.split(r",|\bimplements\b|\bextends\b", types) if t.strip()}
    supers |= _anonymous_types(text)
    return Scope(this, frozenset(names), on_demand, frozenset(supers), twice or Scope.twice, same_package)


def _anonymous_types(text: str) -> set[str]:
    """The types of the anonymous classes, `new T(...) {`: javac reports their inherited names as absent too."""
    out: set[str] = set()
    for m in _NEW.finditer(text):
        pos = _group_end(text, m.end(), "<", ">")  # type arguments
        end = _group_end(text, pos, "(", ")")
        if text.startswith("{", _SPACES.match(text, end).end()):
            out.add("".join(m.group(1).split()))
    return out


def _group_end(text: str, pos: int, open_: str, close: str, limit: int = 10_000) -> int:
    """The index after the bracket group that starts at pos (after spaces), or pos when no group starts there.

    A bracket in a string or a char literal does not count.
    """
    i = _SPACES.match(text, pos).end()
    if not text.startswith(open_, i):
        return pos
    depth, j, end = 0, i, min(len(text), i + limit)
    while j < end:
        c = text[j]
        if c in "\"'":
            j += 1
            while j < end and text[j] != c:
                j += 2 if text[j] == "\\" else 1
        elif c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return pos


@dataclass(frozen=True)
class Job:
    """One classpath and the files to compile against it."""

    id: str
    classpath: tuple[Path, ...]
    files: tuple[Path, ...]
    # A file with an error that the JDK version can cause gets a second try with javac --release <release> (#108).
    release: int | None = None
    twice: Callable[[str], bool] | None = field(default=None, compare=False)  # see Scope
    non_java: frozenset[Path] = frozenset()  # the files of Kotlin, Groovy, and Scala classes (#108)
    # For the files with an absent name: the Scope.same_package of each file.
    refs: Callable[[list[Path]], dict[Path, frozenset[str]]] | None = field(default=None, compare=False)
    detached: frozenset[Path] = frozenset()  # the files of inner classes that an engine wrote apart (#101)


@dataclass(frozen=True)
class FileResult:
    status: str  # "clean" | "noted" | "flagged" | "unchecked"
    reason: str | None = None  # one of UNCHECKED_REASONS when unchecked
    kinds: tuple[str, ...] = ()  # sorted kinds of the decompiler errors
    errors: int = 0  # number of decompiler errors
    first: str | None = None  # "<line>: <message>" of the first error that decided the status
    needs_java: int | None = None  # newer class version: the Java version it needs
    messages: tuple[str, ...] = ()  # "<line>: <message>" of each error in line order, at most MESSAGES_PER_FILE
    bytecode_only: bool = False  # flagged: bytecode can cause the first error legally, so other engines give it too (#108)

    @property
    def passes(self) -> bool:
        return self.status in ("clean", "noted")


def unchecked(reason: str, needs_java: int | None = None, first: str | None = None) -> FileResult:
    return FileResult("unchecked", reason=reason, first=first, needs_java=needs_java)


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


def _symbol(message: str) -> tuple[str, str]:
    """(kind, name) of a cannot-find-symbol message, e.g. ("method", "f"), or ("", "")."""
    m = _SYMBOL.search(message)
    return (m.group(1), m.group(2).split("(", 1)[0]) if m else ("", "")


def _unqualified(message: str, scope: Scope) -> bool:
    """Is a cannot-find-symbol message about a name in the scope of the file's own class?"""
    loc = _LOCATION.search(message)
    where = loc.group(1) or loc.group(2) if loc is not None else None
    return where is None or where == scope.this or where.startswith(scope.this + ".")


def _is_env(code: str, message: str, scope: Scope, inherits_absent: bool = False) -> bool:
    if code in _ENV_CODES or code.startswith(_ENV_PREFIXES):
        return True
    if not code.startswith(_RESOLVE):
        return False
    loc = _LOCATION.search(message)
    if loc is not None and loc.group(3):
        return True  # a class of another package: the package exists, but this class is absent
    if not _unqualified(message, scope):
        return scope.twice(loc.group(1) or loc.group(2))  # a member of a class with two versions in the classpath
    # An unqualified name in the scope of the file: absent only when an import or an absent
    # supertype can supply it (#108).
    kind, name = _symbol(message)
    if not kind:
        return False
    if inherits_absent:
        return True
    if kind == "method":
        return name in scope.imports  # only a static import supplies a method
    return (kind in _TYPE_SYMBOLS or kind == "variable") and (
        name in scope.imports or scope.on_demand or name in scope.same_package
    )


def _synthetic(code: str, message: str) -> bool:
    """A name that javac makes for a nested class: an inner class in a file of its own cannot see it (#101)."""
    return code.startswith(_RESOLVE) and _SYNTHETIC.fullmatch(_symbol(message)[1]) is not None


def _bytecode_only(code: str, message: str) -> bool:
    """An error that bytecode can cause legally: an undeclared checked exception, or a return-type overload."""
    return code.startswith(_UNREPORTED) or (code == "compiler.err.already.defined" and message.startswith("method "))


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


def _line(error: tuple[str, str, int, str]) -> str:
    return f"{error[2]}: {_short(error[3])}"


def classify(
    raw: _Raw | None, timed_out: bool, crash: str | None = None, scope: Scope = Scope(), non_java: bool = False,
    detached: bool = False,
) -> FileResult:
    """Status of one file from its result lines (spec section 3).

    crash: the cause when the checker JVM stopped without a line for the file, e.g. its stderr (#108).
    scope: what can supply an absent name; a parse error never depends on the classpath (#108).
    non_java: the class is Kotlin, Groovy, or Scala bytecode, so its bytecode-only errors are noted (#108).
    detached: an engine wrote the inner class apart from its outer class, so its synthetic names are noted (#101).
    """
    if raw is not None and raw.crash is not None:
        return unchecked("javac crash", first=_short(raw.crash))
    if raw is None or not raw.done:
        return unchecked("timeout") if timed_out else unchecked("javac crash", first=crash)
    errors = sorted(raw.errors, key=lambda e: (e[2], e[3]))  # (phase, code, line, message) in line order
    old = [e for e in errors if e[0] == "P" and e[1] in _OLD_SOURCE]
    if old:
        return unchecked("old source", first=_line(old[0]))
    parts = {part for name in scope.supertypes for part in name.split(".")}
    inherits_absent = any(  # an absent supertype, or a class file that javac cannot find for a supertype
        code == "compiler.err.cant.access"
        or (code.startswith(_RESOLVE) and _symbol(message)[1] in parts)
        or (code == "compiler.err.doesnt.exist" and _starts_a_supertype(message, scope))
        for _, code, _, message in errors
    )
    absent = [e for e in errors if _is_env(e[1], e[3], scope, inherits_absent)]
    env = [] if any(e[0] == "P" for e in errors) else absent
    retry = [e for e in errors if e not in env and _kind(e[0], e[1]) in RETRY_KINDS
             and not (non_java and _bytecode_only(e[1], e[3])) and not (detached and _synthetic(e[1], e[3]))]
    # An unqualified name that no import and no absent supertype can supply is an engine error,
    # also next to absent classes (#108). A flow error or a member of another class is not: an
    # absent catch type can cause the one, and an absent superclass of that class the other.
    if env and not any(_kind(e[0], e[1]) == "name" and _unqualified(e[3], scope) for e in retry):
        return unchecked("absent classes", first=_line(env[0]))
    if not errors:
        return FileResult("clean")
    kinds = tuple(sorted({_kind(phase, code) for phase, code, _, _ in errors}))
    messages = tuple(_line(e) for e in errors[:MESSAGES_PER_FILE])
    if retry:
        # After a parse error, env is empty, but an absent class is still no engine error.
        lead = next((e for e in retry if e not in absent), retry[0])
        return FileResult("flagged", kinds=kinds, errors=len(errors), first=_line(lead), messages=messages,
                          bytecode_only=_bytecode_only(lead[1], lead[3]))
    return FileResult("noted", kinds=kinds, errors=len(errors), first=messages[0], messages=messages)


def _starts_a_supertype(message: str, scope: Scope) -> bool:
    """Is the package of `package P does not exist` the start of a supertype name, as for org.absent.Base or
    Outer.Inner? javac gives no other error for such a supertype."""
    m = _ABSENT_PACKAGE.search(message)
    return m is not None and any(name.startswith(m.group(1) + ".") for name in scope.supertypes)


def write_jobs(path: Path, jobs: list[Job]) -> None:
    """Jobs file for Checker.java: tokens that end with a NUL byte."""
    tokens: list[str] = []
    for job in jobs:
        tokens += ["job", job.id]
        if job.release is not None:
            tokens += ["release", str(job.release)]
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


_CP_SIZES = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}


def _pool(data: bytes) -> tuple[dict[int, bytes], dict[int, int], int] | None:
    """The Utf8 values and the Class name indexes of a class file's constant pool, and the offset after it."""
    if data[:4] != b"\xca\xfe\xba\xbe" or len(data) < 10:
        return None
    count, pos, i = int.from_bytes(data[8:10], "big"), 10, 1
    utf8: dict[int, bytes] = {}
    classes: dict[int, int] = {}
    try:
        while i < count:
            tag = data[pos]
            if tag == 1:  # Utf8
                n = int.from_bytes(data[pos + 1:pos + 3], "big")
                utf8[i] = data[pos + 3:pos + 3 + n]
                pos += 3 + n
            elif tag in _CP_SIZES:
                if tag == 7:
                    classes[i] = int.from_bytes(data[pos + 1:pos + 3], "big")
                pos += 1 + _CP_SIZES[tag]
                i += tag in (5, 6)  # a Long or a Double takes two slots
            else:
                return None
            i += 1
    except IndexError:
        return None
    return utf8, classes, pos


def _class_at(pool: tuple[dict[int, bytes], dict[int, int], int], data: bytes, offset: int) -> bytes | None:
    """The name of the Class entry whose index is at an offset of the class file."""
    utf8, classes, _ = pool
    return utf8.get(classes.get(int.from_bytes(data[offset:offset + 2], "big"), -1))


_DESCRIPTOR_CLASS = re.compile(rb"L([^;<>()\[]+)[;<]")


def package_refs(data: bytes) -> frozenset[str]:
    """The classes of its own package that a class file names, as simple names: B and B$C for p/B$C.

    javac needs each of them to compile the source of the class.
    """
    pool = _pool(data)
    own = _class_at(pool, data, pool[2] + 2) if pool is not None else None
    if own is None:
        return frozenset()
    utf8, classes, _ = pool
    names = {utf8.get(i, b"") for i in classes.values()}
    for text in utf8.values():
        names.update(m.group(1) for m in _DESCRIPTOR_CLASS.finditer(text))
    package = own.rpartition(b"/")[0]
    out: set[str] = set()
    for name in names:
        where, _, simple = name.rpartition(b"/")
        if where != package or not simple:
            continue
        i = simple.find(b"$", 1)  # a $ at the start of the simple name cuts no outer name
        while i != -1:
            out.add(simple[:i].decode("utf-8", "replace"))
            i = simple.find(b"$", i + 1)
        out.add(simple.decode("utf-8", "replace"))
    return frozenset(out)


def class_name(data: bytes) -> str | None:
    """The internal name of a class file (this_class, e.g. p/A), or None (#108)."""
    pool = _pool(data)
    name = _class_at(pool, data, pool[2] + 2) if pool is not None else None
    return name.decode("utf-8", "replace") if name else None


def _utf8(text: bytes) -> bytes:
    """The bytes of a Utf8 entry of a constant pool, to find the entry in a class file."""
    return b"\x01" + len(text).to_bytes(2, "big") + text


# Kotlin or Scala puts one of these annotations on each class.
_ANNOTATIONS = tuple((_utf8(d), name) for d, name in (
    (b"Lkotlin/Metadata;", "kotlin"), (b"Lscala/reflect/ScalaSignature;", "scala"),
    (b"Lscala/reflect/ScalaLongSignature;", "scala"),
))
_GROOVY = b"groovy/lang/GroovyObject"  # Groovy makes a class implement this interface when no superclass does
_SOURCE_SUFFIXES = {b".groovy": "groovy", b".scala": "scala", b".kt": "kotlin"}


def non_java(data: bytes) -> bool:
    """Is a class file from Kotlin, Groovy, or Scala (#108)?

    Their bytecode can throw undeclared checked exceptions and can have two methods that
    differ only in the return type. A Java class that only uses these types is no match.
    """
    return language(data) is not None


def language(data: bytes) -> str | None:
    """The language of a class file from Kotlin, Groovy, or Scala ("kotlin", "groovy", "scala"), else None."""
    for annotation, name in _ANNOTATIONS:
        if annotation in data:
            return name
    pool = _pool(data) if _utf8(_GROOVY) in data or any(s in data for s in _SOURCE_SUFFIXES) else None
    if pool is None:
        return None
    start = pool[2] + 8  # after access_flags, this_class, super_class, and interfaces_count
    count = int.from_bytes(data[start - 2:start], "big")
    if any(_class_at(pool, data, start + 2 * k) == _GROOVY for k in range(count)):
        return "groovy"
    source = _source_file(pool, data, start + 2 * count)  # also a Groovy script or subclass
    return next((name for suffix, name in _SOURCE_SUFFIXES.items() if source is not None and source.endswith(suffix)), None)


def _source_file(pool: tuple[dict[int, bytes], dict[int, int], int], data: bytes, pos: int) -> bytes | None:
    """The SourceFile attribute of a class file, e.g. b"A.groovy"; pos is the offset of fields_count."""
    utf8 = pool[0]
    u2 = lambda at: int.from_bytes(data[at:at + 2], "big")  # noqa: E731
    for _ in range(2):  # the fields, then the methods: skip each with its attributes
        count, pos = u2(pos), pos + 2
        for _ in range(count):
            attributes, pos = u2(pos + 6), pos + 8
            for _ in range(attributes):
                pos += 6 + int.from_bytes(data[pos + 2:pos + 6], "big")
    count, pos = u2(pos), pos + 2
    for _ in range(count):
        if pos + 8 > len(data):
            return None
        if utf8.get(u2(pos)) == b"SourceFile":
            return utf8.get(u2(pos + 6))
        pos += 6 + int.from_bytes(data[pos + 2:pos + 6], "big")
    return None


SOURCE = Path(__file__).with_name("checker") / "Checker.java"


class CheckError(Exception):
    """The checker could not be installed."""


def find_javac(java: str) -> Path | None:
    """The javac of the JDK that runs `java`, through java.home (shims work), or None."""
    try:
        proc = subprocess.run(
            [java, "-XshowSettings:properties", "-version"],
            capture_output=True, text=True, errors="replace", timeout=60,  # a path in another encoding (#108)
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^\s*java\.home = (.+?)\s*$", (proc.stderr or "") + (proc.stdout or ""), re.MULTILINE)
    if not m:
        return None
    javac = Path(m.group(1)) / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    return javac if javac.is_file() else None


JAR = "checker.jar"  # CDS archives no class from a folder on -cp, so the classes go in a jar (#108)


def source_digest() -> str:
    """The hash of Checker.java in the names of its install folders and CDS archives."""
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12]


def install(javac: Path, java_major: int, root: Path | None = None) -> Path:
    """Compile Checker.java once for each source hash and Java version; return its directory."""
    try:
        digest = source_digest()
        base = root or engines.cache_root() / "checker"
        target = base / f"{digest}-{java_major}"
        if (target / JAR).is_file():
            return target
        base.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=base))
    except OSError as exc:  # e.g. a read-only cache: the run goes on without the check
        raise CheckError(f"checker install failed: {exc}") from exc
    try:
        proc = subprocess.run(
            [str(javac), "-nowarn", "-d", str(tmp), str(SOURCE)],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CheckError(f"checker compile failed: {exc}") from exc
    except BaseException:  # SIGTERM and Ctrl-C stop the build: no temp folder stays
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if proc.returncode != 0 or not (tmp / "Checker.class").is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise CheckError(f"checker compile failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    try:
        with zipfile.ZipFile(tmp / JAR, "w") as zf:
            for cls in sorted(tmp.glob("*.class")):
                zf.write(cls, cls.name)
        os.replace(tmp, target)
    except OSError:  # another run installed it first
        shutil.rmtree(tmp, ignore_errors=True)
    if not (target / JAR).is_file():
        raise CheckError(f"checker install incomplete: {target}")
    return target


def build_command(
    java: str,
    install_dir: Path,
    jobs_file: Path,
    result_file: Path,
    *,
    threads: int,
    cds_dir: Path | None = None,
    heap_mb: int | None = None,
) -> list[str]:
    """The one place that builds the checker command."""
    cmd = [java, f"-XX:ActiveProcessorCount={threads}"]
    if heap_mb is not None:
        cmd += memory.heap_flags(heap_mb)  # the heap share of the engine JVM in the same slot (#98)
    if cds_dir is not None:
        cmd += [
            "-XX:+AutoCreateSharedArchive",
            f"-XX:SharedArchiveFile={cds_dir / f'checker-{install_dir.name}.jsa'}",
        ]
    return [*cmd, "-cp", str(install_dir / JAR), "Checker", str(jobs_file), str(result_file), str(threads)]


def run_check(
    jobs: list[Job],
    *,
    timeout: float,
    cpu_budget: int | None,
    java: str,
    install: Path,
    tmp_root: Path,
    cds_dir: Path | None = None,
    heap_mb: int | None = None,
) -> list[list[FileResult]]:
    """One checker JVM for all jobs: one result for each file of each job, in order."""
    work = Path(tempfile.mkdtemp(prefix="check-", dir=tmp_root))
    jobs_file, result_file = work / "jobs.bin", work / "results.tsv"
    write_jobs(jobs_file, jobs)
    threads = max(1, cpu_budget or 1)
    cmd = build_command(java, install, jobs_file, result_file, threads=threads, cds_dir=cds_dir, heap_mb=heap_mb)
    returncode, timed_out, tail, _, _ = engines.run_process(cmd, timeout)
    try:
        text = result_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    shutil.rmtree(work, ignore_errors=True)  # each file of it is read (#97)
    raw = parse_results(text)
    lines = [line for line in tail.splitlines() if line.strip()]
    # The cause of an exception in the main thread comes before its stack frames.
    cause = next((line for line in lines if line.startswith("Exception in thread")), lines[-1] if lines else None)
    crash = _short(cause) if cause is not None else f"checker exit code {returncode}"  # e.g. a JVM out of memory (#108)
    stopped = engines.PROCESSES.closed  # Ctrl-C or SIGTERM killed the checker: no crash (#101)
    out: list[list[FileResult]] = []
    for job in jobs:
        found = [raw.get((job.id, i)) for i in range(len(job.files))]
        named = [f for f, r in zip(job.files, found) if _names_absent(r)]
        refs = job.refs(named) if job.refs is not None and named else {}
        out.append([unchecked("interrupted") if r is None and stopped
                    else _classify_file(r, timed_out, crash, f, job, refs.get(f, frozenset()))
                    for f, r in zip(job.files, found)])
    return out


def _names_absent(raw: _Raw | None) -> bool:
    return raw is not None and any(code.startswith(_RESOLVE) for _, code, _, _ in raw.errors)


def _classify_file(
    raw: _Raw | None, timed_out: bool, crash: str, path: Path, job: Job, same_package: frozenset[str] = frozenset()
) -> FileResult:
    """classify(), with the Scope of the file when one of its errors is an absent name, and its language."""
    scope = Scope()
    if _names_absent(raw):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        scope = scope_of(text, path.stem, job.twice, same_package)
    return classify(raw, timed_out, crash, scope, path in job.non_java, path in job.detached)
