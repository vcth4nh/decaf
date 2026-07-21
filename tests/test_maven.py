import json
from pathlib import Path

import httpx
import pytest

import decaf.maven as maven
from decaf.config import MAVEN_CENTRAL
from decaf.maven import (
    Gav,
    MAX_PROBES,
    NetState,
    NetworkFailure,
    ResolutionLog,
    candidate_coords,
    candidate_groups,
    extract_java,
    fetch_sources,
    gav_from_central_sha1,
    gav_from_pom_properties,
    resolve_sources,
    sha1_of,
    verify_gav,
)

POM = "groupId=com.example\nartifactId=lib\nversion=1.2\n"
POM2 = "groupId=org.other\nartifactId=dep\nversion=9.9\n"


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(maven, "RETRY_BACKOFF", (0.0, 0.0))


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
    assert str(res.gav) == "com.example:lib:1.2"
    assert res.sources_jar is not None
    assert res.resolved_by == "pom-properties"
    assert res.miss is None


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
    assert str(res.gav) == "g:a:1"
    assert res.sources_jar is not None
    assert res.resolved_by == "sha1-index"

    with make_client(handler) as c:
        res2 = resolve_sources(
            jar, ["https://r.test/m2"], c, tmp_path / "cache", allow_sha1=False
        )
    assert res2.sources_jar is None
    assert res2.miss == (
        "no pom.properties; sha1 lookup skipped; "
        "no artifact/version hints in filename or manifest"
    )


@pytest.mark.network
def test_real_sources_roundtrip(tmp_path: Path):
    url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.13/slf4j-api-2.0.13.jar"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        jar = tmp_path / "slf4j-api-2.0.13.jar"
        jar.write_bytes(client.get(url).content)
        res = resolve_sources(jar, [MAVEN_CENTRAL], client, tmp_path / "cache")
        assert str(res.gav) == "org.slf4j:slf4j-api:2.0.13"
        assert res.repo == MAVEN_CENTRAL
        assert extract_java(res.sources_jar, tmp_path / "out") > 10


@pytest.mark.network
def test_real_verified_guess_post_freeze_artifact(tmp_path: Path):
    # spring-jdbc 6.2.17 (2026-03): Gradle-built (no pom.properties) and released
    # after the legacy SHA-1 index froze — only the verified-guess step can find it.
    url = "https://repo1.maven.org/maven2/org/springframework/spring-jdbc/6.2.17/spring-jdbc-6.2.17.jar"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        jar = tmp_path / "spring-jdbc-6.2.17.jar"
        jar.write_bytes(client.get(url).content)
        res = resolve_sources(jar, [MAVEN_CENTRAL], client, tmp_path / "cache")
        assert str(res.gav) == "org.springframework:spring-jdbc:6.2.17"
        assert res.resolved_by == "verified-guess"
        assert res.repo == MAVEN_CENTRAL
        assert extract_java(res.sources_jar, tmp_path / "out") > 10


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


def test_candidate_groups_index_then_packages(make_jar):
    jar = make_jar(
        "spring-jdbc-6.2.17.jar",
        {
            "org/springframework/jdbc/core/JdbcTemplate.class": b"x",
            "org/springframework/jdbc/support/JdbcUtils.class": b"x",
            "module-info.class": b"x",
            "META-INF/services/whatever": b"x",
        },
    )

    def handler(request):
        assert request.url.host == "search.maven.org"
        assert request.url.params["q"] == 'a:"spring-jdbc"'
        assert request.url.params["rows"] == "5"
        docs = [
            {"g": "org.springframework", "a": "spring-jdbc", "v": "6.2.8"},
            {"g": "net.xdob.springframework", "a": "spring-jdbc", "v": "5.3.41"},
        ]
        return httpx.Response(200, json={"response": {"docs": docs}})

    with make_client(handler) as c:
        groups = candidate_groups("spring-jdbc", jar, c)
    assert groups == [
        "org.springframework",
        "net.xdob.springframework",
        "org.springframework.jdbc",
    ]


