"""Real engines on classes that are not at their package root (#199, #200, #170, #171, #173)."""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from decaf.cli import app

pytestmark = pytest.mark.slow

runner = CliRunner()

SOURCES = {
    "p/A.java": "package p;\n\npublic class A {\n    public int one() {\n        return 1;\n    }\n}\n",
    "q/B.java": (
        "package q;\n\npublic class B {\n    public static class Nested {\n"
        "        public int two() {\n            return 2;\n        }\n    }\n}\n"
    ),
    # Vineflower writes a file that the check flags: the method throws an undeclared checked exception (#174)
    "com/y/S.java": (
        "package com.y;\n\npublic class S {\n    @SuppressWarnings(\"unchecked\")\n"
        "    public static <T extends Throwable> void sneaky(Throwable t) throws T {\n        throw (T) t;\n    }\n\n"
        "    public static void use() {\n        S.<RuntimeException>sneaky(new Exception(\"x\"));\n    }\n}\n"
    ),
    # in a static method: in an instance method, JD 1.2.0 fails on the MethodParameters attribute (#170)
    "com/x/A.java": (
        "package com.x;\n\npublic class A {\n    public static Object make() {\n        return new Object() {\n"
        "            @Override\n            public String toString() {\n                return \"inner\";\n"
        "            }\n        };\n    }\n}\n"
    ),
}


@pytest.fixture(scope="module")
def classes(tmp_path_factory) -> Path:
    javac = shutil.which("javac")
    if javac is None:
        pytest.skip("javac not available")
    src = tmp_path_factory.mktemp("src")
    for rel, text in SOURCES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    out = tmp_path_factory.mktemp("classes")
    subprocess.run([javac, "--release", "11", "-d", str(out), *(str(src / rel) for rel in SOURCES)], check=True)
    return out


@pytest.fixture
def class_folder(classes: Path, tmp_path: Path) -> Path:
    root = tmp_path / "in"
    for package in ("p", "q"):
        shutil.copytree(classes / package, root / "target/classes" / package)
    return root


def _archive(classes: Path, path: Path, prefix: str, extra: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in (extra or {}).items():
            zf.writestr(name, text)
        for c in sorted((classes / "com/x").rglob("*.class")):
            zf.write(c, prefix + c.relative_to(classes).as_posix())
    return path


@pytest.fixture
def war(classes: Path, tmp_path: Path) -> Path:
    return _archive(classes, tmp_path / "in/app.war", "WEB-INF/classes/", {"WEB-INF/web.xml": "<web-app/>\n"})


def _run(input: Path, out: Path, *args: str) -> dict:
    result = runner.invoke(app, ["run", str(input), "-o", str(out), "--no-maven", *args])
    assert result.exit_code == 0, result.output
    (row,) = json.loads((out / "decaf-report.json").read_text())["artifacts"]
    return row


def _files(out: Path) -> list[str]:
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != "decaf-report.json")


@pytest.mark.parametrize("engine", ["cfr", "procyon"])
def test_a_class_folder_above_its_package_root_has_no_missing_class(class_folder: Path, tmp_path: Path, engine):
    """Before, CFR wrote _classes/p/A.java, and the row had 2 missing classes (#199)."""
    out = tmp_path / "out"
    row = _run(class_folder, out, "--engine", engine, "--no-fallback")
    assert (row["outcome"], row["classes"], row["missing_classes"], row["java_files"]) == ("ok", 2, 0, 2)
    assert _files(out) == ["_classes/target/classes/p/A.java", "_classes/target/classes/q/B.java"]


def test_the_fallback_after_cfr_writes_no_second_file_for_a_class_folder(class_folder: Path, tmp_path: Path):
    """Before, Vineflower retried both classes, and java_files was 4 (#199)."""
    out = tmp_path / "out"
    row = _run(class_folder, out, "--engine", "cfr")
    assert [a["engine"] for a in row["attempts"]] == ["cfr"]
    assert (row["java_files"], row["missing_classes"]) == (2, 0)
    assert _files(out) == ["_classes/target/classes/p/A.java", "_classes/target/classes/q/B.java"]


def test_jd_gives_a_class_folder_its_package_and_its_inner_classes(class_folder: Path, tmp_path: Path):
    """Before, JD wrote package target.classes.q; and no Nested (#200)."""
    out = tmp_path / "out"
    row = _run(class_folder, out, "--engine", "jd", "--no-fallback")
    assert (row["missing_classes"], row["flagged"]) == (0, [])
    text = (out / "_classes/target/classes/q/B.java").read_text()
    assert "package q;" in text and "class Nested" in text


