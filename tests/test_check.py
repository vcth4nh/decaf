import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from decaf import check
from decaf.check import FileResult, class_major, classify, parse_results, summary, unchecked, write_jobs


def raw(*lines: str):
    return parse_results("\n".join(lines) + "\n").get(("0", 0))


def test_clean_file():
    assert classify(raw("0\t0\tT\t12"), False) == FileResult("clean")


def test_name_error_is_flagged_with_short_first_line():
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t333\tcannot find symbol   symbol:   variable xx"
        "   location: class jenkins.model.PeepholePermalink.DefaultCache",
        "0\t0\tT\t40",
    ), False)
    assert r.status == "flagged" and r.kinds == ("name",) and r.errors == 1
    assert r.first == "333: cannot find symbol: variable xx"
    assert r.passes is False


def test_missing_return_is_flow():
    r = classify(raw("0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t62\tmissing return statement", "0\t0\tT\t5"), False)
    assert r.status == "flagged" and r.kinds == ("flow",)
    assert r.first == "62: missing return statement"


def test_parse_phase_error_is_parse():
    r = classify(raw("0\t0\tE\tP\tcompiler.err.illegal.start.of.expr\t3\tillegal start of expression", "0\t0\tT\t5"), False)
    assert r.status == "flagged" and r.kinds == ("parse",)


def test_type_and_other_errors_only_are_noted():
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.prob.found.req\t2\tincompatible types: java.util.ArrayList<java.lang.Integer>"
        " cannot be converted to java.util.List<java.lang.String>",
        "0\t0\tE\tA\tcompiler.err.does.not.override.abstract\t9\tX is not abstract and does not override abstract method y()",
        "0\t0\tT\t5",
    ), False)
    first = "2: incompatible types: java.util.ArrayList<java.lang.Integer> cannot be converted to java.util.List<java.lang.String>"
    assert r.status == "noted" and r.kinds == ("other", "type") and r.errors == 2 and r.first == first
    assert r.messages == (first, "9: X is not abstract and does not override abstract method y()")
    assert r.passes is True


def test_flagged_first_is_lowest_line_of_retry_kinds():
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.prob.found.req\t1\tincompatible types: a",
        "0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t20\tmissing return statement",
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t7\tcannot find symbol   symbol:   variable tx   location: class a.B",
        "0\t0\tT\t5",
    ), False)
    assert r.kinds == ("flow", "name", "type") and r.errors == 3
    assert r.first == "7: cannot find symbol: variable tx"
    assert r.messages == ("1: incompatible types: a", "7: cannot find symbol: variable tx", "20: missing return statement")


def test_messages_stop_at_the_cap():
    lines = [f"0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t{n}\tmissing return statement" for n in range(150, 0, -1)]
    r = classify(raw(*lines, "0\t0\tT\t5"), False)
    assert r.errors == 150 and len(r.messages) == check.MESSAGES_PER_FILE == 100
    assert (r.messages[0], r.messages[-1]) == ("1: missing return statement", "100: missing return statement")


@pytest.mark.parametrize("line", [
    "0\t0\tE\tA\tcompiler.err.doesnt.exist\t1\tpackage org.absent does not exist",
    # an import can supply these names (the scope below has `import x.*;`)
    "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t2\tcannot find symbol   symbol:   class Thing   location: class Env",
    "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t65\tcannot find symbol   symbol:   variable Binder   location: class Env",
    "0\t0\tE\tA\tcompiler.err.cant.access\t1\tcannot access a.B   bad class file: /x/a/B.class"
    "     class file has wrong version 69.0, should be 65.0",
    "0\t0\tE\tA\tcompiler.err.package.not.visible\t3\tpackage sun.nio.ch is not visible",
    "0\t0\tE\tA\tcompiler.err.error.reading.file\t1\terror reading /x/y.jar; zip END header not found",
    "0\t0\tE\tA\tcompiler.err.not.def.access.class.intf.cant.access\t4\tX.Y is not public in X; cannot be accessed from outside package",
])
def test_environment_errors_make_the_file_unchecked(line):
    scope = check.Scope(this="Env", on_demand=True)
    r = classify(raw(line, "0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t9\tmissing return statement", "0\t0\tT\t5"),
                 False, scope=scope)
    assert (r.status, r.reason, r.messages) == ("unchecked", "absent classes", ())


def test_absent_classes_first_is_the_lowest_line_of_the_environment_errors():
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t3\tmissing return statement",
        "0\t0\tE\tA\tcompiler.err.doesnt.exist\t12\tpackage org.b does not exist",
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t5\tcannot find symbol   symbol:   class Thing   location: class Env",
        "0\t0\tT\t5",
    ), False, scope=check.Scope(this="Env", imports=frozenset({"Thing"})))
    assert r == unchecked("absent classes", first="5: cannot find symbol: class Thing")


def test_old_source_keyword_identifier_is_unchecked():
    r = classify(raw(
        "0\t0\tE\tP\tcompiler.err.enum.as.identifier\t1\tas of release 5, 'enum' is a keyword, and may not be used as an identifier",
        "0\t0\tT\t5",
    ), False)
    assert r == unchecked("old source", first="1: as of release 5, 'enum' is a keyword, and may not be used as an identifier")


