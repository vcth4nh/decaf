# Changelog

All notable changes to decaf are documented here.

## [Unreleased]

### Added

- `--no-resource`: mirror the input layout with decompiled/extracted sources
  only, no resource files. Rejected with `--merge`, which never writes
  resources (#57).
- Resource-only jars are now mirrored instead of skipped, so an empty
  "tombstone" jar (e.g. a relocated dependency shipping only `META-INF/`)
  is visible as such in the output (#57).
- Nested archives beyond `--max-depth` are copied through as files in the
  mirror output instead of silently vanishing (#57).
- New additive `resources_copied` report field and a `Resources` summary
  row (#57).
- The `OK` summary row gains a `resources N` part when resource-only jars
  were mirrored, so its breakdown keeps summing to the total (#57).

### Changed

- Mirror mode is now faithful to the input: resources always come from the
  original archive on every path. Maven hits and sources-jar artifacts
  include the original's resources (previously sources only), solo
  decompiles no longer inherit whatever extra files the engine emitted, and
  artifacts whose engines all failed still carry their resources (#57).
- `resources_skipped` in decaf-report.json now means original-archive
  resources counted but not written (merge mode, or mirror with
  `--no-resource`) instead of its previous path-dependent meanings (#57).

## [1.5.0] - 2026-07-22

### Added

- Live per-jar progress: each executing jar shows a transient row with its
  stage (`fetching` / `decompiling`) and engine, grouped by stage under a
  `M/N done · F fetching · D decompiling · Q queued` header; scrollback
  still gets exactly one line per jar (#50).
- Stable totals: nested jars are pre-counted during the scan
  (`found N artifacts (T top-level + K nested)`), so the total no longer
  grows mid-run and self-corrects if a pre-counted entry cannot be
  extracted (#50).
- Engine preflight visibility: `engines: verifying…` /
  `engines: downloading <name> <version>…` live rows and a persistent
  `✓ <name> <version> downloaded` line on first-run downloads (#50).
- Maven sources-cache hits are now visible: `, cached` on the artifact
  status line, `maven N (K cached)` in the summary, and an additive
  `sources_cached` field in decaf-report.json (#50).
- Faster decompilation (#53): the biggest archives decompile first and keep
  scheduler headroom while they run; small jars share one engine JVM
  (vineflower/fernflower/jd); engine JVMs start from a class-data-sharing
  archive on Java 19+.

### Changed

- Kotlin decompiler output (`.kt`) now counts as decompiled source: no more
  fallback re-decompile of Kotlin jars, `.kt` files reach the output tree in
  both mirror and merge modes, and `java_files` counts include them (#53).
- Fallback order now tries Fernflower before Procyon (Vineflower → CFR →
  Fernflower → Procyon → JD-CLI). On a 19,453-class obfuscated jar, Procyon
  wedged on a single class — burning CPU for 12+ minutes with no output
  after completing 3,668 classes — while Fernflower decompiled the archive
  in full and produced the fewest defects of any engine measured (0 parse
  errors; 61% of a 400-class sample had a javac error attributable to the
  decompiler, versus 62% for Procyon run per-class with a timeout, 67% for
  Vineflower and 73% for CFR). Procyon remains available and unchanged as
  an explicit `--engine procyon` choice.

## [1.4.1] - 2026-07-21

### Fixed

- A broken search endpoint (e.g. an HTTP 404) is now reported honestly in
  network-tainted miss reasons as `index HTTP <code>` instead of "malformed
  index response", and the same condition during the SHA-1 lookup is no
  longer silently misreported as "sha1 not in Central index".
- A pathological `Retry-After` header whose value passes `isdigit()` but not
  `float()` (e.g. "²") no longer crashes the artifact with a traceback.
- The run summary's network line now says artifacts "fell back to
  decompilation" instead of claiming they were all decompiled — the count
  includes artifacts whose fallback decompilation then failed.

## [1.4.0] - 2026-07-21

### Added

- Transient network errors (timeouts, connection errors, HTTP 429/5xx) during
  Maven sources resolution are retried with backoff (3 attempts, `Retry-After`
  honored on 429/503). Falling back to decompilation over a network failure now
  warns loudly without `-v` — one warning per endpoint condition, a run-summary
  count, and an additive `totals["network_misses"]` report field — and a
  per-host circuit breaker gives up loudly after 3 consecutive affected
  artifacts so fully-offline runs stay fast.

### Changed

- The run pipeline is now two stages with independently sized pools: an
  IO-sized resolve/fetch stage (Maven lookups, sources downloads, nested
  discovery) feeds the CPU-sized decompile stage, so network waits no longer
  idle decompile slots and decompile bursts no longer oversubscribe the CPU.
  `--jobs` still sizes the decompile stage; the fetch pool is auto-sized
  (`min(8, 2×jobs)`, reported as `fetch_jobs` in the run report), and a bounded
  hand-off keeps downloads from running unbounded ahead of decompilation.
  Outputs and per-artifact reports are unchanged; the progress bar's total now
  grows as soon as nested archives are discovered, slightly earlier than
  before.

### Fixed

- A network error during an index lookup is no longer negative-cached as "no
  candidates" for the rest of the run, and network-failed resolution steps no
  longer masquerade as verified absences: affected artifacts' `sources_miss`
  now starts with `network:` and names the failing host and step.

## [1.3.1] - 2026-07-21

### Changed

- Package-prefix groupId guessing now strips war/Spring Boot container roots
  (`WEB-INF/classes/`, `BOOT-INF/classes/`), so war-layout archives produce
  real candidates instead of junk like `WEB-INF.classes.…`.
- Repeated artifactIds in one run no longer re-query the legacy index — the
  per-artifactId lookup is memoized for the run.

### Fixed

- `engines update` no longer crashes with a traceback when the config file
  cannot be written (permissions, corrupted TOML): the affected engine is
  reported as failed and the rest proceed. Pins are validated before being
  written, and upstream version strings containing path separators are
  refused before any download.

## [1.3.0] - 2026-07-21

### Added

- Sources resolution no longer depends on the frozen legacy SHA-1 index:
  when it misses, decaf guesses candidate Maven coordinates from the
  artifact's filename, manifest, and package prefixes, and verifies each
  against the repository's `.jar.sha1` before fetching — so artifacts newer
  than the index freeze (mid-2025) resolve to real sources again, and forks
  or patched jars are never mismatched. The run report gains `resolved_by`
  and `sources_miss` fields, and `-v` prints one `maven <artifact>: …` line
  explaining each hit or miss.
- `decaf engines` subcommands: `list` (pins, cache state, Java compatibility),
  `fetch` (pre-download for offline/CI), `clean [--stale]`, and `update`
  (checksum-verified pin updates recorded as `[engines.NAME]` config
  overrides, with `--version` and `--reset`). Plain `decaf INPUT` is
  unchanged; `decaf run INPUT` also works.

## [1.2.0] - 2026-07-20

### Added

- Windows support: engine cleanup no longer relies on POSIX process groups,
  CRLF stderr from engine children is handled, and concurrent sources-jar
  downloads survive Windows rename semantics. CI now tests Linux, macOS, and
  Windows.

### Changed

- The default CPU budget is now all cores minus one, so an unflagged run
  leaves the machine responsive. Pass `--cpus` explicitly to use every core.

### Fixed

- `--cpus` is now actually enforced on Linux: decaf pins itself — and thus
  every engine JVM — to that many cores via CPU affinity. Previously
  `-XX:ActiveProcessorCount` only sized JVM thread pools, and JIT/GC warmup
  burst well past the budget. On macOS/Windows the budget stays hint-only.
- If decaf itself dies uncatchably (`kill -9`, OOM-kill), the kernel now
  reaps the engine JVMs via `PR_SET_PDEATHSIG` on Linux; previously they were
  orphaned and kept decompiling at full tilt.

## [1.1.0] - 2026-07-19

### Added

- `-v` streams engine stderr live while decompiling, each line prefixed
  `<engine> <artifact>:` so parallel workers stay readable. Failure tails in
  the final summary are unchanged.

### Fixed

- `-o` pointing at an existing file now exits 2 with a clean error instead of
  crashing with a traceback.
- `--repo` URLs are validated up front: a non-http(s) scheme (e.g. a
  `htp://` typo) exits 2 immediately instead of failing mid-run.
- `decaf-report.json` is written even when a second Ctrl-C lands during
  teardown, so partial results survive an impatient abort.

## [1.0.0] - 2026-07-18

First public release.

### Added

- Recursive decompilation of `.jar` / `.war` / `.ear` / `.aar` and loose
  `.class` trees, including archives nested inside archives (`--max-depth`,
  default 1; deeper archives are reported as skipped, never silently dropped).
- Five decompiler engines with automatic fallback — Vineflower, CFR, Procyon,
  Fernflower, JD-CLI — retrying at whole-archive and per-class level. Engines
  auto-download on first use (version-pinned, sha256-verified).
- Maven sources-first resolution: embedded `pom.properties` or SHA-1 lookup on
  Maven Central, so artifacts with published sources are fetched instead of
  decompiled. Custom repositories via config file or repeatable `--repo`.
- Mirror output layout by default (one directory per archive, mirroring the
  input tree); `--merge` produces a single combined `src/` package tree with
  deterministic collision handling.
- Parallel workers (`--jobs`) under a CPU budget: each engine JVM runs with
  `-XX:ActiveProcessorCount = cpus ÷ jobs` (`--cpus` to cap) so batches don't
  oversubscribe the machine.
- Live progress whose total grows as nested archives are discovered.
- JSON run report (`decaf-report.json`) with per-artifact outcomes, engine
  attempts, collisions, and skip reasons.
- Exit codes: `0` all succeeded · `1` some failed · `2` usage/environment
  error · `130` interrupted.

[1.5.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.5.0
[1.4.1]: https://github.com/vcth4nh/decaf/releases/tag/v1.4.1
[1.4.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.4.0
[1.3.1]: https://github.com/vcth4nh/decaf/releases/tag/v1.3.1
[1.3.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.3.0
[1.2.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.2.0
[1.1.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.1.0
[1.0.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.0.0
