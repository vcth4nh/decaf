import errno
import os
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

import decaf.engines as engines
import decaf.scanner as scanner
from decaf.engines import (
    ENGINES,
    PROCESSES,
    EngineResult,
    build_command,
    run_engine,
)

J = "/usr/bin/java"
JAR = Path("/cache/engine.jar")
OUT = Path(os.path.abspath("/out"))  # absolute also on Windows, where an engine path gets the drive (#113)


APP = Path(os.path.abspath("/in/app.jar"))  # str() so asserts match native path rendering on Windows


def test_build_command_vineflower():
    cmd = build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), str(APP), str(OUT)]


def test_build_command_cfr():
    cmd = build_command(ENGINES["cfr"], JAR, APP, OUT, java=J)
    assert cmd == [J, "-jar", str(JAR), str(APP), "--outputdir", str(OUT), "--silent", "true"]


def test_build_command_procyon_archive_and_class():
    spec = ENGINES["procyon"]
    war, cls = Path(os.path.abspath("/in/app.war")), Path(os.path.abspath("/in/Foo.class"))
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


def test_build_command_keeps_the_dot_dot_of_a_path():
    """Review E4: abspath removed `..` as text, but the kernel resolves `..` after a symlink."""
    target = Path("link/../libs/a.jar")
    given = str(Path.cwd() / "link" / ".." / "libs" / "a.jar")
    assert given in build_command(ENGINES["cfr"], JAR, target, Path("out"), java=J)
    assert given in engines.build_batch_command(ENGINES["cfr"], JAR, [target], Path("out"), java=J)


@pytest.mark.parametrize("name", list(ENGINES))
def test_build_command_gives_absolute_paths(name):
    """Security (#113): a relative input name that starts with "-" must not reach the engine as an option."""
    cmd = build_command(ENGINES[name], JAR, Path("-in.jar"), Path("out"), java=J)
    assert os.path.abspath("-in.jar") in cmd and os.path.abspath("out") in cmd
    assert not any(arg.startswith("-in") for arg in cmd)
    batch = engines.build_batch_command(ENGINES[name], JAR, [Path("-in.jar"), Path("b.jar")], Path("out"), java=J)
    assert {os.path.abspath("-in.jar"), os.path.abspath("b.jar"), os.path.abspath("out")} <= set(batch)
    assert not any(arg.startswith("-in") for arg in batch)


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


def _fake_build(script: str, python: str = sys.executable):
    """Replace build_command with one that runs a python script; {dest} is substituted."""

    def _b(spec, jar_path, target, dest, java="java", cpu_budget=None):
        return [python, "-c", script.format(dest=str(dest), target=str(target))]

    return _b


def _fake_batch(script: str, seen: list[list[str]] | None = None):
    """Replace build_batch_command with one that runs a python script; {dest} is substituted.

    seen gets the class file names of each command (#101: the one JVM for the classes of a folder).
    """

    def _bb(spec, jar_path, targets, dest, java="java", cpu_budget=None, **kwargs):
        if seen is not None:
            seen.append(sorted(Path(t).name for t in targets))
        return [sys.executable, "-c", script.format(dest=str(dest))]

    return _bb


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
    assert (res.engine, res.returncode, res.timed_out, res.java_files, res.stderr_tail) == (
        "cfr", 0, False, 2, "all good",
    )
    assert res.out_of_memory is False


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