def test_crash_and_unfinished_files():
    assert classify(raw("0\t0\tX\tjava.lang.AssertionError: isSubtype UNKNOWN", "0\t0\tT\t5"), False) == unchecked(
        "javac crash", first="java.lang.AssertionError: isSubtype UNKNOWN"  # the cause shows in the report (#108)
    )
    assert classify(None, False) == unchecked("javac crash")
    assert classify(None, False, "java.lang.OutOfMemoryError: Java heap space") == unchecked(
        "javac crash", first="java.lang.OutOfMemoryError: Java heap space"
    )
    assert classify(None, True) == unchecked("timeout")
    assert classify(raw("0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t9\tm"), True) == unchecked("timeout")


def test_parse_results_keys_by_job_and_index_and_keeps_negative_lines():
    out = parse_results("1\t0\tE\tA\tcompiler.err.foo\t-1\tno position\n1\t0\tT\t3\n2\t4\tT\t1\nnoise\n")
    assert out[("1", 0)].errors == [("A", "compiler.err.foo", -1, "no position")]
    assert out[("1", 0)].done is True
    assert out[("2", 4)].done is True and out[("2", 4)].errors == []


def test_write_jobs_nul_tokens(tmp_path: Path):
    f = tmp_path / "jobs.bin"
    cp, src = Path("/cp/a.jar"), Path("/t/Ünï.java")
    write_jobs(f, [check.Job("0", (cp,), (src,)), check.Job("1", (), ())])
    assert f.read_bytes().split(b"\0") == [
        b"job", b"0", b"cp", str(cp).encode("utf-8"), b"src", str(src).encode("utf-8"), b"job", b"1", b"",
    ]


def test_summary_counts():
    s = summary([
        FileResult("clean"),
        FileResult("noted", kinds=("type",), errors=2),
        FileResult("flagged", kinds=("flow", "name"), errors=3, first="1: x"),
        unchecked("kotlin"),
        unchecked("newer class version", needs_java=25),
        unchecked("newer class version", needs_java=21),
    ])
    assert (s["checked"], s["flagged"], s["noted"], s["unchecked"]) == (3, 1, 1, 3)
    assert s["unchecked_reasons"] == {
        "absent classes": 0, "kotlin": 1, "newer class version": 2,
        "old source": 0, "javac crash": 0, "timeout": 0, "interrupted": 0,
    }
    assert s["kinds"] == {"parse": 0, "name": 1, "flow": 1, "type": 1, "other": 0}
    assert s["needs_java"] == 25


def test_summary_empty():
    s = summary([])
    assert s["checked"] == 0 and s["needs_java"] is None


def test_class_major():
    assert class_major(b"\xca\xfe\xba\xbe\x00\x00\x00\x45") == 69
    assert class_major(b"\xca\xfe\xba\xbe\x00\x00") is None
    assert class_major(b"PK\x03\x04\x00\x00\x00\x45") is None


def test_find_javac_reads_java_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    javac = home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    javac.write_text("")

    def fake_run(cmd, **kw):
        assert cmd == ["/shims/java", "-XshowSettings:properties", "-version"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="",
            stderr=f"Property settings:\n    java.home = {home}\n    java.vendor = X\nopenjdk version \"21.0.2\"\n",
        )

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    assert check.find_javac("/shims/java") == javac


def test_find_javac_none_for_a_jre(tmp_path: Path, monkeypatch):
    home = tmp_path / "jre"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setattr(
        check.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=f"    java.home = {home}\n"),
    )
    assert check.find_javac("java") is None


def test_find_javac_none_when_java_fails(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no java")

    monkeypatch.setattr(check.subprocess, "run", boom)
    assert check.find_javac("java") is None


def test_install_reuses_an_existing_install(tmp_path: Path, monkeypatch):
    digest = hashlib.sha256(check.SOURCE.read_bytes()).hexdigest()[:12]
    target = tmp_path / f"{digest}-21"
    target.mkdir()
    (target / check.JAR).write_bytes(b"x")
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: pytest.fail("must not compile"))
    assert check.install(Path("/jdk/bin/javac"), 21, root=tmp_path) == target


def test_install_failure_raises_check_error_and_cleans_up(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        check.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Checker.java:1: error: boom"),
    )
    with pytest.raises(check.CheckError, match="boom"):
        check.install(Path("/jdk/bin/javac"), 21, root=tmp_path)
    assert not list(tmp_path.glob(".tmp-*"))


