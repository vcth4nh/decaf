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
    assert got["Stub.java"].status == "flagged" and got["Stub.java"].kinds == ("flow",)
    assert got["Kw.java"] == check.unchecked("old source")
    assert got["Env.java"] == check.unchecked("absent classes")
    assert got["Clean.java"].status == "clean"
    assert got["Noted.java"].status == "noted" and got["Noted.java"].kinds == ("type",)
    assert got["Ünï.java"].status == "clean"


def test_install_is_reused(checker):
    _, javac, install = checker
    major = int(install.name.rsplit("-", 1)[1])
    assert check.install(javac, major, root=install.parent) == install
