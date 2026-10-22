import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from decaf.cli import app
from decaf.config import load_config
from decaf.engines import ENGINES, EngineSpec, active_specs, cache_status

runner = CliRunner(env={"COLUMNS": "200"})
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SHA = "b" * 64


def test_active_specs_merges_overrides():
    ov = {"cfr": {"version": "9.9", "url": "https://x.test/cfr-9.9.jar", "sha256": SHA}}
    specs = active_specs(ov)
    assert specs["cfr"].version == "9.9"
    assert specs["cfr"].url == "https://x.test/cfr-9.9.jar"
    assert specs["cfr"].min_java == ENGINES["cfr"].min_java  # not overridable, inherited
    assert specs["vineflower"] is ENGINES["vineflower"]
    assert active_specs(None) == dict(ENGINES)
    assert active_specs({}) == dict(ENGINES)


def test_cache_status_checks_presence_and_hash(tmp_path: Path):
    data = b"jarbytes"
    spec = EngineSpec(
        name="fake", version="1.0", url="https://x.test/fake.jar",
        sha256=hashlib.sha256(data).hexdigest(), min_java=11,
    )
    assert cache_status(spec, tmp_path) is False
    (tmp_path / "fake-1.0.jar").write_bytes(b"rotten")
    assert cache_status(spec, tmp_path) is False
    (tmp_path / "fake-1.0.jar").write_bytes(data)
    assert cache_status(spec, tmp_path) is True


def test_preflight_uses_override_spec(monkeypatch):
    import decaf.engines as engines
    from decaf.pipeline import Settings, _preflight_engines

    seen = {}

    def fake_ensure(spec, client, cache_dir=None):
        seen[spec.name] = spec.version
        return Path(f"/fake/{spec.name}.jar")

    monkeypatch.setattr(engines, "ensure_engine", fake_ensure)
    settings = Settings(
        input=Path("."), output=Path("out"), engine="cfr", fallback=False,
        engine_overrides={"cfr": {"version": "9.9", "url": "https://x.test/c.jar", "sha256": SHA}},
    )
    chain, jars = _preflight_engines(settings, java_major=21, client=None)
    assert chain == ["cfr"]
    assert seen["cfr"] == "9.9"
    assert jars["cfr"] == Path("/fake/cfr.jar")


@pytest.fixture(autouse=True)
def _isolated_default_config(tmp_path: Path, monkeypatch):
    """CLI invocations without --config must never read the developer's real user config."""
    import decaf.config as config

    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "no-user-config.toml")


def _row(plain: str, name: str) -> str:
    return next(line for line in plain.splitlines() if name in line)