def test_install_cleans_up_when_a_signal_stops_the_compile(tmp_path: Path, monkeypatch):
    """Review S4: SIGTERM ends the run like Ctrl-C; the javac build left its temp folder."""
    def interrupted(cmd, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(check.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        check.install(Path("/jdk/bin/javac"), 21, root=tmp_path)
    assert not list(tmp_path.glob(".tmp-*"))


def test_install_os_error_raises_check_error(tmp_path: Path):
    root = tmp_path / "not-a-dir"
    root.write_text("x")  # the cache root cannot be made: the run must go on without the check
    with pytest.raises(check.CheckError, match="checker install failed"):
        check.install(Path("/jdk/bin/javac"), 21, root=root)


def test_build_command_with_and_without_cds(tmp_path: Path):
    inst = tmp_path / "abc123-21"
    jobs, res = tmp_path / "j", tmp_path / "r"
    assert check.build_command("java", inst, jobs, res, threads=3) == [
        "java", "-XX:ActiveProcessorCount=3", "-cp", str(inst / "checker.jar"), "Checker", str(jobs), str(res), "3",
    ]
    cmd = check.build_command("java", inst, jobs, res, threads=2, cds_dir=tmp_path / "cds")
    assert cmd[2:4] == [
        "-XX:+AutoCreateSharedArchive",
        f"-XX:SharedArchiveFile={tmp_path / 'cds' / 'checker-abc123-21.jsa'}",
    ]


def test_build_command_heap_flag(tmp_path: Path):
    inst = tmp_path / "abc123-21"
    cmd = check.build_command(
        "java", inst, tmp_path / "j", tmp_path / "r", threads=3, cds_dir=tmp_path / "cds", heap_mb=2048
    )
    assert cmd[:5] == [
        "java", "-XX:ActiveProcessorCount=3", "-Xmx2048m", "-XX:+DisplayVMOutputToStderr",
        "-XX:+AutoCreateSharedArchive",
    ]


def test_run_check_maps_results_and_missing_lines(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_process(cmd, timeout, on_stderr_line=None):
        seen["cmd"] = cmd
        Path(cmd[-2]).write_text(
            "0\t0\tT\t1\n"
            "0\t1\tE\tA\tcompiler.err.missing.ret.stmt\t3\tmissing return statement\n"
            "0\t1\tT\t2\n"
        )
        return 0, False, "", None, False

    monkeypatch.setattr(check.engines, "run_process", fake_process)
    jobs = [check.Job("0", (tmp_path / "a.jar",), (tmp_path / "A.java", tmp_path / "B.java", tmp_path / "C.java"))]
    out = check.run_check(jobs, timeout=5, cpu_budget=2, java="java", install=tmp_path / "inst", tmp_root=tmp_path)
    assert [r.status for r in out[0]] == ["clean", "flagged", "unchecked"]
    assert out[0][2].reason == "javac crash"
    assert seen["cmd"][-1] == "2"


def test_run_check_deletes_its_work_folder(tmp_path: Path, monkeypatch):
    """#97: each checker JVM left its jobs file and results file in the temp root until the end of the run."""

    def fake_process(cmd, timeout, on_stderr_line=None):
        Path(cmd[-2]).write_text("0\t0\tT\t1\n")
        return 0, False, "", None, False

    monkeypatch.setattr(check.engines, "run_process", fake_process)
    root = tmp_path / "tmp"
    root.mkdir()
    out = check.run_check([check.Job("0", (), (tmp_path / "A.java",))], timeout=5, cpu_budget=1, java="java",
                          install=tmp_path, tmp_root=root)
    assert [r.status for r in out[0]] == ["clean"] and list(root.iterdir()) == []


def test_run_check_gives_the_checker_jvm_its_heap(tmp_path: Path, monkeypatch):
    cmds = []

    def fake_process(cmd, timeout, on_stderr_line=None):
        cmds.append(cmd)
        return 0, False, "", None, False

    monkeypatch.setattr(check.engines, "run_process", fake_process)
    jobs = [check.Job("0", (), (tmp_path / "A.java",))]
    for heap in (1660, None):
        check.run_check(jobs, timeout=5, cpu_budget=2, java="java", install=tmp_path, tmp_root=tmp_path, heap_mb=heap)
    assert "-Xmx1660m" in cmds[0]
    assert not any(t.startswith("-Xmx") for t in cmds[1])


def test_run_check_timeout_marks_unfinished_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(check.engines, "run_process", lambda cmd, timeout, on_stderr_line=None: (-9, True, "", None, False))
    jobs = [check.Job("0", (), (tmp_path / "A.java",))]
    out = check.run_check(jobs, timeout=5, cpu_budget=1, java="java", install=tmp_path, tmp_root=tmp_path)
    assert out == [[unchecked("timeout")]]


def test_run_check_marks_the_files_of_a_killed_checker_interrupted(tmp_path: Path, monkeypatch):
    """#101: after Ctrl-C, the files without a result line got the reason "javac crash"."""

    def killed(cmd, timeout, on_stderr_line=None):
        Path(cmd[-2]).write_text("0\t0\tT\t1\n")  # the result of A; then Ctrl-C closes the registry and kills the JVM
        check.engines.PROCESSES.kill_all()
        return -9, False, "", None, False

    monkeypatch.setattr(check.engines, "run_process", killed)
    try:
        out = check.run_check([check.Job("0", (), (tmp_path / "A.java", tmp_path / "B.java"))], timeout=5,
                              cpu_budget=1, java="java", install=tmp_path, tmp_root=tmp_path)
    finally:
        check.engines.PROCESSES.reset()
    assert [(r.status, r.reason) for r in out[0]] == [("clean", None), ("unchecked", "interrupted")]


def _class_file(name: str, long_first: bool = False) -> bytes:
    """A minimal class file whose this_class is name; long_first puts a Long (two pool slots) first."""
    utf = name.encode()
    pool = b"\x05" + bytes(8) if long_first else b""
    first = 3 if long_first else 1
    pool += b"\x01" + len(utf).to_bytes(2, "big") + utf + b"\x07" + first.to_bytes(2, "big")
    count = first + 2
    return (b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + count.to_bytes(2, "big") + pool
            + b"\x00\x21" + (first + 1).to_bytes(2, "big") + b"\x00\x00")


def test_class_name_reads_this_class():
    assert check.class_name(_class_file("p/A")) == "p/A"
    assert check.class_name(_class_file("q/B$1", long_first=True)) == "q/B$1"
    assert check.class_name(b"not a class") is None
    assert check.class_name(_class_file("p/A")[:12]) is None


def test_run_check_gives_the_crash_text(tmp_path: Path, monkeypatch):
    """#108: a checker JVM that stops with no result line shows its cause, e.g. an out-of-memory error."""
    tail = "Exception in thread \"main\" java.lang.OutOfMemoryError: Java heap space\n"
    monkeypatch.setattr(check.engines, "run_process", lambda cmd, timeout: (1, False, tail, None, True))
    out = check.run_check([check.Job("0", (), (tmp_path / "A.java",))], timeout=5, cpu_budget=1,
                          java="java", install=tmp_path, tmp_root=tmp_path)
    assert out == [[unchecked("javac crash", first='Exception in thread "main" java.lang.OutOfMemoryError: Java heap space')]]


def test_run_check_gives_the_exception_line_of_a_stack_trace(tmp_path: Path, monkeypatch):
    """Review C7: after an exception in the main thread of the checker, the last line of stderr is a stack frame."""
    cause = 'Exception in thread "main" java.nio.file.FileSystemException: /r/results.tsv: No space left on device'
    tail = cause + "\n\tat java.base/sun.nio.fs.UnixException.translateToIOException(UnixException.java:100)\n" \
        "\tat Checker.main(Checker.java:62)\n"
    monkeypatch.setattr(check.engines, "run_process", lambda cmd, timeout: (1, False, tail, None, False))
    out = check.run_check([check.Job("0", (), (tmp_path / "A.java",))], timeout=5, cpu_budget=1,
                          java="java", install=tmp_path, tmp_root=tmp_path)
    assert out == [[unchecked("javac crash", first=cause)]]


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script stands in for java")
def test_find_javac_reads_a_property_dump_that_is_no_utf8(tmp_path: Path):
    """#108: a property value in another encoding must not stop the run."""
    jdk = tmp_path / "jdk"
    (jdk / "bin").mkdir(parents=True)
    (jdk / "bin/javac").write_text("")
    java = tmp_path / "java"
    java.write_bytes(b"#!/bin/sh\nprintf '    java.home = " + str(jdk).encode() + b"\\n    user.dir = /x\\377y\\n' >&2\n")
    java.chmod(0o755)
    assert check.find_javac(str(java)) == jdk / "bin/javac"


def test_parse_error_flags_also_next_to_an_absent_class():
    """#108: a parse error never depends on the classpath, e.g. CFR's `** GOTO lbl12`."""
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.doesnt.exist\t1\tpackage org.absent does not exist",
        "0\t0\tE\tP\tcompiler.err.illegal.start.of.expr\t9\tillegal start of expression",
        "0\t0\tT\t5",
    ), False)
    assert (r.status, r.kinds, r.first) == ("flagged", ("other", "parse"), "9: illegal start of expression")



def test_a_parse_error_next_to_an_absent_class_is_the_first_error():
    """Review C6: a parse error empties the absent classes, but an absent class is no engine error.

    junit-platform-commons with JD: the flag came from line 176, but the first error was line 24.
    """
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t24\tcannot find symbol   symbol:   class API"
        "   location: class p.A",
        "0\t0\tE\tA\tcompiler.err.unreported.exception.need.to.catch.or.throw\t30\tunreported exception"
        " java.io.IOException; must be caught or declared to be thrown",
        "0\t0\tE\tP\tcompiler.err.expected\t176\t-> expected",
        "0\t0\tT\t5",
    ), False, scope=check.Scope(this="p.A", imports=frozenset({"API"})))
    assert (r.first, r.bytecode_only) == (
        "30: unreported exception java.io.IOException; must be caught or declared to be thrown", True)
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t24\tcannot find symbol   symbol:   class API"
        "   location: class p.A",
        "0\t0\tE\tP\tcompiler.err.expected\t176\t-> expected",
        "0\t0\tT\t5",
    ), False, scope=check.Scope(this="p.A", imports=frozenset({"API"})))
    assert (r.status, r.first, r.bytecode_only) == ("flagged", "176: -> expected", False)


