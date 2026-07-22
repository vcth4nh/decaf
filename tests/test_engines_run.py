import os
import signal
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


APP = Path("/in/app.jar")  # str() so asserts match native path rendering on Windows


def test_build_command_vineflower():
    cmd = build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), str(APP), str(OUT)]


def test_build_command_cfr():
    cmd = build_command(ENGINES["cfr"], JAR, APP, OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), str(APP), "--outputdir", str(OUT), "--silent", "true"]


def test_build_command_procyon_archive_and_class():
    spec = ENGINES["procyon"]
    war, cls = Path("/in/app.war"), Path("/in/Foo.class")
    assert build_command(spec, JAR, war, OUT, java=J) == [
        J, "-jar", str(JAR), "-jar", str(war), "-o", str(OUT),
    ]
    assert build_command(spec, JAR, cls, OUT, java=J) == [
        J, "-jar", str(JAR), "-o", str(OUT), str(cls),
    ]


def test_build_command_fernflower_uses_main_class():
    spec = ENGINES["fernflower"]
    cmd = build_command(spec, JAR, APP, OUT, java=J)
    assert cmd == [J, "-cp", str(JAR), spec.main_class, str(APP), str(OUT)]


def test_build_command_jd():
    cmd = build_command(ENGINES["jd"], JAR, APP, OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), str(APP), "-od", str(OUT)]


def test_build_command_cpu_budget():
    cmd = build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J, cpu_budget=3)
    assert cmd == [J, "-XX:ActiveProcessorCount=3", "-jar", str(JAR), str(APP), str(OUT)]
    spec = ENGINES["fernflower"]
    cmd = build_command(spec, JAR, APP, OUT, java=J, cpu_budget=3)
    assert cmd == [J, "-XX:ActiveProcessorCount=3", "-cp", str(JAR), spec.main_class, str(APP), str(OUT)]


def test_run_engine_forwards_cpu_budget(tmp_path: Path, monkeypatch):
    seen = {}

    def _b(spec, jar_path, target, dest, java="java", cpu_budget=None):
        seen["budget"] = cpu_budget
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(engines, "build_command", _b)
    run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=10, cpu_budget=5)
    assert seen["budget"] == 5


def _fake_build(script: str):
    """Replace build_command with one that runs a python script; {dest} is substituted."""

    def _b(spec, jar_path, target, dest, java="java", cpu_budget=None):
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


def test_run_engine_streams_stderr_lines_live(tmp_path: Path, monkeypatch):
    # The engine writes one line, then waits for a gate file the callback creates
    # on seeing that line — so the test only finishes fast if lines stream live.
    script = (
        "import pathlib, sys, time\n"
        "sys.stderr.write('first line\\n'); sys.stderr.flush()\n"
        "gate = pathlib.Path(r'{dest}') / 'gate'\n"
        "deadline = time.time() + 10\n"
        "while not gate.exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "sys.stderr.write('second line\\n')\n"
    )
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    lines: list[str] = []

    def on_line(line: str) -> None:
        lines.append(line)
        if line == "first line":
            (tmp_path / "out" / "gate").write_text("go")

    start = time.monotonic()
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out",
                     timeout=30, on_stderr_line=on_line)
    assert time.monotonic() - start < 8  # engine exit was gated on the callback
    assert lines == ["first line", "second line"]
    assert res.stderr_tail.replace("\r\n", "\n") == "first line\nsecond line\n"


def test_run_engine_streaming_strips_crlf(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engines, "build_command",
                        _fake_build("import sys; sys.stderr.write('warn one\\r\\n')"))
    lines: list[str] = []
    run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out",
               timeout=30, on_stderr_line=lines.append)
    assert lines == ["warn one"]  # Windows children write \r\n line endings


def test_run_engine_streaming_timeout_still_kills(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engines, "build_command", _fake_build("import time; time.sleep(60)"))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out",
                     timeout=1, on_stderr_line=lambda line: None)
    assert res.timed_out is True


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

    def _b(spec, jar_path, target, dest, java="java", cpu_budget=None):
        seen.append(str(target))
        return [sys.executable, "-c", real_targets_script.format(dest=str(dest))]

    monkeypatch.setattr(engines, "build_command", _b)
    res = run_engine(ENGINES["procyon"], JAR, tree, tmp_path / "out", timeout=30)
    assert sorted(Path(s).name for s in seen) == ["A.class", "B.class"]  # no A$1
    assert res.returncode == 0


