"""decaf self-update (#155): the latest release of decaf on GitHub.

`fetch_latest` gets its httpx client from the caller. The tests replace `client`, so no
test reaches GitHub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

REPO_URL = "https://github.com/vcth4nh/decaf"
LATEST_API = "https://api.github.com/repos/vcth4nh/decaf/releases/latest"

_VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")


class SelfUpdateError(Exception):
    """The text is the reason for the user."""


@dataclass(frozen=True)
class Release:
    version: tuple[int, int, int]

    @property
    def text(self) -> str:
        return ".".join(map(str, self.version))

    @property
    def tag(self) -> str:
        return f"v{self.text}"

    @property
    def url(self) -> str:
        return f"{REPO_URL}/releases/tag/{self.tag}"

    @property
    def spec(self) -> str:
        return f"git+{REPO_URL}@{self.tag}"


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.fullmatch(text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def client() -> httpx.Client:
    """The client of the release lookup. The tests replace this factory."""
    return httpx.Client()


def fetch_latest(http: httpx.Client, timeout: float) -> Release:
    try:
        resp = http.get(LATEST_API, follow_redirects=True, timeout=timeout,
                        headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise SelfUpdateError(str(exc)) from exc
    if resp.status_code != 200:
        raise SelfUpdateError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SelfUpdateError("bad response") from exc
    if not isinstance(data, dict):
        raise SelfUpdateError("bad response")
    tag = data.get("tag_name")
    version = parse_version(tag) if isinstance(tag, str) else None
    if version is None or Release(version).tag != tag:  # Release builds its tag, url, and spec from the numbers
        raise SelfUpdateError(f"unexpected tag {tag!r}")
    return Release(version)
