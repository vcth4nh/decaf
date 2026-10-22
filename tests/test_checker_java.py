import os
import subprocess
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


def test_the_synthetic_names_of_a_detached_inner_class_file_are_noted(checker, tmp_path: Path):
    """#101: javac's codes and texts for the names that javac makes for a nested class."""
    java, javac, install = checker
    classes = _compile(javac, tmp_path / "classes", {"p/A.java": "package p;\npublic class A {\n  private int count;\n}\n"})
    (tmp_path / "p").mkdir()
    body = ("  void f() {{\n    Object v = val$x;\n    A a = this$0;\n    int c = a.count;\n"
            "    Object k = class$(\"java.lang.String\");\n    Object m = A.access$300(a);\n{}  }}\n")
    files = []
    for name, more in (("A$1", ""), ("A$2", "    int r = lastReturnedNode;\n")):
        f = tmp_path / f"p/{name}.java"
        f.write_text(f"package p;\nclass {name} {{\n" + body.format(more) + "}\n")
        files.append(f)
    plain = tmp_path / "p/B.java"
    plain.write_text(files[0].read_text().replace("A$1", "B"))
    out = check.run_check(
        [check.Job("0", (classes,), (*files, plain), detached=frozenset(files))],
        timeout=120, cpu_budget=1, java=java, install=install, tmp_root=tmp_path,
    )[0]
    assert (out[0].status, out[0].errors) == ("noted", 5)
    assert (out[1].status, out[1].first) == ("flagged", "9: cannot find symbol: variable lastReturnedNode")
    assert out[2].status == "flagged"


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


def test_each_form_of_an_absent_supertype_is_no_flag(checker, tmp_path: Path):
    """Review C1: an anonymous class of an absent type, a qualified absent supertype, and a nested type of an
    absent class. javac reports their inherited names as absent too. An engine error stays a flag."""
    java, _, install = checker
    sources = {
        "Q.java": "class Q extends org.absent.Base {\n  void f() { inherited(); }\n}\n",
        "N.java": "import org.absent.Outer;\nclass N extends Outer.Inner {\n  void f() { inherited(); int v = field; }\n}\n",
        "G.java": ("import java.io.OutputStream;\nimport org.absent.CountingOutputStream;\nclass G {\n"
                   "  OutputStream m(OutputStream out) {\n    return new CountingOutputStream(wrap(out, 1)) {\n"
                   "      public void write(int b) throws java.io.IOException { super.write(b); count(1); }\n"
                   "    };\n  }\n  OutputStream wrap(OutputStream o, int n) { return o; }\n}\n"),
        "J.java": "class J {\n  Runnable m() {\n    return new Runnable() { public void run() { int var1 = 1; f(var2); } };\n  }\n}\n",
    }
    files = []
    for name, text in sources.items():
        (tmp_path / name).write_text(text)
        files.append(tmp_path / name)
    out = check.run_check([check.Job("0", (), tuple(files))], timeout=120, cpu_budget=1, java=java,
                          install=install, tmp_root=tmp_path)[0]
    assert [r.reason or r.status for r in out] == ["absent classes"] * 3 + ["flagged"]


def test_a_same_package_class_of_another_jar_is_no_flag(checker, tmp_path: Path):
    """Review C3: a -tests.jar without its main jar. The class files name p.B and p.C, so these names are
    absent classes; a name that no class file names stays an engine error."""
    import zipfile

    java, javac, install = checker
    text = ("package p;\npublic class A {\n  Object f() { return new B(); }\n"
            "  Runnable g() { return new Runnable() { public void run() { C.go(); } }; }\n}\n")
    classes = _compile(javac, tmp_path / "classes", {
        "p/A.java": text, "p/B.java": "package p; class B {}", "p/C.java": "package p; class C { static void go() {} }",
    })
    jar = tmp_path / "a-tests.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        for name in ("A", "A$1"):
            zf.write(classes / f"p/{name}.class", f"p/{name}.class")
    refs = check.package_refs((classes / "p/A.class").read_bytes()) | check.package_refs(
        (classes / "p/A$1.class").read_bytes())
    tree = tmp_path / "tree/p"
    tree.mkdir(parents=True)
    (tree / "A.java").write_text(text)
    (tree / "Wrong.java").write_text("package p;\nclass Wrong {\n  Object f() { return new Name(); }\n}\n")
    out = check.run_check([check.Job("0", (jar,), (tree / "A.java", tree / "Wrong.java"), refs=lambda paths: dict.fromkeys(paths, refs))],
                          timeout=120, cpu_budget=1, java=java, install=install, tmp_root=tmp_path)[0]
    assert [r.reason or r.status for r in out] == ["absent classes", "flagged"]


