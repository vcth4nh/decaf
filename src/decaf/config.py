"""Config file loading and Maven repository list assembly."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import platformdirs

MAVEN_CENTRAL = "https://repo1.maven.org/maven2"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    repositories: tuple[str, ...]


def default_config_path() -> Path:
    return platformdirs.user_config_path("decaf") / "config.toml"


def load_config(path: Path | None = None, extra_repos: Sequence[str] = ()) -> Config:
    file_repos: list[str] = []
    cfg_path = path or default_config_path()
    if cfg_path.is_file():
        try:
            data = tomllib.loads(cfg_path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path}: invalid TOML: {exc}") from exc
        for key in data:
            if key != "repositories":
                raise ConfigError(f"{cfg_path}: unknown key {key!r}")
        repos = data.get("repositories", [])
        if not isinstance(repos, list):
            raise ConfigError(f"{cfg_path}: repositories must be a list")
        if not all(isinstance(r, str) for r in repos):
            raise ConfigError(f"{cfg_path}: repositories entries must be strings")
        for r in repos:
            if not r.startswith(("http://", "https://")):
                raise ConfigError(f"{cfg_path}: repository {r!r} must be an http(s) URL")
        file_repos = repos

    ordered: list[str] = []
    for repo in [*extra_repos, *file_repos, MAVEN_CENTRAL]:
        repo = repo.rstrip("/")
        if repo not in ordered:
            ordered.append(repo)
    return Config(repositories=tuple(ordered))
