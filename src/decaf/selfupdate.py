"""decaf self-update (#155): the latest release of decaf on GitHub, the facts of the active
install, the uv command that installs a release, the rename of the active exe on Windows,
the uv child, and the notice after a run.

`fetch_latest` gets its httpx client from the caller. The tests replace `client`, so no
test reaches GitHub.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
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
    import importlib.metadata  # only here: at the top, each decaf process would pay its import time (#155)

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


def uv_env(prefix: Path, install: Install, env: Mapping[str, str]) -> dict[str, str]:
    """The environment of the uv child: `env` with the tool folder and the bin folder of the
    active install. `prefix` is the venv of the active decaf in the tool folder.

    uv reads these folders from UV_TOOL_DIR and UV_TOOL_BIN_DIR, else from XDG_DATA_HOME and
    its defaults. With other values in the shell of the update, uv installs a second decaf.
    """
    child = {**env, "UV_TOOL_DIR": str(prefix.parent)}
    if install.exe is not None:
        child["UV_TOOL_BIN_DIR"] = str(install.exe.parent)
    return child


def move_exe_aside(exe: Path) -> Path | None:
    """Rename the exe to a free .old name, and give that path; None when `exe` is not a file.
    On Windows, uv cannot copy a new exe over an active one (astral-sh/uv#11930), but a
    program can rename an active exe. First delete the .old files of earlier updates: a
    file that an old process still uses stays."""
    if not exe.is_file():
        return None
    for old in exe.parent.glob(exe.name + "*.old"):
        try:
            old.unlink()
        except OSError:  # an old decaf process still runs from this file
            pass
    aside, n = exe.with_name(exe.name + ".old"), 0
    while aside.exists():  # POSIX rename overwrites a file that exists
        n += 1
        aside = exe.with_name(f"{exe.name}.{n}.old")
    exe.rename(aside)
    return aside


def restore_exe(exe: Path, aside: Path | None) -> None:
    """Give the exe of move_exe_aside its name again, unless uv wrote a new exe there."""
    if aside is not None and not exe.exists():
        aside.rename(exe)


def run_uv(cmd: list[str], env: Mapping[str, str] | None = None) -> tuple[int, bool]:
    """Run uv with the stdout and the stderr of decaf, in the environment `env`. Give the
    exit code of uv, and True when a Ctrl-C came. An OSError of Popen means that uv did not
    start.

    A Ctrl-C goes to uv too. While uv runs, the SIGINT handler only notes the Ctrl-C, so
    decaf waits for uv to end, then restores the exe and writes its text. SIG_IGN is not
    an option: a child keeps an ignored signal, and uv must get the Ctrl-C.
    """
    stops: list[int] = []
    old = signal.signal(signal.SIGINT, lambda signum, frame: stops.append(signum))
    try:
        code = subprocess.Popen(cmd, env=env).wait()
    finally:
        signal.signal(signal.SIGINT, old)
    return code, bool(stops)


NOTICE_FILE = "update-check.json"  # in the decaf cache folder
NOTICE_MAX_AGE = 86400  # seconds: the notice asks GitHub at most one time each day


def available_text(new: str, cur: str) -> str:
    """The line of `decaf self-update --check` and of the notice after a run."""
    return f"decaf {new} is available (this is {cur}). Run `decaf self-update` to install it."


def _read_notice(path: Path) -> tuple[float, tuple[int, int, int] | None] | None:
    """The time and the version of the notice file; None for a missing or damaged file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked, latest = data["checked"], data["latest"]
    except (OSError, ValueError, TypeError, KeyError):  # no file, not UTF-8, not JSON, or no such keys
        return None
    version = parse_version(latest) if isinstance(latest, str) else None
    if not isinstance(checked, (int, float)) or (latest is not None and version is None):
        return None
    return checked, version


def _write_notice(path: Path, checked: float, latest: tuple[int, int, int] | None) -> None:
    """Write the notice file through a temp file and os.replace. A write error keeps the old file."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False, encoding="utf-8",
        )
        with tmp:
            json.dump({"checked": checked, "latest": None if latest is None else Release(latest).text}, tmp)
        os.replace(tmp.name, path)
    except OSError:
        if tmp is not None:
            try:
                Path(tmp.name).unlink()
            except OSError:
                pass


def latest_known(cache_dir: Path, now: float, fetch: Callable[[], Release]) -> tuple[int, int, int] | None:
    """The latest release version for the notice after a run.

    A notice file younger than NOTICE_MAX_AGE gives its version with no request; a time in
    the future counts as old. Else fetch() asks GitHub. A failed request keeps the version of
    the file, and both save the time, so an offline machine asks one time each day.
    """
    path = cache_dir / NOTICE_FILE
    saved = _read_notice(path)
    if saved is not None and 0 <= now - saved[0] < NOTICE_MAX_AGE:
        return saved[1]
    latest = saved[1] if saved is not None else None
    try:
        latest = fetch().version
    except Exception:  # for example no network, a GitHub refusal, or a bad proxy setting
        pass
    _write_notice(path, now, latest)
    return latest


def _ask_github() -> Release:
    with client() as http:  # the module global at call time: the tests replace it
        return fetch_latest(http, timeout=5)


class NoticeCheck:
    """The check behind the notice after a run. start() asks in a daemon thread, and line()
    never waits for that thread: a slow answer gives no line, and the run ends at once."""

    def __init__(self, cache_dir: Path, current: str, fetch: Callable[[], Release] | None = None) -> None:
        self._cache_dir = cache_dir
        self._current = current
        self._fetch = fetch if fetch is not None else _ask_github
        self._thread: threading.Thread | None = None
        self._latest: tuple[int, int, int] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._latest = latest_known(self._cache_dir, time.time(), self._fetch)
        except Exception:  # the notice is only a hint: an error gives no line
            pass

    def line(self) -> str | None:
        """The notice, or None while the thread lives, after an error, or with no newer release."""
        if self._thread is not None and self._thread.is_alive():
            return None
        current = parse_version(self._current)
        if self._latest is None or current is None or self._latest <= current:
            return None
        return available_text(Release(self._latest).text, self._current)
