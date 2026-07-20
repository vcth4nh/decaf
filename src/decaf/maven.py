"""Maven artifact identification and -sources.jar fetching."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
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

    def jar_path(self) -> str:
        return (
            f"{self.group.replace('.', '/')}/{self.artifact}/{self.version}/"
            f"{self.artifact}-{self.version}.jar"
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


_FILENAME_RE = re.compile(r"^(?P<artifact>.+?)-(?P<version>\d.*)$")
_ARTIFACT_RE = re.compile(r"[A-Za-z0-9._-]+")


def _coords_from_filename(stem: str) -> tuple[str, str] | None:
    m = _FILENAME_RE.match(stem)  # splits at the first '-' followed by a digit
    if m:
        return m.group("artifact"), m.group("version")
    return None


def _parse_manifest(jar_path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(jar_path) as zf:
            text = zf.read("META-INF/MANIFEST.MF").decode(errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return {}
    attrs: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if key is not None and line.startswith(" "):
            attrs[key] += line[1:]
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            attrs[key] = value.strip()
    return attrs


def candidate_coords(jar_path: Path) -> list[tuple[str, str]]:
    """Ordered (artifact, version) guesses from filename, then manifest."""
    coords: list[tuple[str, str]] = []
    from_name = _coords_from_filename(jar_path.stem)
    if from_name:
        coords.append(from_name)
    attrs = _parse_manifest(jar_path)
    title, impl_version = attrs.get("Implementation-Title", ""), attrs.get("Implementation-Version", "")
    if _ARTIFACT_RE.fullmatch(title) and impl_version[:1].isdigit():
        coords.append((title, impl_version))
    bsn, bundle_version = attrs.get("Bundle-SymbolicName", ""), attrs.get("Bundle-Version", "")
    bsn_artifact = bsn.partition(";")[0].strip().rpartition(".")[2]
    if _ARTIFACT_RE.fullmatch(bsn_artifact) and bundle_version[:1].isdigit():
        coords.append((bsn_artifact, bundle_version))
    return list(dict.fromkeys(coords))


MAX_INDEX_GROUPS = 5


def _groups_from_index(artifact: str, client: httpx.Client) -> list[str]:
    try:
        resp = client.get(
            SEARCH_URL,
            params={"q": f'a:"{artifact}"', "rows": str(MAX_INDEX_GROUPS), "wt": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        docs = resp.json()["response"]["docs"]
        return [d["g"] for d in docs if isinstance(d.get("g"), str)]
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError):
        return []


def _groups_from_packages(jar_path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return []
    packages = [
        name.rpartition("/")[0].split("/")
        for name in names
        if name.endswith(".class")
        and "/" in name
        and not name.startswith("META-INF/")
        and name != "module-info.class"
    ]
    if not packages:
        return []
    prefix = packages[0]
    for parts in packages[1:]:
        keep = 0
        for a, b in zip(prefix, parts):
            if a != b:
                break
            keep += 1
        prefix = prefix[:keep]
    return [".".join(prefix[:n]) for n in range(len(prefix), 1, -1)]


def candidate_groups(artifact: str, jar_path: Path, client: httpx.Client) -> list[str]:
    """Ordered groupId guesses: Central index by artifactId, then package prefixes."""
    return list(dict.fromkeys(_groups_from_index(artifact, client) + _groups_from_packages(jar_path)))


MAX_PROBES = 8


def verify_gav(
    gav: Gav,
    sha1: str,
    repos: Sequence[str],
    client: httpx.Client,
    budget: int = MAX_PROBES,
) -> tuple[str | None, int]:
    """Probe each repo's .jar.sha1 sidecar for gav. Returns (verifying repo, probes spent)."""
    used = 0
    for repo in repos:
        if used >= budget:
            break
        used += 1
        try:
            resp = client.get(f"{repo}/{gav.jar_path()}.sha1", follow_redirects=True, timeout=10)
        except (httpx.HTTPError, httpx.InvalidURL):
            continue
        if resp.status_code != 200:
            continue
        tokens = resp.text.split()
        if tokens and tokens[0].lower() == sha1.lower():
            return repo, used
    return None, used


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
        tmp = tempfile.NamedTemporaryFile(
            dir=cache_dir, prefix=cached.name + ".", suffix=".part", delete=False
        )
        tmp_name = Path(tmp.name)
        ok = False
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=30) as resp:
                if resp.status_code != 200:
                    continue
                for chunk in resp.iter_bytes():
                    tmp.write(chunk)
                ok = True
        except httpx.HTTPError:
            continue
        finally:
            tmp.close()
            if not ok:
                os.unlink(tmp_name)
        if not zipfile.is_zipfile(tmp_name):
            os.unlink(tmp_name)
            continue
        try:
            os.replace(tmp_name, cached)
        except OSError:
            # Windows denies replace when another thread races the same GAV;
            # the winner's copy is identical, so drop ours and use it.
            os.unlink(tmp_name)
            if not cached.is_file():
                continue
        marker.write_text(repo)
        return cached, repo
    return None


