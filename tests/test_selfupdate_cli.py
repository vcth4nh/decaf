import dataclasses
import re
import signal
import sys
import types

import pytest
from typer.testing import CliRunner

import decaf.cli as cli
from decaf import engines, runlock, selfupdate
from decaf.cli import app

runner = CliRunner(env={"COLUMNS": "200"})
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

GIT_RECEIPT = (
    "[tool]\n"
    'requirements = [{ name = "decaf", git = "https://github.com/vcth4nh/decaf?rev=v1.11.0" }]\n'
    'entrypoints = [{ name = "decaf", install-path = "/home/u/.local/bin/decaf", from = "decaf" }]\n'
)
SPEC = "git+https://github.com/vcth4nh/decaf@v1.12.0"


class Update:
    """One `decaf self-update` with fakes: the GitHub answer, the install facts, and a uv
    that writes down what it got."""

    def __init__(self, receipt):
        self.tag, self.status = "v1.12.0", 200
        self.receipt = receipt
        self.install = selfupdate.Install("uv-tool", receipt)
        self.result = (0, False)
        self.write_receipt = True
        self.during = None
        self.seen_during = []
        self.snapshot_modules = False
        self.modules = None
        self.cmds = []

    def run_uv(self, cmd):
        self.cmds.append(cmd)
        if self.during is not None:
            self.seen_during.append(self.during())
        if self.snapshot_modules:
            self.modules = set(sys.modules)
        if self.write_receipt:  # a uv install writes the rev of the new release
            self.receipt.write_text(GIT_RECEIPT.replace("rev=v1.11.0", "rev=v1.12.0"))
        return self.result

    def invoke(self, *args):
        r = runner.invoke(app, ["self-update", *args])
        if r.exception is not None and not isinstance(r.exception, SystemExit):
            raise r.exception  # a crash also exits 1: show its traceback
        return types.SimpleNamespace(exit_code=r.exit_code, output=ANSI.sub("", r.output))


