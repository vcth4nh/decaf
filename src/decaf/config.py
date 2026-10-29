"""Config file loading and Maven repository list assembly."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import platformdirs
import tomli_w

from .maven import redact_url

MAVEN_CENTRAL = "https://repo1.maven.org/maven2"
_CENTRAL_HOSTS = ("repo1.maven.org", "repo.maven.apache.org")  # two names of one repository


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    repositories: tuple[str, ...]
    engine_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    update_check: bool = True


def default_config_path() -> Path:
    return platformdirs.user_config_path("decaf") / "config.toml"


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OVERRIDE_REQUIRED = ("version", "url", "sha256")
_OVERRIDE_OPTIONAL = ("download_sha256", "archive_member")


def _validate_override(cfg_path: Path, name: str, entry: object) -> dict[str, str]:
    from .engines import ENGINES

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
    return dict(entry)


def _parse_engines(cfg_path: Path, raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: engines must be a table")
    return {name: _validate_override(cfg_path, name, entry) for name, entry in raw.items()}


def _read_toml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")  # TOML is UTF-8, also where the locale codec is not
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: cannot read config: not UTF-8 (byte {exc.start})") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def _is_central(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    # Central answers plain HTTP with 501, so only an https entry is Central (#115).
    return parts.scheme == "https" and parts.hostname in _CENTRAL_HOSTS and parts.path.rstrip("/") == "/maven2"


def _check_repo(where: str, r: str) -> None:
    """Refuse a repository URL that httpx cannot read, that has no host, or with an unclear user part (#115).

    redact_url hides the text up to the last @. httpx reads the user part only before the first / ? or #,
    so for an @ after one of them, the request goes to a wrong host.
    """
    if not r.startswith(("http://", "https://")):
        raise ConfigError(f"{where} {redact_url(r)!r} must be an http(s) URL")
    try:
        host = httpx.URL(r).host
    except httpx.InvalidURL:
        host = ""
    if not host or "@" in re.sub(r"^[^/?#]*", "", r.partition("://")[2]):
        hint = "; write a / ? # or @ of a user name or a password as %2F %3F %23 %40" if "@" in r else ""
        raise ConfigError(f"{where} {redact_url(r)!r} is not a valid URL{hint}")


def load_config(path: Path | None = None, extra_repos: Sequence[str] = ()) -> Config:
    file_repos: list[str] = []
    engine_overrides: dict[str, dict[str, str]] = {}
    update_check = True
    cfg_path = path or default_config_path()
    if path is not None and not path.is_file():
        raise ConfigError(f"{path}: config file not found")
    if cfg_path.is_file():
        data = _read_toml(cfg_path)
        for key in data:
            if key not in ("repositories", "engines", "update_check"):
                raise ConfigError(f"{cfg_path}: unknown key {key!r}")
        repos = data.get("repositories", [])
        if not isinstance(repos, list):
            raise ConfigError(f"{cfg_path}: repositories must be a list")
        if not all(isinstance(r, str) for r in repos):
            raise ConfigError(f"{cfg_path}: repositories entries must be strings")
        for r in repos:
            _check_repo(f"{cfg_path}: repository", r)
        file_repos = repos
        engine_overrides = _parse_engines(cfg_path, data["engines"]) if "engines" in data else {}
        update_check = data.get("update_check", True)
        if not isinstance(update_check, bool):
            raise ConfigError(f"{cfg_path}: update_check must be true or false")

    for r in extra_repos:
        _check_repo("--repo", r)

    ordered: list[str] = []
    for repo in [*extra_repos, *file_repos]:
        repo = repo.rstrip("/")
        if repo not in ordered:
            ordered.append(repo)
    if not any(_is_central(r) for r in ordered):
        ordered.append(MAVEN_CENTRAL)
    return Config(repositories=tuple(ordered), engine_overrides=engine_overrides, update_check=update_check)


def write_engine_pins(path: Path, overrides: dict[str, dict[str, str]]) -> None:
    for name, entry in overrides.items():
        _validate_override(path, name, entry)
    data: dict = _read_toml(path) if path.is_file() else {}
    if overrides:
        data["engines"] = overrides
    else:
        data.pop("engines", None)
    # The config can hold repository credentials: the new file gets the mode of the old one,
    # and a symlink, for example into a dotfiles folder, stays a symlink (#115).
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else 0o666
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w", encoding="utf-8") as f:
        f.write(tomli_w.dumps(data))
    os.replace(tmp, target)
