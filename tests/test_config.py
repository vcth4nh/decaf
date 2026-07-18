from pathlib import Path

import pytest

from decaf.config import MAVEN_CENTRAL, Config, ConfigError, load_config


def test_missing_file_gives_central_only(tmp_path: Path):
    cfg = load_config(tmp_path / "nope.toml")
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
