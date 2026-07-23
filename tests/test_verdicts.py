import json
import time
from pathlib import Path

from decaf.verdicts import NEGATIVE_TTL, ShaVerdict, VerdictCache

REPOS = ["https://repo1.maven.org/maven2"]
GAV = ("org.springframework", "spring-jdbc", "6.2.17")


def test_sha1_positive_roundtrip(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    assert cache.lookup_sha1("a" * 40, REPOS) is None
    cache.record_sha1("a" * 40, GAV, "verified-guess")
    got = cache.lookup_sha1("a" * 40, REPOS)
    assert got == ShaVerdict(gav=GAV, resolved_by="verified-guess")
    # positives are repo-independent
    assert cache.lookup_sha1("a" * 40, ["https://other.test/m2"]) == got


def test_sha1_negative_roundtrip_and_ttl(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    cache.record_sha1_miss("b" * 40, "no pom.properties; sha1 not in Central index", REPOS)
    got = cache.lookup_sha1("b" * 40, REPOS)
    assert got == ShaVerdict(gav=None, miss="no pom.properties; sha1 not in Central index")
    path = tmp_path / "sha1" / f"{'b' * 40}.json"
    data = json.loads(path.read_text())
    data["ts"] = time.time() - NEGATIVE_TTL - 1
    path.write_text(json.dumps(data))
    assert cache.lookup_sha1("b" * 40, REPOS) is None
    assert not path.exists()  # expired entries are pruned on read


def test_sha1_negative_repo_set_rules(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    two = ["https://a.test/m2", "https://b.test/m2"]
    cache.record_sha1_miss("c" * 40, "miss", two)
    assert cache.lookup_sha1("c" * 40, list(reversed(two))) is not None  # order-insensitive
    assert cache.lookup_sha1("c" * 40, two[:1]) is None  # subset does NOT match
    assert cache.lookup_sha1("c" * 40, [*two, "https://c.test/m2"]) is None
    assert (tmp_path / "sha1" / f"{'c' * 40}.json").exists()  # mismatch is kept on disk


def test_corrupt_and_wrong_shape_read_as_absent(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    d = tmp_path / "sha1"
    d.mkdir(parents=True)
    (d / f"{'d' * 40}.json").write_text("{not json")
    assert cache.lookup_sha1("d" * 40, REPOS) is None
    (d / f"{'e' * 40}.json").write_text('["a list"]')
    assert cache.lookup_sha1("e" * 40, REPOS) is None
    (d / f"{'f' * 40}.json").write_text('{"gav": ["only", "two"], "resolved_by": "x", "ts": 1}')
    assert cache.lookup_sha1("f" * 40, REPOS) is None


def test_fresh_mode_skips_lookups_but_writes(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    cache.record_sha1("a" * 40, GAV, "sha1-index")
    cache.record_no_sources(GAV, REPOS)
    fresh = VerdictCache(tmp_path, fresh=True)
    assert fresh.lookup_sha1("a" * 40, REPOS) is None
    assert fresh.has_no_sources(GAV, REPOS) is False
    fresh.record_sha1("g" * 40, GAV, "sha1-index")
    assert cache.lookup_sha1("g" * 40, REPOS) is not None  # fresh writes are visible


def test_no_sources_roundtrip(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    assert cache.has_no_sources(GAV, REPOS) is False
    cache.record_no_sources(GAV, REPOS)
    assert cache.has_no_sources(GAV, REPOS) is True
    assert cache.has_no_sources(GAV, ["https://other.test/m2"]) is False  # repo-set gate
    path = tmp_path / "gav" / "org.springframework_spring-jdbc_6.2.17.json"
    data = json.loads(path.read_text())
    data["ts"] = time.time() - NEGATIVE_TTL - 1
    path.write_text(json.dumps(data))
    assert cache.has_no_sources(GAV, REPOS) is False
    assert not path.exists()


def test_record_overwrites(tmp_path: Path):
    cache = VerdictCache(tmp_path)
    cache.record_sha1_miss("a" * 40, "old miss", REPOS)
    cache.record_sha1("a" * 40, GAV, "verified-guess")
    got = cache.lookup_sha1("a" * 40, REPOS)
    assert got is not None and got.gav == GAV
