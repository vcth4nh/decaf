import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from decaf.cli import app
from decaf.engines import ENGINE_ORDER, ENGINES, ensure_engine, find_java, run_engine

pytestmark = [pytest.mark.slow, pytest.mark.network]

runner = CliRunner()


@pytest.fixture(scope="module")
def engine_jars() -> dict[str, Path]:
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        return {name: ensure_engine(spec, client) for name, spec in ENGINES.items()}


@pytest.mark.parametrize("name", ENGINE_ORDER)
def test_each_engine_decompiles_fixture(name, engine_jars, fixture_jar, tmp_path: Path):
    found = find_java()
    assert found is not None, "java required"
    java_exe, major = found
    spec = ENGINES[name]
    if spec.min_java > major:
        pytest.skip(f"{name} needs Java {spec.min_java}, runtime is {major}")
    res = run_engine(spec, engine_jars[name], fixture_jar, tmp_path / name, timeout=180, java=java_exe)
    assert res.timed_out is False
    assert res.java_files >= 2, res.stderr_tail
    greeter = tmp_path / name / "com/example/Greeter.java"
    assert greeter.is_file(), f"{name} did not produce Greeter.java"
    text = greeter.read_text()
    assert "Greeter" in text and "Hello" in text


def test_cli_end_to_end_real_engine(fixture_jar, tmp_path: Path):
    input_dir = tmp_path / "in"
    (input_dir / "libs").mkdir(parents=True)
    shutil.copyfile(fixture_jar, input_dir / "libs/greeter.jar")
    war = input_dir / "site.war"
    with zipfile.ZipFile(war, "w") as zf:
        with zipfile.ZipFile(fixture_jar) as src:
            for info in src.infolist():
                if info.filename.endswith(".class"):
                    zf.writestr("WEB-INF/classes/" + info.filename, src.read(info))
        zf.writestr("WEB-INF/lib/greeter.jar", fixture_jar.read_bytes())
    out = tmp_path / "out"
    result = runner.invoke(app, [str(input_dir), "-o", str(out), "--no-maven"])
    assert result.exit_code == 0, result.output
    assert (out / "src/com/example/Greeter.java").is_file()
    report = json.loads((out / "decaf-report.json").read_text())
    assert report["totals"]["failed"] == 0
    rels = [a["rel"] for a in report["artifacts"]]
    assert "site.war!/WEB-INF/lib/greeter.jar" in rels


def test_cli_maven_first_real(tmp_path: Path):
    url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.13/slf4j-api-2.0.13.jar"
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        (input_dir / "slf4j-api-2.0.13.jar").write_bytes(client.get(url).content)
    out = tmp_path / "out"
    result = runner.invoke(app, [str(input_dir), "-o", str(out)])
    assert result.exit_code == 0, result.output
    report = json.loads((out / "decaf-report.json").read_text())
    assert report["artifacts"][0]["method"] == "maven"
    assert report["artifacts"][0]["gav"] == "org.slf4j:slf4j-api:2.0.13"
    assert (out / "src/org/slf4j/Logger.java").is_file()
