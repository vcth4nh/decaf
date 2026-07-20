"""Engine pin resolution and updating (decaf engines update)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .engines import EngineError, EngineSpec, _sha256, fetch_to
from .maven import sha1_of

_HEX256 = re.compile(r"[0-9a-f]{64}")
_HEX1 = re.compile(r"[0-9a-f]{40}")
_GITHUB = re.compile(r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)$")


@dataclass(frozen=True)
class UpdateResult:
    name: str
    old_version: str
    version: str
    pin: dict[str, str]
    verified_via: str  # "sha256" | "sha1" | "github-digest"


def update_engine(
    spec: EngineSpec,
    client: httpx.Client,
    cache_dir: Path,
    version: str | None = None,
    warn: Callable[[str], None] = lambda msg: None,
) -> UpdateResult | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    if version == spec.version:
        return None
    gh = _GITHUB.match(spec.url)
    if gh:
        return _update_github(spec, client, cache_dir, version, gh)
    target = version or _maven_latest(spec, client)
    if target == spec.version:
        return None
    return _update_maven(spec, client, cache_dir, target, warn)


def _maven_latest(spec: EngineSpec, client: httpx.Client) -> str:
    url = spec.url.rsplit("/", 2)[0] + "/maven-metadata.xml"
    try:
        resp = client.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise EngineError(f"{spec.name}: metadata fetch failed: {exc}") from exc
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise EngineError(f"{spec.name}: bad maven-metadata.xml: {exc}") from exc
    version = root.findtext("versioning/release") or root.findtext("versioning/latest")
    if not version:
        raise EngineError(f"{spec.name}: maven-metadata.xml has no release version")
    return version


def _fetch_checksum(client: httpx.Client, url: str, pattern: re.Pattern[str]) -> str | None:
    try:
        resp = client.get(url, follow_redirects=True, timeout=30)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    m = pattern.search(resp.text.strip().lower())
    return m.group(0) if m else None


def _update_maven(
    spec: EngineSpec,
    client: httpx.Client,
    cache_dir: Path,
    target: str,
    warn: Callable[[str], None],
) -> UpdateResult:
    url = spec.url.replace(spec.version, target)
    part = cache_dir / f"{spec.name}-{target}.part"
    fetch_to(client, url, part, spec.name)
    try:
        expected = _fetch_checksum(client, url + ".sha256", _HEX256)
        via = "sha256"
        if expected is None:
            sha1 = _fetch_checksum(client, url + ".sha1", _HEX1)
            if sha1 is None:
                raise EngineError(
                    f"{spec.name}: upstream publishes no sha256 or sha1 for {target} — not updating"
                )
            got1 = sha1_of(part)
            if got1 != sha1:
                raise EngineError(
                    f"{spec.name}: sha1 mismatch for {url}: expected {sha1}, got {got1}"
                )
            via = "sha1"
            warn(f"{spec.name}: verified via sha1 — upstream publishes no sha256")
        digest = _sha256(part)
        if via == "sha256" and digest != expected:
            raise EngineError(
                f"{spec.name}: checksum mismatch for {url}: expected {expected}, got {digest}"
            )
    except EngineError:
        part.unlink(missing_ok=True)
        raise
    part.replace(cache_dir / f"{spec.name}-{target}.jar")
    return UpdateResult(
        spec.name, spec.version, target,
        {"version": target, "url": url, "sha256": digest}, via,
    )


def _update_github(
    spec: EngineSpec,
    client: httpx.Client,
    cache_dir: Path,
    version: str | None,
    gh: re.Match[str],
) -> UpdateResult | None:
    raise EngineError(f"{spec.name}: GitHub update not implemented yet")
