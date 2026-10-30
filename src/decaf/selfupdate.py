"""decaf self-update (#155): the latest release of decaf on GitHub, the facts of the active
install, and the uv command that installs a release.

`fetch_latest` gets its httpx client from the caller. The tests replace `client`, so no
test reaches GitHub.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

REPO_URL = "https://github.com/vcth4nh/decaf"
LATEST_API = "https://api.github.com/repos/vcth4nh/decaf/releases/latest"
WINDOWS: bool = sys.platform == "win32"  # the tests set it

_VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")


class SelfUpdateError(Exception):
    """The text is the reason for the user."""


@dataclass(frozen=True)
class Release:
    version: tuple[int, int, int]

    @property
    def text(self) -> str:
        return ".".join(map(str, self.version))

    @property
    def tag(self) -> str:
        return f"v{self.text}"

    @property
    def url(self) -> str:
        return f"{REPO_URL}/releases/tag/{self.tag}"

    @property
    def spec(self) -> str:
        return f"git+{REPO_URL}@{self.tag}"


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.fullmatch(text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def client() -> httpx.Client:
    """The client of the release lookup. The tests replace this factory."""
    return httpx.Client()


def fetch_latest(http: httpx.Client, timeout: float) -> Release:
    try:
        resp = http.get(LATEST_API, follow_redirects=True, timeout=timeout,
                        headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise SelfUpdateError(str(exc)) from exc
    if resp.status_code != 200:
        raise SelfUpdateError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SelfUpdateError("bad response") from exc
    if not isinstance(data, dict):
        raise SelfUpdateError("bad response")
    tag = data.get("tag_name")
    version = parse_version(tag) if isinstance(tag, str) else None
    if version is None or Release(version).tag != tag:  # Release builds its tag, url, and spec from the numbers
        raise SelfUpdateError(f"unexpected tag {tag!r}")
    return Release(version)


@dataclass(frozen=True)
class Install:
    kind: str  # "uv-tool", "editable", or "other"
    receipt: Path | None = None  # the uv-receipt.toml of a uv tool install
    python: str | None = None  # the --python value of the last uv tool install
    exe: Path | None = None  # the decaf command that uv installed


def direct_url() -> str | None:
    """The direct_url.json text (PEP 610) of the installed decaf distribution."""
    try:
        return importlib.metadata.distribution("decaf").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None


def _receipt_tool(receipt: Path) -> dict:
    """The [tool] table of a uv receipt; empty for a missing or damaged file."""
    try:
        tool = tomllib.loads(receipt.read_text(encoding="utf-8")).get("tool")
    except (OSError, ValueError):  # ValueError: not UTF-8, or not TOML
        return {}
    return tool if isinstance(tool, dict) else {}


def _decaf_item(items: object) -> dict:
    """The table with name = "decaf" in a list of a uv receipt (requirements, entrypoints);
    empty when there is no such table."""
    for item in items if isinstance(items, list) else ():
        if isinstance(item, dict) and item.get("name") == "decaf":
            return item
    return {}


def _editable_url(text: str | None) -> bool:
    """PEP 610: the direct_url.json of an editable install has dir_info.editable = true."""
    try:
        return json.loads(text)["dir_info"]["editable"] is True
    except (TypeError, ValueError, KeyError):  # no text, not JSON, or no dir_info.editable
        return False


def find_install(prefix: Path, direct_url_text: str | None) -> Install:
    """The install type of the decaf in the venv `prefix`, from its uv receipt and the
    text of its direct_url.json."""
    receipt = prefix / "uv-receipt.toml"
    tool = _receipt_tool(receipt)
    if _editable_url(direct_url_text) or "editable" in _decaf_item(tool.get("requirements")):
        return Install("editable")
    if "requirements" not in tool:
        return Install("other")
    python = tool.get("python")
    exe = _decaf_item(tool.get("entrypoints")).get("install-path")
    return Install("uv-tool", receipt, python if isinstance(python, str) else None,
                   Path(exe) if isinstance(exe, str) else None)


def receipt_rev(receipt: Path) -> str | None:
    """The rev of the decaf git source in a uv receipt: v1.11.0 for
    git = "https://github.com/vcth4nh/decaf?rev=v1.11.0"."""
    git = _decaf_item(_receipt_tool(receipt).get("requirements")).get("git")
    if not isinstance(git, str):
        return None
    return parse_qs(urlsplit(git).query).get("rev", [None])[0]


def find_uv() -> str | None:
    """The uv on PATH."""
    return shutil.which("uv")


def base_python() -> str | None:
    """The base Python of the active venv, or None when sys._base_executable names no file.
    On Windows, the python.exe of a venv is only a launcher of this Python."""
    base = getattr(sys, "_base_executable", None)
    return base if base and os.path.isfile(base) else None


def uv_command(uv: str, release: Release, install: Install, *, windows: bool, base: str | None) -> list[str]:
    """The uv command that installs the release over the active install.

    On Windows, --python names the base Python of the active venv. This request matches
    the venv, so uv keeps the venv: uv cannot delete a venv while its python.exe runs.
    Else --python gives the Python of the last install again, when that install had one.
    """
    python = base if windows and base is not None else install.python
    return [uv, "tool", "install", *(["--python", python] if python is not None else []), release.spec]
