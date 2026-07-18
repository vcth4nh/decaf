import shutil
import subprocess
import zipfile
from pathlib import Path

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