def test_engines_list_shows_pins_cache_and_java(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 17))
    data = b"vf-jar-bytes"
    digest = hashlib.sha256(data).hexdigest()
    cache = tmp_path / "engines"
    cache.mkdir()
    (cache / "vineflower-9.9.jar").write_bytes(data)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        f'[engines.vineflower]\nversion = "9.9"\nurl = "https://x.test/vf.jar"\nsha256 = "{digest}"\n'
    )
    result = runner.invoke(app, ["engines", "list", "--config", str(cfgf)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    vf = _row(plain, "vineflower")
    assert "9.9†" in vf and "yes" in vf          # override marker + cached (real hash match)
    assert "no" in _row(plain, "cfr")            # not cached
    assert "needs 21+" in _row(plain, "fernflower")  # java 17 too old
    assert str(tmp_path / "engines") in plain    # cache dir footer


def _jar_bytes(java: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("e/Main.class", b"\xca\xfe\xba\xbe\x00\x00" + (java + 44).to_bytes(2, "big"))
    return buf.getvalue()


def test_engines_list_reads_the_java_need_of_a_cached_jar(tmp_path: Path, monkeypatch):
    """#113: the newest Fernflower needs Java 25, but its built-in spec says 21."""
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    data = _jar_bytes(25)
    (tmp_path / "engines").mkdir()
    (tmp_path / "engines/fernflower-261.1.jar").write_bytes(data)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        '[engines.fernflower]\nversion = "261.1"\nurl = "https://x.test/ff.jar"\n'
        f'sha256 = "{hashlib.sha256(data).hexdigest()}"\n'
    )
    result = runner.invoke(app, ["engines", "list", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert "needs 25+" in _row(ANSI.sub("", result.output), "fernflower")


def test_engines_list_without_java(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: None)
    result = runner.invoke(app, ["engines", "list"])
    assert result.exit_code == 0
    assert "not found" in ANSI.sub("", result.output)


def test_engines_word_hits_subcommand_not_run(tmp_path: Path):
    # spec edge: `decaf engines` reaches the subcommand group; a folder literally
    # named engines needs `decaf ./engines`
    result = runner.invoke(app, ["engines"])
    plain = ANSI.sub("", result.output)
    assert "does not exist" not in plain  # never treated as run's INPUT
    assert "list" in plain               # engines group help/usage


def test_engines_fetch_reports_and_exit_code(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    fetched = []

    def fake_ensure(spec, client, cache_dir=None):
        if spec.name == "cfr":
            raise engines.EngineError("cfr: download failed: boom")
        fetched.append(spec.name)
        return tmp_path / "engines" / f"{spec.name}-{spec.version}.jar"

    monkeypatch.setattr(engines, "ensure_engine", fake_ensure)
    result = runner.invoke(app, ["engines", "fetch"])
    assert result.exit_code == 1
    plain = ANSI.sub("", result.output)
    assert "downloaded" in plain and "cfr" in plain and "boom" in plain
    assert fetched == ["vineflower", "fernflower", "procyon", "jd"]

    fetched.clear()
    result = runner.invoke(app, ["engines", "fetch", "vineflower"])
    assert result.exit_code == 0
    assert fetched == ["vineflower"]


@pytest.mark.parametrize("command, broken", [
    ("fetch", "ensure_engine"),  # for example a read-only engine cache
    ("update", "update_engine"),
    ("list", "cache_status"),  # for example an unreadable jar
])
def test_engine_commands_give_the_cause_of_a_cache_error(tmp_path: Path, monkeypatch, command, broken):
    """Review E5: an OSError of the engine cache gave a traceback in these commands."""
    import decaf.engines as engines
    import decaf.update as upd

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    part = tmp_path / "engines" / "x.part"

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(part))

    monkeypatch.setattr(upd if broken == "update_engine" else engines, broken, denied)
    cfgf = tmp_path / "config.toml"
    cfgf.write_text("")
    result = runner.invoke(app, ["engines", command, "--config", str(cfgf)])
    assert not isinstance(result.exception, OSError)
    plain = ANSI.sub("", result.output)
    assert f"vineflower: Permission denied: {part}" in plain.replace("\n", "")
    assert result.exit_code == (0 if command == "list" else 1)


def test_engines_fetch_already_cached(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    data = b"vf-jar-bytes"
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "engines").mkdir()
    (tmp_path / "engines" / "vineflower-9.9.jar").write_bytes(data)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        f'[engines.vineflower]\nversion = "9.9"\nurl = "https://x.test/vf.jar"\nsha256 = "{digest}"\n'
    )
    monkeypatch.setattr(engines, "ensure_engine", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    result = runner.invoke(app, ["engines", "fetch", "vineflower", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert "already cached" in ANSI.sub("", result.output)


def _seed_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "engines"
    cache.mkdir(parents=True)
    (cache / f"cfr-{ENGINES['cfr'].version}.jar").write_bytes(b"current")
    (cache / "cfr-0.001.jar").write_bytes(b"superseded")
    (cache / "vineflower-1.12.0.part").write_bytes(b"debris")
    (cache / "jd-extract").mkdir()
    (cache / "jd-extract" / "leftover").write_bytes(b"x")
    return cache


def test_engines_clean_stale_keeps_active_pins(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cache = _seed_cache(tmp_path)
    result = runner.invoke(app, ["engines", "clean", "--stale"])
    assert result.exit_code == 0
    assert sorted(p.name for p in cache.iterdir()) == [f"cfr-{ENGINES['cfr'].version}.jar"]
    assert "removed" in ANSI.sub("", result.output)


def test_engines_clean_stale_keeps_override_version(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cache = tmp_path / "engines"
    cache.mkdir(parents=True)
    (cache / "cfr-9.9.jar").write_bytes(b"override-active")
    (cache / f"cfr-{ENGINES['cfr'].version}.jar").write_bytes(b"superseded by override")
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(f'[engines.cfr]\nversion = "9.9"\nurl = "https://x.test/cfr-9.9.jar"\nsha256 = "{SHA}"\n')
    result = runner.invoke(app, ["engines", "clean", "--stale", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert sorted(p.name for p in cache.iterdir()) == ["cfr-9.9.jar"]


def test_engines_clean_stale_keeps_the_cds_archives_of_the_active_pins(tmp_path: Path, monkeypatch):
    """#113: --stale deleted the .jsa files of the active pins and of the current checker."""
    import decaf.check as check
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cache = tmp_path / "engines"
    cache.mkdir(parents=True)
    cfr = f"cfr-{ENGINES['cfr'].version}"
    keep = [f"{cfr}.jar", f"{cfr}.jsa", f"checker-{check.source_digest()}-21.jsa"]
    for name in [*keep, "cfr-0.001.jsa", "checker-000000000000-21.jsa"]:
        (cache / name).write_bytes(b"x")
    result = runner.invoke(app, ["engines", "clean", "--stale"])
    assert result.exit_code == 0
    assert sorted(p.name for p in cache.iterdir()) == sorted(keep)


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs extra rights on Windows")
@pytest.mark.parametrize("args", [["--stale"], []])
def test_engines_clean_removes_a_symlink_but_not_its_target(tmp_path: Path, monkeypatch, args):
    """#113: a symlink to a folder in the cache gave a traceback."""
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "cache")
    cache = tmp_path / "cache/engines"
    cache.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"x")
    (target / "also.txt").write_bytes(b"y")  # the count is of the link, not of these files
    (cache / "jd-extract").symlink_to(target, target_is_directory=True)
    result = runner.invoke(app, ["engines", "clean", *args])
    assert result.exit_code == 0, result.output
    assert not (cache / "jd-extract").is_symlink() and (target / "keep.txt").is_file()
    assert "removed 1 files" in ANSI.sub("", result.output)


def test_engines_clean_removes_the_compiled_checker(tmp_path: Path, monkeypatch):
    """#101: no command removed cache_root()/checker; --stale keeps the checker of the current source."""
    import decaf.check as check
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    current = f"{check.source_digest()}-21"
    for name in (current, "000000000000-21"):
        (tmp_path / "checker" / name).mkdir(parents=True)
        (tmp_path / "checker" / name / check.JAR).write_bytes(b"x")
    result = runner.invoke(app, ["engines", "clean", "--stale"])
    assert result.exit_code == 0, result.output
    assert [p.name for p in (tmp_path / "checker").iterdir()] == [current]
    assert "removed 1 files" in ANSI.sub("", result.output)
    result = runner.invoke(app, ["engines", "clean"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "checker").exists()
    assert "removed 1 files" in ANSI.sub("", result.output)


def test_engines_clean_removes_everything(tmp_path: Path, monkeypatch):
    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cache = _seed_cache(tmp_path)
    result = runner.invoke(app, ["engines", "clean"])
    assert result.exit_code == 0
    assert not cache.exists()

    result = runner.invoke(app, ["engines", "clean"])  # idempotent on empty
    assert result.exit_code == 0
    assert "nothing to clean" in ANSI.sub("", result.output)


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is a link of Windows")
@pytest.mark.parametrize("args", [["--stale"], []])
def test_engines_clean_removes_a_junction_but_not_its_target(tmp_path: Path, monkeypatch, args):
    """Review E7: Path.is_symlink is False for a junction, and rmtree refuses it."""
    import subprocess

    import decaf.engines as engines

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path / "cache")
    cache = tmp_path / "cache/engines"
    cache.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"x")
    (target / "also.txt").write_bytes(b"y")  # the count is of the junction, not of these files
    subprocess.run(["cmd", "/c", "mklink", "/J", str(cache / "jd-extract"), str(target)], check=True, capture_output=True)
    result = runner.invoke(app, ["engines", "clean", *args])
    assert result.exit_code == 0, result.output
    assert not (cache / "jd-extract").exists() and (target / "keep.txt").is_file()
    assert "removed 1 files" in ANSI.sub("", result.output)


def test_a_windows_junction_counts_as_a_link(tmp_path: Path, monkeypatch):
    """Review E7: Python 3.11 has no Path.is_junction, so decaf reads the reparse tag."""
    import stat
    from types import SimpleNamespace

    from decaf import cli

    junction = tmp_path / "jd-extract"
    junction.mkdir()
    real = Path.lstat
    fake = SimpleNamespace(st_mode=stat.S_IFDIR, st_size=8, st_reparse_tag=0xA0000003)  # IO_REPARSE_TAG_MOUNT_POINT
    monkeypatch.setattr(Path, "lstat", lambda self: fake if self == junction else real(self))
    assert cli._is_link(junction) and not cli._is_link(tmp_path)


def _fake_result(name: str, old: str):
    import decaf.update as upd

    return upd.UpdateResult(
        name, old, "9.9",
        {"version": "9.9", "url": f"https://x.test/{name}-9.9.jar", "sha256": "c" * 64},
        "sha256",
    )


def test_update_writes_pins_and_reports(tmp_path: Path, monkeypatch):
    import decaf.engines as engines
    import decaf.update as upd

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cfgf = tmp_path / "config.toml"
    cfgf.write_text("")

    def fake_update(spec, client, cache_dir, version=None, warn=None):
        return _fake_result("cfr", spec.version) if spec.name == "cfr" else None

    monkeypatch.setattr(upd, "update_engine", fake_update)
    result = runner.invoke(app, ["engines", "update", "--config", str(cfgf)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert f"cfr {ENGINES['cfr'].version} → 9.9" in plain
    assert "already at latest" in plain  # the four None engines
    assert load_config(cfgf).engine_overrides["cfr"]["version"] == "9.9"


def test_update_warns_when_the_new_pin_needs_a_newer_java(tmp_path: Path, monkeypatch):
    """#113: `engines update` pinned a Fernflower that needs Java 25 and said only ok."""
    import decaf.engines as engines
    import decaf.update as upd

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(engines, "find_java", lambda: ("java", 21))
    (tmp_path / "engines").mkdir()
    (tmp_path / "engines/fernflower-9.9.jar").write_bytes(_jar_bytes(25))
    cfgf = tmp_path / "config.toml"
    cfgf.write_text("")

    def fake_update(spec, client, cache_dir, version=None, warn=None):
        return _fake_result("fernflower", spec.version) if spec.name == "fernflower" else None

    monkeypatch.setattr(upd, "update_engine", fake_update)
    result = runner.invoke(app, ["engines", "update", "--config", str(cfgf)])
    assert result.exit_code == 0
    plain = ANSI.sub("", result.output)
    assert "fernflower 9.9 needs Java 25+, and the java on PATH is Java 21" in plain
    assert load_config(cfgf).engine_overrides["fernflower"]["version"] == "9.9"  # pinned all the same


def test_update_failure_keeps_config_and_exits_1(tmp_path: Path, monkeypatch):
    import decaf.engines as engines
    import decaf.update as upd

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cfgf = tmp_path / "config.toml"
    cfgf.write_text("")

    def fake_update(spec, client, cache_dir, version=None, warn=None):
        if spec.name == "cfr":
            raise engines.EngineError("cfr: upstream publishes no sha256 or sha1 for 2.0 — not updating")
        return None

    monkeypatch.setattr(upd, "update_engine", fake_update)
    result = runner.invoke(app, ["engines", "update", "--config", str(cfgf)])
    assert result.exit_code == 1
    assert "not updating" in ANSI.sub("", result.output)
    assert load_config(cfgf).engine_overrides == {}


def test_update_version_requires_exactly_one_engine(tmp_path: Path):
    cfgf = tmp_path / "config.toml"
    cfgf.write_text("")
    r1 = runner.invoke(app, ["engines", "update", "--version", "9.9", "--config", str(cfgf)])
    r2 = runner.invoke(app, ["engines", "update", "cfr", "jd", "--version", "9.9", "--config", str(cfgf)])
    assert r1.exit_code == 2 and r2.exit_code == 2


def test_update_reset_restores_builtin_pins(tmp_path: Path, monkeypatch):
    sha = "c" * 64
    cfgf = tmp_path / "config.toml"
    cfgf.write_text(
        f'[engines.cfr]\nversion = "9.9"\nurl = "https://x.test/cfr.jar"\nsha256 = "{sha}"\n'
        f'[engines.jd]\nversion = "8.8"\nurl = "https://x.test/jd.jar"\nsha256 = "{sha}"\n'
    )
    result = runner.invoke(app, ["engines", "update", "--reset", "cfr", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert "cfr" in ANSI.sub("", result.output)
    assert list(load_config(cfgf).engine_overrides) == ["jd"]

    result = runner.invoke(app, ["engines", "update", "--reset", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert load_config(cfgf).engine_overrides == {}

    result = runner.invoke(app, ["engines", "update", "--reset", "--config", str(cfgf)])
    assert result.exit_code == 0
    assert "no overrides" in ANSI.sub("", result.output)


def test_update_config_write_failure_fails_engine_cleanly(tmp_path: Path, monkeypatch):
    import decaf.engines as engines
    import decaf.update as upd

    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    cfgf = tmp_path / "config.toml"
    cfgf.write_text("")
    (tmp_path / "config.toml.tmp").mkdir()  # blocks the atomic-write temp file

    def fake_update(spec, client, cache_dir, version=None, warn=None):
        return _fake_result("cfr", spec.version) if spec.name == "cfr" else None

    monkeypatch.setattr(upd, "update_engine", fake_update)
    result = runner.invoke(app, ["engines", "update", "--config", str(cfgf)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback escaped
    plain = ANSI.sub("", result.output)
    assert "cfr: config write failed" in plain
    assert load_config(cfgf).engine_overrides == {}


def test_reset_config_write_failure_is_clean_error(tmp_path: Path):
    sha = "c" * 64
    cfgf = tmp_path / "config.toml"
    cfgf.write_text(f'[engines.cfr]\nversion = "9.9"\nurl = "https://x.test/cfr.jar"\nsha256 = "{sha}"\n')
    (tmp_path / "config.toml.tmp").mkdir()
    result = runner.invoke(app, ["engines", "update", "--reset", "cfr", "--config", str(cfgf)])
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "error: config write failed" in ANSI.sub("", result.output)
    assert load_config(cfgf).engine_overrides["cfr"]["version"] == "9.9"  # override intact
