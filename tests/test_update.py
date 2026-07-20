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


import io
import zipfile

GH_SPEC = EngineSpec(
    name="ghfake", version="1.0",
    url="https://github.com/own/proj/releases/download/v1.0/tool-1.0.jar",
    sha256=hashlib.sha256(OLD).hexdigest(), min_java=11,
)
API = "https://api.github.com/repos/own/proj"
DL = "https://gh.test/dl/tool-2.0.jar"


def _release(tag: str, assets: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"tag_name": tag, "assets": assets})


def test_github_update_latest_with_digest(tmp_path: Path):
    routes = {
        f"{API}/releases/latest": _release(
            "v2.0",
            [{"name": "tool-2.0.jar", "digest": f"sha256:{NEW_SHA}", "browser_download_url": DL}],
        ),
        DL: httpx.Response(200, content=NEW),
    }
    with make_client(routes) as c:
        res = update.update_engine(GH_SPEC, c, tmp_path)
    assert res.version == "2.0" and res.verified_via == "github-digest"
    assert res.pin == {"version": "2.0", "url": DL, "sha256": NEW_SHA}
    assert (tmp_path / "ghfake-2.0.jar").read_bytes() == NEW


def test_github_no_digest_fails_closed(tmp_path: Path):
    routes = {
        f"{API}/releases/latest": _release(
            "v2.0", [{"name": "tool-2.0.jar", "browser_download_url": DL}]
        ),
    }
    with make_client(routes) as c:
        with pytest.raises(EngineError, match="no sha256 digest"):
            update.update_engine(GH_SPEC, c, tmp_path)


def test_github_unrecognized_tag_fails(tmp_path: Path):
    routes = {f"{API}/releases/latest": _release("release-2.0", [])}
    with make_client(routes) as c:
        with pytest.raises(EngineError, match="tag"):
            update.update_engine(GH_SPEC, c, tmp_path)


def test_github_explicit_version_uses_tag_endpoint(tmp_path: Path):
    log: list[str] = []
    routes = {
        f"{API}/releases/tags/v3.0": _release(
            "v3.0",
            [{"name": "tool-3.0.jar", "digest": f"sha256:{NEW_SHA}", "browser_download_url": DL}],
        ),
        DL: httpx.Response(200, content=NEW),
    }
    with make_client(routes, log) as c:
        res = update.update_engine(GH_SPEC, c, tmp_path, version="3.0")
    assert res.version == "3.0"
    assert any("releases/tags/v3.0" in u for u in log)


def test_github_zip_dist_records_both_hashes(tmp_path: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tool.jar", NEW)
        zf.writestr("README", b"hi")
    dist = buf.getvalue()
    dist_sha = hashlib.sha256(dist).hexdigest()
    spec = EngineSpec(
        name="ghzip", version="1.0",
        url="https://github.com/own/proj/releases/download/v1.0/tool-1.0-dist.zip",
        sha256=hashlib.sha256(OLD).hexdigest(), min_java=11,
        download_sha256="0" * 64, archive_member="tool.jar",
    )
    dl = "https://gh.test/dl/tool-2.0-dist.zip"
    routes = {
        f"{API}/releases/latest": _release(
            "v2.0",
            [{"name": "tool-2.0-dist.zip", "digest": f"sha256:{dist_sha}", "browser_download_url": dl}],
        ),
        dl: httpx.Response(200, content=dist),
    }
    with make_client(routes) as c:
        res = update.update_engine(spec, c, tmp_path)
    assert res.pin == {
        "version": "2.0", "url": dl, "sha256": NEW_SHA,
        "download_sha256": dist_sha, "archive_member": "tool.jar",
    }
    assert (tmp_path / "ghzip-2.0.jar").read_bytes() == NEW
