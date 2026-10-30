import base64
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from decaf import selfupdate


@pytest.mark.parametrize("text,want", [
    ("1.11.0", (1, 11, 0)), ("v1.12.3", (1, 12, 3)),
    ("1.12", None), ("1.12.0.dev0", None), ("v1.x.0", None), ("", None), ("1.2.3 ", None),
])
def test_parse_version(text, want):
    assert selfupdate.parse_version(text) == want


def test_fetch_latest_reads_the_tag(github):
    seen = []
    rel = selfupdate.fetch_latest(github(seen=seen), timeout=30)
    assert rel.version == (1, 12, 0) and rel.text == "1.12.0" and rel.tag == "v1.12.0"
    assert rel.url == "https://github.com/vcth4nh/decaf/releases/tag/v1.12.0"
    assert rel.spec == "git+https://github.com/vcth4nh/decaf@v1.12.0"
    assert str(seen[0].url) == "https://api.github.com/repos/vcth4nh/decaf/releases/latest"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"
    assert seen[0].extensions["timeout"]["read"] == 30  # not the httpx default of 5 s


def test_fetch_latest_follows_a_redirect():
    # GitHub answers 301 for a renamed or moved repo; the lookup must still find the release
    moved = "https://api.github.com/repositories/1/releases/latest"

    def handler(request):
        if str(request.url) == selfupdate.LATEST_API:
            return httpx.Response(301, headers={"Location": moved})
        return httpx.Response(200, json={"tag_name": "v1.12.0"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert selfupdate.fetch_latest(http, timeout=30).version == (1, 12, 0)


@pytest.mark.parametrize("kw,reason", [
    ({"status": 403}, "HTTP 403"),
    ({"status": 429}, "HTTP 429"),
    ({"body": "<html>"}, "bad response"),
    ({"body": "[]"}, "bad response"),  # JSON, but not an object: the command catches only SelfUpdateError
    ({"tag": "nightly"}, "unexpected tag 'nightly'"),
    ({"tag": "1.12.0"}, "unexpected tag '1.12.0'"),  # Release.url and .spec use v1.12.0, a tag that does not exist
    ({"tag": None}, "unexpected tag None"),
])
def test_fetch_latest_errors(github, kw, reason):
    with pytest.raises(selfupdate.SelfUpdateError, match=re.escape(reason)):
        selfupdate.fetch_latest(github(**kw), timeout=30)


def test_the_suite_cannot_reach_github():
    with pytest.raises(selfupdate.SelfUpdateError, match="network blocked in tests"):
        selfupdate.fetch_latest(selfupdate.client(), timeout=5)


GIT_RECEIPT = (
    "[tool]\n"
    'requirements = [{ name = "decaf", git = "https://github.com/vcth4nh/decaf?rev=v1.11.0" }]\n'
    'entrypoints = [{ name = "decaf", install-path = "/home/u/.local/bin/decaf", from = "decaf" }]\n'
)
GIT_URL = '{"url": "https://github.com/vcth4nh/decaf", "vcs_info": {"vcs": "git", "commit_id": "bbd7063", "requested_revision": "v1.11.0"}}'
EDITABLE_URL = '{"url": "file:///src/decaf", "dir_info": {"editable": true}}'
REL = selfupdate.Release((1, 12, 0))
SPEC = "git+https://github.com/vcth4nh/decaf@v1.12.0"


def test_find_install_uv_tool(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text(GIT_RECEIPT)
    inst = selfupdate.find_install(tmp_path, GIT_URL)
    assert inst == selfupdate.Install("uv-tool", tmp_path / "uv-receipt.toml", None,
                                      Path("/home/u/.local/bin/decaf"))


def test_find_install_reads_the_python_pin(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text(GIT_RECEIPT.replace("[tool]\n", '[tool]\npython = "3.12"\n'))
    assert selfupdate.find_install(tmp_path, GIT_URL).python == "3.12"


def test_find_install_editable_by_direct_url(tmp_path):
    assert selfupdate.find_install(tmp_path, EDITABLE_URL).kind == "editable"


def test_find_install_editable_receipt(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "decaf", editable = "/src/decaf" }]\n')
    assert selfupdate.find_install(tmp_path, None).kind == "editable"


@pytest.mark.parametrize("receipt", [None, "not [toml"])
def test_find_install_other(tmp_path, receipt):
    if receipt is not None:
        (tmp_path / "uv-receipt.toml").write_text(receipt)
    assert selfupdate.find_install(tmp_path, None) == selfupdate.Install("other")


def test_receipt_rev(tmp_path):
    r = tmp_path / "uv-receipt.toml"
    assert selfupdate.receipt_rev(r) is None
    r.write_text(GIT_RECEIPT)
    assert selfupdate.receipt_rev(r) == "v1.11.0"
    r.write_text('[tool]\nrequirements = [{ name = "decaf", directory = "/src/decaf" }]\n')
    assert selfupdate.receipt_rev(r) is None


@pytest.mark.parametrize("python,want", [(None, []), ("3.12", ["--python", "3.12"])])
def test_uv_command_posix(python, want):
    inst = selfupdate.Install("uv-tool", python=python)
    assert selfupdate.uv_command("uv", REL, inst, windows=False, base="/py/bin/python3") == [
        "uv", "tool", "install", *want, SPEC]


@pytest.mark.parametrize("base,python,want", [
    ("C:\\py\\python.exe", "3.12", ["--python", "C:\\py\\python.exe"]),
    (None, "3.12", ["--python", "3.12"]),
    (None, None, []),
])
def test_uv_command_windows_pins_the_venv_python(base, python, want):
    inst = selfupdate.Install("uv-tool", python=python)
    assert selfupdate.uv_command("uv", REL, inst, windows=True, base=base) == [
        "uv", "tool", "install", *want, SPEC]


@pytest.mark.parametrize("with_exe", [False, True])
def test_uv_env_names_the_folders_of_this_install(tmp_path, with_exe):
    # uv takes its tool folder and its bin folder from the environment: with the values of another
    # shell, uv installs a second decaf in other folders and leaves this install as it is
    env = {"PATH": "/bin", "UV_TOOL_DIR": "/other/tools", "UV_TOOL_BIN_DIR": "/other/bin"}
    inst = selfupdate.Install("uv-tool", exe=tmp_path / "bin" / "decaf" if with_exe else None)
    want = {**env, "UV_TOOL_DIR": str(tmp_path / "tools")}
    if with_exe:
        want["UV_TOOL_BIN_DIR"] = str(tmp_path / "bin")
    assert selfupdate.uv_env(tmp_path / "tools" / "decaf", inst, env) == want
    assert env["UV_TOOL_DIR"] == "/other/tools"  # a new dict: the environment of decaf stays as it is


def test_base_python_needs_a_file(tmp_path, monkeypatch):
    # without a base Python file, uv_command on Windows uses the python pin of the receipt
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    monkeypatch.setattr(sys, "_base_executable", str(py))
    assert selfupdate.base_python() == str(py)
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "gone.exe"))
    assert selfupdate.base_python() is None
    monkeypatch.setattr(sys, "_base_executable", "")
    assert selfupdate.base_python() is None


def test_direct_url_reads_the_decaf_metadata(tmp_path, monkeypatch):
    info = tmp_path / "decaf-9.9.9.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: decaf\nVersion: 9.9.9\n")
    monkeypatch.syspath_prepend(str(tmp_path))  # this decaf metadata comes before the real one
    assert selfupdate.direct_url() is None  # an install from a package index writes no direct_url.json
    (info / "direct_url.json").write_text(EDITABLE_URL)
    assert selfupdate.direct_url() == EDITABLE_URL


