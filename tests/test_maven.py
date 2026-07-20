import json
from pathlib import Path

import httpx
import pytest

from decaf.config import MAVEN_CENTRAL
from decaf.maven import (
    Gav,
    candidate_coords,
    extract_java,
    fetch_sources,
    gav_from_central_sha1,
    gav_from_pom_properties,
    resolve_sources,
    sha1_of,
)

POM = "groupId=com.example\nartifactId=lib\nversion=1.2\n"
POM2 = "groupId=org.other\nartifactId=dep\nversion=9.9\n"


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gav_from_single_pom_properties(make_jar):
    jar = make_jar("lib-1.2.jar", {"META-INF/maven/com.example/lib/pom.properties": POM})
    assert gav_from_pom_properties(jar) == Gav("com.example", "lib", "1.2")


def test_gav_multi_pom_uses_filename_match(make_jar):
    entries = {
        "META-INF/maven/com.example/lib/pom.properties": POM,
        "META-INF/maven/org.other/dep/pom.properties": POM2,
    }
    shaded_named_like_lib = make_jar("lib-1.2.jar", entries)
    assert gav_from_pom_properties(shaded_named_like_lib) == Gav("com.example", "lib", "1.2")
    fat = make_jar("everything-fat.jar", entries)
    assert gav_from_pom_properties(fat) is None


def test_gav_missing_or_corrupt(make_jar, tmp_path: Path):
    plain = make_jar("plain.jar", {"A.class": b"x"})
    assert gav_from_pom_properties(plain) is None
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip")
    assert gav_from_pom_properties(bad) is None


def test_sha1_of_known_value(tmp_path: Path):
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    assert sha1_of(f) == "a9993e364706816aba3e25717850c26c9cd0d89d"


def test_sha1_lookup_hit_and_miss():
    def handler(request):
        assert request.url.host == "search.maven.org"
        q = request.url.params["q"]
        if "feedface" in q:
            docs = [{"g": "com.example", "a": "lib", "v": "1.2"}]
        else:
            docs = []
        return httpx.Response(200, json={"response": {"docs": docs}})

    with make_client(handler) as c:
        assert gav_from_central_sha1("feedface", c) == Gav("com.example", "lib", "1.2")
        assert gav_from_central_sha1("00000000", c) is None


def test_sha1_lookup_error_returns_none():
    with make_client(lambda r: httpx.Response(500)) as c:
        assert gav_from_central_sha1("feedface", c) is None


def test_fetch_sources_tries_repos_in_order_and_caches(tmp_path: Path, make_jar):
    gav = Gav("com.example", "lib", "1.2")
    sources = make_jar("payload.jar", {"com/example/A.java": "class A {}"})
    payload = sources.read_bytes()
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "second.test":
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    repos = ["https://first.test/m2", "https://second.test/m2"]
    with make_client(handler) as c:
        got = fetch_sources(gav, repos, c, tmp_path / "cache")
        assert got is not None
        path, repo = got
        assert repo == "https://second.test/m2"
        assert path.read_bytes() == payload
    assert calls == [
        "https://first.test/m2/com/example/lib/1.2/lib-1.2-sources.jar",
        "https://second.test/m2/com/example/lib/1.2/lib-1.2-sources.jar",
    ]

    def explode(request):
        raise AssertionError("cache should have been used")

    with make_client(explode) as c:
        again = fetch_sources(gav, repos, c, tmp_path / "cache")
        assert again == (path, "https://second.test/m2")


def test_fetch_sources_all_miss(tmp_path: Path):
    with make_client(lambda r: httpx.Response(404)) as c:
        assert fetch_sources(Gav("g", "a", "1"), ["https://x.test"], c, tmp_path) is None


def test_fetch_sources_survives_windows_replace_race(tmp_path: Path, make_jar, monkeypatch):
    import os as _os

    gav = Gav("com.example", "lib", "1.2")
    payload = make_jar("payload.jar", {"com/example/A.java": "class A {}"}).read_bytes()
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / "com.example_lib_1.2-sources.jar"
    cached.write_bytes(payload)  # a concurrent winner already put it in place

    def deny(src, dst):
        raise PermissionError(13, "Access is denied")  # Windows MoveFileEx race

    monkeypatch.setattr(_os, "replace", deny)
    with make_client(lambda request: httpx.Response(200, content=payload)) as c:
        result = fetch_sources(gav, ["https://r.test/m2"], c, cache)
    assert result == (cached, "https://r.test/m2")
    assert not list(cache.glob("*.part"))  # loser's temp file cleaned up