@pytest.fixture
def upd(monkeypatch, tmp_path, github):
    u = Update(tmp_path / "uv-receipt.toml")
    u.receipt.write_text(GIT_RECEIPT)
    monkeypatch.setattr(cli, "__version__", "1.11.0")
    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(selfupdate, "client", lambda: github(tag=u.tag, status=u.status))
    monkeypatch.setattr(selfupdate, "find_install", lambda prefix, direct_url_text: u.install)
    monkeypatch.setattr(selfupdate, "find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(selfupdate, "run_uv", u.run_uv)
    monkeypatch.setattr(selfupdate, "WINDOWS", False)  # the POSIX path also on Windows CI; the Windows tests set True
    return u


def test_latest_release_is_active(upd):
    upd.tag = "v1.11.0"
    r = upd.invoke()
    assert r.exit_code == 0 and "decaf 1.11.0 is the latest release" in r.output and upd.cmds == []


def test_newer_local_build(upd):
    upd.tag = "v1.10.0"
    r = upd.invoke()
    assert r.exit_code == 0 and "decaf 1.11.0 is newer than the latest release (1.10.0)" in r.output


def test_check_only_reports(upd):
    r = upd.invoke("--check")
    assert r.exit_code == 0 and upd.cmds == []
    assert "decaf 1.12.0 is available (this is 1.11.0). Run `decaf self-update` to install it." in r.output


@pytest.mark.parametrize("status", [403, 429])
def test_github_refusal_is_one_line(upd, status):
    # GitHub limits the requests of a shared NAT address: the user gets the reason, not a traceback
    upd.status = status
    r = upd.invoke()
    assert r.exit_code == 1 and f"error: cannot read the latest release: HTTP {status}" in r.output
    assert "Traceback" not in r.output


def test_bad_proxy_setting_is_one_line(upd, monkeypatch):
    # httpx reads HTTPS_PROXY when decaf makes the client, before the request: a bad proxy URL raises there
    def bad_proxy():
        raise ValueError("Unknown scheme for proxy URL")

    monkeypatch.setattr(selfupdate, "client", bad_proxy)
    r = upd.invoke()
    assert r.exit_code == 1 and "error: cannot read the latest release: Unknown scheme for proxy URL" in r.output


def test_unparsable_active_version(upd, monkeypatch):
    monkeypatch.setattr(cli, "__version__", "1.12.0.dev0")
    r = upd.invoke()
    assert r.exit_code == 1 and "cannot compare versions: this decaf is '1.12.0.dev0'" in r.output


def test_editable_install_refuses(upd):
    upd.install = selfupdate.Install("editable")
    r = upd.invoke()
    assert r.exit_code == 1 and "update the folder with git pull" in r.output and upd.cmds == []


def test_other_install_prints_the_command(upd):
    upd.install = selfupdate.Install("other")
    r = upd.invoke()
    assert r.exit_code == 1 and "This decaf is not a uv tool install" in r.output
    assert "uv tool install git+https://github.com/vcth4nh/decaf@v1.12.0" in r.output


def test_no_uv_prints_the_command(upd, monkeypatch):
    monkeypatch.setattr(selfupdate, "find_uv", lambda: None)
    r = upd.invoke()
    assert r.exit_code == 1 and "uv is not on PATH" in r.output
    assert "uv tool install git+https://github.com/vcth4nh/decaf@v1.12.0" in r.output


def test_update_runs_uv_and_checks_the_receipt(upd):
    r = upd.invoke()
    assert r.exit_code == 0
    assert upd.cmds == [["/usr/bin/uv", "tool", "install", "git+https://github.com/vcth4nh/decaf@v1.12.0"]]
    assert "decaf 1.11.0 -> 1.12.0" in r.output
    assert "https://github.com/vcth4nh/decaf/releases/tag/v1.12.0" in r.output


def test_update_passes_the_receipt_python(upd):
    upd.install = dataclasses.replace(upd.install, python="3.12")
    upd.invoke()
    assert upd.cmds[0][3:5] == ["--python", "3.12"]


def test_receipt_without_the_new_rev(upd):
    upd.write_receipt = False
    r = upd.invoke()
    assert r.exit_code == 1 and "uv ended, but the receipt does not show v1.12.0" in r.output


def test_uv_failure_prints_the_command(upd):
    upd.result = (2, False)
    r = upd.invoke()
    assert r.exit_code == 1 and "uv exited with code 2" in r.output
    assert "uv tool install git+https://github.com/vcth4nh/decaf@v1.12.0" in r.output


def test_active_run_stops_the_update(upd, lock_holder, tmp_path):
    with lock_holder(tmp_path / "cache" / "decaf.lock", exclusive=False):
        r = upd.invoke()
    assert r.exit_code == 1 and upd.cmds == []
    assert "another decaf run is active: let it end, then run `decaf self-update` again" in r.output


@pytest.mark.parametrize("result,code", [((0, False), 0), ((2, False), 1)])
def test_update_holds_the_lock_while_uv_runs(upd, tmp_path, result, code):
    # a run that starts during the install must stop, and the next run must start, also after a failure
    lock = tmp_path / "cache" / "decaf.lock"

    def run_starts():
        try:
            runlock.acquire(exclusive=False, path=lock).release()
        except runlock.LockBusy:
            return False
        return True

    upd.during = run_starts
    upd.result = result
    assert upd.invoke().exit_code == code and upd.seen_during == [False]
    assert run_starts()


def test_unusable_lock_file_warns_and_goes_on(upd, monkeypatch, tmp_path):
    (tmp_path / "file").write_text("x")
    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "file" / "cache")
    r = upd.invoke()
    assert r.exit_code == 0 and "warning: cannot check for active decaf runs" in r.output


def test_windows_moves_the_exe_and_pins_the_venv_python(upd, monkeypatch, tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "WINDOWS", True)
    monkeypatch.setattr(selfupdate, "base_python", lambda: "C:\\py\\python.exe")
    upd.install = dataclasses.replace(upd.install, exe=exe)
    upd.during = lambda: (not exe.exists(), (tmp_path / "decaf.exe.old").exists())
    r = upd.invoke()
    assert r.exit_code == 0 and upd.seen_during == [(True, True)]
    assert upd.cmds[0][3:5] == ["--python", "C:\\py\\python.exe"]


def test_windows_failure_restores_the_exe(upd, monkeypatch, tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "WINDOWS", True)
    upd.install = dataclasses.replace(upd.install, exe=exe)
    upd.result = (1, False)
    assert upd.invoke().exit_code == 1 and exe.read_bytes() == b"old"


