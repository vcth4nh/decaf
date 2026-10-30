import base64
import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
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


def test_move_exe_aside_and_restore(tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    aside = selfupdate.move_exe_aside(exe)
    assert aside == tmp_path / "decaf.exe.old" and aside.read_bytes() == b"old" and not exe.exists()
    selfupdate.restore_exe(exe, aside)
    assert exe.read_bytes() == b"old" and not aside.exists()


def test_restore_keeps_a_new_exe(tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    aside = selfupdate.move_exe_aside(exe)
    exe.write_bytes(b"new")
    selfupdate.restore_exe(exe, aside)
    assert exe.read_bytes() == b"new" and aside.read_bytes() == b"old"


def test_move_exe_aside_deletes_old_leftovers(tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    (tmp_path / "decaf.exe.old").write_bytes(b"1")
    (tmp_path / "decaf.exe.1.old").write_bytes(b"2")
    assert selfupdate.move_exe_aside(exe) == tmp_path / "decaf.exe.old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["decaf.exe.old"]


def test_move_exe_aside_skips_a_leftover_in_use(tmp_path, monkeypatch):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    (tmp_path / "decaf.exe.old").write_bytes(b"in use")
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "decaf.exe.old":
            raise PermissionError("in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert selfupdate.move_exe_aside(exe) == tmp_path / "decaf.exe.1.old"
    assert (tmp_path / "decaf.exe.old").read_bytes() == b"in use"


def test_move_exe_aside_without_an_exe(tmp_path):
    assert selfupdate.move_exe_aside(tmp_path / "decaf.exe") is None


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
    subprocess.run([uv, "tool", "install", "--offline", str(_probe_wheel(tmp_path, "1.0"))], env=env, check=True)
    exe = tmp_path / "bin" / "decafprobe.exe"
    venv = tmp_path / "tools" / "decafprobe"
    proc = subprocess.Popen([str(exe), "wait"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "1.0"
        (venv / "marker").write_text("x")
        base = subprocess.run([str(venv / "Scripts" / "python.exe"), "-c", "import sys; print(sys._base_executable)"],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert selfupdate.move_exe_aside(exe) == tmp_path / "bin" / "decafprobe.exe.old"
        r = subprocess.run([uv, "tool", "install", "--offline", "--python", base, str(_probe_wheel(tmp_path, "2.0"))],
                           env=env, capture_output=True, encoding="utf-8", errors="replace")  # uv writes UTF-8
        assert r.returncode == 0, r.stderr
        assert subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.strip() == "2.0"
        assert (venv / "marker").exists()  # uv kept the venv
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()