def test_run_engine_spawns_with_pdeathsig_hook(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engines, "build_command", _fake_build("pass"))
    seen = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    if sys.platform == "linux":
        assert seen["preexec_fn"] is engines._set_pdeathsig
    else:
        assert "preexec_fn" not in seen  # Windows rejects it; macOS has no prctl


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux-only")
def test_pdeathsig_reaps_child_when_spawner_hard_killed(tmp_path: Path):
    # Issue #18: SIGKILL the spawning process (decaf) and the kernel must reap
    # the engine child — no orphaned JVM decompiling at full tilt.
    script = (
        "import subprocess, sys, time\n"
        "import decaf.engines as engines\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                         start_new_session=True, **engines._SPAWN_KWARGS)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return  # kernel reaped it
            time.sleep(0.05)
        pytest.fail("child survived SIGKILL of its spawner")
    finally:
        for pid in (parent.pid, child_pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


def test_kill_group_falls_back_without_killpg(monkeypatch):
    # Windows has no os.killpg; the fallback must still kill the engine process.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        monkeypatch.delattr(os, "killpg", raising=False)  # absent on Windows already
        engines._kill_group(proc)
        proc.wait(timeout=5)
        assert proc.returncode != 0
    finally:
        if proc.poll() is None:
            proc.kill()


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

    def _b(spec, jar_path, target, dest, java="java", cpu_budget=None):
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


def test_run_engine_counts_kotlin_sources(tmp_path: Path, monkeypatch):
    script = (
        "import pathlib\n"
        "d = pathlib.Path(r'{dest}')\n"
        "(d / 'A.kt').write_text('fun a() {{}}')\n"
        "(d / 'B.java').write_text('class B {{}}')\n"
        "(d / 'notes.txt').write_text('not source')\n"
    )
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res.java_files == 2


def test_build_batch_command_multi_source():
    from decaf.engines import build_batch_command

    a, b = Path("/in/a.jar"), Path("/in/b.jar")
    assert build_batch_command(ENGINES["vineflower"], JAR, [a, b], OUT, java=J) == [
        J, "-jar", str(JAR), str(a), str(b), str(OUT),
    ]
    ff = ENGINES["fernflower"]
    assert build_batch_command(ff, JAR, [a, b], OUT, java=J, cpu_budget=3) == [
        J, "-XX:ActiveProcessorCount=3", "-cp", str(JAR), ff.main_class, str(a), str(b), str(OUT),
    ]
    assert build_batch_command(ENGINES["jd"], JAR, [a, b], OUT, java=J) == [
        J, "-jar", str(JAR), str(a), str(b), "-od", str(OUT),
    ]


def test_build_batch_command_rejects_single_input_engines():
    from decaf.engines import EngineError, build_batch_command

    with pytest.raises(EngineError, match="cannot batch"):
        build_batch_command(ENGINES["cfr"], JAR, [Path("/in/a.jar")], OUT, java=J)


def test_run_engine_batch_counts_merged_sources(tmp_path: Path, monkeypatch):
    from decaf.engines import run_engine_batch

    script = (
        "import pathlib\n"
        "d = pathlib.Path(r'{dest}')\n"
        "(d / 'com').mkdir(parents=True, exist_ok=True)\n"
        "(d / 'com' / 'A.java').write_text('class A {{}}')\n"
        "(d / 'com' / 'B.kt').write_text('fun b() {{}}')\n"
    )

    def fake_batch_build(spec, jar_path, targets, dest, java="java", cpu_budget=None):
        return [sys.executable, "-c", script.format(dest=str(dest))]

    monkeypatch.setattr(engines, "build_batch_command", fake_batch_build)
    res = run_engine_batch(
        ENGINES["vineflower"], JAR, [tmp_path / "a.jar", tmp_path / "b.jar"],
        tmp_path / "out", timeout=30,
    )
    assert res.returncode == 0 and res.java_files == 2


def test_build_command_cds_flags():
    cds = Path("/cache/engines")
    cmd = build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J, cds_dir=cds)
    ver = ENGINES["vineflower"].version
    assert cmd == [
        J,
        "-XX:+AutoCreateSharedArchive",
        f"-XX:SharedArchiveFile={cds / f'vineflower-{ver}.jsa'}",
        "-jar", str(JAR), str(APP), str(OUT),
    ]
    assert build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J) == [
        J, "-jar", str(JAR), str(APP), str(OUT),
    ]  # unchanged without cds_dir


def test_build_command_cds_follows_cpu_budget():
    cds = Path("/cache/engines")
    cmd = build_command(ENGINES["cfr"], JAR, APP, OUT, java=J, cpu_budget=3, cds_dir=cds)
    assert cmd[:4] == [
        J, "-XX:ActiveProcessorCount=3",
        "-XX:+AutoCreateSharedArchive",
        f"-XX:SharedArchiveFile={cds / ('cfr-' + ENGINES['cfr'].version + '.jsa')}",
    ]