def test_run_engine_survives_an_incomplete_emitted_archive(tmp_path: Path, monkeypatch):
    """#113: a Fernflower timeout leaves a cut-off jar, and the unpack must not stop the chain."""
    script = "import pathlib; pathlib.Path(r'{dest}').joinpath('in.jar').write_bytes(b'PK\\x03\\x04 cut off')"
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["fernflower"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res.java_files == 0 and not (tmp_path / "out/in.jar").exists()


def test_run_engine_stops_at_a_disk_error_of_the_unpack(tmp_path: Path, monkeypatch):
    """Review E1: the unpack hid a full disk, so a cut-off file went to the output."""
    script = (
        "import pathlib, zipfile\n"
        "d = pathlib.Path(r'{dest}')\n"
        "with zipfile.ZipFile(d / 'in.jar', 'w') as zf:\n"
        "    zf.writestr('com/x/A.java', 'class A {{}}')\n"
    )
    monkeypatch.setattr(engines, "build_command", _fake_build(script))

    def full(src, out):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(scanner.shutil, "copyfileobj", full)
    with pytest.raises(OSError, match="No space left"):
        run_engine(ENGINES["fernflower"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)


def test_class_runs_of_a_folder_share_the_timeout(tmp_path: Path, monkeypatch):
    """#113: --timeout is for one archive or folder, not for each of its classes."""
    tree = tmp_path / "classes"
    tree.mkdir()
    for i in range(5):
        (tree / f"C{i}.class").write_bytes(b"c")
    clock = [100.0]
    given: list[float] = []

    def fake_exec(spec, cmd, timeout, on_stderr_line):  # the one JVM for all classes fails after 0.5 s
        given.append(timeout)
        clock[0] += 0.5
        return EngineResult(spec.name, 1, False, 0, "boom")

    def fake_run_once(spec, jar_path, target, dest, timeout, *args, **kwargs):
        given.append(timeout)
        clock[0] += 0.5  # each class takes 0.5 s, in its time
        return EngineResult(spec.name, 0, False, 0, "")

    monkeypatch.setattr(engines.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engines, "_exec_command", fake_exec)
    monkeypatch.setattr(engines, "_run_once", fake_run_once)
    res = run_engine(ENGINES["cfr"], JAR, tree, tmp_path / "out", timeout=1.0)
    assert given == [1.0, 0.5] and res.timed_out  # no time is left for the other four classes


def test_run_engine_dir_target_runs_the_top_level_classes_in_one_jvm(tmp_path: Path, monkeypatch):
    """#101: CFR and Procyon take class files as positional arguments, so the classes of a folder need one
    JVM, not one JVM for each class. An inner class rides with its outer class."""
    tree = tmp_path / "classes"
    (tree / "com/x").mkdir(parents=True)
    (tree / "com/x/A.class").write_bytes(b"a")
    (tree / "com/x/A$1.class").write_bytes(b"inner")
    (tree / "B.class").write_bytes(b"b")
    seen: list[list[str]] = []
    script = "import pathlib; pathlib.Path(r'{dest}').joinpath('x.java').write_text('x')"
    monkeypatch.setattr(engines, "build_batch_command", _fake_batch(script, seen))
    res = run_engine(ENGINES["procyon"], JAR, tree, tmp_path / "out", timeout=30)
    assert seen == [["A.class", "B.class"]]  # one JVM, no A$1
    assert res.returncode == 0 and res.java_files == 1


def test_run_per_class_runs_top_level_dollar_classes(tmp_path: Path, monkeypatch):
    """#106: only a class whose outer class file exists rides with it."""
    tree = tmp_path / "classes"
    (tree / "com/x").mkdir(parents=True)
    for name in ("A", "A$1", "$Util", "$Util$In", "Gone$In", "Gone$In$1"):
        (tree / f"com/x/{name}.class").write_bytes(b"c")
    seen: list[list[str]] = []
    monkeypatch.setattr(engines, "build_batch_command", _fake_batch("pass", seen))
    run_engine(ENGINES["cfr"], JAR, tree, tmp_path / "out", timeout=30)
    assert seen == [["$Util.class", "A.class", "Gone$In.class"]]


def test_run_per_class_runs_one_jvm_for_each_class_when_the_one_jvm_fails(tmp_path: Path, monkeypatch):
    """#101: a JVM that stops with an error can leave classes without a file, so each class of its group
    then gets a JVM of its own, also a class that got a file."""
    tree = tmp_path / "classes"
    (tree / "p").mkdir(parents=True)
    for name in ("A", "B", "C"):
        (tree / f"p/{name}.class").write_bytes(b"c")
    script = (
        "import pathlib, sys\n"
        "d = pathlib.Path(r'{dest}') / 'p'\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        "(d / 'A.java').write_text('class A {{}}')\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(engines, "build_batch_command", _fake_batch(script))
    alone: list[str] = []

    def once(spec, jar_path, target, dest, timeout, *args, **kwargs):
        alone.append(Path(target).name)
        (Path(dest) / "p").mkdir(parents=True, exist_ok=True)
        (Path(dest) / "p" / f"{Path(target).stem}.java").write_text("class X {}")
        return EngineResult(spec.name, 0, False, 0, "")

    monkeypatch.setattr(engines, "_run_once", once)
    res = run_engine(ENGINES["cfr"], JAR, tree, tmp_path / "out", timeout=30)
    assert alone == ["A.class", "B.class", "C.class"]
    assert res.returncode == 0 and res.java_files == 3  # the class JVMs decide the exit code


def test_run_per_class_keeps_the_exit_code_of_a_class_jvm_that_fails(tmp_path: Path, monkeypatch):
    tree = tmp_path / "classes"
    (tree / "p").mkdir(parents=True)
    (tree / "p/A.class").write_bytes(b"a")
    (tree / "p/B.class").write_bytes(b"b")
    monkeypatch.setattr(engines, "build_batch_command", _fake_batch("import sys; sys.exit(1)"))
    results = iter([EngineResult("cfr", 0, False, 0, ""), EngineResult("cfr", 2, False, 0, "bad B")])
    monkeypatch.setattr(engines, "_run_once", lambda *a, **k: next(results))
    res = run_engine(ENGINES["cfr"], JAR, tree, tmp_path / "out", timeout=30)
    assert res.returncode == 2 and "bad B" in res.stderr_tail


def test_run_per_class_runs_one_jvm_for_each_class_when_the_one_jvm_runs_out_of_memory(tmp_path: Path, monkeypatch):
    tree = tmp_path / "classes"
    (tree / "p").mkdir(parents=True)
    (tree / "p/A.class").write_bytes(b"a")
    (tree / "p/B.class").write_bytes(b"b")
    oom = "java.lang.OutOfMemoryError: Java heap space"
    monkeypatch.setattr(
        engines, "_exec_command", lambda spec, cmd, timeout, on_stderr_line: EngineResult(spec.name, 0, False, 0, oom, 500, True)
    )
    alone: list[str] = []

    def once(spec, jar_path, target, dest, timeout, *args, **kwargs):
        alone.append(Path(target).name)
        return EngineResult(spec.name, 0, False, 0, "")

    monkeypatch.setattr(engines, "_run_once", once)
    res = run_engine(ENGINES["cfr"], JAR, tree, tmp_path / "out", timeout=30)
    assert alone == ["A.class", "B.class"] and res.out_of_memory and res.returncode == 0


def test_run_per_class_bounds_the_length_of_one_command(tmp_path: Path, monkeypatch):
    """#101: Windows takes at most 32,767 characters in one command line."""
    tree = tmp_path / "classes"
    (tree / "p").mkdir(parents=True)
    for name in ("A", "B", "C"):
        (tree / f"p/{name}.class").write_bytes(b"c")
    seen: list[list[str]] = []
    monkeypatch.setattr(engines, "build_batch_command", _fake_batch("pass", seen))
    monkeypatch.setattr(engines, "_ARGS_MAX", 2 * (len(str((tree / "p/A.class").absolute())) + 1))
    run_engine(ENGINES["cfr"], JAR, tree, tmp_path / "out", timeout=30)
    assert seen == [["A.class", "B.class"], ["C.class"]]


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

    a, b = Path(os.path.abspath("/in/a.jar")), Path(os.path.abspath("/in/b.jar"))
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
    assert build_batch_command(ENGINES["cfr"], JAR, [a, b], OUT, java=J) == [
        J, "-jar", str(JAR), str(a), str(b), "--outputdir", str(OUT), "--silent", "true",
    ]
    assert build_batch_command(ENGINES["procyon"], JAR, [a, b], OUT, java=J) == [
        J, "-jar", str(JAR), str(a), str(b), "-o", str(OUT),
    ]


def test_build_batch_command_rejects_non_batch_engines(monkeypatch):
    from dataclasses import replace

    from decaf.engines import EngineError, build_batch_command

    bogus = replace(ENGINES["cfr"], name="bogus")
    with pytest.raises(EngineError, match="cannot batch"):
        build_batch_command(bogus, JAR, [Path("/in/a.jar")], OUT, java=J)
    # In BATCH_ENGINES but with no dispatch arm: loud failure, no fallthrough.
    monkeypatch.setattr(engines, "BATCH_ENGINES", engines.BATCH_ENGINES | {"bogus"})
    with pytest.raises(EngineError, match="unknown engine"):
        build_batch_command(bogus, JAR, [Path("/in/a.jar")], OUT, java=J)


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


EA = ["--renameillegalidents", "true"]  # opaque tokens; position is what's under test


def test_build_command_splices_engine_args():
    # vineflower/fernflower dialects take options BEFORE the positionals
    assert build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), *EA, str(APP), str(OUT),
    ]
    ff = ENGINES["fernflower"]
    assert build_command(ff, JAR, APP, OUT, java=J, engine_args=EA) == [
        J, "-cp", str(JAR), ff.main_class, *EA, str(APP), str(OUT),
    ]
    # cfr/procyon/jd take flags anywhere: appended after the built-ins (last wins)
    assert build_command(ENGINES["cfr"], JAR, APP, OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), str(APP), "--outputdir", str(OUT), "--silent", "true", *EA,
    ]
    assert build_command(ENGINES["procyon"], JAR, APP, OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), "-jar", str(APP), "-o", str(OUT), *EA,
    ]
    cls = Path(os.path.abspath("/in/Foo.class"))
    assert build_command(ENGINES["procyon"], JAR, cls, OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), "-o", str(OUT), str(cls), *EA,
    ]
    assert build_command(ENGINES["jd"], JAR, APP, OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), str(APP), "-od", str(OUT), *EA,
    ]