@pytest.mark.parametrize("message, scope, absent", [
    # an unqualified name: absent only when an import can supply it
    ("symbol:   class Bar   location: class p.E", check.Scope(this="p.E"), False),
    ("symbol:   class Bar   location: class p.E", check.Scope(this="p.E", imports=frozenset({"Bar"})), True),
    ("symbol:   variable a   location: class p.E", check.Scope(this="p.E", imports=frozenset({"a"})), True),
    ("symbol:   variable Bar   location: class p.E.In", check.Scope(this="p.E", on_demand=True), True),
    ("symbol:   method g()   location: class p.E", check.Scope(this="p.E", on_demand=True), False),
    ("symbol:   method g()   location: class p.E", check.Scope(this="p.E", imports=frozenset({"g"})), True),
    # a member of another class: absent only when that class has two versions in the classpath
    ("symbol:   method baz()   location: variable foo of type lib.Foo",
     check.Scope(this="p.E", twice=lambda n: n == "lib.Foo"), True),
    ("symbol:   method baz()   location: variable foo of type lib.Foo", check.Scope(this="p.E"), False),
    ("symbol:   method baz()   location: variable o of type java.lang.Object", check.Scope(this="p.E"), False),
    ("symbol:   class Out   location: class lib.Foo", check.Scope(this="p.E", twice=lambda n: True), True),
    # a class of another package
    ("symbol:   class Gone   location: package lib", check.Scope(this="p.E"), True),
])
def test_absent_name_rules(message, scope, absent):
    line = f"0\t0\tE\tA\tcompiler.err.cant.resolve.location\t3\tcannot find symbol   {message}"
    r = classify(raw(line, "0\t0\tT\t5"), False, scope=scope)
    assert (r.status == "unchecked") is absent


