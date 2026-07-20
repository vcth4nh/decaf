from pathlib import Path

import pytest

import decaf.config as config
from decaf.config import MAVEN_CENTRAL, Config, ConfigError, load_config


def test_missing_default_config_gives_central_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "nope.toml")
    cfg = load_config(None)
    assert cfg == Config(repositories=(MAVEN_CENTRAL,))


def test_file_repos_ordered_before_central(tmp_path: Path):
    f = tmp_path / "config.toml"
    f.write_text('repositories = ["https://nexus.example.com/repo/"]\n')
    cfg = load_config(f)
    assert cfg.repositories == ("https://nexus.example.com/repo", MAVEN_CENTRAL)


def test_extra_repos_prepended_and_deduped(tmp_path: Path):
    f = tmp_path / "config.toml"
    f.write_text(f'repositories = ["https://a.example/m2", "{MAVEN_CENTRAL}"]\n')
    cfg = load_config(f, extra_repos=["https://b.example/m2", "https://a.example/m2"])
    assert cfg.repositories == (
        "https://b.example/m2",
        "https://a.example/m2",
        MAVEN_CENTRAL,
    )


def test_extra_repo_bad_scheme_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="http"):
        load_config(None, extra_repos=["htp://typo.example/m2"])


def test_bad_toml_raises(tmp_path: Path):
    f = tmp_path / "config.toml"
    f.write_text("repositories = [unclosed\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(f)


@pytest.mark.parametrize(
    "content,msg",
    [
        ('repositories = "https://a.example"', "must be a list"),
        ("repositories = [1, 2]", "must be strings"),
        ('repositories = ["ftp://a.example"]', "http"),
        ('unknown_key = true\nrepositories = []', "unknown key"),
    ],
)
def test_schema_errors(tmp_path: Path, content: str, msg: str):
    f = tmp_path / "config.toml"
    f.write_text(content + "\n")
    with pytest.raises(ConfigError, match=msg):
        load_config(f)


def test_explicit_missing_config_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "typo.toml")


SHA = "a" * 64


def test_engine_overrides_parsed(tmp_path: Path):
    f = tmp_path / "config.toml"
    f.write_text(
        "[engines.cfr]\n"
        'version = "0.153"\n'
        'url = "https://x.test/cfr-0.153.jar"\n'
        f'sha256 = "{SHA}"\n'
        "[engines.jd]\n"
        'version = "1.3.0"\n'
        'url = "https://x.test/jd-cli-1.3.0-dist.zip"\n'
        f'sha256 = "{SHA}"\n'
        f'download_sha256 = "{SHA}"\n'
        'archive_member = "jd-cli.jar"\n'
    )
    cfg = load_config(f)
    assert cfg.engine_overrides["cfr"] == {
        "version": "0.153", "url": "https://x.test/cfr-0.153.jar", "sha256": SHA,
    }
    assert cfg.engine_overrides["jd"]["archive_member"] == "jd-cli.jar"
    assert cfg.repositories == (MAVEN_CENTRAL,)


def test_no_engines_table_gives_empty_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "nope.toml")
    assert load_config(None).engine_overrides == {}


@pytest.mark.parametrize(
    "content,msg",
    [
        ("engines = 3", "must be a table"),
        ("[engines.nope]\n" f'version = "1"\nurl = "https://x.test/j.jar"\nsha256 = "{SHA}"', "unknown engine"),
        ("[engines.cfr]\n" f'url = "https://x.test/j.jar"\nsha256 = "{SHA}"', "missing key 'version'"),
        ("[engines.cfr]\n" f'version = "1"\nurl = "http://x.test/j.jar"\nsha256 = "{SHA}"', "https"),
        ('[engines.cfr]\nversion = "1"\nurl = "https://x.test/j.jar"\nsha256 = "ZZ"', "64 hex"),
        ("[engines.cfr]\n" f'version = "1"\nurl = "https://x.test/j.jar"\nsha256 = "{SHA}"\nbogus = "x"', "unknown key"),
        ('[engines.cfr]\nversion = 1\nurl = "https://x.test/j.jar"\nsha256 = "' + SHA + '"', "must be a string"),
    ],
)
def test_engine_override_schema_errors(tmp_path: Path, content: str, msg: str):
    f = tmp_path / "config.toml"
    f.write_text(content + "\n")
    with pytest.raises(ConfigError, match=msg):
        load_config(f)


def test_write_engine_pins_round_trip_preserves_repos(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('repositories = ["https://nexus.example.com/repo"]\n')
    pins = {"cfr": {"version": "0.153", "url": "https://x.test/cfr.jar", "sha256": SHA}}
    config.write_engine_pins(path, pins)
    cfg = load_config(path)
    assert cfg.engine_overrides == pins
    assert cfg.repositories[0] == "https://nexus.example.com/repo"


def test_write_engine_pins_creates_and_clears(tmp_path: Path):
    path = tmp_path / "sub" / "config.toml"
    pins = {"cfr": {"version": "0.153", "url": "https://x.test/cfr.jar", "sha256": SHA}}
    config.write_engine_pins(path, pins)
    assert load_config(path).engine_overrides == pins
    config.write_engine_pins(path, {})
    assert load_config(path).engine_overrides == {}
    assert "engines" not in path.read_text()


def test_write_engine_pins_bad_existing_toml_raises(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("repositories = [unclosed\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        config.write_engine_pins(path, {})