def test_build_batch_command_splices_engine_args():
    from decaf.engines import build_batch_command

    a, b = Path(os.path.abspath("/in/a.jar")), Path(os.path.abspath("/in/b.jar"))
    ff = ENGINES["fernflower"]
    assert build_batch_command(ENGINES["vineflower"], JAR, [a, b], OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), *EA, str(a), str(b), str(OUT),
    ]
    assert build_batch_command(ff, JAR, [a, b], OUT, java=J, engine_args=EA) == [
        J, "-cp", str(JAR), ff.main_class, *EA, str(a), str(b), str(OUT),
    ]
    assert build_batch_command(ENGINES["cfr"], JAR, [a, b], OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), str(a), str(b), "--outputdir", str(OUT), "--silent", "true", *EA,
    ]
    assert build_batch_command(ENGINES["procyon"], JAR, [a, b], OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), str(a), str(b), "-o", str(OUT), *EA,
    ]
    assert build_batch_command(ENGINES["jd"], JAR, [a, b], OUT, java=J, engine_args=EA) == [
        J, "-jar", str(JAR), str(a), str(b), "-od", str(OUT), *EA,
    ]


def test_run_engine_threads_engine_args(tmp_path: Path, monkeypatch):
    PROCESSES.reset()
    seen = {}

    def fake_exec(spec, cmd, timeout, on_stderr_line):
        seen["cmd"] = cmd
        return EngineResult(spec.name, 0, False, 0, "")

    monkeypatch.setattr(engines, "_exec_command", fake_exec)
    target = tmp_path / "app.jar"
    target.write_bytes(b"")
    run_engine(ENGINES["vineflower"], JAR, target, tmp_path / "out", 10.0, java=J, engine_args=["-dgs=1"])
    assert "-dgs=1" in seen["cmd"]


