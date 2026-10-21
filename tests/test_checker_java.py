from pathlib import Path

import pytest

from decaf import check
from decaf.engines import find_java

pytestmark = pytest.mark.slow

SOURCES = {
    "Ex.java": (
        "class Ex {\n"
        "  int f(String name) {\n"
        "    try { return 1; } catch (RuntimeException x) { return xx.hashCode(); }\n"
        "  }\n"
        "}\n"
    ),
    "Stub.java": "import java.util.List;\nclass Stub {\n  List<String> f() {\n    // $VF: Couldn't be decompiled\n  }\n}\n",
    "Kw.java": "class Kw { void f() { int enum = 1; } }\n",
    "Env.java": "import org.absent.Thing;\nclass Env { Thing t; }\n",
    "Clean.java": "class Clean { int f() { return 1; } }\n",
    "Noted.java": "import java.util.*;\nclass Noted { List<String> l = new ArrayList<Integer>(); }\n",
    "Ünï.java": "class Ünï { }\n",
}


@pytest.fixture(scope="module")
def checker(tmp_path_factory):
    found = find_java()
    assert found is not None, "java required"
    java, major = found
    javac = check.find_javac(java)
    if javac is None:
        pytest.skip("no JDK on PATH")
    return java, javac, check.install(javac, major, root=tmp_path_factory.mktemp("checker"))


def test_real_javac_statuses(checker, tmp_path: Path):
    java, _, install = checker
    files = []
    for name, text in SOURCES.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        files.append(p)
    out = check.run_check(
        [check.Job("0", (), tuple(files))],
        timeout=120, cpu_budget=2, java=java, install=install, tmp_root=tmp_path,
    )
    got = dict(zip(SOURCES, out[0]))
    assert got["Ex.java"].status == "flagged" and got["Ex.java"].kinds == ("name",)
    assert got["Ex.java"].first == "3: cannot find symbol: variable xx"
    assert got["Ex.java"].messages == ("3: cannot find symbol: variable xx",)
    assert got["Stub.java"].status == "flagged" and got["Stub.java"].kinds == ("flow",)
    assert got["Kw.java"] == check.unchecked(
        "old source", first="1: as of release 5, 'enum' is a keyword, and may not be used as an identifier"
    )
    assert got["Env.java"] == check.unchecked("absent classes", first="1: package org.absent does not exist")
    assert got["Clean.java"].status == "clean"
    assert got["Noted.java"].status == "noted" and got["Noted.java"].kinds == ("type",)
    assert got["Noted.java"].first.startswith("2: incompatible types")
    assert got["Noted.java"].messages == (got["Noted.java"].first,)
    assert got["Ünï.java"].status == "clean"


def test_install_is_reused(checker):
    _, javac, install = checker
    major = int(install.name.rsplit("-", 1)[1])
    assert check.install(javac, major, root=install.parent) == install


def _jar(path: Path, classes: Path, manifest: str | None = None) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        if manifest is not None:
            zf.writestr("META-INF/MANIFEST.MF", manifest)
        for c in sorted(classes.rglob("*.class")):
            zf.write(c, c.relative_to(classes).as_posix())
    return path


def _compile(javac: Path, out: Path, sources: dict[str, str]) -> Path:
    import subprocess

    src = out.with_name(out.name + "-src")
    for rel, text in sources.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    subprocess.run([str(javac), "--release", "11", "-d", str(out), *(str(src / r) for r in sources)], check=True)
    return out


def test_classpath_jar_manifest_is_not_followed(checker, tmp_path: Path):
    """#108: javac reads a jar on the classpath as a folder, so a Class-Path entry outside the input is no class source."""
    java, javac, install = checker
    _jar(tmp_path / "far.jar", _compile(javac, tmp_path / "far", {"x/Far.java": "package x; public class Far {}"}))
    (tmp_path / "in").mkdir()
    dep = _jar(tmp_path / "in/dep.jar", _compile(javac, tmp_path / "dep", {"q/Dep.java": "package q; public class Dep {}"}),
               manifest="Manifest-Version: 1.0\nClass-Path: ../far.jar\n")
    (tmp_path / "UseDep.java").write_text("class UseDep { q.Dep d; }\n")
    (tmp_path / "UseFar.java").write_text("class UseFar { x.Far f; }\n")
    out = check.run_check([check.Job("0", (dep,), (tmp_path / "UseDep.java", tmp_path / "UseFar.java"))],
                          timeout=120, cpu_budget=1, java=java, install=install, tmp_root=tmp_path)
    assert out[0][0].status == "clean"
    assert out[0][1] == check.unchecked("absent classes", first="1: package x does not exist")


def test_unreadable_classpath_jar_costs_only_its_classes(checker, tmp_path: Path):
    java, _, install = checker
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip")
    (tmp_path / "Clean.java").write_text("class Clean { int f() { return 1; } }\n")
    out = check.run_check([check.Job("0", (bad,), (tmp_path / "Clean.java",))],
                          timeout=120, cpu_budget=1, java=java, install=install, tmp_root=tmp_path)
    assert out[0][0].status == "clean"


def test_results_survive_a_timeout(checker, tmp_path: Path):
    """#108: the checker flushes each result, so a kill at the timeout keeps the finished files."""
    java, _, install = checker
    files = []
    for i in range(4000):
        f = tmp_path / f"C{i}.java"
        f.write_text(f"class C{i} {{ int f() {{ return {i}; }} }}\n")
        files.append(f)
    out = check.run_check([check.Job("0", (), tuple(files))], timeout=4, cpu_budget=1,
                          java=java, install=install, tmp_root=tmp_path)
    statuses = [r.status if r.status != "unchecked" else r.reason for r in out[0]]
    assert "clean" in statuses and "timeout" in statuses