def test_windows_failure_prints_a_windows_command(upd, monkeypatch):
    # the user pastes the command into a Windows terminal: it needs Windows quotes, not POSIX quotes
    monkeypatch.setattr(selfupdate, "WINDOWS", True)
    monkeypatch.setattr(selfupdate, "base_python", lambda: "C:\\Program Files\\Python\\python.exe")
    upd.result = (1, False)
    r = upd.invoke()
    assert f'/usr/bin/uv tool install --python "C:\\Program Files\\Python\\python.exe" {SPEC}' in r.output


def test_windows_without_the_exe_still_updates(upd, monkeypatch, tmp_path):
    # a user deleted decaf.exe: there is nothing to rename, and uv writes a new exe
    monkeypatch.setattr(selfupdate, "WINDOWS", True)
    upd.install = dataclasses.replace(upd.install, exe=tmp_path / "gone.exe")
    assert upd.invoke().exit_code == 0 and len(upd.cmds) == 1


def test_exe_that_cannot_move_stops_the_update(upd, monkeypatch, tmp_path):
    # without the rename, uv cannot replace the active exe on Windows: uv must not start
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "WINDOWS", True)

    def in_use(path):
        raise PermissionError("in use")

    monkeypatch.setattr(selfupdate, "move_exe_aside", in_use)
    upd.install = dataclasses.replace(upd.install, exe=exe)
    r = upd.invoke()
    assert r.exit_code == 1 and upd.cmds == []
    assert f"error: cannot move {exe} aside: in use" in r.output


@pytest.mark.parametrize("result,write_receipt,text,code", [
    ((1, False), True, "error: uv exited with code 1.", 1),
    ((130, True), True, "error: the update stopped.", 130),
    ((0, False), False, "error: uv ended, but the receipt does not show v1.12.0", 1),
])
def test_exe_that_cannot_return_names_both_paths(upd, monkeypatch, tmp_path, result, write_receipt, text, code):
    # the old exe keeps its .old name, so decaf has no command until the user renames it
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "WINDOWS", True)

    def in_use(exe, aside):
        raise PermissionError("in use")

    monkeypatch.setattr(selfupdate, "restore_exe", in_use)
    upd.install = dataclasses.replace(upd.install, exe=exe)
    upd.result, upd.write_receipt = result, write_receipt
    r = upd.invoke()
    assert r.exit_code == code and text in r.output
    assert r.output.endswith(f"\nrename {tmp_path / 'decaf.exe.old'} to {exe} by hand\n")


def test_ctrl_c_during_uv(upd, monkeypatch, tmp_path):
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "WINDOWS", True)
    upd.install = dataclasses.replace(upd.install, exe=exe)
    upd.result = (130, True)
    r = upd.invoke()
    assert r.exit_code == 130 and "the update stopped" in r.output and exe.read_bytes() == b"old"


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("result,write_receipt", [((0, False), True), ((2, False), True), ((130, True), True),
                                                  ((0, False), False)])
def test_no_new_imports_after_uv(upd, monkeypatch, tmp_path, windows, result, write_receipt):
    # uv can replace the files of a later import, so no exit path may import: done, failure, Ctrl-C, no rev
    exe = tmp_path / "decaf.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "WINDOWS", windows)  # True: the exe rename and its restore run too
    upd.install = dataclasses.replace(upd.install, exe=exe)
    upd.result, upd.write_receipt = result, write_receipt
    upd.snapshot_modules = True  # the fake run_uv saves set(sys.modules) in upd.modules
    upd.invoke()
    assert set(sys.modules) - upd.modules == set()


def test_run_uv_gives_the_exit_code():
    assert selfupdate.run_uv([sys.executable, "-c", "raise SystemExit(3)"]) == (3, False)


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill cannot send SIGINT to one process on Windows")
def test_run_uv_notes_a_ctrl_c():
    # The terminal sends a Ctrl-C to decaf and to uv. decaf must wait for uv to end, so that it can
    # restore the exe and print its text, and uv must get the Ctrl-C: the child must not ignore SIGINT.
    seen = []

    def outside(signum, frame):
        seen.append(signum)

    old = signal.signal(signal.SIGINT, outside)
    try:
        child = ("import os, signal, sys; os.kill(os.getppid(), signal.SIGINT); "
                 "sys.exit(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)")
        assert selfupdate.run_uv([sys.executable, "-c", child]) == (0, True)
        assert seen == [] and signal.getsignal(signal.SIGINT) is outside  # run_uv gave the handler back
    finally:
        signal.signal(signal.SIGINT, old)
