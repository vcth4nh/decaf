# Changelog

All notable changes to decaf are documented here.

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

[1.1.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.1.0
[1.0.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.0.0
