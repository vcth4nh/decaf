import hashlib
from pathlib import Path

import httpx
import pytest

from decaf.engines import EngineError, EngineSpec
from decaf import update

OLD = b"old jar bytes"
NEW = b"new jar bytes"
NEW_SHA = hashlib.sha256(NEW).hexdigest()
NEW_SHA1 = hashlib.sha1(NEW).hexdigest()

MAVEN_SPEC = EngineSpec(
    name="fake", version="1.0",
    url="https://repo.test/maven2/org/x/fake/1.0/fake-1.0.jar",
    sha256=hashlib.sha256(OLD).hexdigest(), min_java=11,
)

METADATA = "<metadata><versioning><release>2.0</release></versioning></metadata>"


def make_client(routes: dict[str, httpx.Response], log: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)
        return routes.get(url, httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler))


BASE = "https://repo.test/maven2/org/x/fake"


def test_maven_update_latest_sha256(tmp_path: Path):
    routes = {
        f"{BASE}/maven-metadata.xml": httpx.Response(200, text=METADATA),
        f"{BASE}/2.0/fake-2.0.jar": httpx.Response(200, content=NEW),
        f"{BASE}/2.0/fake-2.0.jar.sha256": httpx.Response(200, text=NEW_SHA + "  fake-2.0.jar\n"),
    }
    with make_client(routes) as c:
        res = update.update_engine(MAVEN_SPEC, c, tmp_path)
    assert res.version == "2.0" and res.old_version == "1.0"
    assert res.verified_via == "sha256"
    assert res.pin == {"version": "2.0", "url": f"{BASE}/2.0/fake-2.0.jar", "sha256": NEW_SHA}
    assert (tmp_path / "fake-2.0.jar").read_bytes() == NEW


def test_maven_already_latest_returns_none_without_download(tmp_path: Path):
    log: list[str] = []
    meta = "<metadata><versioning><release>1.0</release></versioning></metadata>"
    with make_client({f"{BASE}/maven-metadata.xml": httpx.Response(200, text=meta)}, log) as c:
        assert update.update_engine(MAVEN_SPEC, c, tmp_path) is None
    assert log == [f"{BASE}/maven-metadata.xml"]


def test_maven_sha1_fallback_warns_and_records_computed_sha256(tmp_path: Path):
    routes = {
        f"{BASE}/maven-metadata.xml": httpx.Response(200, text=METADATA),
        f"{BASE}/2.0/fake-2.0.jar": httpx.Response(200, content=NEW),
        f"{BASE}/2.0/fake-2.0.jar.sha1": httpx.Response(200, text=NEW_SHA1),
    }
    warnings: list[str] = []
    with make_client(routes) as c:
        res = update.update_engine(MAVEN_SPEC, c, tmp_path, warn=warnings.append)
    assert res.verified_via == "sha1"
    assert res.pin["sha256"] == NEW_SHA
    assert warnings and "sha1" in warnings[0]


def test_maven_no_checksum_fails_closed(tmp_path: Path):
    routes = {
        f"{BASE}/maven-metadata.xml": httpx.Response(200, text=METADATA),
        f"{BASE}/2.0/fake-2.0.jar": httpx.Response(200, content=NEW),
    }
    with make_client(routes) as c:
        with pytest.raises(EngineError, match="no sha256 or sha1"):
            update.update_engine(MAVEN_SPEC, c, tmp_path)
    assert list(tmp_path.iterdir()) == []  # no partial files left


def test_maven_checksum_mismatch_fails(tmp_path: Path):
    routes = {
        f"{BASE}/maven-metadata.xml": httpx.Response(200, text=METADATA),
        f"{BASE}/2.0/fake-2.0.jar": httpx.Response(200, content=b"tampered"),
        f"{BASE}/2.0/fake-2.0.jar.sha256": httpx.Response(200, text=NEW_SHA),
    }
    with make_client(routes) as c:
        with pytest.raises(EngineError, match="checksum mismatch"):
            update.update_engine(MAVEN_SPEC, c, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_maven_explicit_version_skips_metadata(tmp_path: Path):
    log: list[str] = []
    routes = {
        f"{BASE}/3.1/fake-3.1.jar": httpx.Response(200, content=NEW),
        f"{BASE}/3.1/fake-3.1.jar.sha256": httpx.Response(200, text=NEW_SHA),
    }
    with make_client(routes, log) as c:
        res = update.update_engine(MAVEN_SPEC, c, tmp_path, version="3.1")
    assert res.version == "3.1"
    assert not any("maven-metadata" in u for u in log)


def test_explicit_version_equal_to_pin_is_noop(tmp_path: Path):
    with make_client({}) as c:
        assert update.update_engine(MAVEN_SPEC, c, tmp_path, version="1.0") is None