def test_scope_of_reads_the_package_the_imports_and_the_supertypes():
    text = ("package p.q;\nimport a.B;\nimport static c.D.e;\nimport f.*;\n\n"
            "class A extends x.Base<Map<String, B>> implements Run, java.io.Serializable {}\n")
    got = check.scope_of(text, "A")
    assert (got.this, got.imports, got.on_demand) == ("p.q.A", frozenset({"B", "e"}), True)
    assert got.supertypes == frozenset({"x.Base", "Run", "java.io.Serializable"})
    assert check.scope_of("class A {}", "A") == check.Scope(this="A")


def test_an_absent_supertype_supplies_unqualified_names():
    """javac reports an inherited member as absent when the supertype is absent."""
    scope = check.Scope(this="E", imports=frozenset({"Base"}), supertypes=frozenset({"Base"}))
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.doesnt.exist\t1\tpackage org.absent does not exist",
        "0\t0\tE\tA\tcompiler.err.cant.resolve\t2\tcannot find symbol   symbol: class Base",
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location.args\t3\tcannot find symbol   symbol:   method inherited()"
        "   location: class E",
        "0\t0\tT\t5",
    ), False, scope=scope)
    assert r.reason == "absent classes"


def _e(code: str, line: int, message: str) -> str:
    return f"0\t0\tE\tA\t{code}\t{line}\t{message}"


_NOT_FOUND = "cannot find symbol   symbol:   "
# Each case: a source file whose supertype is absent, and the errors of javac for it (review C1).
_ABSENT_SUPERTYPES = {
    "qualified in a present package": ("package p;\nclass Q extends java.util.AbsentBase {\n  void f() { inherited(); }\n}\n", [
        _e("compiler.err.cant.resolve.location", 2, _NOT_FOUND + "class AbsentBase   location: package java.util"),
        _e("compiler.err.cant.resolve.location.args", 3, _NOT_FOUND + "method inherited()   location: class p.Q"),
    ]),
    "qualified": ("package p;\nclass Base extends org.absent.Base {\n  void f() { inherited(); }\n}\n", [
        _e("compiler.err.doesnt.exist", 2, "package org.absent does not exist"),
        _e("compiler.err.cant.resolve.location.args", 3, _NOT_FOUND + "method inherited()   location: class p.Base"),
    ]),
    "nested": ("package p;\nimport org.absent.Outer;\nclass C extends Outer.Inner {\n  void f() { inherited(); }\n}\n", [
        _e("compiler.err.doesnt.exist", 2, "package org.absent does not exist"),
        _e("compiler.err.doesnt.exist", 3, "package Outer does not exist"),
        _e("compiler.err.cant.resolve.location.args", 4, _NOT_FOUND + "method inherited()   location: class p.C"),
    ]),
    "anonymous": ("package p;\nimport com.google.inject.AbstractModule;\nclass G {\n  Object m() {\n"
                  "    return new AbstractModule() {\n      protected void configure() { bind(String.class); }\n"
                  "    };\n  }\n}\n", [
        _e("compiler.err.doesnt.exist", 2, "package com.google.inject does not exist"),
        _e("compiler.err.cant.resolve.location", 5, _NOT_FOUND + "class AbstractModule   location: class p.G"),
        _e("compiler.err.cant.resolve.args", 6, _NOT_FOUND + "method bind(java.lang.Class<java.lang.String>)"),
    ]),
    "anonymous with arguments": (
        "package p;\nimport org.absent.CountingOutputStream;\nclass G2 {\n  Object m(java.io.OutputStream out) {\n"
        "    return new CountingOutputStream(wrap(out, \"(\" + 1 + \")\")) {\n"
        "      public void write(int b) { super.write(b); }\n    };\n  }\n}\n", [
            _e("compiler.err.doesnt.exist", 2, "package org.absent does not exist"),
            _e("compiler.err.cant.resolve.location", 5, _NOT_FOUND + "class CountingOutputStream   location: class p.G2"),
            _e("compiler.err.cant.resolve.args", 6, _NOT_FOUND + "method write(int)"),
        ]),
    "anonymous generic": (
        "package p;\nimport org.absent.TypeToken;\nclass G3 {\n  Object m() {\n"
        "    return new TypeToken<java.util.List<String>>() {\n      void f() { getType(); }\n    };\n  }\n}\n", [
            _e("compiler.err.doesnt.exist", 2, "package org.absent does not exist"),
            _e("compiler.err.cant.resolve.location", 5, _NOT_FOUND + "class TypeToken   location: class p.G3"),
            _e("compiler.err.cant.resolve.args", 6, _NOT_FOUND + "method getType()"),
        ]),
    "anonymous qualified": (
        "package p;\nclass G4 {\n  Object m() {\n    return new org.absent.Module() {\n      void f() { bind(); }\n"
        "    };\n  }\n}\n", [
            _e("compiler.err.doesnt.exist", 4, "package org.absent does not exist"),
            _e("compiler.err.cant.resolve.args", 5, _NOT_FOUND + "method bind()"),
        ]),
    "anonymous nested": (
        "package p;\nimport org.absent.Outer;\nclass G5 {\n  Object m() {\n    return new Outer.Inner() {\n"
        "      void f() { bind(); }\n    };\n  }\n}\n", [
            _e("compiler.err.doesnt.exist", 2, "package org.absent does not exist"),
            _e("compiler.err.doesnt.exist", 5, "package Outer does not exist"),
            _e("compiler.err.cant.resolve.args", 6, _NOT_FOUND + "method bind()"),
        ]),
}


