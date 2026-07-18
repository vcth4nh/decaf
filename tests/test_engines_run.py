import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

import decaf.engines as engines
from decaf.engines import (
    ENGINES,
    PROCESSES,
    EngineResult,
    build_command,
    run_engine,
)

J = "/usr/bin/java"
JAR = Path("/cache/engine.jar")
OUT = Path("/out")


def test_build_command_vineflower():
    cmd = build_command(ENGINES["vineflower"], JAR, Path("/in/app.jar"), OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), "/in/app.jar", "/out"]


def test_build_command_cfr():
    cmd = build_command(ENGINES["cfr"], JAR, Path("/in/app.jar"), OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), "/in/app.jar", "--outputdir", "/out", "--silent", "true"]


def test_build_command_procyon_archive_and_class():
    spec = ENGINES["procyon"]
    assert build_command(spec, JAR, Path("/in/app.war"), OUT, java=J) == [
        J, "-jar", str(JAR), "-jar", "/in/app.war", "-o", "/out",
    ]
    assert build_command(spec, JAR, Path("/in/Foo.class"), OUT, java=J) == [
        J, "-jar", str(JAR), "-o", "/out", "/in/Foo.class",
    ]


def test_build_command_fernflower_uses_main_class():
    spec = ENGINES["fernflower"]
    cmd = build_command(spec, JAR, Path("/in/app.jar"), OUT, java=J)
    assert cmd == [J, "-cp", str(JAR), spec.main_class, "/in/app.jar", "/out"]


def test_build_command_jd():
    cmd = build_command(ENGINES["jd"], JAR, Path("/in/app.jar"), OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), "/in/app.jar", "-od", "/out"]


def _fake_build(script: str):
    """Replace build_command with one that runs a python script; {dest} is substituted."""

    def _b(spec, jar_path, target, dest, java="java"):
        return [sys.executable, "-c", script.format(dest=str(dest), target=str(target))]

    return _b


def test_run_engine_success(tmp_path: Path, monkeypatch):
    script = (
        "import pathlib, sys\n"
        "d = pathlib.Path(r'{dest}')\n"
        "(d / 'A.java').write_text('class A {{}}')\n"
        "(d / 'sub').mkdir()\n"
        "(d / 'sub' / 'B.java').write_text('class B {{}}')\n"
        "sys.stderr.write('all good')\n"
    )
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res == EngineResult("cfr", 0, False, 2, "all good")


def test_run_engine_timeout_kills_group(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engines, "build_command", _fake_build("import time; time.sleep(60)"))
    start = time.monotonic()
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=1)
    assert res.timed_out is True
    assert res.java_files == 0
    assert time.monotonic() - start < 10


def test_run_engine_unpacks_emitted_archive(tmp_path: Path, monkeypatch):
    script = (
        "import pathlib, zipfile\n"
        "d = pathlib.Path(r'{dest}')\n"
        "with zipfile.ZipFile(d / 'in.jar', 'w') as zf:\n"
        "    zf.writestr('com/x/A.java', 'class A {{}}')\n"
    )
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["fernflower"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res.java_files == 1
    assert (tmp_path / "out/com/x/A.java").read_text() == "class A {}"
    assert not (tmp_path / "out/in.jar").exists()


def test_run_engine_dir_target_iterates_for_non_native(tmp_path: Path, monkeypatch):
    tree = tmp_path / "classes"
    (tree / "com/x").mkdir(parents=True)
    (tree / "com/x/A.class").write_bytes(b"a")
    (tree / "com/x/A$1.class").write_bytes(b"inner")
    (tree / "B.class").write_bytes(b"b")

    seen: list[str] = []
    real_targets_script = "import pathlib; pathlib.Path(r'{dest}').joinpath('x.java').write_text('x')"

    def _b(spec, jar_path, target, dest, java="java"):
        seen.append(str(target))
        return [sys.executable, "-c", real_targets_script.format(dest=str(dest))]

    monkeypatch.setattr(engines, "build_command", _b)
    res = run_engine(ENGINES["procyon"], JAR, tree, tmp_path / "out", timeout=30)
    assert sorted(Path(s).name for s in seen) == ["A.class", "B.class"]  # no A$1
    assert res.returncode == 0


def test_process_registry_kill_all():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    PROCESSES.register(proc)
    assert PROCESSES.kill_all() == 1
    proc.wait(timeout=5)
    assert proc.returncode != 0


def test_closed_registry_refuses_to_spawn(tmp_path, monkeypatch):
    calls = []

    def _b(spec, jar_path, target, dest, java="java"):
        calls.append(1)
        return [sys.executable, "-c", "print('x')"]

    monkeypatch.setattr(engines, "build_command", _b)
    PROCESSES.kill_all()  # closes the registry
    try:
        res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=5)
        assert res.java_files == 0
        assert res.returncode != 0
        assert calls == []  # nothing spawned
    finally:
        PROCESSES.reset()


def test_registry_reset_reopens(tmp_path, monkeypatch):
    script = "import pathlib; pathlib.Path(r'{dest}').joinpath('A.java').write_text('class A {{}}')"
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    PROCESSES.kill_all()
    PROCESSES.reset()
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res.java_files == 1


def test_register_on_closed_registry_kills(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    PROCESSES.kill_all()
    try:
        PROCESSES.register(proc)
        proc.wait(timeout=5)
        assert proc.returncode != 0
    finally:
        PROCESSES.reset()