def test_fetch_sources_concurrent_same_gav(tmp_path: Path, make_jar):
    import threading
    import zipfile as _zip

    gav = Gav("com.example", "lib", "1.2")
    payload = make_jar("payload.jar", {"com/example/A.java": "class A {}"}).read_bytes()

    def handler(request):
        return httpx.Response(200, content=payload)

    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            with make_client(handler) as c:
                results.append(fetch_sources(gav, ["https://r.test/m2"], c, tmp_path / "cache"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 8 and all(r is not None for r in results)
    path = results[0][0]
    assert _zip.is_zipfile(path)
    assert path.read_bytes() == payload


def test_fetch_sources_rejects_non_zip_payload(tmp_path: Path):
    def handler(request):
        return httpx.Response(200, content=b"<html>error page</html>")

    with make_client(handler) as c:
        assert fetch_sources(Gav("g", "a", "1"), ["https://x.test"], c, tmp_path / "cache") is None
    assert not (tmp_path / "cache" / "g_a_1-sources.jar").exists()


def test_extract_java_only_java_files(make_jar, tmp_path: Path):
    jar = make_jar(
        "s.jar",
        {"com/x/A.java": "class A {}", "META-INF/MANIFEST.MF": "m", "com/x/B.java": "class B {}"},
    )
    out = tmp_path / "out"
    assert extract_java(jar, out) == 2
    assert (out / "com/x/A.java").is_file()
    assert not (out / "META-INF/MANIFEST.MF").exists()


def test_resolve_sources_pom_path_skips_sha1(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.2.jar", {"META-INF/maven/com.example/lib/pom.properties": POM})
    sources_payload = make_jar("p.jar", {"com/example/A.java": "class A {}"}).read_bytes()

    def handler(request):
        assert request.url.host != "search.maven.org", "sha1 lookup must not run"
        return httpx.Response(200, content=sources_payload)

    with make_client(handler) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert res is not None
    gav, path, repo = res
    assert str(gav) == "com.example:lib:1.2"


def test_resolve_sources_sha1_fallback(make_jar, tmp_path: Path):
    jar = make_jar("mystery.jar", {"A.class": b"x"})
    sources_payload = make_jar("p.jar", {"A.java": "class A {}"}).read_bytes()

    def handler(request):
        if request.url.host == "search.maven.org":
            return httpx.Response(
                200, json={"response": {"docs": [{"g": "g", "a": "a", "v": "1"}]}}
            )
        return httpx.Response(200, content=sources_payload)

    with make_client(handler) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert res is not None and str(res[0]) == "g:a:1"
    with make_client(handler) as c:
        res2 = resolve_sources(
            jar, ["https://r.test/m2"], c, tmp_path / "cache", allow_sha1=False
        )
    assert res2 is None


@pytest.mark.network
def test_real_sources_roundtrip(tmp_path: Path):
    url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.13/slf4j-api-2.0.13.jar"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        jar = tmp_path / "slf4j-api-2.0.13.jar"
        jar.write_bytes(client.get(url).content)
        res = resolve_sources(jar, [MAVEN_CENTRAL], client, tmp_path / "cache")
        assert res is not None
        gav, sources, repo = res
        assert str(gav) == "org.slf4j:slf4j-api:2.0.13"
        assert repo == MAVEN_CENTRAL
        assert extract_java(sources, tmp_path / "out") > 10


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("spring-jdbc-6.2.17.jar", [("spring-jdbc", "6.2.17")]),
        ("commons-lang3-3.12.0.jar", [("commons-lang3", "3.12.0")]),
        ("guava-33.0.0-jre.jar", [("guava", "33.0.0-jre")]),
        ("foo-2.jar", [("foo", "2")]),
        ("mystery.jar", []),  # no dash-followed-by-digit
        ("lib-abc.jar", []),  # dash but no digit after it
    ],
)
def test_candidate_coords_from_filename(make_jar, filename, expected):
    jar = make_jar(filename, {"com/x/A.class": b"x"})
    assert candidate_coords(jar) == expected


MANIFEST = (
    "Manifest-Version: 1.0\r\n"
    "Implementation-Title: spring-jdbc\r\n"
    "Implementation-Version: 6.2.17\r\n"
    "Automatic-Module-Name: spring.jdbc\r\n"
)


def test_candidate_coords_manifest_rescues_renamed_jar(make_jar):
    jar = make_jar("renamed.jar", {"META-INF/MANIFEST.MF": MANIFEST})
    assert candidate_coords(jar) == [("spring-jdbc", "6.2.17")]


def test_candidate_coords_dedups_filename_and_manifest(make_jar):
    jar = make_jar("spring-jdbc-6.2.17.jar", {"META-INF/MANIFEST.MF": MANIFEST})
    assert candidate_coords(jar) == [("spring-jdbc", "6.2.17")]


def test_candidate_coords_rejects_spacey_title(make_jar):
    manifest = "Implementation-Title: Spring JDBC\r\nImplementation-Version: 6.2.17\r\n"
    jar = make_jar("renamed.jar", {"META-INF/MANIFEST.MF": manifest})
    assert candidate_coords(jar) == []


def test_candidate_coords_bundle_symbolic_name(make_jar):
    manifest = (
        "Bundle-SymbolicName: org.springframework.spring-jdbc;singleton:=true\r\n"
        "Bundle-Version: 6.2.17\r\n"
    )
    jar = make_jar("renamed.jar", {"META-INF/MANIFEST.MF": manifest})
    assert candidate_coords(jar) == [("spring-jdbc", "6.2.17")]


def test_candidate_coords_manifest_continuation_line(make_jar):
    manifest = (
        "Implementation-Title: spring-\r\n jdbc\r\n"
        "Implementation-Version: 6.2.17\r\n"
    )
    jar = make_jar("renamed.jar", {"META-INF/MANIFEST.MF": manifest})
    assert candidate_coords(jar) == [("spring-jdbc", "6.2.17")]


def test_candidate_coords_bad_zip(tmp_path: Path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip")
    assert candidate_coords(bad) == []
