# decaf

All-in-one Java decompiler CLI. Point it at a folder; it decompiles every
`.jar` / `.war` / `.ear` / `.aar` / loose `.class` it finds (including archives
nested inside archives — one level deep by default, `--max-depth` to change)
into a source tree that mirrors your input, or one merged package tree with
`--merge`.

- **Sources first:** artifacts that resolve to a Maven GAV (embedded
  `pom.properties`, SHA-1 lookup on Maven Central, or coordinates guessed
  from the filename/manifest and proven against the repo's `.jar.sha1`)
  get their real `-sources.jar` downloaded instead of decompiled. The
  report records how each artifact resolved — or why it didn't.
- **Five engines, automatic fallback:** Vineflower → CFR → Procyon →
  Fernflower → JD-CLI. If an engine crashes, times out, or misses classes,
  the next one takes over (whole-archive and per-class retries).
- **Engines auto-download** on first use (pinned versions, sha256-verified)
  into your user cache dir. Manage them with `decaf engines list|fetch|clean|update`.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Java 11+ on PATH (Java 21+ enables all five engines; Vineflower needs 17+,
  Fernflower 21+)
- Linux, macOS, or Windows (the CPU budget is hard-enforced on Linux,
  hint-only elsewhere)

## Install

```bash
uv tool install git+https://github.com/vcth4nh/decaf   # no checkout needed
uv tool install .        # from a checkout → `decaf` on PATH
# or run without installing:
uvx --from . decaf --help
```

## Usage

```bash
decaf ./libs                        # one folder per archive under ./decaf-out
decaf app.war -o out --merge        # single merged source tree in out/src
decaf ./libs --engine cfr --no-fallback
decaf ./libs --no-maven -j 8 --timeout 120
decaf ./libs --cpus 8               # cap total CPU (shared machine)
decaf ./libs --max-depth 2          # also unpack jars nested two archives deep
decaf ./libs --repo https://nexus.mycorp.com/repository/maven-public
```

Exit codes: `0` all artifacts succeeded · `1` some failed (see
`decaf-out/decaf-report.json`) · `2` usage/environment error.

Archive nesting is capped by `--max-depth` (default 1: jars inside a war or
fat jar are processed, but not jars inside those). Deeper archives are listed
in the report as skipped. Folder recursion is never limited.

CPU use is budgeted. On Linux the budget is enforced with CPU affinity:
decaf pins itself to the first `cpus` cores and every engine JVM inherits
the mask, so the rest of the machine stays genuinely free. Each JVM is also
started with `-XX:ActiveProcessorCount = cpus ÷ jobs` to size its thread
pools (on other platforms this hint is the only limit). The default budget
is all cores minus one, keeping the machine responsive during long runs;
`--cpus N` sets it exactly (e.g. your full core count to use everything).
Workers are clamped so they never exceed the budget.

## Output layouts

**Mirror (default):** the output mirrors the input tree, one directory per
archive with its full engine output including resources:
`in/libs/app.war` → `out/libs/app.war/WEB-INF/lib/dep.jar/<sources>`.

**Merged (`--merge`):** every artifact's `.java` files are merged into
`OUTPUT/src/` by package — ready to open in an IDE. Duplicate classes are
deduped; conflicting duplicates are first-wins (deterministic by input path
order) and recorded in the report. Container prefixes like `WEB-INF/classes/`
are stripped. Resources are skipped (counted in the report).

## Configuration

`~/.config/decaf/config.toml` (or `--config PATH`):

```toml
# Maven repositories, tried in order.
# Maven Central is appended automatically unless listed explicitly.
repositories = [
  "https://nexus.mycorp.com/repository/maven-public",
  "https://user:pass@private.repo/maven2",   # basic auth via URL userinfo
]
```

`--repo URL` (repeatable) prepends ad-hoc repositories. SHA-1 lookup is
Central-only; jars built by Maven almost always embed `pom.properties`, which
works against any repository.

## Engines

| Engine | Version | Min Java | License |
|---|---|---|---|
| [Vineflower](https://github.com/Vineflower/vineflower) | 1.12.0 | 17 | Apache-2.0 |
| [CFR](https://github.com/leibnitz27/cfr) | 0.152 | 11 | MIT |
| [Procyon](https://github.com/mstrobel/procyon) | 0.6.0 | 11 | Apache-2.0 |
| [Fernflower](https://github.com/JetBrains/intellij-community/tree/master/plugins/java-decompiler/engine) (JetBrains `java-decompiler-engine`) | 253.33813.25 | 21 | Apache-2.0 |
| [JD-CLI](https://github.com/intoolswetrust/jd-cli) | 1.2.0 | 11 | GPL-3.0 |

### Managing engines

```bash
decaf engines list             # pins, cache state, Java compatibility
decaf engines fetch            # pre-download all engines (offline/CI prep)
decaf engines clean --stale    # drop superseded jars (bare: wipe the cache)
decaf engines update           # update pins to upstream latest
decaf engines update cfr --version 0.150   # pin an exact version
decaf engines update --reset   # back to built-in pins
```

`update` verifies downloads against upstream-published checksums (sha256, or
sha1 with a warning when that's all the repo offers; an engine publishing no checksum at all fails closed — reported with a red ✗ and exit code 1 — keeping its current pin) and records the new pin under `[engines.NAME]`
in your config file — note that rewriting drops hand-written comments there.
Every later run verifies the cached jar against that pin, exactly like the
built-in ones. Like a folder named `engines`, a folder literally named `run`
needs an explicit path (`decaf ./run`).

## Development

```bash
uv sync
uv run pytest                      # fast offline suite
uv run pytest -m "slow or network" # + real-engine and live-Maven integration
```