def test_direct_url_without_decaf_metadata(monkeypatch):
    # a decaf from a source folder on PYTHONPATH has no metadata: the result is None, not an error
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    assert selfupdate.direct_url() is None


def test_aside_path_and_restore(tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    aside = selfupdate.aside_path(exe)
    assert aside == tmp_path / "decaf.exe.old" and exe.read_bytes() == b"old"  # the command renames the exe
    exe.rename(aside)
    selfupdate.restore_exe(exe, aside)
    assert exe.read_bytes() == b"old" and not aside.exists()


def test_restore_keeps_a_new_exe(tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    aside = selfupdate.aside_path(exe)
    exe.rename(aside)
    exe.write_bytes(b"new")
    selfupdate.restore_exe(exe, aside)
    assert exe.read_bytes() == b"new" and aside.read_bytes() == b"old"


def test_aside_path_deletes_old_leftovers(tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    (tmp_path / "decaf.exe.old").write_bytes(b"1")
    (tmp_path / "decaf.exe.1.old").write_bytes(b"2")
    assert selfupdate.aside_path(exe) == tmp_path / "decaf.exe.old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["decaf.exe"]


def test_aside_path_skips_a_leftover_in_use(tmp_path, monkeypatch):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    (tmp_path / "decaf.exe.old").write_bytes(b"in use")
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "decaf.exe.old":
            raise PermissionError("in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert selfupdate.aside_path(exe) == tmp_path / "decaf.exe.1.old"
    assert (tmp_path / "decaf.exe.old").read_bytes() == b"in use"


def test_aside_path_without_an_exe(tmp_path):
    assert selfupdate.aside_path(tmp_path / "decaf.exe") is None


def _probe_wheel(folder: Path, version: str) -> Path:
    """A wheel of the small package decafprobe. Its command prints the version. With the
    argument wait, it then reads stdin to its end, so the test sets when it ends."""
    info = f"decafprobe-{version}.dist-info"
    files = {
        "decafprobe/__init__.py": (
            "import sys\n\n\n"
            "def main():\n"
            f"    print({version!r}, flush=True)\n"
            "    if sys.argv[1:] == ['wait']:\n"
            "        sys.stdin.read()\n"
        ),
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: decafprobe\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": "[console_scripts]\ndecafprobe = decafprobe:main\n",
    }
    record = ""
    for name, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record += f"{name},sha256={digest},{len(data)}\n"
    files[f"{info}/RECORD"] = record + f"{info}/RECORD,,\n"
    wheel = folder / f"decafprobe-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return wheel


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows locks an active exe")
def test_uv_replaces_an_active_tool_exe_after_the_rename(tmp_path):
    # uv cannot copy a new tool exe over an active one (astral-sh/uv#11930). After the
    # rename, the name is free. The python.exe of the tool venv is active too, so uv must
    # keep the venv: --python with the base Python of the venv asks for that.
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    env = {**os.environ, "UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": str(tmp_path / "bin")}
    subprocess.run([uv, "tool", "install", "--offline", str(_probe_wheel(tmp_path, "1.0"))], env=env, check=True,
                   timeout=120)  # a hang must not hold the CI job
    exe = tmp_path / "bin" / "decafprobe.exe"
    venv = tmp_path / "tools" / "decafprobe"
    proc = subprocess.Popen([str(exe), "wait"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "1.0"
        (venv / "marker").write_text("x")
        base = subprocess.run([str(venv / "Scripts" / "python.exe"), "-c", "import sys; print(sys._base_executable)"],
                              capture_output=True, text=True, check=True, timeout=120).stdout.strip()
        aside = selfupdate.aside_path(exe)
        assert aside == tmp_path / "bin" / "decafprobe.exe.old"
        exe.rename(aside)  # what decaf self-update does with its own active exe
        r = subprocess.run([uv, "tool", "install", "--offline", "--python", base, str(_probe_wheel(tmp_path, "2.0"))],
                           env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)  # uv writes UTF-8
        assert r.returncode == 0, r.stderr
        assert proc.poll() is None  # the old exe was active during the whole install
        assert subprocess.run([str(exe)], capture_output=True, text=True, check=True, timeout=120).stdout.strip() == "2.0"
        assert (venv / "marker").exists()  # uv kept the venv
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()


R12 = selfupdate.Release((1, 12, 0))


def saved(d):
    return json.loads((d / "update-check.json").read_text())


def test_latest_known_asks_github_without_a_file(tmp_path):
    assert selfupdate.latest_known(tmp_path, 1000.0, lambda: R12) == (1, 12, 0)
    assert saved(tmp_path) == {"checked": 1000.0, "latest": "1.12.0"}


def test_latest_known_uses_a_new_file(tmp_path):
    (tmp_path / "update-check.json").write_text('{"checked": 1000.0, "latest": "1.12.0"}')
    assert selfupdate.latest_known(tmp_path, 1000.0 + 86399, lambda: pytest.fail("asked")) == (1, 12, 0)


@pytest.mark.parametrize("text", [
    '{"checked": 1000.0, "latest": "1.11.0"}',      # one day old (now = 87400)
    '{"checked": 90000.0, "latest": "1.11.0"}',     # in the future
    "{not json", '{"checked": "x", "latest": 5}',
    # damaged files with a new time: only the damage makes the check ask again
    '{"checked": "x", "latest": "1.11.0"}', '{"checked": 87000.0, "latest": "1.x"}',
    '{"checked": 87000.0}', "[]",
])
def test_latest_known_asks_again(tmp_path, text):
    (tmp_path / "update-check.json").write_text(text)
    assert selfupdate.latest_known(tmp_path, 87400.0, lambda: R12) == (1, 12, 0)


# A bad HTTPS_PROXY makes httpx raise ValueError, not SelfUpdateError: it must not cost a request on each run
@pytest.mark.parametrize("error", [selfupdate.SelfUpdateError("HTTP 403"), ValueError("Unknown scheme for proxy URL")])
def test_failed_request_keeps_the_version_and_saves_the_time(tmp_path, error):
    (tmp_path / "update-check.json").write_text('{"checked": 0.0, "latest": "1.12.0"}')

    def fail():
        raise error

    assert selfupdate.latest_known(tmp_path, 100000.0, fail) == (1, 12, 0)
    assert saved(tmp_path) == {"checked": 100000.0, "latest": "1.12.0"}


def test_write_error_is_ignored(tmp_path):
    (tmp_path / "file").write_text("x")
    assert selfupdate.latest_known(tmp_path / "file", 1.0, lambda: R12) == (1, 12, 0)


def test_notice_line(tmp_path):
    n = selfupdate.NoticeCheck(tmp_path, "1.11.0", fetch=lambda: R12)
    n.start()
    n._thread.join(5)
    assert n.line() == "decaf 1.12.0 is available (this is 1.11.0). Run `decaf self-update` to install it."
    assert selfupdate.NoticeCheck(tmp_path, "1.12.0", fetch=lambda: R12).line() is None  # not started


@pytest.mark.parametrize("current", ["1.12.0", "1.13.0", "1.12.0.dev0"])
def test_notice_line_needs_a_newer_release(tmp_path, current):
    # the same release, a newer local build, or a version that does not parse: no line
    n = selfupdate.NoticeCheck(tmp_path, current, fetch=lambda: R12)
    n.start()
    n._thread.join(5)
    assert n.line() is None


def test_notice_asks_through_the_client_of_the_module(tmp_path, monkeypatch, github):
    # the test guard replaces selfupdate.client: a client taken at import time would reach GitHub
    seen = []
    monkeypatch.setattr(selfupdate, "client", lambda: github(tag="v1.12.0", seen=seen))
    n = selfupdate.NoticeCheck(tmp_path, "1.11.0")
    n.start()
    n._thread.join(5)
    assert n.line() is not None and len(seen) == 1
    assert seen[0].extensions["timeout"]["read"] == 5  # the notice waits less than the command (30 s)


def test_notice_check_never_raises(tmp_path, monkeypatch):
    # an error in the thread must not print a traceback at the end of a run
    def broken(cache_dir, now, fetch):
        raise RuntimeError("bug")

    monkeypatch.setattr(selfupdate, "latest_known", broken)
    n = selfupdate.NoticeCheck(tmp_path, "1.11.0", fetch=lambda: R12)
    n._run()
    assert n.line() is None


def test_notice_line_waits_for_nothing(tmp_path):
    gate = threading.Event()
    n = selfupdate.NoticeCheck(tmp_path, "1.11.0", fetch=lambda: (gate.wait(5), R12)[1])
    n.start()
    assert n.line() is None
    gate.set()


def test_notice_survives_a_fetch_error(tmp_path):
    def boom():
        raise RuntimeError("boom")

    n = selfupdate.NoticeCheck(tmp_path, "1.11.0", fetch=boom)
    n.start()
    n._thread.join(5)
    assert n.line() is None


def test_exit_while_the_check_waits(tmp_path):
    # the check runs in a daemon thread: the process ends at once, also while the request waits
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from decaf import selfupdate\n"
        "n = selfupdate.NoticeCheck(Path(sys.argv[1]), '1.11.0', fetch=lambda: time.sleep(30))\n"
        "n.start()\n"
        "print(n.line())\n"
    )
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout.strip() == "None" and r.stderr == ""
    assert time.monotonic() - t0 < 10