@pytest.mark.parametrize("case", _ABSENT_SUPERTYPES)
def test_each_form_of_an_absent_supertype_supplies_unqualified_names(case):
    """Review C1: an absent supertype in an anonymous class, with a qualified name, or as a nested type."""
    text, lines = _ABSENT_SUPERTYPES[case]
    stem = text.split("class ", 1)[1].split()[0]
    r = classify(raw(*lines, "0\t0\tT\t5"), False, scope=check.scope_of(text, stem))
    assert r.reason == "absent classes", r


def test_an_absent_package_that_is_no_part_of_a_supertype_name_does_not_hide_an_engine_error():
    text = "package p;\nimport java.uti.Thing;\nclass A extends java.util.AbstractList {\n  int f() { return var2; }\n}\n"
    r = classify(raw(
        _e("compiler.err.doesnt.exist", 2, "package java.uti does not exist"),
        _e("compiler.err.cant.resolve.location", 4, _NOT_FOUND + "variable var2   location: class p.A"),
        "0\t0\tT\t5",
    ), False, scope=check.scope_of(text, "A"))
    assert (r.status, r.first) == ("flagged", "4: cannot find symbol: variable var2")


def test_scope_of_reads_the_types_of_anonymous_classes():
    text = ("class A {\n  Object[] f() {\n    Runnable r = new Runnable() { public void run() {} };\n"
            "    Object o = new Thread(\"(\" + g(1, h(2))) {\n    };\n    int[] a = new int[] {1};\n"
            "    Object b = new Base(\"\\\"(\", ')') {};\n"
            "    return new Object[] { new StringBuilder(), new java.util.ArrayList<java.util.Map<String, A>>() {} };\n"
            "  }\n}\n")
    assert check.scope_of(text, "A").supertypes == frozenset({"Runnable", "Thread", "Base", "java.util.ArrayList"})


def test_an_engine_error_in_an_anonymous_class_of_a_present_type_flags_the_file():
    text = "package p;\nclass J {\n  Runnable m() {\n    return new Runnable() {\n      public void run() { f(var2); }\n    };\n  }\n}\n"
    r = classify(raw(_e("compiler.err.cant.resolve", 5, _NOT_FOUND + "variable var2"), "0\t0\tT\t5"), False,
                 scope=check.scope_of(text, "J"))
    assert (r.status, r.first) == ("flagged", "5: cannot find symbol: variable var2")


def test_a_same_package_class_that_the_class_file_names_is_absent():
    """Review C3: a split package or a -tests.jar: javac needs a class of the same package that the
    classpath lacks. The class file names it, so the name is no engine error."""
    line = _e("compiler.err.cant.resolve.location", 5, _NOT_FOUND + "class B   location: class p.A")
    as_variable = _e("compiler.err.cant.resolve.location", 6, _NOT_FOUND + "variable B   location: class p.A")
    for lines in ([line], [line, as_variable]):
        r = classify(raw(*lines, "0\t0\tT\t9"), False, scope=check.Scope(this="p.A", same_package=frozenset({"B"})))
        assert r.reason == "absent classes"
    r = classify(raw(line, "0\t0\tT\t9"), False, scope=check.Scope(this="p.A", same_package=frozenset({"C"})))
    assert r.status == "flagged"


def test_package_refs_reads_the_classes_of_the_own_package_that_a_class_file_names():
    data = _class_with("p/A", interfaces=("p/B", "q/C", "[Lp/D;"), utf8=(
        "(Lp/E;Lp/q/F;)V", "Ljava/util/List<Lp/G$In;>;", "p/H", "Lp/$Util;", "Lxp/I;",
    ))
    assert check.package_refs(data) == frozenset({"A", "B", "D", "E", "G", "G$In", "$Util"})
    assert check.package_refs(_class_with("A", interfaces=("B", "q/C"), utf8=("(LD;)V", "E"))) == frozenset({"A", "B", "D"})
    assert check.package_refs(b"not a class") == frozenset()