def test_candidate_groups_index_error_falls_back_to_packages(make_jar):
    jar = make_jar("lib-1.0.jar", {"com/acme/lib/A.class": b"x"})

    def handler(request):
        return httpx.Response(500)

    with make_client(handler) as c:
        groups = candidate_groups("lib", jar, c)
    assert groups == ["com.acme.lib", "com.acme"]


def test_groups_from_index_memoized_per_client(make_jar):
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"response": {"docs": [{"g": "com.acme", "a": "lib", "v": "1"}]}})

    jar = make_jar("lib-1.0.jar", {"com/acme/lib/A.class": b"x"})
    with make_client(handler) as c:
        first = candidate_groups("lib", jar, c)
        second = candidate_groups("lib", jar, c)
    assert first == second
    assert len(calls) == 1  # repeated artifactId served from the per-run cache

    with make_client(handler) as c2:  # a new client (new run) queries afresh
        candidate_groups("lib", jar, c2)
    assert len(calls) == 2


@pytest.mark.parametrize("root", ["WEB-INF/classes/", "BOOT-INF/classes/"])
def test_candidate_groups_strips_container_roots(make_jar, root):
    war = make_jar(
        "app-1.0.war",
        {
            f"{root}com/acme/app/Main.class": b"x",
            f"{root}com/acme/app/util/Util.class": b"x",
            "WEB-INF/lib/dep.jar": b"x",
        },
    )

    def handler(request):
        return httpx.Response(200, json={"response": {"docs": []}})

    with make_client(handler) as c:
        groups = candidate_groups("app", war, c)
    assert groups == ["com.acme.app", "com.acme"]


def test_candidate_groups_no_classes_or_default_package(make_jar):
    jar = make_jar("lib-1.0.jar", {"A.class": b"x", "README": b"x"})

    def handler(request):
        return httpx.Response(200, json={"response": {"docs": []}})

    with make_client(handler) as c:
        assert candidate_groups("lib", jar, c) == []


def test_candidate_groups_dedups_index_and_packages(make_jar):
    jar = make_jar("lib-1.0.jar", {"com/acme/lib/A.class": b"x"})

    def handler(request):
        return httpx.Response(
            200, json={"response": {"docs": [{"g": "com.acme", "a": "lib", "v": "1"}]}}
        )

    with make_client(handler) as c:
        assert candidate_groups("lib", jar, c) == ["com.acme", "com.acme.lib"]


def test_candidate_groups_non_dict_docs_entry(make_jar):
    jar = make_jar("lib-1.0.jar", {"A.class": b"x"})

    def handler(request):
        return httpx.Response(200, json={"response": {"docs": ["notadict"]}})

    with make_client(handler) as c:
        assert candidate_groups("lib", jar, c) == []


SHA = "664fddbf6f727666cfacd2bb058720feab15be62"


def test_gav_jar_path():
    gav = Gav("org.springframework", "spring-jdbc", "6.2.17")
    assert gav.jar_path() == (
        "org/springframework/spring-jdbc/6.2.17/spring-jdbc-6.2.17.jar"
    )


def test_verify_gav_match_and_lenient_parse():
    def handler(request):
        assert request.url.path.endswith("/spring-jdbc-6.2.17.jar.sha1")
        return httpx.Response(200, text=f"{SHA.upper()}  spring-jdbc-6.2.17.jar\n")

    gav = Gav("org.springframework", "spring-jdbc", "6.2.17")
    with make_client(handler) as c:
        repo, used = verify_gav(gav, SHA, ["https://r.test/m2"], c)
    assert repo == "https://r.test/m2"
    assert used == 1


def test_verify_gav_mismatch_tries_next_repo():
    def handler(request):
        if request.url.host == "one.test":
            return httpx.Response(200, text="deadbeef" * 5)
        return httpx.Response(200, text=SHA)

    gav = Gav("g", "a", "1")
    with make_client(handler) as c:
        repo, used = verify_gav(gav, SHA, ["https://one.test/m2", "https://two.test/m2"], c)
    assert repo == "https://two.test/m2"
    assert used == 2


