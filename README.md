# decaf

All-in-one Java decompiler CLI. Point it at a folder; it decompiles every
`.jar` / `.war` / `.ear` / `.aar` / loose `.class` it finds (including archives
nested inside archives — one level deep by default, `--max-depth` to change)
into a source tree that mirrors your input, or one merged package tree with
`--merge`.

- **Sources first:** artifacts that resolve to a Maven GAV (embedded
  `pom.properties`, or SHA-1 lookup on Maven Central) get their real
  `-sources.jar` downloaded instead of decompiled.
- **Five engines, automatic fallback:** Vineflower → CFR → Procyon →
  Fernflower → JD-CLI. If an engine crashes, times out, or misses classes,
  the next one takes over (whole-archive and per-class retries).
- **Engines auto-download** on first use (pinned versions, sha256-verified)
  into your user cache dir.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Java 11+ on PATH (Java 21+ enables all five engines; Vineflower needs 17+,
  Fernflower 21+)

## Install

```bash
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

CPU use is budgeted: each engine JVM is started with
`-XX:ActiveProcessorCount = cpus ÷ jobs`, so the total stays near your core
count instead of oversubscribing (decompilers like Vineflower default to one
thread per visible core). `--cpus N` lowers the overall budget; workers are
clamped so they never exceed it.

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

| Engine | Version | Min Java |
|---|---|---|
| [Vineflower](https://github.com/Vineflower/vineflower) | 1.12.0 | 17 |
| [CFR](https://github.com/leibnitz27/cfr) | 0.152 | 11 |
| [Procyon](https://github.com/mstrobel/procyon) | 0.6.0 | 11 |
| Fernflower (JetBrains `java-decompiler-engine`) | 253.33813.25 | 21 |
| [JD-CLI](https://github.com/intoolswetrust/jd-cli) | 1.2.0 | 11 |

## Development

```bash
uv sync
uv run pytest                      # fast offline suite
uv run pytest -m "slow or network" # + real-engine and live-Maven integration
```
