"""Maven artifact identification and -sources.jar fetching."""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .scanner import safe_extract_zip

SEARCH_URL = "https://search.maven.org/solrsearch/select"


@dataclass(frozen=True)
class Gav:
    group: str
    artifact: str
    version: str

    def __str__(self) -> str:
        return f"{self.group}:{self.artifact}:{self.version}"

    def sources_path(self) -> str:
        return (
            f"{self.group.replace('.', '/')}/{self.artifact}/{self.version}/"
            f"{self.artifact}-{self.version}-sources.jar"
        )


def _parse_pom_properties(text: str) -> Gav | None:
    props: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    if all(k in props for k in ("groupId", "artifactId", "version")):
        return Gav(props["groupId"], props["artifactId"], props["version"])
    return None


def gav_from_pom_properties(jar_path: Path) -> Gav | None:
    gavs: list[Gav] = []
    try:
        with zipfile.ZipFile(jar_path) as zf:
            for name in zf.namelist():
                if name.startswith("META-INF/maven/") and name.endswith("/pom.properties"):
                    gav = _parse_pom_properties(zf.read(name).decode(errors="replace"))
                    if gav:
                        gavs.append(gav)
    except (zipfile.BadZipFile, OSError):
        return None
    if len(gavs) == 1:
        return gavs[0]
    if len(gavs) > 1:  # shaded/fat jar: trust only an exact filename match
        for gav in gavs:
            if jar_path.stem == f"{gav.artifact}-{gav.version}":
                return gav
    return None


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gav_from_central_sha1(sha1: str, client: httpx.Client) -> Gav | None:
    try:
        resp = client.get(
            SEARCH_URL, params={"q": f'1:"{sha1}"', "rows": "1", "wt": "json"}, timeout=10
        )
        resp.raise_for_status()
        docs = resp.json()["response"]["docs"]
        if not docs:
            return None
        doc = docs[0]
        return Gav(doc["g"], doc["a"], doc["v"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None


def fetch_sources(
    gav: Gav, repos: Sequence[str], client: httpx.Client, cache_dir: Path
) -> tuple[Path, str] | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{gav.group}_{gav.artifact}_{gav.version}-sources.jar"
    marker = cached.with_suffix(".repo")
    if cached.is_file() and marker.is_file():
        return cached, marker.read_text().strip()
    for repo in repos:
        url = f"{repo}/{gav.sources_path()}"
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=30) as resp:
                if resp.status_code != 200:
                    continue
                part = cached.with_suffix(".part")
                with open(part, "wb") as out:
                    for chunk in resp.iter_bytes():
                        out.write(chunk)
        except httpx.HTTPError:
            continue
        part.replace(cached)
        marker.write_text(repo)
        return cached, repo
    return None


def extract_java(sources_jar: Path, dest: Path) -> int:
    try:
        return safe_extract_zip(sources_jar, dest, suffixes=(".java",))
    except (zipfile.BadZipFile, OSError):
        return 0


def resolve_sources(
    jar_path: Path,
    repos: Sequence[str],
    client: httpx.Client,
    cache_dir: Path,
    *,
    allow_sha1: bool = True,
) -> tuple[Gav, Path, str] | None:
    gav = gav_from_pom_properties(jar_path)
    if gav is None and allow_sha1:
        gav = gav_from_central_sha1(sha1_of(jar_path), client)
    if gav is None:
        return None
    fetched = fetch_sources(gav, repos, client, cache_dir)
    if fetched is None:
        return None
    sources_jar, repo = fetched
    return gav, sources_jar, repo