def test_run_engine_batch_threads_engine_args(tmp_path: Path, monkeypatch):
    from decaf.engines import run_engine_batch

    seen = {}

    def fake_exec(spec, cmd, timeout, on_stderr_line):
        seen["cmd"] = cmd
        return EngineResult(spec.name, 0, False, 0, "")

    monkeypatch.setattr(engines, "_exec_command", fake_exec)
    run_engine_batch(
        ENGINES["vineflower"], JAR, [tmp_path / "a.jar", tmp_path / "b.jar"],
        tmp_path / "out", 10.0, java=J, engine_args=["-dgs=1"],
    )
    assert "-dgs=1" in seen["cmd"]


def test_run_process_reports_exit_code_and_stderr_tail():
    PROCESSES.reset()
    rc, timed_out, tail, _, _ = engines.run_process(
        [sys.executable, "-c", "import sys; sys.stderr.write('bad thing'); sys.exit(3)"], 30
    )
    assert (rc, timed_out) == (3, False)
    assert "bad thing" in tail


def test_run_process_times_out():
    PROCESSES.reset()
    _, timed_out, _, _, _ = engines.run_process([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
    assert timed_out is True


def test_run_process_refuses_after_registry_closed():
    PROCESSES.kill_all()
    try:
        assert engines.run_process([sys.executable, "-c", "pass"], 5) == (-1, False, "interrupted", None, False)
    finally:
        PROCESSES.reset()


def test_run_process_kills_the_child_when_the_wait_raises(monkeypatch):
    """Review E3: a wait error removed the JVM from the registry and left it running."""
    PROCESSES.reset()
    killed: list = []
    real_kill = engines._kill_group

    def kill(proc):
        killed.append(proc.pid)
        real_kill(proc)

    def broken_wait(self, timeout):
        raise OverflowError("timestamp out of range for platform time_t")

    monkeypatch.setattr(engines, "_kill_group", kill)
    monkeypatch.setattr(engines._PeakWatch, "wait", broken_wait)
    with pytest.raises(OverflowError):
        engines.run_process([sys.executable, "-c", "import time; time.sleep(30)"], 5)
    assert len(killed) == 1


def test_build_command_heap_flag():
    cmd = build_command(ENGINES["vineflower"], JAR, APP, OUT, java=J, cpu_budget=3, heap_mb=2048)
    assert cmd[:3] == [J, "-XX:ActiveProcessorCount=3", "-Xmx2048m"]
    assert build_command(ENGINES["cfr"], JAR, APP, OUT, java=J, heap_mb=512)[:2] == [J, "-Xmx512m"]
    for name in ENGINES:
        assert "-Xmx512m" in build_command(ENGINES[name], JAR, APP, OUT, java=J, heap_mb=512)
        assert not any(t.startswith("-Xmx") for t in build_command(ENGINES[name], JAR, APP, OUT, java=J))


def test_build_batch_command_heap_flag():
    from decaf.engines import build_batch_command

    a, b = Path("/in/a.jar"), Path("/in/b.jar")
    cmd = build_batch_command(ENGINES["vineflower"], JAR, [a, b], OUT, java=J, cpu_budget=2, heap_mb=1700)
    assert cmd[:3] == [J, "-XX:ActiveProcessorCount=2", "-Xmx1700m"]
    assert not any(t.startswith("-Xmx") for t in build_batch_command(ENGINES["cfr"], JAR, [a, b], OUT, java=J))


def test_run_engine_forwards_heap(tmp_path: Path, monkeypatch):
    seen = {}

    def _b(spec, jar_path, target, dest, java="java", cpu_budget=None, heap_mb=None):
        seen["heap"] = heap_mb
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(engines, "build_command", _b)
    run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=10, heap_mb=1700)
    assert seen["heap"] == 1700


def _above_own_rss_mb() -> int:
    """MB for a test child to touch: on Linux, a forked child starts with the pages of this process (#113)."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 2**20 + 64
    except OSError:
        return 64


def test_run_engine_measures_the_peak_rss(tmp_path: Path, monkeypatch):
    mb = _above_own_rss_mb()
    script = f"b = bytearray({mb} * 1024 * 1024)\nfor i in range(0, len(b), 4096):\n    b[i] = 1\n"
    # On Windows, the python.exe of a venv is a launcher that starts the interpreter as a child
    # process, and decaf measures the process that it starts: start the interpreter itself.
    monkeypatch.setattr(engines, "build_command", _fake_build(script, python=sys._base_executable))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=60)
    assert res.returncode == 0
    assert res.peak_rss_mb is not None and mb <= res.peak_rss_mb < mb + 1024  # a wrong ru_maxrss unit gives ×1024


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the floor is for the fork of Linux")
def test_a_child_below_the_rss_of_decaf_has_no_peak(tmp_path: Path, monkeypatch):
    """#113: ru_maxrss of a forked child keeps the pages of decaf from before the exec."""
    monkeypatch.setattr(engines, "build_command", _fake_build("pass", python=sys._base_executable))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=60)
    assert res.returncode == 0 and res.peak_rss_mb is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the floor is for the fork of Linux")
