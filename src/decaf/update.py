"""Engine pin resolution and updating (decaf engines update)."""

from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .engines import EngineError, EngineSpec, _sha256, fetch_to
from .maven import sha1_of
from .scanner import safe_extract_zip

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


def _fetch_checksum(
    client: httpx.Client, url: str, pattern: re.Pattern[str], label: str
) -> str | None:
    try:
        resp = client.get(url, follow_redirects=True, timeout=30)
    except httpx.HTTPError as exc:
        raise EngineError(f"{label}: checksum fetch failed for {url}: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise EngineError(f"{label}: checksum fetch got HTTP {resp.status_code} for {url}")
    m = pattern.search(resp.text.strip().lower())
    if not m:
        raise EngineError(f"{label}: unparseable checksum body at {url}")
    return m.group(0)


def _update_maven(
    spec: EngineSpec,
    client: httpx.Client,
    cache_dir: Path,
    target: str,
    warn: Callable[[str], None],
) -> UpdateResult:
    base = spec.url.rsplit("/", 2)[0]
    filename = spec.url.rsplit("/", 1)[1]
    url = f"{base}/{target}/{filename.replace(spec.version, target)}"
    part = cache_dir / f"{spec.name}-{target}.part"
    fetch_to(client, url, part, spec.name)
    try:
        expected = _fetch_checksum(client, url + ".sha256", _HEX256, spec.name)
        via = "sha256"
        if expected is None:
            sha1 = _fetch_checksum(client, url + ".sha1", _HEX1, spec.name)
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


def _github_release(client: httpx.Client, url: str, name: str) -> dict:
    try:
        resp = client.get(url, follow_redirects=True, timeout=30,
                          headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EngineError(f"{name}: GitHub release lookup failed: {exc}") from exc


def _update_github(
    spec: EngineSpec,
    client: httpx.Client,
    cache_dir: Path,
    version: str | None,
    gh: re.Match[str],
) -> UpdateResult | None:
    owner, repo, tag, filename = gh.groups()
    idx = tag.find(spec.version)
    if idx < 0:
        raise EngineError(f"{spec.name}: cannot map version into tag {tag!r}")
    tag_pre, tag_suf = tag[:idx], tag[idx + len(spec.version):]
    if version is None:
        rel = _github_release(
            client, f"https://api.github.com/repos/{owner}/{repo}/releases/latest", spec.name
        )
        new_tag = str(rel.get("tag_name") or "")
        core = new_tag[len(tag_pre): len(new_tag) - len(tag_suf) or None]
        if not (new_tag.startswith(tag_pre) and new_tag.endswith(tag_suf) and core):
            raise EngineError(f"{spec.name}: unrecognized release tag {new_tag!r}")
        target = core
    else:
        target = version
        rel = _github_release(
            client,
            f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag_pre}{version}{tag_suf}",
            spec.name,
        )
    if target == spec.version:
        return None
    want = filename.replace(spec.version, target)
    asset = next((a for a in rel.get("assets", []) if a.get("name") == want), None)
    if asset is None:
        raise EngineError(f"{spec.name}: release has no asset {want!r}")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise EngineError(f"{spec.name}: release asset publishes no sha256 digest — not updating")
    expected = digest.removeprefix("sha256:").lower()
    url = asset.get("browser_download_url")
    if not url:
        raise EngineError(f"{spec.name}: release asset has no download URL")
    part = cache_dir / f"{spec.name}-{target}.part"
    fetch_to(client, url, part, spec.name)
    got = _sha256(part)
    if got != expected:
        part.unlink()
        raise EngineError(f"{spec.name}: checksum mismatch for {url}: expected {expected}, got {got}")
    jar = cache_dir / f"{spec.name}-{target}.jar"
    if spec.archive_member:
        extract_dir = cache_dir / f"{spec.name}-extract"
        shutil.rmtree(extract_dir, ignore_errors=True)
        found = safe_extract_zip(part, extract_dir, members=[spec.archive_member])
        part.unlink()
        member = extract_dir / spec.archive_member
        if found != 1:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise EngineError(f"{spec.name}: member {spec.archive_member!r} missing from {want}")
        inner = _sha256(member)
        member.replace(jar)
        shutil.rmtree(extract_dir, ignore_errors=True)
        pin = {"version": target, "url": url, "sha256": inner,
               "download_sha256": expected, "archive_member": spec.archive_member}
    else:
        part.replace(jar)
        pin = {"version": target, "url": url, "sha256": got}
    return UpdateResult(spec.name, spec.version, target, pin, "github-digest")
