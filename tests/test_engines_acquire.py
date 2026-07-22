import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from decaf.engines import (
    ENGINE_ORDER,
    ENGINES,
    EngineError,
    EngineSpec,
    ensure_engine,
    parse_java_major,
)


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def spec_for(data: bytes, **kw) -> EngineSpec:
    return EngineSpec(
        name="fake",
        version="1.0",
        url="https://x.test/fake.jar",
        sha256=hashlib.sha256(data).hexdigest(),
        min_java=11,
        **kw,
    )


def test_download_writes_cache_then_skips_network(tmp_path: Path):
    data = b"jarbytes"
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=data)

    spec = spec_for(data)
    with make_client(handler) as c:
        jar = ensure_engine(spec, c, cache_dir=tmp_path)
        again = ensure_engine(spec, c, cache_dir=tmp_path)
    assert jar == again == tmp_path / "fake-1.0.jar"
    assert jar.read_bytes() == data
    assert calls == ["https://x.test/fake.jar"]


def test_checksum_mismatch_raises_and_leaves_no_jar(tmp_path: Path):
    spec = spec_for(b"expected")
    with make_client(lambda r: httpx.Response(200, content=b"tampered")) as c:
        with pytest.raises(EngineError, match="checksum mismatch"):
            ensure_engine(spec, c, cache_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_http_error_raises(tmp_path: Path):
    spec = spec_for(b"x")
    with make_client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(EngineError, match="download failed"):
            ensure_engine(spec, c, cache_dir=tmp_path)


def test_corrupted_cache_is_redownloaded(tmp_path: Path):
    data = b"goodbytes"
    spec = spec_for(data)
    (tmp_path / "fake-1.0.jar").write_bytes(b"rotten")
    with make_client(lambda r: httpx.Response(200, content=data)) as c:
        jar = ensure_engine(spec, c, cache_dir=tmp_path)
    assert jar.read_bytes() == data


def test_zip_member_extraction(tmp_path: Path):
    inner = b"inner jar bytes"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tool.jar", inner)
        zf.writestr("README", b"hi")
    zipped = buf.getvalue()
    spec = EngineSpec(
        name="fake",
        version="1.0",
        url="https://x.test/dist.zip",
        sha256=hashlib.sha256(inner).hexdigest(),
        min_java=11,
        download_sha256=hashlib.sha256(zipped).hexdigest(),
        archive_member="tool.jar",
    )
    with make_client(lambda r: httpx.Response(200, content=zipped)) as c:
        jar = ensure_engine(spec, c, cache_dir=tmp_path)
    assert jar == tmp_path / "fake-1.0.jar"
    assert jar.read_bytes() == inner


def test_registry_complete_and_ordered():
    assert list(ENGINES) == ENGINE_ORDER == ["vineflower", "cfr", "fernflower", "procyon", "jd"]
    for spec in ENGINES.values():
        assert spec.url.startswith("https://")
        assert len(spec.sha256) == 64
        assert spec.min_java >= 11
    assert ENGINES["fernflower"].main_class is not None
    assert ENGINES["jd"].archive_member == "jd-cli.jar"


@pytest.mark.parametrize(
    "text,major",
    [
        ('openjdk version "21.0.2" 2024-01-16', 21),
        ('java version "1.8.0_392"', 8),
        ('openjdk version "11.0.22" 2024-01-16', 11),
        ('openjdk version "25" 2025-09-16', 25),
        ("no version here", None),
    ],
)
def test_parse_java_major(text: str, major: int | None):
    assert parse_java_major(text) == major


def test_ensure_engine_download_hook_fires_only_on_download(tmp_path: Path):
    data = b"jarbytes"
    fired: list[int] = []
    spec = spec_for(data)
    with make_client(lambda r: httpx.Response(200, content=data)) as c:
        ensure_engine(spec, c, cache_dir=tmp_path, on_download=lambda: fired.append(1))
        ensure_engine(spec, c, cache_dir=tmp_path, on_download=lambda: fired.append(2))
    assert fired == [1]  # second call served from cache, hook not fired


@pytest.mark.network
def test_real_engine_downloads(tmp_path: Path):
    with httpx.Client(follow_redirects=True, timeout=120) as c:
        for spec in ENGINES.values():
            jar = ensure_engine(spec, c, cache_dir=tmp_path)
            assert jar.stat().st_size > 1_000_000