def test_verify_gav_missing_sha1_is_a_miss():
    def handler(request):
        return httpx.Response(404)

    with make_client(handler) as c:
        repo, used = verify_gav(Gav("g", "a", "1"), SHA, ["https://r.test/m2"], c)
    assert repo is None
    assert used == 1


def test_verify_gav_empty_body_and_network_error():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "one.test":
            return httpx.Response(200, text="")
        raise httpx.ConnectError("boom")

    with make_client(handler) as c:
        repo, _ = verify_gav(
            Gav("g", "a", "1"), SHA, ["https://one.test/m2", "https://two.test/m2"], c
        )
    assert repo is None
    assert calls == ["one.test", "two.test"]


def test_verify_gav_respects_budget():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    with make_client(handler) as c:
        repo, used = verify_gav(
            Gav("g", "a", "1"), SHA, ["https://one.test/m2", "https://two.test/m2"], c, budget=1
        )
    assert repo is None
    assert used == 1
    assert len(calls) == 1


def test_verify_gav_invalid_url_candidate_is_a_miss():
    def handler(request):
        return httpx.Response(200, text=SHA)

    with make_client(handler) as c:
        repo, used = verify_gav(Gav("com.we\x01ird", "a", "1"), SHA, ["https://r.test/m2"], c)
    assert repo is None
    assert used == 1


def _post_freeze_handler(sources_payload: bytes, probe_hits: set[str], sha: str):
    """Simulates: sha1 search empty (frozen index), a: query knows the group,
    .sha1 sidecar verifies, sources jar downloads."""

    def handler(request):
        if request.url.host == "search.maven.org":
            q = request.url.params["q"]
            if q.startswith('1:"'):
                return httpx.Response(200, json={"response": {"docs": []}})
            docs = [{"g": "org.springframework", "a": "spring-jdbc", "v": "6.2.8"}]
            return httpx.Response(200, json={"response": {"docs": docs}})
        if request.url.path.endswith(".jar.sha1"):
            if request.url.path in probe_hits:
                return httpx.Response(200, text=sha)
            return httpx.Response(404)
        if request.url.path.endswith("-sources.jar"):
            return httpx.Response(200, content=sources_payload)
        return httpx.Response(404)

    return handler


def test_resolve_sources_verified_guess(make_jar, tmp_path: Path):
    jar = make_jar(
        "spring-jdbc-6.2.17.jar", {"org/springframework/jdbc/core/A.class": b"x"}
    )
    payload = make_jar("p.jar", {"org/springframework/jdbc/core/A.java": "class A {}"}).read_bytes()
    hit = "/m2/org/springframework/spring-jdbc/6.2.17/spring-jdbc-6.2.17.jar.sha1"
    with make_client(_post_freeze_handler(payload, {hit}, sha1_of(jar))) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert str(res.gav) == "org.springframework:spring-jdbc:6.2.17"
    assert res.resolved_by == "verified-guess"
    assert res.repo == "https://r.test/m2"
    assert res.sources_jar is not None and res.miss is None


def test_resolve_sources_verified_but_no_sources(make_jar, tmp_path: Path):
    jar = make_jar(
        "spring-jdbc-6.2.17.jar", {"org/springframework/jdbc/core/A.class": b"x"}
    )
    hit = "/m2/org/springframework/spring-jdbc/6.2.17/spring-jdbc-6.2.17.jar.sha1"
    inner = _post_freeze_handler(b"", {hit}, sha1_of(jar))

    def handler(request):
        if request.url.path.endswith("-sources.jar"):
            return httpx.Response(404)
        return inner(request)

    with make_client(handler) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert res.sources_jar is None
    assert str(res.gav) == "org.springframework:spring-jdbc:6.2.17"
    assert res.miss == (
        "verified org.springframework:spring-jdbc:6.2.17 via https://r.test/m2 "
        "but no -sources.jar published"
    )


