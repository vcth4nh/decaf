import contextlib
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def make_jar(tmp_path: Path):
    """Build a zip/jar at tmp_path/relname with {entry_name: content} members."""

    def _make(relname: str, entries: dict[str, bytes | str], base: Path | None = None) -> Path:
        path = (base or tmp_path) / relname
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return path

    return _make


@pytest.fixture
def make_damaged_jar(tmp_path: Path):
    """Build a deflated jar; the data of each `damaged` entry starts with an invalid deflate block."""

    def _make(relname: str, entries: dict[str, bytes | str], damaged: tuple[str, ...], base: Path | None = None) -> Path:
        path = (base or tmp_path) / relname
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        for name in damaged:
            with zipfile.ZipFile(path) as zf:
                offset = zf.getinfo(name).header_offset
            with open(path, "r+b") as f:
                f.seek(offset + 26)
                name_len, extra_len = struct.unpack("<HH", f.read(4))
                f.seek(offset + 30 + name_len + extra_len)
                f.write(b"\xff")
        return path

    return _make


JAVA_SOURCES = {
    "com/example/Greeter.java": (
        "package com.example;\n"
        "public class Greeter {\n"
        "    public String greet(String name) {\n"
        "        Runnable r = () -> System.out.println(\"side effect\");\n"
        "        r.run();\n"
        "        return \"Hello, \" + name;\n"
        "    }\n"
        "    public static class Inner {\n"
        "        public int answer() { return 42; }\n"
        "    }\n"
        "}\n"
    ),
    "com/example/Main.java": (
        "package com.example;\n"
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        System.out.println(new Greeter().greet(args.length > 0 ? args[0] : \"world\"));\n"
        "    }\n"
        "}\n"
    ),
}


@pytest.fixture(scope="session")
def compiled_classes(tmp_path_factory) -> Path:
    javac = shutil.which("javac")
    if javac is None:
        pytest.skip("javac not available")
    src_root = tmp_path_factory.mktemp("javasrc")
    files = []
    for rel, source in JAVA_SOURCES.items():
        p = src_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source)
        files.append(str(p))
    out = tmp_path_factory.mktemp("classes")
    # --release 11: class file v55, parseable by ALL five engines (JD-Core is
    # from 2021 and cannot read current-JDK bytecode).
    subprocess.run([javac, "--release", "11", "-d", str(out), *files], check=True)
    return out


@pytest.fixture(scope="session")
def fixture_jar(compiled_classes: Path, tmp_path_factory) -> Path:
    jar = tmp_path_factory.mktemp("fixturejar") / "greeter.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        for p in sorted(compiled_classes.rglob("*.class")):
            zf.write(p, p.relative_to(compiled_classes).as_posix())
    return jar


HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from decaf import runlock\n"
    "lock = runlock.acquire(exclusive=sys.argv[2] == '1', path=Path(sys.argv[1]))\n"
    "print('held' if lock else 'none', flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def lock_holder():
    """A child process holds decaf.lock until the with-block ends (#155)."""

    @contextlib.contextmanager
    def hold(path: Path, *, exclusive: bool):
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(path), "1" if exclusive else "0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "held"
            yield proc
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
            proc.stdout.close()

    return hold


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    """No test reaches GitHub through decaf.selfupdate (#155)."""
    from decaf import selfupdate

    def blocked(request):
        raise httpx.ConnectError("network blocked in tests", request=request)

    monkeypatch.setattr(selfupdate, "client", lambda: httpx.Client(transport=httpx.MockTransport(blocked)))


@pytest.fixture
def github():
    """A fake GitHub releases API: github(tag=..., status=..., body=..., seen=[]) -> httpx.Client (#155)."""

    def make(tag="v1.12.0", status=200, body=None, seen=None) -> httpx.Client:
        def handler(request):
            if seen is not None:
                seen.append(request)
            if body is not None:
                return httpx.Response(status, text=body)
            return httpx.Response(status, json={"tag_name": tag})

        return httpx.Client(transport=httpx.MockTransport(handler))

    return make
