import hashlib
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from decaf.cli import app
from decaf.engines import ENGINES, EngineSpec, active_specs, cache_status

runner = CliRunner(env={"COLUMNS": "200"})
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SHA = "b" * 64


def test_active_specs_merges_overrides():
    ov = {"cfr": {"version": "9.9", "url": "https://x.test/cfr-9.9.jar", "sha256": SHA}}
    specs = active_specs(ov)
    assert specs["cfr"].version == "9.9"
    assert specs["cfr"].url == "https://x.test/cfr-9.9.jar"
    assert specs["cfr"].min_java == ENGINES["cfr"].min_java  # not overridable, inherited
    assert specs["vineflower"] is ENGINES["vineflower"]
    assert active_specs(None) == dict(ENGINES)
    assert active_specs({}) == dict(ENGINES)


def test_cache_status_checks_presence_and_hash(tmp_path: Path):
    data = b"jarbytes"
    spec = EngineSpec(
        name="fake", version="1.0", url="https://x.test/fake.jar",
        sha256=hashlib.sha256(data).hexdigest(), min_java=11,
    )
    assert cache_status(spec, tmp_path) is False
    (tmp_path / "fake-1.0.jar").write_bytes(b"rotten")
    assert cache_status(spec, tmp_path) is False
    (tmp_path / "fake-1.0.jar").write_bytes(data)
    assert cache_status(spec, tmp_path) is True


def test_preflight_uses_override_spec(monkeypatch):
    import decaf.engines as engines
    from decaf.pipeline import Settings, _preflight_engines

    seen = {}

    def fake_ensure(spec, client, cache_dir=None):
        seen[spec.name] = spec.version
        return Path(f"/fake/{spec.name}.jar")

    monkeypatch.setattr(engines, "ensure_engine", fake_ensure)
    settings = Settings(
        input=Path("."), output=Path("out"), engine="cfr", fallback=False,
        engine_overrides={"cfr": {"version": "9.9", "url": "https://x.test/c.jar", "sha256": SHA}},
    )
    chain, jars = _preflight_engines(settings, java_major=21, client=None)
    assert chain == ["cfr"]
    assert seen["cfr"] == "9.9"
    assert jars["cfr"] == Path("/fake/cfr.jar")


@pytest.fixture(autouse=True)
def _isolated_default_config(tmp_path: Path, monkeypatch):
    """CLI invocations without --config must never read the developer's real user config."""
    import decaf.config as config

    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "no-user-config.toml")


def _row(plain: str, name: str) -> str:
    return next(line for line in plain.splitlines() if name in line)


def test_engines_list_shows_pins_cache_and_java(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 17))
    data = b"vf-jar-bytes"
    digest = hashlib.sha256(data).hexdigest()
    cache = tmp_path / "engines"
    cache.mkdir()
    (cache / "vineflower-9.9.jar").write_bytes(data)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        f'[engines.vineflower]\nversion = "9.9"\nurl = "https://x.test/vf.jar"\nsha256 = "{digest}"\n'
    )
    result = runner.invoke(app, ["engines", "list", "--config", str(cfgf)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    vf = _row(plain, "vineflower")
    assert "9.9†" in vf and "yes" in vf          # override marker + cached (real hash match)
    assert "no" in _row(plain, "cfr")            # not cached
    assert "needs 21+" in _row(plain, "fernflower")  # java 17 too old
    assert str(tmp_path / "engines") in plain    # cache dir footer


def test_engines_list_without_java(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: None)
    result = runner.invoke(app, ["engines", "list"])
    assert result.exit_code == 0
    assert "not found" in ANSI.sub("", result.output)


def test_engines_word_hits_subcommand_not_run(tmp_path: Path):
    # spec edge: `decaf engines` reaches the subcommand group; a folder literally
    # named engines needs `decaf ./engines`
    result = runner.invoke(app, ["engines"])
    plain = ANSI.sub("", result.output)
    assert "does not exist" not in plain  # never treated as run's INPUT
    assert "list" in plain               # engines group help/usage


def test_engines_fetch_reports_and_exit_code(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    fetched = []

    def fake_ensure(spec, client, cache_dir=None):
        if spec.name == "cfr":
            raise engines.EngineError("cfr: download failed: boom")
        fetched.append(spec.name)
        return tmp_path / "engines" / f"{spec.name}-{spec.version}.jar"

    monkeypatch.setattr(engines, "ensure_engine", fake_ensure)
    result = runner.invoke(app, ["engines", "fetch"])
    assert result.exit_code == 1
    plain = ANSI.sub("", result.output)
    assert "downloaded" in plain and "cfr" in plain and "boom" in plain
    assert fetched == ["vineflower", "procyon", "fernflower", "jd"]

    fetched.clear()
    result = runner.invoke(app, ["engines", "fetch", "vineflower"])
    assert result.exit_code == 0
    assert fetched == ["vineflower"]


def test_engines_fetch_already_cached(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    data = b"vf-jar-bytes"
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "engines").mkdir()
    (tmp_path / "engines" / "vineflower-9.9.jar").write_bytes(data)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        f'[engines.vineflower]\nversion = "9.9"\nurl = "https://x.test/vf.jar"\nsha256 = "{digest}"\n'
    )
    monkeypatch.setattr(engines, "ensure_engine", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    result = runner.invoke(app, ["engines", "fetch", "vineflower", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert "already cached" in ANSI.sub("", result.output)
