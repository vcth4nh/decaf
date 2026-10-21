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
    "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t2\tcannot find symbol   symbol:   class Thing   location: class Env",
    "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t65\tcannot find symbol   symbol:   variable Binder   location: class a.B",
    "0\t0\tE\tA\tcompiler.err.cant.access\t1\tcannot access a.B   bad class file: /x/a/B.class"
    "     class file has wrong version 69.0, should be 65.0",
    "0\t0\tE\tA\tcompiler.err.package.not.visible\t3\tpackage sun.nio.ch is not visible",
    "0\t0\tE\tA\tcompiler.err.error.reading.file\t1\terror reading /x/y.jar; zip END header not found",
    "0\t0\tE\tA\tcompiler.err.not.def.access.class.intf.cant.access\t4\tX.Y is not public in X; cannot be accessed from outside package",
])
def test_environment_errors_make_the_file_unchecked(line):
    r = classify(raw(line, "0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t9\tmissing return statement", "0\t0\tT\t5"), False)
    assert (r.status, r.reason, r.messages) == ("unchecked", "absent classes", ())


def test_absent_classes_first_is_the_lowest_line_of_the_environment_errors():
    r = classify(raw(
        "0\t0\tE\tA\tcompiler.err.missing.ret.stmt\t3\tmissing return statement",
        "0\t0\tE\tA\tcompiler.err.doesnt.exist\t12\tpackage org.b does not exist",
        "0\t0\tE\tA\tcompiler.err.cant.resolve.location\t5\tcannot find symbol   symbol:   class Thing   location: class Env",
        "0\t0\tT\t5",
    ), False)
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
        "old source": 0, "javac crash": 0, "timeout": 0,
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