def test_run_check_reads_the_package_refs_only_for_a_file_with_an_absent_name(tmp_path: Path, monkeypatch):
    lines = {"A.java": _e("compiler.err.cant.resolve.location", 1, _NOT_FOUND + "class B   location: class p.A"),
             "C.java": _e("compiler.err.missing.ret.stmt", 1, "missing return statement")}

    def fake_process(cmd, timeout, on_stderr_line=None):
        Path(cmd[-2]).write_text("".join(
            line.replace("0\t0\t", f"0\t{i}\t", 1) + f"\n0\t{i}\tT\t1\n" for i, line in enumerate(lines.values())))
        return 0, False, "", None, False

    asked: list = []

    def refs(paths):
        asked.append([p.name for p in paths])
        return {p: frozenset({"B"}) for p in paths}

    monkeypatch.setattr(check.engines, "run_process", fake_process)
    files = []
    for name in lines:
        (tmp_path / name).write_text("package p;\nclass X {}\n")
        files.append(tmp_path / name)
    out = check.run_check([check.Job("0", (), tuple(files), refs=refs)], timeout=5, cpu_budget=1, java="java",
                          install=tmp_path, tmp_root=tmp_path)
    assert [r.reason or r.status for r in out[0]] == ["absent classes", "flagged"]
    assert asked == [["A.java"]]


def test_a_member_of_an_own_class_with_an_absent_superclass_is_absent():
    """javac cannot find an inherited member when the superclass of the receiver's class is absent."""
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.cant.access\t18\tcannot access o.Tracker   class file for o.Tracker not found",
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location.args\t22\tcannot find symbol   symbol:   method close()"
        "   location: variable tracker of type p.OwnTracker",
        "0\t0\tT\t5",
    ), False, scope=check.Scope(this="p.E"))
    assert r.reason == "absent classes"


def test_a_name_error_next_to_an_absent_class_flags_the_file():
    """#108: an absent class must not hide an engine error such as Vineflower's renamed variable."""
    scope = check.Scope(this="E", twice=lambda name: name == "lib.Foo")
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t52\tcannot find symbol   symbol:   variable listx"
        "   location: class E",
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location.args\t53\tcannot find symbol   symbol:   method add(int)"
        "   location: variable list of type lib.Foo",
        "0\t0\tT\t5",
    ), False, scope=scope)
    assert (r.status, r.first) == ("flagged", "52: cannot find symbol: variable listx")


@pytest.mark.parametrize("code", [
    "compiler.err.cant.ref.non.effectively.final.var",
    "compiler.err.except.already.caught",
    "compiler.err.var.might.be.assigned.in.loop",
    "compiler.err.final.parameter.may.not.be.assigned",
])
def test_engine_flow_errors_get_a_retry(code):
    r = classify(raw(f"0\t0\tE\tA\t{code}\t4\tx", "0\t0\tT\t5"), False)
    assert (r.status, r.kinds) == ("flagged", ("flow",))


def test_package_in_another_module_is_an_absent_class():
    r = classify(raw("0\t0\tE\tA\tcompiler.err.package.in.other.module\t1\tpackage exists in another module: java.xml",
                     "0\t0\tT\t5"), False)
    assert r.reason == "absent classes"


def test_write_jobs_writes_the_release(tmp_path: Path):
    f = tmp_path / "jobs.bin"
    write_jobs(f, [check.Job("0", (), (Path("/t/A.java"),), release=8)])
    assert f.read_bytes() == b"job\x000\x00release\x008\x00src\x00/t/A.java\x00".replace(b"/", os.sep.encode())


UNREPORTED = ("0\t0\tE\tA\tcompiler.err.unreported.exception.need.to.catch.or.throw\t9\tunreported exception"
              " java.io.IOException; must be caught or declared to be thrown")
SAME_METHOD = "0\t0\tE\tA\tcompiler.err.already.defined\t12\tmethod get() is already defined in class p.A"


def test_bytecode_only_errors_of_a_kotlin_groovy_or_scala_class_are_noted():
    """#108: these languages throw undeclared checked exceptions and overload methods on the return type."""
    r = classify(raw(UNREPORTED, SAME_METHOD, "0\t0\tT\t5"), False, non_java=True)
    assert (r.status, r.kinds, r.errors) == ("noted", ("flow", "name"), 2)
    assert r.first == "9: unreported exception java.io.IOException; must be caught or declared to be thrown"


def test_bytecode_only_errors_of_a_java_class_are_flagged():
    """Lombok's @SneakyThrows has no marker: the retries stop at the same first error instead (#108)."""
    for line in (UNREPORTED, SAME_METHOD):
        r = classify(raw(line, "0\t0\tT\t5"), False)
        assert (r.status, r.bytecode_only) == ("flagged", True)
    r = classify(raw(UNREPORTED, "0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t3\tmissing return statement",
                     "0\t0\tT\t5"), False)
    assert (r.first, r.bytecode_only) == ("3: missing return statement", False)


