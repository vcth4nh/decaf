"""Real engines on a class that has the name of an inner class but is top-level for the JVM (#206)."""

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
    "p/Foo.java": (
        "package p;\n\nclass Foo {\n    Runnable r = new Runnable() {\n        public void run() {\n"
        "            System.out.println(\"anonymous body\");\n        }\n    };\n}\n"
    ),
    # javac gives Foo$ no InnerClasses attribute, as scalac does for the module class of an object
    "p/Foo$.java": "package p;\n\npublic final class Foo$ {\n}\n",
    "p/Bar.java": "package p;\n\nclass Bar {\n}\n",
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
    assert sorted(p.name for p in (out / "p").iterdir()) == ["Bar.class", "Foo$.class", "Foo$1.class", "Foo.class"]
    return out


@pytest.fixture(params=["folder", "jar"])
def source(request, classes: Path, tmp_path: Path) -> Path:
    if request.param == "folder":
        return shutil.copytree(classes, tmp_path / "in")
    jar = tmp_path / "in/app.jar"
    jar.parent.mkdir()
    with zipfile.ZipFile(jar, "w") as zf:
        for c in sorted(classes.rglob("*.class")):
            zf.write(c, c.relative_to(classes).as_posix())
    return jar


def _run(input: Path, out: Path, *args: str) -> dict:
    result = runner.invoke(app, ["run", str(input), "-o", str(out), "--no-maven", *args])
    assert result.exit_code == 0, result.output
    (row,) = json.loads((out / "decaf-report.json").read_text())["artifacts"]
    return row


def _sources(out: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in out.rglob("*.java")}


@pytest.mark.parametrize("engine", ["cfr", "procyon"])
def test_a_class_that_is_top_level_for_the_jvm_gets_a_file(source: Path, tmp_path: Path, engine):
    """Before, CFR and Procyon wrote no Foo$.java from a class folder, and the row had 2 classes and no missing
    class (#206). Foo.java holds the anonymous class one time."""
    out = tmp_path / "out"
    row = _run(source, out, "--engine", engine, "--no-fallback")
    files = _sources(out)
    assert sorted(files) == ["Bar.java", "Foo$.java", "Foo.java"]
    assert files["Foo.java"].count("anonymous body") == 1
    assert (row["outcome"], row["classes"], row["missing_classes"]) == ("ok", 3, 0)


def test_the_default_chain_on_a_class_folder_has_no_missing_class(classes: Path, tmp_path: Path):
    out = tmp_path / "out"
    row = _run(shutil.copytree(classes, tmp_path / "in"), out)
    assert "Foo$.java" in _sources(out)
    assert (row["outcome"], row["classes"], row["missing_classes"]) == ("ok", 3, 0)