def test_resolve_sources_nothing_verified(make_jar, tmp_path: Path):
    jar = make_jar("spring-jdbc-6.2.17.jar", {"org/springframework/jdbc/A.class": b"x"})
    with make_client(_post_freeze_handler(b"", set(), "")) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert res.sources_jar is None and res.gav is None
    assert res.miss == (
        "no pom.properties; sha1 not in Central index; 2 candidates, none verified"
    )
    # 2 candidates for (spring-jdbc, 6.2.17): group org.springframework (index,
    # deduped with the pkg ancestor) and org.springframework.jdbc (pkg prefix).


def test_resolve_sources_no_candidate_groups(make_jar, tmp_path: Path):
    jar = make_jar("mystery-1.0.jar", {"A.class": b"x"})  # default package: no prefix groups

    def handler(request):
        if request.url.host == "search.maven.org":
            return httpx.Response(200, json={"response": {"docs": []}})
        raise AssertionError(f"no probes expected without candidates: {request.url}")

    with make_client(handler) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert res.sources_jar is None and res.gav is None
    assert res.miss == "no pom.properties; sha1 not in Central index; no candidate groups found"


def test_resolve_sources_probe_budget_caps_requests(make_jar, tmp_path: Path):
    jar = make_jar("lib-1.0.jar", {"com/acme/lib/A.class": b"x"})
    probes = []

    def handler(request):
        if request.url.host == "search.maven.org":
            q = request.url.params["q"]
            if q.startswith('1:"'):
                return httpx.Response(200, json={"response": {"docs": []}})
            docs = [{"g": f"g{i}", "a": "lib", "v": "1.0"} for i in range(5)]
            return httpx.Response(200, json={"response": {"docs": docs}})
        if request.url.path.endswith(".jar.sha1"):
            probes.append(request.url.path)
            return httpx.Response(404)
        return httpx.Response(404)

    with make_client(handler) as c:
        res = resolve_sources(
            jar, ["https://one.test/m2", "https://two.test/m2"], c, tmp_path / "cache"
        )
    assert res.sources_jar is None
    assert len(probes) == 8  # MAX_PROBES, not 7 candidates x 2 repos = 14


def test_resolve_sources_survives_poisoned_package_names(make_jar, tmp_path: Path):
    jar = make_jar("weird-1.0.jar", {"com/we\x01ird/A.class": b"x"})

    def handler(request):
        if request.url.host == "search.maven.org":
            return httpx.Response(200, json={"response": {"docs": []}})
        return httpx.Response(404)

    with make_client(handler) as c:
        res = resolve_sources(jar, ["https://r.test/m2"], c, tmp_path / "cache")
    assert res.sources_jar is None
    assert res.miss is not None


EXHAUST_WARN = (
    "maven: r.test: connection error persisted after 3 attempts; "
    "artifacts may fall back to decompilation without sources"
)


def test_get_retry_retries_transport_errors_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="ok")

    net, log = NetState(), ResolutionLog()
    with make_client(handler) as c:
        resp = maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert resp.status_code == 200 and calls["n"] == 3
    assert log.ok_hosts == {"r.test"} and log.failed_hosts == set()


def test_get_retry_exhausts_strikes_and_warns_once():
    warnings: list[str] = []
    net = NetState(warn=warnings.append)
    log = ResolutionLog()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    with make_client(handler) as c:
        with pytest.raises(NetworkFailure) as exc:
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
        with pytest.raises(NetworkFailure):
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert calls["n"] == 6  # RETRY_ATTEMPTS per request
    assert exc.value.host == "r.test"
    assert exc.value.kind == "connection error"
    assert exc.value.detail == "r.test: connection error"
    assert log.failed_hosts == {"r.test"}
    assert warnings == [EXHAUST_WARN]  # deduped per (host, kind)