def test_non_java_reads_the_source_file_of_a_class_file_of_javac(checker, tmp_path: Path):
    """Review C8: the SourceFile attribute of a real class file, with fields and methods before it."""
    _, javac, _ = checker
    out = _compile(javac, tmp_path / "classes", {
        "p/G.java": "package p; class G { int f = 1; static final String S = \"G.groovy\"; void m() {} }",
    })
    data = (out / "p/G.class").read_bytes()
    assert not check.non_java(data)  # the constant "G.groovy" is no SourceFile
    assert data.count(b"\x01\x00\x06G.java") == 1
    assert check.non_java(data.replace(b"\x01\x00\x06G.java", b"\x01\x00\x08G.groovy"))


def test_a_multi_release_jar_shows_its_versioned_classes(checker, tmp_path: Path):
    """Review C4: javac saw only the base classes of a multi-release jar on the classpath."""
    import zipfile

    java, javac, install = checker
    base = _compile(javac, tmp_path / "base", {"p/Util.java": "package p; public class Util { public static int f() { return 0; } }"})
    text = "package p; public class Util { public static int f() { return Impl11.g(); } }"
    v11 = _compile(javac, tmp_path / "v11", {"p/Util.java": text, "p/Impl11.java": "package p; class Impl11 { static int g() { return 11; } }"})
    jar = tmp_path / "mr.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nMulti-Release: true\n")
        zf.write(base / "p/Util.class", "p/Util.class")
        for name in ("Util", "Impl11"):
            zf.write(v11 / f"p/{name}.class", f"META-INF/versions/11/p/{name}.class")
    (tmp_path / "Util.java").write_text(text)
    r = check.run_check([check.Job("0", (jar,), (tmp_path / "Util.java",))], timeout=120, cpu_budget=1,
                        java=java, install=install, tmp_root=tmp_path)[0][0]
    assert r.status == "clean", r


def test_only_an_error_that_the_jdk_version_can_cause_gets_the_second_try(checker, tmp_path: Path):
    """Review C9: the second try cost a third more checker time, and for the other errors it changed no result."""
    java, javac, install = checker
    (tmp_path / "Gate.java").write_text(
        "public class Gate {\n  public static void main(String[] args) {\n    for (String r : args) {\n"
        "      System.out.println(Checker.byVersion(r.replace('|', '\\t').replace('~', '\\n')));\n    }\n  }\n}\n")
    jar = install / check.JAR
    subprocess.run([str(javac), "-cp", str(jar), "-d", str(tmp_path), str(tmp_path / "Gate.java")], check=True)
    cases = {
        "0|0|E|A|compiler.err.doesnt.exist|1|package javax.xml.bind does not exist": True,
        "0|0|E|A|compiler.err.doesnt.exist|1|package javax.activation does not exist": True,
        "0|0|E|A|compiler.err.doesnt.exist|1|package javax.servlet.http does not exist": False,  # no JDK package
        "0|0|E|A|compiler.err.cant.resolve.location.args|3|cannot find symbol symbol: method stop(java.lang.Throwable)"
        " location: class java.lang.Thread": True,
        "0|0|E|P|compiler.err.restricted.type.not.allowed|2|'var' not allowed here": True,
        "0|0|E|P|compiler.err.underscore.as.identifier|2|as of release 9, '_' is a keyword": True,
        "0|0|E|P|compiler.err.invalid.yield|5|invalid use of a restricted identifier 'yield'": True,
        "0|0|X|java.lang.AssertionError": True,
        "0|0|E|A|compiler.err.missing.ret.stmt|4|missing return statement~0|0|E|A|compiler.err.cant.apply.symbol|7|"
        "method f in class p.A cannot be applied to given types; required: java.util.List": True,
        "0|0|E|A|compiler.err.cant.resolve.location|6|cannot find symbol symbol: variable var2 location: class p.I"
        "~0|0|T|12": False,
        "0|0|E|A|compiler.err.missing.ret.stmt|4|missing return statement": False,
        "0|0|E|A|compiler.err.unreported.exception.need.to.catch.or.throw|9|unreported exception java.lang.Throwable;"
        " must be caught or declared to be thrown": False,
        "0|0|E|A|compiler.err.cant.resolve.location|6|cannot find symbol symbol: class javaxy location: class p.I": False,
    }
    out = subprocess.run([java, "-cp", os.pathsep.join([str(jar), str(tmp_path)]), "Gate", *cases],
                         capture_output=True, text=True, check=True)
    assert out.stdout.split() == [str(v).lower() for v in cases.values()]
