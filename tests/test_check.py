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
    assert r.status == "noted" and r.kinds == ("other", "type") and r.errors == 2 and r.first is None
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
    assert r == unchecked("absent classes")


def test_old_source_keyword_identifier_is_unchecked():
    r = classify(raw(
        "0\t0\tE\tP\tcompiler.err.enum.as.identifier\t1\tas of release 5, 'enum' is a keyword, and may not be used as an identifier",
        "0\t0\tT\t5",
    ), False)
    assert r == unchecked("old source")


def test_crash_and_unfinished_files():
    assert classify(raw("0\t0\tX\tjava.lang.AssertionError: isSubtype UNKNOWN", "0\t0\tT\t5"), False) == unchecked("javac crash")
    assert classify(None, False) == unchecked("javac crash")
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