def test_get_retry_different_kind_warns_again():
    warnings: list[str] = []
    net = NetState(warn=warnings.append)
    log = ResolutionLog()
    mode = {"exc": httpx.ConnectError}

    def handler(request):
        raise mode["exc"]("boom")

    with make_client(handler) as c:
        with pytest.raises(NetworkFailure):
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
        mode["exc"] = httpx.ReadTimeout
        with pytest.raises(NetworkFailure) as exc:
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert exc.value.kind == "timeout"
    assert len(warnings) == 2
    assert "timeout persisted after 3 attempts" in warnings[1]


def test_get_retry_429_uses_retry_after_and_never_strikes(monkeypatch):
    waits: list = []
    monkeypatch.setattr(
        maven, "_wait_before_retry", lambda net, attempt, ra: waits.append(ra) or False
    )
    net, log = NetState(), ResolutionLog()

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "7"})

    with make_client(handler) as c:
        with pytest.raises(NetworkFailure) as exc:
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert waits == [7.0, 7.0]
    assert exc.value.kind == "HTTP 429"
    assert log.failed_hosts == set()  # 429 taints but never strikes


def test_get_retry_retry_after_capped_and_malformed(monkeypatch):
    waits: list = []
    monkeypatch.setattr(
        maven, "_wait_before_retry", lambda net, attempt, ra: waits.append(ra) or False
    )
    net, log = NetState(), ResolutionLog()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "100"}),
            httpx.Response(503, headers={"Retry-After": "soon"}),
            httpx.Response(200),
        ]
    )
    with make_client(lambda r: next(responses)) as c:
        resp = maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert resp.status_code == 200
    assert waits == [15.0, None]  # capped; malformed falls back to backoff


def test_wait_before_retry_jitters_backoff_and_reports_abort(monkeypatch):
    monkeypatch.setattr(maven, "RETRY_BACKOFF", (8.0, 16.0))
    net = NetState()
    seen: list[float] = []
    monkeypatch.setattr(net.abort, "wait", lambda d: seen.append(d) or True)
    assert maven._wait_before_retry(net, 1, None) is True
    assert 6.0 <= seen[0] <= 10.0  # 8s base, 0.75–1.25 jitter
    assert maven._wait_before_retry(net, 2, 7.0) is True
    assert seen[1] == 7.0  # retry-after passes through unjittered


def test_get_retry_pre_set_abort_gives_up_after_first_failure():
    net = NetState()
    net.abort.set()
    log = ResolutionLog()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    with make_client(handler) as c:
        with pytest.raises(NetworkFailure):
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert calls["n"] == 1
    assert log.failed_hosts == set()  # aborted give-up: no strike, no warn


def test_get_retry_non_transient_status_passes_through():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    net, log = NetState(), ResolutionLog()
    with make_client(handler) as c:
        resp = maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert resp.status_code == 404 and calls["n"] == 1
    assert log.ok_hosts == {"r.test"}


def test_netstate_breaker_trips_after_three_consecutive_artifacts():
    warnings: list[str] = []
    net = NetState(warn=warnings.append)
    net.record_artifact({"r.test"}, set())
    net.record_artifact({"r.test"}, {"r.test"})  # success beats failure: reset
    net.record_artifact({"r.test"}, set())
    net.record_artifact({"r.test"}, set())
    assert not net.is_dead("r.test")
    net.record_artifact({"r.test"}, set())
    assert net.is_dead("r.test")
    assert warnings == [
        "maven: giving up on r.test for the rest of the run "
        "(3 artifacts hit network failures in a row)"
    ]
    net.record_artifact({"r.test"}, set())  # already dead: no second warning
    assert len(warnings) == 1


def test_get_retry_skips_dead_host_without_request():
    net = NetState()
    for _ in range(3):
        net.record_artifact({"r.test"}, set())
    log = ResolutionLog()

    def handler(request):
        raise AssertionError("dead host must not be contacted")

    with make_client(handler) as c:
        with pytest.raises(NetworkFailure) as exc:
            maven._get_retry(c, "https://r.test/x", net=net, log=log)
    assert exc.value.kind == "skipped"
    assert exc.value.detail == "r.test skipped (unreachable this run)"
