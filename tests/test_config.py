import os
import sys
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
        ("[engines.cfr]\n" f'version = "1"\nurl = "https://x.test/j.jar"\nsha256 = "{SHA}"\nmin_java = 99', "unknown key"),
        ("[engines.cfr]\n" f'version = "1"\nurl = "https://x.test/j.jar"\nsha256 = "{SHA}"\nmain_class = "X"', "unknown key"),
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


def test_write_engine_pins_rejects_malformed_pin(tmp_path: Path):
    path = tmp_path / "config.toml"
    bad = {"cfr": {"version": "0.153", "url": "https://x.test/cfr.jar", "sha256": "nothex"}}
    with pytest.raises(ConfigError, match="64 hex"):
        config.write_engine_pins(path, bad)
    assert not path.exists()


def test_write_engine_pins_rejects_unknown_engine(tmp_path: Path):
    path = tmp_path / "config.toml"
    bad = {"nope": {"version": "1.0", "url": "https://x.test/n.jar", "sha256": SHA}}
    with pytest.raises(ConfigError, match="unknown engine"):
        config.write_engine_pins(path, bad)
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_write_engine_pins_keeps_the_file_mode(tmp_path: Path):
    """#115: the config can hold repository credentials, so a pin write must not widen its mode."""
    path = tmp_path / "config.toml"
    path.write_text('repositories = ["https://deploy:s3cr3t@nexus.example/m2"]\n')
    path.chmod(0o600)
    config.write_engine_pins(path, {"cfr": {"version": "0.153", "url": "https://x.test/cfr.jar", "sha256": SHA}})
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_write_engine_pins_writes_through_a_symlink(tmp_path: Path):
    """#115: a config that is a symlink, for example into a dotfiles folder, stays a symlink."""
    target = tmp_path / "dotfiles/decaf.toml"
    target.parent.mkdir()
    target.write_text('repositories = ["https://nexus.example/m2"]\n')
    link = tmp_path / "config.toml"
    link.symlink_to(target)
    pins = {"cfr": {"version": "0.153", "url": "https://x.test/cfr.jar", "sha256": SHA}}
    config.write_engine_pins(link, pins)
    assert link.is_symlink() and load_config(target).engine_overrides == pins


def test_repository_url_errors_hide_the_credentials(tmp_path: Path, monkeypatch):
    """#115: an error message goes to the terminal and to CI logs."""
    f = tmp_path / "config.toml"
    f.write_text('repositories = ["ftp://deploy:s3cr3t@nexus.example/m2"]\n')
    with pytest.raises(ConfigError, match="ftp://nexus.example/m2") as err:
        load_config(f)
    assert "s3cr3t" not in str(err.value)
    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="nexus.example/m2") as err:
        load_config(None, extra_repos=["deploy:s3cr3t@nexus.example/m2"])
    assert "s3cr3t" not in str(err.value)


def test_config_is_utf8_on_each_platform(tmp_path: Path):
    """The config is TOML, and TOML is UTF-8, also where the locale codec is not (Windows)."""
    path = tmp_path / "config.toml"
    path.write_bytes('repositories = ["https://nexus.example/dépôt"]\n'.encode())
    pins = {"cfr": {"version": "0.153", "url": "https://x.test/cfr-é.jar", "sha256": SHA}}
    config.write_engine_pins(path, pins)
    cfg = load_config(path)
    assert cfg.repositories[0] == "https://nexus.example/dépôt" and cfg.engine_overrides == pins
    assert "dépôt".encode() in path.read_bytes()


def test_config_that_is_not_utf8_gives_a_config_error(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_bytes(b'repositories = ["https://nexus.example/\xff"]\n')
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)
    with pytest.raises(ConfigError, match="UTF-8"):
        config.write_engine_pins(path, {})


def test_central_by_its_other_name_is_one_repository(tmp_path: Path):
    """repo.maven.apache.org and repo1.maven.org serve the same repository: two probes for each miss."""
    f = tmp_path / "config.toml"
    f.write_text('repositories = ["https://nexus.example/m2", "https://repo.maven.apache.org/maven2/"]\n')
    assert load_config(f).repositories == ("https://nexus.example/m2", "https://repo.maven.apache.org/maven2")
