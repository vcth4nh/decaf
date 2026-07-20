"""Config file loading and Maven repository list assembly."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs
import tomli_w

MAVEN_CENTRAL = "https://repo1.maven.org/maven2"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    repositories: tuple[str, ...]
    engine_overrides: dict[str, dict[str, str]] = field(default_factory=dict)


def default_config_path() -> Path:
    return platformdirs.user_config_path("decaf") / "config.toml"


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OVERRIDE_REQUIRED = ("version", "url", "sha256")
_OVERRIDE_OPTIONAL = ("download_sha256", "archive_member")


def _parse_engines(cfg_path: Path, raw: object) -> dict[str, dict[str, str]]:
    from .engines import ENGINES

    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: engines must be a table")
    overrides: dict[str, dict[str, str]] = {}
    for name, entry in raw.items():
        if name not in ENGINES:
            raise ConfigError(f"{cfg_path}: unknown engine {name!r}")
        if not isinstance(entry, dict):
            raise ConfigError(f"{cfg_path}: engines.{name} must be a table")
        for key in entry:
            if key not in _OVERRIDE_REQUIRED + _OVERRIDE_OPTIONAL:
                raise ConfigError(f"{cfg_path}: engines.{name}: unknown key {key!r}")
        for key in _OVERRIDE_REQUIRED:
            if key not in entry:
                raise ConfigError(f"{cfg_path}: engines.{name}: missing key {key!r}")
        for key, value in entry.items():
            if not isinstance(value, str):
                raise ConfigError(f"{cfg_path}: engines.{name}: {key} must be a string")
        if not entry["url"].startswith("https://"):
            raise ConfigError(f"{cfg_path}: engines.{name}: url must be an https URL")
        for key in ("sha256", "download_sha256"):
            if key in entry and not _SHA256_RE.fullmatch(entry[key]):
                raise ConfigError(f"{cfg_path}: engines.{name}: {key} must be 64 hex chars")
        overrides[name] = dict(entry)
    return overrides


def load_config(path: Path | None = None, extra_repos: Sequence[str] = ()) -> Config:
    file_repos: list[str] = []
    engine_overrides: dict[str, dict[str, str]] = {}
    cfg_path = path or default_config_path()
    if path is not None and not path.is_file():
        raise ConfigError(f"{path}: config file not found")
    if cfg_path.is_file():
        try:
            text = cfg_path.read_text()
        except OSError as exc:
            raise ConfigError(f"{cfg_path}: cannot read config: {exc}") from exc
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path}: invalid TOML: {exc}") from exc
        for key in data:
            if key not in ("repositories", "engines"):
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
        engine_overrides = _parse_engines(cfg_path, data["engines"]) if "engines" in data else {}

    for r in extra_repos:
        if not r.startswith(("http://", "https://")):
            raise ConfigError(f"--repo {r!r} must be an http(s) URL")

    ordered: list[str] = []
    for repo in [*extra_repos, *file_repos, MAVEN_CENTRAL]:
        repo = repo.rstrip("/")
        if repo not in ordered:
            ordered.append(repo)
    return Config(repositories=tuple(ordered), engine_overrides=engine_overrides)


def write_engine_pins(path: Path, overrides: dict[str, dict[str, str]]) -> None:
    data: dict = {}
    if path.is_file():
        data = tomllib.loads(path.read_text())
    if overrides:
        data["engines"] = overrides
    else:
        data.pop("engines", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tomli_w.dumps(data))
    os.replace(tmp, path)
