# decaf

All-in-one Java decompiler CLI. Point it at a folder; it decompiles every
`.jar` / `.war` / `.ear` / `.aar` / loose `.class` it finds (including archives
nested inside archives — one level deep by default, `--max-depth` to change)
into a source tree that mirrors your input, or one merged package tree with
`--merge`.

![decaf demo — sources-first resolution and live decompile progress](docs/demo.gif)

- **Sources first:** artifacts that resolve to a Maven GAV (embedded
  `pom.properties` or coordinates guessed from the filename/manifest, each
  proven against the repo's `.jar.sha1`, or a SHA-1 lookup on Maven Central)
  get their real `-sources.jar` downloaded instead of decompiled. The
  report records how each artifact resolved — or why it didn't. Transient
  network errors are retried with backoff and never mistaken for missing
  sources; when resolution degrades to decompilation over a network
  failure, decaf warns loudly and tags the report (`sources_miss:
  "network: …"`, `totals.network_misses`). Lookup verdicts are cached
  on disk as well — sha1→GAV matches forever, "no sources published"
  answers for a week — so warm re-runs skip the index and probe traffic
  entirely, and a network hiccup can't hide sources that are already
  cached. `--fresh-maven` re-derives verdicts; `decaf cache clean`
  wipes cached sources and verdicts.
- **Five engines, automatic fallback:** Vineflower → CFR → Fernflower →
  Procyon → JD-CLI. If an engine crashes, times out, misses classes, or
  writes a stub in place of a method body, the next one takes over
  (whole-archive and per-class retries).
- **Compile check:** decaf compiles each decompiled file with `javac` and
  gives a class with decompiler errors to the next engine (see
  [Compile check](#compile-check)).
- **Engines auto-download** on first use (pinned versions, sha256-verified)
  into your user cache dir. Manage them with `decaf engines list|fetch|clean|update`.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Java 11+ on PATH (Java 21+ enables all five engines; Vineflower needs 17+,
  Fernflower 21+)
- A JDK (with `javac`) for the compile check. With a JRE only, decaf runs
  without the check and prints one note.
- Linux, macOS, or Windows (the CPU budget is hard-enforced on Linux,
  hint-only elsewhere)
- A case-sensitive output tree for archives whose classes differ only by case
  (common in ProGuard-obfuscated jars). On Windows with WSL installed, decaf
  makes its output and temp folders case-sensitive automatically. Elsewhere on
  a case-insensitive tree (default macOS, Windows without WSL, FAT/exFAT) such
  archives are refused with a `case collision` failure and a hint, because two
  classes cannot share one file. To decompile them there anyway, use a
  case-sensitive volume for the output and point `TMPDIR` at it too: decaf's
  temp folder must not ignore case either.

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
decaf ./libs --no-resource          # mirrored layout, sources only
decaf ./libs --engine cfr --no-fallback
decaf ./libs --engine cfr --no-fallback -- --renameillegalidents true
decaf ./libs --no-maven -j 8 --timeout 120
decaf ./libs --cpus 8               # cap total CPU (shared machine)
decaf ./libs --memory 8g            # cap the engine heap (shared machine)
decaf ./libs --max-depth 2          # also unpack jars nested two archives deep
decaf ./libs --repo https://nexus.mycorp.com/repository/maven-public
```

Exit codes: `0` all artifacts succeeded · `1` some failed (see
`decaf-out/decaf-report.json`) · `2` usage/environment error · `130`
interrupted by Ctrl-C, SIGTERM, or SIGHUP. An interrupted run still writes
its report (status `interrupted`), and a run that an error stops writes it
with the status `aborted`.

`--format json` prints the run report as one JSON document to stdout after
the run (byte-identical to `decaf-report.json`); `--format ndjson` streams
one JSON object per line as the run progresses instead — `scan`, `artifact`
(per completion), `warning`, then a final `summary` line carrying `status`
and the report path. Both modes send all human narration (status lines, the
ending block) to stderr instead of stdout, so stdout stays machine-parseable
even on a usage error (empty stdout, exit `2`); `-q`/`-v` still govern that
stderr narration exactly as in the default `human` format. Exit codes are
unchanged across all three formats.

Everything after a literal `--` is passed verbatim to the engine invocation
— the escape hatch for the engines' own option surfaces (CFR's
`--renameillegalidents true`, fernflower's `-dgs=1`, …). It requires
`--no-fallback`: option dialects are engine-specific, and a fallback engine
would not understand them. Passthrough tokens are yours — decaf does not
validate them, and colliding with the flags decaf manages itself (like CFR's
`--outputdir`) is unsupported.

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
Workers are clamped so they never exceed the budget. Maven resolution and
sources downloads run on a separate IO-sized fetch stage feeding the
decompile workers through a bounded queue, so network waits don't idle
decompile slots.

Memory use is budgeted too. All engine JVMs together get one heap budget:
80% of the RAM by default (or of a lower memory limit of decaf's cgroup or a
parent cgroup, for example in a container or a systemd scope),
`--memory 8g` to set it, `--memory 0` to turn the cap off (each JVM then takes
the JVM default, 25% of the RAM). The budget is split in equal shares, one for
each job; a whale (3000 or more classes) gets two shares, and `-j` is lowered
when a share would fall below 1 GiB of heap. Each heap is its share less the
JVM's own memory: 300 MB plus 2% of the share. With the default budget, no JVM
gets more heap than the JVM default either, so a whale or a low `-j` never
gets more than before; an explicit `--memory` uses the plain shares. Before
the decompile starts, decaf prints the split and warns when an artifact
probably needs more heap than its share. The report records the peak memory
and the heap of each engine attempt, the ending shows the largest peak and
counts the attempts that ran out of heap, and `decaf report out --memory`
lists the ten largest peaks. A heap flag in `JAVA_TOOL_OPTIONS` or
`JDK_JAVA_OPTIONS` loses to decaf's `-Xmx`; one in `_JAVA_OPTIONS` overrides
it, and decaf warns that the budget is then not enforced. The same applies to
an initial heap (`-Xms`) above a JVM's heap: decaf lowers it to the heap, or,
from `_JAVA_OPTIONS`, gives that JVM no cap. Under an address-space limit
(`ulimit -v`), each heap is at most half the limit, as the JVM's own default.

### Inspecting a report

```bash
decaf report                          # ./decaf-out/decaf-report.json, outcome-led ending
decaf report out --problems           # failed + partial artifacts, grouped by cause
decaf report out --artifact '*.jar'   # full detail for artifacts matching a glob (repeatable)
decaf report out --network-fallbacks  # artifacts that lost sources to network failures
decaf report out --memory             # the ten engine attempts with the largest peak memory
```

`decaf report` re-renders a `decaf-report.json` offline, without re-running anything —
no flags reprints the same outcome-led ending shown at the end of a run. `PATH` may be
the output directory or the JSON file itself (default: `decaf-out`).

## Output layouts

**Mirror (default):** the output mirrors the input tree, one directory per
archive: `in/libs/app.war` → `out/libs/app.war/WEB-INF/lib/dep.jar/<sources>`.
The tree is faithful to the input — every non-class file is carried over
from the original archive byte-for-byte (even when the sources came from
Maven, or when every engine failed), `.class` files are replaced by
decompiled `.java`/`.kt`, and nested archives become directories. Archives
nested beyond `--max-depth` are copied through unchanged as files.
`--no-resource` keeps the mirrored layout but writes decompiled/extracted
sources only. Carve-outs: corrupt archives are reported but not copied,
and a stray `.java`/`.kt` shipped inside a binary jar is dropped in favor
of the decompiler's output.

**Merged (`--merge`):** every artifact's source files (`.java`/`.kt`) are
merged into `OUTPUT/src/` by package — ready to open in an IDE. Duplicate classes are
deduped; conflicting duplicates are first-wins (deterministic by input path
order) and recorded in the report. Container prefixes like `WEB-INF/classes/`
are stripped. Resources are skipped (counted in the report).

## Compile check

After an engine writes a class, decaf compiles its source file alone with
`javac` and writes no class files. This finds errors that the decompiler made,
for example a variable that the engine renamed in its uses but not in its
declaration. A class with a parse, name, or flow error goes to the next engine
of the fallback chain, and the first version without such errors goes to the
output. When no version passes the check, the file of the first engine stays,
the report lists it under `flagged`, and the artifact is partial (yellow `!`).
Type errors, for example lost generic type arguments, do not cause a retry:
the report lists such files under `noted`. Bytecode can throw undeclared
checked exceptions and can have two methods that differ only in their return
type, but javac rejects both in a source file. For a class with the Kotlin or
Scala annotation or the Groovy interface `GroovyObject`, these two errors also
go under `noted`. For other classes, for example a Lombok class with
`@SneakyThrows`, the retries stop when two engines in a row give the same
first error of these two kinds. For each flagged and noted file,
the report gives the javac errors in line order (at most 100). It also lists
the files that the check could not examine under `unchecked`, with the reason.
`decaf report OUTPUT --artifact NAME` shows these lists.

The classpath of the check is the input itself: the archive, the other
archives of the same war, fat jar, or input folder, and their
`WEB-INF/classes` or `BOOT-INF/classes`. A file that refers to a class outside
this classpath is not checked (`unchecked`), so an absent dependency never
causes a false flag. Thus the check examines most files of a war or a fat jar,
but few files of a single library jar without its dependencies.

decaf uses the `javac` of the `java` on PATH. It must be at least as new as
the class files: for newer class files, decaf prints the Java version that the
check needs.

The check makes a run longer. In one test on `jenkins.war`, a normal run took
7 minutes with the check and 5 minutes without it. Maven supplied the sources
of 46 of its 80 archives, and decaf does not check Maven sources. With
`--no-maven`, decaf decompiled and checked all 80 archives: 23 minutes with the
check, 7 minutes without it. A run on one library jar took 24 seconds with the
check and 12 seconds without it. The checker JVM runs in the job of its engine,
after the engine JVM stops, and gets the same heap. `--no-check` turns the
check off.

## Configuration

The config file is `~/.config/decaf/config.toml` on Linux,
`~/Library/Application Support/decaf/config.toml` on macOS, and
`%LOCALAPPDATA%\decaf\decaf\config.toml` on Windows (or `--config PATH`).
It is TOML, so it must be UTF-8. When `XDG_CONFIG_HOME` is set, the file is
`$XDG_CONFIG_HOME/decaf/config.toml` on Linux and macOS.

```toml
# Maven repositories, tried in order.
# Maven Central is appended automatically unless listed explicitly
# (as repo1.maven.org or repo.maven.apache.org).
repositories = [
  "https://nexus.mycorp.com/repository/maven-public",
  "https://user:pass@private.repo/maven2",   # basic auth via URL userinfo
]
```

`--repo URL` (repeatable) prepends ad-hoc repositories. SHA-1 lookup is
Central-only; jars built by Maven almost always embed `pom.properties`, which
works against any repository that publishes `.jar.sha1` files. A jar whose
SHA-1 differs from the published jar (patched, shaded, or re-signed) gets
no upstream sources: decaf decompiles it.

## Engines

| Engine | Version | Min Java | License |
|---|---|---|---|
| [Vineflower](https://github.com/Vineflower/vineflower) | 1.12.0 | 17 | Apache-2.0 |
| [CFR](https://github.com/leibnitz27/cfr) | 0.152 | 11 | MIT |
| [Procyon](https://github.com/mstrobel/procyon) | 0.6.0 | 11 | Apache-2.0 |
| [Fernflower](https://github.com/JetBrains/intellij-community/tree/master/plugins/java-decompiler/engine) (JetBrains `java-decompiler-engine`) | 253.33813.25 | 21 | Apache-2.0 |
| [JD-CLI](https://github.com/intoolswetrust/jd-cli) | 1.2.0 | 11 | GPL-3.0 |

A multi-release jar can have more versions of a class under
`META-INF/versions/N/`. Vineflower, Fernflower, and Procyon write only the
base version of such a class, and decaf does not count the other versions as
missing. CFR and JD also write the other versions: use `--engine cfr` or
`--engine jd` to get them. With `--merge`, the merged tree keeps the base
version, and the report lists each other version as a collision. A class that
has no base version still goes to the output: when the first engine does not
write it, the next engine decompiles the class of its lowest version folder.

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
built-in ones. Like folders named `engines`, `cache`, or `report`, a folder literally
named `run` needs an explicit path (`decaf ./run`).

## Development

```bash
uv sync
uv run pytest                      # fast offline suite
uv run pytest -m "slow or network" # + real-engine and live-Maven integration
```

## Credits

decaf is inspired by [@rskvp93](https://github.com/rskvp93)'s srctools.