def test_the_floor_allows_for_growth_of_decaf_before_the_fork(tmp_path: Path, monkeypatch):
    """Other threads of decaf can allocate between the read of its RSS and the fork."""
    rss = engines._own_rss()
    monkeypatch.setattr(engines, "_own_rss", lambda: rss - (12 << 20))  # decaf grew by 12 MB
    monkeypatch.setattr(engines, "build_command", _fake_build("pass", python=sys._base_executable))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=60)
    assert res.returncode == 0 and res.peak_rss_mb is None


@pytest.mark.parametrize("text", ["Java heap space", "GC overhead limit exceeded"])
def test_run_engine_flags_the_short_out_of_heap_text(tmp_path: Path, monkeypatch, text: str):
    """#113: on a small heap, Procyon prints only the message of the OutOfMemoryError and exits with 255."""
    script = f"import sys\nsys.stderr.write({text!r} + '\\n')\nsys.exit(255)\n"
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["procyon"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res.returncode == 255 and res.out_of_memory is True


def test_run_engine_keeps_the_exit_code(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engines, "build_command", _fake_build("import sys; sys.exit(3)"))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert res.returncode == 3


def test_run_engine_flags_out_of_memory_in_the_full_stderr(tmp_path: Path, monkeypatch):
    script = (
        "import sys\n"
        "sys.stderr.write('Exception in thread main java.lang.OutOfMemoryError: Java heap space\\n')\n"
        "sys.stderr.write('x' * 5000)\n"
    )
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=30)
    assert "OutOfMemoryError" not in res.stderr_tail  # cut from the 2000-character tail
    assert res.out_of_memory is True