@pytest.mark.parametrize("name, prefix", [("app.war", "WEB-INF/classes/"), ("boot.jar", "BOOT-INF/classes/")])
def test_jd_gives_the_classes_of_a_container_their_package_and_their_inner_classes(
    classes: Path, tmp_path: Path, name, prefix
):
    """Before, JD wrote package WEB-INF.classes.com.x; and return new Object(); (#170)."""
    archive = _archive(classes, tmp_path / "in" / name, prefix)
    out = tmp_path / "out"
    row = _run(archive, out, "--engine", "jd", "--no-fallback")
    assert (row["outcome"], row["missing_classes"], row["flagged"]) == ("ok", 0, [])
    text = (out / name / prefix / "com/x/A.java").read_text()
    assert "package com.x;" in text and '"inner"' in text


def test_procyon_writes_the_anonymous_class_of_a_war(war: Path, tmp_path: Path):
    """Before, Procyon wrote return new A$1(); and no body for the anonymous class (#171)."""
    out = tmp_path / "out"
    row = _run(war, out, "--engine", "procyon", "--no-fallback")
    assert row["flagged"] == []
    text = (out / "app.war/WEB-INF/classes/com/x/A.java").read_text()
    assert '"inner"' in text and "new A$1()" not in text


@pytest.mark.parametrize("engine", [
    "vineflower", "cfr", pytest.param("fernflower", marks=pytest.mark.xfail(
        reason="#169: Fernflower opens only .jar and .zip files", strict=True)), "procyon", "jd",
])
def test_each_engine_writes_the_source_of_a_war_class_at_its_entry_path(war: Path, tmp_path: Path, engine):
    """Before, CFR and Procyon wrote app.war/com/x/A.java in mirror mode (#173)."""
    out = tmp_path / "out"
    _run(war, out, "--engine", engine, "--no-fallback")
    assert _files(out) == ["app.war/WEB-INF/classes/com/x/A.java", "app.war/WEB-INF/web.xml"]


@pytest.mark.parametrize("engine", ["vineflower", "cfr"])
def test_merge_writes_a_class_folder_above_its_package_root_by_package(class_folder: Path, tmp_path: Path, engine):
    """Before, --merge wrote src/target/classes/p/A.java (#199)."""
    out = tmp_path / "out"
    row = _run(class_folder, out, "--merge", "--engine", engine, "--no-fallback")
    assert row["missing_classes"] == 0
    assert _files(out) == ["src/p/A.java", "src/q/B.java"]


@pytest.mark.parametrize("kind", ["war", "folder"])
def test_merge_report_path_is_the_path_below_src(classes: Path, tmp_path: Path, kind):
    """Before, the report gave WEB-INF/classes/com/y/S.java for the file src/com/y/S.java (#174)."""
    if kind == "war":
        source = tmp_path / "in/sneaky.war"
        source.parent.mkdir(parents=True)
        with zipfile.ZipFile(source, "w") as zf:
            zf.write(classes / "com/y/S.class", "WEB-INF/classes/com/y/S.class")
        rel = "sneaky.war"
    else:
        source = tmp_path / "in"
        (source / "target/classes/com/y").mkdir(parents=True)
        shutil.copyfile(classes / "com/y/S.class", source / "target/classes/com/y/S.class")
        rel = "_classes"
    out = tmp_path / "out"
    row = _run(source, out, "--merge", "--engine", "vineflower", "--no-fallback")
    assert [f["path"] for f in row["flagged"]] == ["com/y/S.java"]
    assert _files(out) == ["src/com/y/S.java"]
    shown = runner.invoke(app, ["report", str(out), "--artifact", rel])
    assert shown.exit_code == 0, shown.output
    assert "flagged com/y/S.java (vineflower)" in shown.output


@pytest.mark.parametrize("engine", ["cfr", "procyon", "jd"])
def test_the_sources_of_a_war_are_the_sources_of_a_plain_jar(classes: Path, tmp_path: Path, engine):
    """The engine gets the classes of WEB-INF/classes/ as a jar, so it writes the same text as for a plain jar. From a
    folder, CFR writes other text: other casts, and no last blank line (#173)."""
    entries = sorted(p for d in ("p", "q", "com/x") for p in (classes / d).glob("*.class"))
    jar, war = tmp_path / "in-jar/app.jar", tmp_path / "in-war/app.war"
    for path, prefix in ((jar, ""), (war, "WEB-INF/classes/")):
        path.parent.mkdir(parents=True)
        with zipfile.ZipFile(path, "w") as zf:
            for c in entries:
                zf.write(c, prefix + c.relative_to(classes).as_posix())
    _run(jar, tmp_path / "out-jar", "--engine", engine, "--no-fallback")
    _run(war, tmp_path / "out-war", "--engine", engine, "--no-fallback")
    from_jar, from_war = tmp_path / "out-jar/app.jar", tmp_path / "out-war/app.war/WEB-INF/classes"
    texts = {p.relative_to(from_jar).as_posix(): p.read_bytes() for p in from_jar.rglob("*.java")}
    assert sorted(texts) == ["com/x/A.java", "p/A.java", "q/B.java"]
    assert {p.relative_to(from_war).as_posix(): p.read_bytes() for p in from_war.rglob("*.java")} == texts