def test_other_errors_of_a_kotlin_groovy_or_scala_class_are_flagged():
    r = classify(raw(
        UNREPORTED,
        "0\t0\tE\tA\tcompiler.err.already.defined\t14\tvariable x is already defined in method f()",
        "0\t0\tT\t5",
    ), False, non_java=True)
    assert (r.status, r.first) == ("flagged", "14: variable x is already defined in method f()")


def _class_with(name: str, interfaces: tuple[str, ...] = (), utf8: tuple[str, ...] = ()) -> bytes:
    """A minimal class file with interfaces and more Utf8 constants, e.g. an annotation descriptor."""
    pool, index = b"", {}
    for i, n in enumerate((name, *interfaces)):
        u = n.encode()
        pool += b"\x01" + len(u).to_bytes(2, "big") + u + b"\x07" + (2 * i + 1).to_bytes(2, "big")
        index[n] = 2 * i + 2
    for s in utf8:
        u = s.encode()
        pool += b"\x01" + len(u).to_bytes(2, "big") + u
    count = 2 * len(index) + len(utf8) + 1
    body = b"\x00\x21" + index[name].to_bytes(2, "big") + b"\x00\x00" + len(interfaces).to_bytes(2, "big")
    body += b"".join(index[n].to_bytes(2, "big") for n in interfaces) + bytes(6)
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + count.to_bytes(2, "big") + pool + body


def test_non_java_reads_the_markers_of_kotlin_groovy_and_scala():
    assert check.non_java(_class_with("p/K", utf8=("Lkotlin/Metadata;",)))
    assert check.non_java(_class_with("p/S", utf8=("Lscala/reflect/ScalaSignature;",)))
    assert check.non_java(_class_with("p/L", utf8=("Lscala/reflect/ScalaLongSignature;",)))
    assert check.non_java(_class_with("p/G", interfaces=("java/io/Serializable", "groovy/lang/GroovyObject")))


def test_non_java_is_false_for_java_classes_that_use_these_types():
    assert not check.non_java(_class_with("p/A", interfaces=("java/io/Serializable",)))
    assert not check.non_java(_class_with("p/J", utf8=("groovy/lang/GroovyObject", "()Lkotlin/Metadata;")))
    assert not check.non_java(b"not a class")
    assert not check.non_java(_class_with("p/G", interfaces=("groovy/lang/GroovyObject",))[:-12])



def _class_with_members(name: str, source_file: str) -> bytes:
    """A class file with one field and one method, each with an attribute, and a SourceFile attribute."""
    names = (name, "f", "I", "ConstantValue", "m", "()V", "Code", "SourceFile", source_file)
    pool = b"\x01" + len(name.encode()).to_bytes(2, "big") + name.encode() + b"\x07\x00\x01"
    for n in names[1:]:
        u = n.encode()
        pool += b"\x01" + len(u).to_bytes(2, "big") + u
    index = {n: i + 3 for i, n in enumerate(names[1:])}  # 1: the name, 2: its Class entry
    count = len(names) + 2
    u2 = lambda v: v.to_bytes(2, "big")  # noqa: E731
    field_ = u2(0) + u2(index["f"]) + u2(index["I"]) + u2(1) + u2(index["ConstantValue"]) + (2).to_bytes(4, "big") + u2(1)
    code = bytes(12)
    method = u2(0) + u2(index["m"]) + u2(index["()V"]) + u2(1) + u2(index["Code"]) + len(code).to_bytes(4, "big") + code
    source = u2(index["SourceFile"]) + (2).to_bytes(4, "big") + u2(index[source_file])
    body = b"\x00\x21" + u2(2) + u2(0) + u2(0) + u2(1) + field_ + u2(1) + method + u2(1) + source
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + u2(count) + pool + body


def test_non_java_reads_the_source_file_attribute():
    """Review C8: a Groovy script or a Groovy subclass of a Groovy class has no GroovyObject in its own interfaces."""
    assert check.non_java(_class_with_members("p/Script1", "Script1.groovy"))
    assert check.non_java(_class_with_members("p/S", "S.scala"))
    assert not check.non_java(_class_with_members("p/J", "J.java"))
    assert not check.non_java(_class_with("p/J", utf8=("SourceFile", "J.groovy")))  # a constant, no attribute
    assert not check.non_java(_class_with_members("p/Script1", "Script1.groovy")[:-4])


def test_run_check_notes_bytecode_only_errors_of_the_non_java_files(tmp_path: Path, monkeypatch):
    def fake_process(cmd, timeout, on_stderr_line=None):
        Path(cmd[-2]).write_text("".join(
            UNREPORTED.replace("0\t0\t", f"0\t{i}\t", 1) + f"\n0\t{i}\tT\t1\n" for i in range(2)))
        return 0, False, "", None, False

    monkeypatch.setattr(check.engines, "run_process", fake_process)
    groovy, java = tmp_path / "G.java", tmp_path / "J.java"
    jobs = [check.Job("0", (), (groovy, java), non_java=frozenset({groovy}))]
    out = check.run_check(jobs, timeout=5, cpu_budget=1, java="java", install=tmp_path, tmp_root=tmp_path)
    assert [r.status for r in out[0]] == ["noted", "flagged"]
