import re

import httpx
import pytest

from decaf import selfupdate


@pytest.mark.parametrize("text,want", [
    ("1.11.0", (1, 11, 0)), ("v1.12.3", (1, 12, 3)),
    ("1.12", None), ("1.12.0.dev0", None), ("v1.x.0", None), ("", None), ("1.2.3 ", None),
])
def test_parse_version(text, want):
    assert selfupdate.parse_version(text) == want


def test_fetch_latest_reads_the_tag(github):
    seen = []
    rel = selfupdate.fetch_latest(github(seen=seen), timeout=30)
    assert rel.version == (1, 12, 0) and rel.text == "1.12.0" and rel.tag == "v1.12.0"
    assert rel.url == "https://github.com/vcth4nh/decaf/releases/tag/v1.12.0"
    assert rel.spec == "git+https://github.com/vcth4nh/decaf@v1.12.0"
    assert str(seen[0].url) == "https://api.github.com/repos/vcth4nh/decaf/releases/latest"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"
    assert seen[0].extensions["timeout"]["read"] == 30  # not the httpx default of 5 s


def test_fetch_latest_follows_a_redirect():
    # GitHub answers 301 for a renamed or moved repo; the lookup must still find the release
    moved = "https://api.github.com/repositories/1/releases/latest"

    def handler(request):
        if str(request.url) == selfupdate.LATEST_API:
            return httpx.Response(301, headers={"Location": moved})
        return httpx.Response(200, json={"tag_name": "v1.12.0"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert selfupdate.fetch_latest(http, timeout=30).version == (1, 12, 0)


@pytest.mark.parametrize("kw,reason", [
    ({"status": 403}, "HTTP 403"),
    ({"status": 429}, "HTTP 429"),
    ({"body": "<html>"}, "bad response"),
    ({"body": "[]"}, "bad response"),  # JSON, but not an object: the command catches only SelfUpdateError
    ({"tag": "nightly"}, "unexpected tag 'nightly'"),
    ({"tag": "1.12.0"}, "unexpected tag '1.12.0'"),  # Release.url and .spec use v1.12.0, a tag that does not exist
    ({"tag": None}, "unexpected tag None"),
])
def test_fetch_latest_errors(github, kw, reason):
    with pytest.raises(selfupdate.SelfUpdateError, match=re.escape(reason)):
        selfupdate.fetch_latest(github(**kw), timeout=30)


def test_the_suite_cannot_reach_github():
    with pytest.raises(selfupdate.SelfUpdateError, match="network blocked in tests"):
        selfupdate.fetch_latest(selfupdate.client(), timeout=5)