def extract_java(sources_jar: Path, dest: Path) -> int:
    try:
        return safe_extract_zip(sources_jar, dest, suffixes=(".java",))
    except (zipfile.BadZipFile, OSError):
        return 0


@dataclass
class Resolution:
    """Outcome of the sources-first chain: a hit, or a miss with the reason."""

    gav: Gav | None = None
    sources_jar: Path | None = None
    repo: str | None = None
    resolved_by: str | None = None  # "pom-properties" | "sha1-index" | "verified-guess"
    miss: str | None = None


def _fetched(gav: Gav, fetched: tuple[Path, str] | None, resolved_by: str, no_sources: str) -> Resolution:
    if fetched is None:
        return Resolution(gav=gav, miss=no_sources)
    return Resolution(gav=gav, sources_jar=fetched[0], repo=fetched[1], resolved_by=resolved_by)


def resolve_sources(
    jar_path: Path,
    repos: Sequence[str],
    client: httpx.Client,
    cache_dir: Path,
    *,
    allow_sha1: bool = True,
) -> Resolution:
    gav = gav_from_pom_properties(jar_path)
    if gav is not None:
        return _fetched(
            gav,
            fetch_sources(gav, repos, client, cache_dir),
            "pom-properties",
            f"found {gav} in pom.properties but no -sources.jar in any repo",
        )

    trail = ["no pom.properties"]
    sha1 = sha1_of(jar_path)
    if allow_sha1:
        gav = gav_from_central_sha1(sha1, client)
        if gav is not None:
            return _fetched(
                gav,
                fetch_sources(gav, repos, client, cache_dir),
                "sha1-index",
                f"sha1 matched {gav} in Central index but no -sources.jar in any repo",
            )
        trail.append("sha1 not in Central index")
    else:
        trail.append("sha1 lookup skipped")

    coords = candidate_coords(jar_path)
    if not coords:
        trail.append("no artifact/version hints in filename or manifest")
        return Resolution(miss="; ".join(trail))

    budget = MAX_PROBES
    tried: set[Gav] = set()
    for artifact, version in coords:
        if budget <= 0:
            break
        for group in candidate_groups(artifact, jar_path, client):
            if budget <= 0:
                break
            candidate = Gav(group, artifact, version)
            if candidate in tried:
                continue
            tried.add(candidate)
            repo, used = verify_gav(candidate, sha1, repos, client, budget)
            budget -= used
            if repo is None:
                continue
            ordered = [repo, *[r for r in repos if r != repo]]
            return _fetched(
                candidate,
                fetch_sources(candidate, ordered, client, cache_dir),
                "verified-guess",
                f"verified {candidate} via {repo} but no -sources.jar published",
            )
    trail.append(f"{len(tried)} candidates, none verified" if tried else "no candidate groups found")
    return Resolution(miss="; ".join(trail))
