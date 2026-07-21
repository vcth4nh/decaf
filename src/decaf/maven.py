"""Maven artifact identification and -sources.jar fetching."""

from __future__ import annotations

import hashlib
import os
import random
import re
import tempfile
import threading
import weakref
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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

# Per-client (i.e. per-run) memo of index lookups: repeated artifactIds in one
# batch would otherwise re-issue identical queries. Weak keys, so no client
# outlives its run because of the cache.
_INDEX_CACHE: weakref.WeakKeyDictionary[httpx.Client, dict[str, list[str]]] = (
    weakref.WeakKeyDictionary()
)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (1.0, 2.0)  # seconds before retry 1 and retry 2; len == RETRY_ATTEMPTS - 1
RETRY_AFTER_CAP = 15.0
BREAKER_STRIKES = 3


class NetworkFailure(Exception):
    """A request gave up: retries exhausted, run aborted, or breaker-dead host."""

    def __init__(self, host: str, kind: str, detail: str):
        super().__init__(detail)
        self.host = host
        self.kind = kind
        self.detail = detail


@dataclass
class NetState:
    """Per-run network policy state, shared across fetch threads."""

    abort: threading.Event = field(default_factory=threading.Event)
    warn: Callable[[str], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _strikes: dict[str, int] = field(default_factory=dict, repr=False)
    _dead: set[str] = field(default_factory=set, repr=False)
    _warned: set[tuple[str, str]] = field(default_factory=set, repr=False)

    def is_dead(self, host: str) -> bool:
        with self._lock:
            return host in self._dead

    def warn_once(self, host: str, kind_class: str, msg: str) -> None:
        if self.warn is None:
            return
        with self._lock:
            if (host, kind_class) in self._warned:
                return
            self._warned.add((host, kind_class))
        self.warn(msg)

    def record_artifact(self, failed_hosts: set[str], ok_hosts: set[str]) -> None:
        """Per-resolution breaker accounting; a host success beats its failures."""
        tripped: list[str] = []
        with self._lock:
            for host in ok_hosts:
                self._strikes[host] = 0
            for host in failed_hosts - ok_hosts:
                self._strikes[host] = self._strikes.get(host, 0) + 1
                if self._strikes[host] >= BREAKER_STRIKES and host not in self._dead:
                    self._dead.add(host)
                    tripped.append(host)
        if self.warn is not None:
            for host in tripped:
                self.warn(
                    f"maven: giving up on {host} for the rest of the run "
                    f"({BREAKER_STRIKES} artifacts hit network failures in a row)"
                )


@dataclass
class ResolutionLog:
    """Per-resolve_sources accumulation (owned by one call, no lock needed)."""

    events: list[str] = field(default_factory=list)
    failed_hosts: set[str] = field(default_factory=set)
    ok_hosts: set[str] = field(default_factory=set)
    probe_failures: int = 0
    download_failures: int = 0


def _exc_kind(exc: httpx.TransportError) -> str:
    return "timeout" if isinstance(exc, httpx.TimeoutException) else "connection error"


def _status_kind(code: int) -> str | None:
    if code == 429:
        return "HTTP 429"
    if 500 <= code < 600:
        return f"HTTP {code}"
    return None


def _kind_class(kind: str) -> str:
    return "HTTP 5xx" if kind.startswith("HTTP 5") else kind


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    if resp.status_code not in (429, 503):
        return None
    value = resp.headers.get("Retry-After", "")
    if value.isdecimal():
        return min(float(value), RETRY_AFTER_CAP)
    return None


def _wait_before_retry(net: NetState, attempt: int, retry_after: float | None) -> bool:
    """Back off before retry `attempt` (1-based). True if the run aborted mid-wait."""
    if retry_after is not None:
        delay = retry_after
    else:
        delay = RETRY_BACKOFF[attempt - 1] * (0.75 + random.random() * 0.5)
    return net.abort.wait(delay)


def _check_alive(net: NetState, host: str) -> None:
    if net.is_dead(host):
        raise NetworkFailure(host, "skipped", f"{host} skipped (unreachable this run)")


def _exhausted(net: NetState, log: ResolutionLog, host: str, kind: str) -> NetworkFailure:
    if kind in ("timeout", "connection error"):
        log.failed_hosts.add(host)  # only transport-class failures strike the breaker
    net.warn_once(
        host,
        _kind_class(kind),
        f"maven: {host}: {_kind_class(kind)} persisted after {RETRY_ATTEMPTS} attempts; "
        "artifacts may fall back to decompilation without sources",
    )
    return NetworkFailure(host, kind, f"{host}: {kind}")


def _get_retry(
    client: httpx.Client,
    url: str,
    *,
    net: NetState,
    log: ResolutionLog,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
    follow_redirects: bool = False,
) -> httpx.Response:
    """GET with transient-error retries; returns any non-transient response.

    Raises NetworkFailure when the host is breaker-dead, retries exhaust, or
    the run aborts mid-backoff.
    """
    host = httpx.URL(url).host
    _check_alive(net, host)
    kind = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        retry_after = None
        try:
            resp = client.get(
                url, params=params, timeout=timeout, follow_redirects=follow_redirects
            )
        except httpx.TransportError as exc:
            kind = _exc_kind(exc)
        else:
            status_kind = _status_kind(resp.status_code)
            if status_kind is None:
                log.ok_hosts.add(host)
                return resp
            kind = status_kind
            retry_after = _retry_after_seconds(resp)
        if attempt < RETRY_ATTEMPTS and _wait_before_retry(net, attempt, retry_after):
            raise NetworkFailure(host, kind, f"{host}: {kind}")  # aborted: quiet give-up
    raise _exhausted(net, log, host, kind)


def _groups_from_index(
    artifact: str, client: httpx.Client, net: NetState, log: ResolutionLog
) -> list[str] | None:
    """GroupIds indexed for artifact, [] if none, None if the lookup couldn't run."""
    cache = _INDEX_CACHE.setdefault(client, {})
    if artifact in cache:
        return cache[artifact]
    try:
        resp = _get_retry(
            client,
            SEARCH_URL,
            net=net,
            log=log,
            params={"q": f'a:"{artifact}"', "rows": str(MAX_INDEX_GROUPS), "wt": "json"},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            host = httpx.URL(SEARCH_URL).host
            log.events.append(f"{host}: index HTTP {resp.status_code} during index lookup")
            return None
        docs = resp.json()["response"]["docs"]
        groups = [d["g"] for d in docs if isinstance(d.get("g"), str)]
    except NetworkFailure as nf:
        log.events.append(f"{nf.detail} during index lookup")
        return None
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError):
        host = httpx.URL(SEARCH_URL).host
        log.events.append(f"{host}: malformed index response during index lookup")
        return None
    cache[artifact] = groups  # only successful lookups are memoized (incl. empty)
    return groups


def _strip_container_root(name: str) -> str:
    for root in ("WEB-INF/classes/", "BOOT-INF/classes/"):
        if name.startswith(root):
            return name[len(root):]
    return name


def _groups_from_packages(jar_path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return []
    packages = [
        name.rpartition("/")[0].split("/")
        for name in (_strip_container_root(n) for n in names)
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


def candidate_groups(
    artifact: str,
    jar_path: Path,
    client: httpx.Client,
    net: NetState | None = None,
    log: ResolutionLog | None = None,
) -> list[str]:
    """Ordered groupId guesses: Central index by artifactId, then package prefixes."""
    if net is None:
        net = NetState()
    if log is None:
        log = ResolutionLog()
    from_index = _groups_from_index(artifact, client, net, log) or []
    return list(dict.fromkeys(from_index + _groups_from_packages(jar_path)))


MAX_PROBES = 8


def verify_gav(
    gav: Gav,
    sha1: str,
    repos: Sequence[str],
    client: httpx.Client,
    budget: int = MAX_PROBES,
    net: NetState | None = None,
    log: ResolutionLog | None = None,
) -> tuple[str | None, int]:
    """Probe each repo's .jar.sha1 sidecar for gav. Returns (verifying repo, probes spent)."""
    if net is None:
        net = NetState()
    if log is None:
        log = ResolutionLog()
    used = 0
    for repo in repos:
        if used >= budget:
            break
        used += 1
        try:
            resp = _get_retry(
                client,
                f"{repo}/{gav.jar_path()}.sha1",
                net=net,
                log=log,
                follow_redirects=True,
            )
        except NetworkFailure as nf:
            log.events.append(f"{nf.detail} during candidate probe")
            log.probe_failures += 1
            continue
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


def gav_from_central_sha1(
    sha1: str,
    client: httpx.Client,
    net: NetState | None = None,
    log: ResolutionLog | None = None,
) -> Gav | None:
    if net is None:
        net = NetState()
    if log is None:
        log = ResolutionLog()
    try:
        resp = _get_retry(
            client,
            SEARCH_URL,
            net=net,
            log=log,
            params={"q": f'1:"{sha1}"', "rows": "1", "wt": "json"},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            host = httpx.URL(SEARCH_URL).host
            log.events.append(f"{host}: index HTTP {resp.status_code} during sha1 lookup")
            return None
        docs = resp.json()["response"]["docs"]
        if not docs:
            return None
        doc = docs[0]
        return Gav(doc["g"], doc["a"], doc["v"])
    except NetworkFailure as nf:
        log.events.append(f"{nf.detail} during sha1 lookup")
        return None
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        host = httpx.URL(SEARCH_URL).host
        log.events.append(f"{host}: malformed index response during sha1 lookup")
        return None


def fetch_sources(
    gav: Gav,
    repos: Sequence[str],
    client: httpx.Client,
    cache_dir: Path,
    net: NetState | None = None,
    log: ResolutionLog | None = None,
) -> tuple[Path, str, bool] | None:
    if net is None:
        net = NetState()
    if log is None:
        log = ResolutionLog()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{gav.group}_{gav.artifact}_{gav.version}-sources.jar"
    marker = cached.with_suffix(".repo")
    if cached.is_file() and marker.is_file():
        return cached, marker.read_text().strip(), True
    for repo in repos:
        url = f"{repo}/{gav.sources_path()}"
        try:
            got = _download(client, url, cached, net, log)
        except NetworkFailure as nf:
            log.events.append(f"{nf.detail} during sources download")
            log.download_failures += 1
            continue
        except httpx.HTTPError:
            continue
        if got is None:
            continue
        marker.write_text(repo)
        return cached, repo, False
    return None


def _download(
    client: httpx.Client, url: str, cached: Path, net: NetState, log: ResolutionLog
) -> Path | None:
    """One repo's sources download with transient retries.

    Returns the cached path on success, None for a verified miss (non-200 or
    not a zip); raises NetworkFailure after exhausted or aborted retries.
    """
    host = httpx.URL(url).host
    _check_alive(net, host)
    kind = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        retry_after = None
        tmp = tempfile.NamedTemporaryFile(
            dir=cached.parent, prefix=cached.name + ".", suffix=".part", delete=False
        )
        tmp_name = Path(tmp.name)
        ok = False
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=30) as resp:
                status_kind = _status_kind(resp.status_code)
                if status_kind is not None:
                    kind = status_kind
                    retry_after = _retry_after_seconds(resp)
                elif resp.status_code != 200:
                    log.ok_hosts.add(host)
                    return None
                else:
                    for chunk in resp.iter_bytes():
                        tmp.write(chunk)
                    ok = True
        except httpx.TransportError as exc:
            kind = _exc_kind(exc)
        finally:
            tmp.close()
            if not ok:
                os.unlink(tmp_name)
        if ok:
            log.ok_hosts.add(host)
            if not zipfile.is_zipfile(tmp_name):
                os.unlink(tmp_name)
                return None
            try:
                os.replace(tmp_name, cached)
            except OSError:
                # Windows denies replace when another thread races the same GAV;
                # the winner's copy is identical, so drop ours and use it.
                os.unlink(tmp_name)
                if not cached.is_file():
                    return None
            return cached
        if attempt < RETRY_ATTEMPTS and _wait_before_retry(net, attempt, retry_after):
            raise NetworkFailure(host, kind, f"{host}: {kind}")  # aborted: quiet give-up
    raise _exhausted(net, log, host, kind)


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
    cached: bool = False  # sources jar served from the on-disk cache, no download
    miss: str | None = None


def _fetched(gav: Gav, fetched: tuple[Path, str, bool] | None, resolved_by: str, no_sources: str) -> Resolution:
    if fetched is None:
        return Resolution(gav=gav, miss=no_sources)
    return Resolution(
        gav=gav, sources_jar=fetched[0], repo=fetched[1],
        resolved_by=resolved_by, cached=fetched[2],
    )


def _no_sources(prefix: str, absent: str, log: ResolutionLog) -> str:
    if log.download_failures:
        return f"{prefix} but no -sources.jar in any reachable repo"
    return f"{prefix} but {absent}"


def _finish(res: Resolution, log: ResolutionLog, net: NetState) -> Resolution:
    net.record_artifact(log.failed_hosts, log.ok_hosts)
    if res.miss is not None and log.events:
        seen = "; ".join(dict.fromkeys(log.events))
        res.miss = f"network: {seen}; {res.miss}"
    return res


def resolve_sources(
    jar_path: Path,
    repos: Sequence[str],
    client: httpx.Client,
    cache_dir: Path,
    *,
    allow_sha1: bool = True,
    net: NetState | None = None,
) -> Resolution:
    if net is None:
        net = NetState()
    log = ResolutionLog()

    gav = gav_from_pom_properties(jar_path)
    if gav is not None:
        fetched = fetch_sources(gav, repos, client, cache_dir, net, log)
        msg = _no_sources(
            f"found {gav} in pom.properties", "no -sources.jar in any repo", log
        )
        return _finish(_fetched(gav, fetched, "pom-properties", msg), log, net)

    trail = ["no pom.properties"]
    sha1 = sha1_of(jar_path)
    if allow_sha1:
        before = len(log.events)
        gav = gav_from_central_sha1(sha1, client, net, log)
        if gav is not None:
            fetched = fetch_sources(gav, repos, client, cache_dir, net, log)
            msg = _no_sources(
                f"sha1 matched {gav} in Central index", "no -sources.jar in any repo", log
            )
            return _finish(_fetched(gav, fetched, "sha1-index", msg), log, net)
        trail.append(
            "sha1 lookup errored (network)"
            if len(log.events) > before
            else "sha1 not in Central index"
        )
    else:
        trail.append("sha1 lookup skipped")

    coords = candidate_coords(jar_path)
    if not coords:
        trail.append("no artifact/version hints in filename or manifest")
        return _finish(Resolution(miss="; ".join(trail)), log, net)

    budget = MAX_PROBES
    tried: set[Gav] = set()
    for artifact, version in coords:
        if budget <= 0:
            break
        for group in candidate_groups(artifact, jar_path, client, net, log):
            if budget <= 0:
                break
            candidate = Gav(group, artifact, version)
            if candidate in tried:
                continue
            tried.add(candidate)
            repo, used = verify_gav(candidate, sha1, repos, client, budget, net, log)
            budget -= used
            if repo is None:
                continue
            ordered = [repo, *[r for r in repos if r != repo]]
            fetched = fetch_sources(candidate, ordered, client, cache_dir, net, log)
            msg = _no_sources(
                f"verified {candidate} via {repo}", "no -sources.jar published", log
            )
            return _finish(_fetched(candidate, fetched, "verified-guess", msg), log, net)
    if tried:
        errs = f" ({log.probe_failures} probe(s) errored)" if log.probe_failures else ""
        trail.append(f"{len(tried)} candidates, none verified{errs}")
    elif any(e.endswith("during index lookup") for e in log.events):
        trail.append("no candidate groups found (index unavailable)")
    else:
        trail.append("no candidate groups found")
    return _finish(Resolution(miss="; ".join(trail)), log, net)
