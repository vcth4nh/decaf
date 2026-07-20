import hashlib
from pathlib import Path

from decaf.engines import ENGINES, EngineSpec, active_specs, cache_status

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
