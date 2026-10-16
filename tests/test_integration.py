import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from decaf import stubs
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
    result = runner.invoke(app, [str(input_dir), "-o", str(out), "--no-maven", "--merge"])
    assert result.exit_code == 0, result.output
    assert (out / "src/com/example/Greeter.java").is_file()
    report = json.loads((out / "decaf-report.json").read_text())
    assert report["totals"]["failed"] == 0
    rels = [a["rel"] for a in report["artifacts"]]
    assert "site.war!/WEB-INF/lib/greeter.jar" in rels

    out_mirror = tmp_path / "out-mirror"
    result_mirror = runner.invoke(
        app, [str(input_dir), "-o", str(out_mirror), "--no-maven"]
    )
    assert result_mirror.exit_code == 0, result_mirror.output
    assert (out_mirror / "site.war/WEB-INF/lib/greeter.jar/com/example/Greeter.java").is_file()


def test_cli_maven_first_real(tmp_path: Path):
    url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.13/slf4j-api-2.0.13.jar"
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        (input_dir / "slf4j-api-2.0.13.jar").write_bytes(client.get(url).content)
    out = tmp_path / "out"
    result = runner.invoke(app, [str(input_dir), "-o", str(out), "--merge"])
    assert result.exit_code == 0, result.output
    report = json.loads((out / "decaf-report.json").read_text())
    assert report["artifacts"][0]["method"] == "maven"
    assert report["artifacts"][0]["gav"] == "org.slf4j:slf4j-api:2.0.13"
    assert (out / "src/org/slf4j/Logger.java").is_file()


SPRING_BOOT_URL = (
    "https://repo1.maven.org/maven2/org/springframework/boot/spring-boot/3.5.12/spring-boot-3.5.12.jar"
)
SPRING_BOOT_SHA1 = "da8301111d026baad823ca00e19b636bc5ef0699"


def test_cli_reports_every_engine_stub_real(tmp_path: Path):
    """Vineflower stubs StructuredLoggingJsonProperties at default settings (#96)."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        data = client.get(SPRING_BOOT_URL).content
    assert hashlib.sha1(data).hexdigest() == SPRING_BOOT_SHA1
    (input_dir / "spring-boot-3.5.12.jar").write_bytes(data)
    out = tmp_path / "out"
    result = runner.invoke(app, [str(input_dir), "-o", str(out), "--no-maven"])
    assert result.exit_code == 0, result.output
    art = json.loads((out / "decaf-report.json").read_text())["artifacts"][0]
    assert art["attempts"][0]["engine"] == "vineflower" and art["attempts"][0]["stubbed"] >= 1
    root = out / "spring-boot-3.5.12.jar"
    marked = {s.path for name in ENGINE_ORDER for s in stubs.scan(root, name)}
    assert marked == {s["path"] for s in art["stubs"]}


def test_cli_caps_and_measures_engine_memory_real(fixture_jar, tmp_path: Path):
    """Each real engine JVM gets its share and reports its peak (#98)."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    shutil.copyfile(fixture_jar, input_dir / "greeter.jar")
    out = tmp_path / "out"
    result = runner.invoke(app, [str(input_dir), "-o", str(out), "--no-maven", "--memory", "2g"])
    assert result.exit_code == 0, result.output
    report = json.loads((out / "decaf-report.json").read_text())
    assert report["settings"]["memory_budget_mb"] == 2048
    attempts = [a for art in report["artifacts"] for a in art["attempts"]]
    assert attempts
    for a in attempts:
        assert a["heap_mb"] == 1748  # one job: 2048 - 300
        assert 0 < a["peak_rss_mb"] <= a["heap_mb"] + 300
