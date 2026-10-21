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


def test_release_from_the_class_version_accepts_old_code(checker, tmp_path: Path):
    """#108: Java 8 code may use APIs that newer JDKs removed, and names that newer Java restricts.

    The release is only a second try: a Java 8 class of a library that a newer JDK built can use
    newer APIs, and the second try must not change the first file manager for the next file.
    """
    java, _, install = checker
    old = tmp_path / "Old.java"
    old.write_text("class Old { String f(byte[] b) { return javax.xml.bind.DatatypeConverter.printHexBinary(b); } }\n"
                   "class var { }\n")
    new = tmp_path / "New.java"
    new.write_text("class New { void f(Object o) { java.lang.ref.Reference.reachabilityFence(o); java.util.List.of(); } }\n")
    run = lambda release: check.run_check([check.Job("0", (), (old, new), release)], timeout=120, cpu_budget=1,  # noqa: E731
                                          java=java, install=install, tmp_root=tmp_path)[0]
    assert [r.status for r in run(8)] == ["clean", "clean"]
    assert [r.status != "clean" for r in run(None)] == [True, False]


def test_cfr_goto_next_to_an_absent_package_is_flagged(checker, tmp_path: Path):
    """#108: broken engine output must not hide as unchecked."""
    java, _, install = checker
    f = tmp_path / "Goto.java"
    f.write_text("import org.absent.Thing;\nclass Goto { void f() {\n ** GOTO lbl12;\n } }\n")
    r = check.run_check([check.Job("0", (), (f,))], timeout=120, cpu_budget=1, java=java, install=install,
                        tmp_root=tmp_path)[0][0]
    assert r.status == "flagged" and "parse" in r.kinds


def test_absent_supertype_is_no_flag(checker, tmp_path: Path):
    """javac reports inherited members as absent when the supertype is absent: unchecked, no retry."""
    java, _, install = checker
    f = tmp_path / "E.java"
    f.write_text("import org.absent.Base;\nclass E extends Base {\n  void f() { inherited(); int v = field; }\n}\n")
    r = check.run_check([check.Job("0", (), (f,))], timeout=120, cpu_budget=1, java=java, install=install,
                        tmp_root=tmp_path)[0][0]
    assert r.reason == "absent classes"


def test_bytecode_only_errors_of_a_non_java_class_are_noted(checker, tmp_path: Path):
    """#108: javac's codes and texts for an undeclared checked exception and a return-type overload."""
    java, _, install = checker
    text = "class {} {{\n  void f() {{ throw new java.io.IOException(); }}\n  int g() {{ return 1; }}\n  long g() {{ return 2; }}\n}}\n"
    groovy, plain = tmp_path / "G.java", tmp_path / "J.java"
    groovy.write_text(text.format("G"))
    plain.write_text(text.format("J"))
    out = check.run_check([check.Job("0", (), (groovy, plain), non_java=frozenset({groovy}))], timeout=120,
                          cpu_budget=1, java=java, install=install, tmp_root=tmp_path)[0]
    assert (out[0].status, out[0].kinds, out[0].errors) == ("noted", ("flow", "name"), 2)
    assert (out[1].status, out[1].kinds, out[1].errors) == ("flagged", ("flow", "name"), 2)


def test_non_java_reads_class_files_of_javac(checker, tmp_path: Path):
    """#108: the markers in real class files, with stand-ins for the Kotlin and Groovy types."""
    _, javac, _ = checker
    out = _compile(javac, tmp_path / "classes", {
        "kotlin/Metadata.java": "package kotlin; public @interface Metadata {}",
        "groovy/lang/GroovyObject.java": "package groovy.lang; public interface GroovyObject {}",
        "p/K.java": "package p; @kotlin.Metadata class K {}",
        "p/G.java": "package p; class G implements Runnable, groovy.lang.GroovyObject { public void run() {} }",
        "p/J.java": "package p; class J { groovy.lang.GroovyObject g; kotlin.Metadata m() { return null; } }",
    })
    assert {n: check.non_java((out / f"p/{n}.class").read_bytes()) for n in "KGJ"} == {"K": True, "G": True, "J": False}