def test_run_engine_timeout_keeps_a_peak(tmp_path: Path, monkeypatch):
    mb = _above_own_rss_mb()
    script = f"b = bytearray({mb} * 1024 * 1024)\nfor i in range(0, len(b), 4096):\n    b[i] = 1\nimport time\ntime.sleep(60)\n"
    monkeypatch.setattr(engines, "build_command", _fake_build(script, python=sys._base_executable))
    start = time.monotonic()
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=3)
    assert res.timed_out is True
    assert isinstance(res.peak_rss_mb, int)
    assert time.monotonic() - start < 10


def test_kill_all_ends_a_measured_run(tmp_path: Path, monkeypatch):
    PROCESSES.reset()
    monkeypatch.setattr(engines, "build_command", _fake_build("import time; time.sleep(60)"))
    killer = threading.Timer(1.0, PROCESSES.kill_all)
    killer.start()
    start = time.monotonic()
    try:
        res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=60)
    finally:
        killer.join()
        PROCESSES.reset()
    assert time.monotonic() - start < 10
    assert res.returncode != 0


def test_run_engine_drains_a_large_stderr(tmp_path: Path, monkeypatch):
    script = "import sys\nsys.stderr.write('x' * (5 * 1024 * 1024))\nsys.stderr.write('END')\n"
    monkeypatch.setattr(engines, "build_command", _fake_build(script))
    res = run_engine(ENGINES["cfr"], JAR, tmp_path / "in.jar", tmp_path / "out", timeout=60)
    assert res.returncode == 0
    assert res.stderr_tail.endswith("END") and len(res.stderr_tail) == 2000


def test_run_per_class_keeps_the_largest_peak(tmp_path: Path, monkeypatch):
    root = tmp_path / "classes"
    (root / "p").mkdir(parents=True)
    (root / "p/A.class").write_bytes(b"a")
    (root / "p/B.class").write_bytes(b"b")
    monkeypatch.setattr(
        engines, "_exec_command", lambda spec, cmd, timeout, on_stderr_line: EngineResult(spec.name, 1, False, 0, "", 10, False)
    )
    results = iter([
        EngineResult("cfr", 0, False, 0, "", 30, True),
        EngineResult("cfr", 0, False, 0, "", 5, False),
    ])
    monkeypatch.setattr(engines, "_run_once", lambda *a, **k: next(results))
    res = run_engine(ENGINES["cfr"], JAR, root, tmp_path / "out", timeout=10)
    assert (res.peak_rss_mb, res.out_of_memory) == (30, True)


def test_run_per_class_gives_the_one_jvm_and_each_class_jvm_the_heap(tmp_path: Path, monkeypatch):
    root = tmp_path / "classes"
    (root / "p").mkdir(parents=True)
    (root / "p/A.class").write_bytes(b"a")
    (root / "p/B.class").write_bytes(b"b")
    heaps = []

    def _bb(spec, jar_path, targets, dest, java="java", cpu_budget=None, heap_mb=None, **kwargs):
        heaps.append(("all", heap_mb))
        return [sys.executable, "-c", "import sys; sys.exit(1)"]

    def once(*args, heap_mb=None, **kwargs):
        heaps.append(("one", heap_mb))
        return EngineResult("cfr", 0, False, 0, "")

    monkeypatch.setattr(engines, "build_batch_command", _bb)
    monkeypatch.setattr(engines, "_run_once", once)
    run_engine(ENGINES["cfr"], JAR, root, tmp_path / "out", timeout=10, heap_mb=1700)
    assert heaps == [("all", 1700), ("one", 1700), ("one", 1700)]  # each JVM gets the artifact's heap
