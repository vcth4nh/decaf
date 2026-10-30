# decaf

decaf turns Java archives and loose `.class` files into source trees. It
scans folders for `.jar`, `.war`, `.ear`, and `.aar` files, including nested
archives (one level deep by default; change it with `--max-depth`). Sources
mirror your input, or form one merged package tree with `--merge`.

![decaf decompiling a sample WAR, checking its Java sources, and inspecting the report](docs/demo.gif)

- **Sources first:** decaf downloads published `-sources.jar` files when it
  can match an artifact to Maven coordinates (group, artifact, version).
  Coordinates from `pom.properties` or filename/manifest guesses must match
  the repository's `.jar.sha1`; a SHA-1 lookup on Maven Central can also
  resolve them. The report records each match or miss. Network errors are
  retried with backoff; falling back to decompilation after a network failure
  produces a warning and report entries (`sources_miss: "network: …"`,
  `totals.network_misses`). SHA-1 matches are cached indefinitely and verified
  misses for a week, so later runs can reuse cached sources without a lookup.
  `--fresh-maven` rechecks verdicts; `decaf cache clean` removes cached
  sources and verdicts.
- **Five engines, automatic fallback:** Vineflower → CFR → Fernflower →
  Procyon → JD-CLI. If an engine crashes, times out, misses classes, or
  writes a stub in place of a method body, the next one takes over
  (whole-archive and per-class retries).
- **Compile check:** decaf checks decompiled Java files with `javac` and
  gives a class with decompiler errors to the next engine (see
  [Compile check](#compile-check)).
- **Engines auto-download** on first use (pinned versions, sha256-verified)
  into your user cache dir. Manage them with `decaf engines list|fetch|clean|update`.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Java on PATH that can run the engines that you use. `decaf engines list`
  shows their compatibility with your Java. When Java is too old for the
  primary engine, the run stops and gives a hint.
- A JDK (with `javac`) for the compile check. With a JRE only, decaf runs
  without the check and prints one note.
- Linux, macOS, or Windows (the CPU budget is hard-enforced on Linux,
  hint-only elsewhere)
- A case-sensitive output tree for archives whose classes differ only by case
  (common in ProGuard-obfuscated jars). On Windows with WSL installed, decaf
  makes its output and temp folders case-sensitive automatically. Elsewhere on
  a case-insensitive tree (default macOS, Windows without WSL, FAT/exFAT) such
  archives are refused with a `case collision` failure and a hint, because two
  classes cannot share one file. decaf compares names after NFD and full case
  folding, so it also refuses names that differ only by the Unicode normal
  form, and some names that NTFS keeps apart (for example `ß` and `ss`).
  To decompile them there anyway, use a case-sensitive volume for the output
  and point `TMPDIR` at it too: decaf's temp folder must not ignore case
  either.

## Install

```bash
uv tool install git+https://github.com/vcth4nh/decaf   # no checkout needed
uv tool install .        # from a checkout → `decaf` on PATH
# or run without installing:
uvx --from . decaf --help
```

## Agent skill

For AI coding agents (Claude Code, Codex, and others), decaf ships an
[Agent Skill](https://agentskills.io) in `skills/decaf/SKILL.md`: which
command fits which task, what a run writes, how long it takes, and how to
read the report. Install it with the
[skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add vcth4nh/decaf
```

## Usage

Repository URLs in the examples are placeholders; replace them with your own.

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

Use a new or empty output directory, or pass `--force` to write into an
existing one. `--force` does not remove files left by earlier runs.

Exit codes: `0` no artifacts failed · `1` some failed (see
`decaf-out/decaf-report.json`) · `2` usage/environment error · `130`
interrupted by Ctrl-C, SIGTERM, or SIGHUP (under `nohup`, SIGHUP stays
ignored). Interrupted runs write a report with status `interrupted`;
errors that stop a run set `aborted`.

Partial results, skipped archives, and merge collisions can still exit `0`.
Check the report's `totals.partial`, `totals.skipped`, and `totals.collisions`
when completeness matters.

`--format json` prints the run report as one JSON document to stdout after
the run (byte-identical to `decaf-report.json`); `--format ndjson` streams
one JSON object per line as the run progresses: `scan`, `artifact`
(per completion), `warning`, then a final `summary` line carrying `status`
and the report path. Both modes send narration to stderr, keeping stdout
machine-parseable (empty on usage errors, exit `2`). `-q`/`-v` control stderr
narration. Exit codes are the same in all three formats.

Everything after a literal `--` is passed verbatim to the engine invocation
(CFR's `--renameillegalidents true`, Fernflower's `-dgs=1`, …). It requires
`--no-fallback`: option dialects are engine-specific, and a fallback engine
would not understand them. decaf does not validate these tokens; colliding
with flags decaf manages itself (like CFR's `--outputdir`) is unsupported.

Archive nesting is capped by `--max-depth` (default 1: jars inside a war or
fat jar are processed, but not jars inside those). Deeper archives are listed
in the report as skipped. Folder recursion is never limited. When an
archive has two entries with one name, decaf uses the last one, as Java
does, and warns when the two are not the same.

CPU use is budgeted. On Linux the budget is enforced with CPU affinity:
decaf pins itself to the first `cpus` cores and every engine JVM inherits
the mask. Each JVM is also
started with `-XX:ActiveProcessorCount = cpus ÷ jobs` to size its thread
pools (on other platforms this hint is the only limit). The default budget
is all the cores that decaf may use (its CPU affinity, for example from
`taskset` or the cpuset of a container) minus one, keeping the machine
responsive during long runs;
`--cpus N` sets it exactly (e.g. your full core count to use everything).
Workers are clamped so they never exceed the budget. Maven resolution and
sources downloads run on a separate IO-sized fetch stage feeding the
decompile workers through a bounded queue, so network waits don't idle
decompile slots.

Memory use is budgeted too. All engine JVMs together get one heap budget:
80% of the RAM by default (or of a lower memory limit of decaf's cgroup or a
parent cgroup, for example in a container or a systemd scope).
Use `--memory 8g` to set it or `--memory 0` to let the JVM choose its heap. The
budget is split in equal shares, one for each job; an archive with 3000 or
more classes (a "whale") gets two shares, and `-j` is lowered
when a share would fall below 1 GiB of heap. Each heap is its share less the
JVM's own memory: 300 MB plus 2% of the share. With the default budget, no JVM
gets more than 25% of the available RAM; an explicit `--memory` uses the
plain shares. This caps Java heaps, not total process memory. Before
the decompile starts, decaf prints the split and warns when an artifact
probably needs more heap than its share. The report records the peak memory
(on Linux, unknown for a peak near or below the memory of decaf)
and the heap of each engine attempt, the ending shows the largest peak and
counts the attempts that ran out of heap (the jars of one batch share one
JVM, which counts once), and `decaf report out --memory`
lists the ten JVMs with the largest peaks (one row for a batch). A heap flag in `JAVA_TOOL_OPTIONS` or
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
decaf report out --memory             # the ten engine JVMs with the largest peak memory
```

`decaf report` reads `decaf-report.json` offline. With no flags, it prints
the run summary. `PATH` may be the output directory or the JSON file itself
(default: `decaf-out`).

![Report excerpts from a sample WAR, showing completed artifacts, source counts, heap limits, and measured memory peaks](docs/report.svg)

## Output layouts

**Mirror (default):** the output mirrors the input tree, one directory per
archive: `in/libs/app.war` → `out/libs/app.war/WEB-INF/lib/dep.jar/<sources>`.
Non-class files are copied from the original archive byte-for-byte
(even when sources came from Maven or every engine failed). `.class` files
are replaced by decompiled `.java`/`.kt`, and nested archives become
directories. Archives nested beyond `--max-depth` are copied through unchanged as files.
`--no-resource` keeps the mirrored layout but writes decompiled/extracted
sources only. Corrupt archives are reported but not copied, damaged entries
are skipped, and a stray `.java`/`.kt` shipped inside a binary jar is dropped
in favor of the decompiler's output.

**Merged (`--merge`):** every artifact's source files (`.java`/`.kt`) are
merged into `OUTPUT/src/` by package. Duplicate classes are
deduped; conflicting duplicates are first-wins (deterministic by input path
order) and recorded in the report. Container prefixes like `WEB-INF/classes/`
are stripped. Resources are skipped (counted in the report).

![The same sample WAR in mirror and merged layouts: mirror keeps the nested JAR and web.xml; merge puts both Java files under src/demo](docs/output-layouts.svg)

## Compile check

After an engine writes a Java class, decaf checks its source file alone with
`javac` and writes no class files. This finds errors that the decompiler made,
for example a variable that the engine renamed in its uses but not in its
declaration. A class with a parse, name, or flow error goes to the next engine
of the fallback chain, and the first version without such errors goes to the
output. When no version passes the check, the file of the first engine stays,
the report lists it under `flagged`, and the artifact is partial (yellow `!`).
Type errors, for example lost generic type arguments, do not cause a retry:
the report lists such files under `noted`. Bytecode can throw undeclared
checked exceptions and can have two methods that differ only in their return
type, but javac rejects both in a source file. For a class from Kotlin, Scala,
or Groovy, these two errors also go under `noted`: decaf finds such a class by
its Kotlin or Scala annotation, the Groovy interface `GroovyObject`, or the
source file name in the class file. For other classes, for example a Lombok
class with `@SneakyThrows`, the retries stop when two engines in a row give the
same first error of these two kinds. An undeclared `Throwable` or `Exception`
does not stop them, because some engines drop the cast of `throw (T) t`.
For a Groovy class, the retries stop when two engines in a row flag its file.
No engine writes compilable Java for the call-site and meta-class code of
groovyc, and each engine gives a different first error for it.
For old bytecode, an engine can write an inner class to a file of its own, for
example `XStream$1.java`. Such a file uses names that javac makes for a nested
class, for example `this$0`, and these errors go under `noted`. When such a
file has other errors, its outer class goes to the next engine. That engine can
nest the inner class in the file of the outer class.
For each flagged and noted file,
the report gives the javac errors in line order (at most 100). It also lists
the files that the check could not examine under `unchecked`, with the reason.
`decaf report OUTPUT --artifact NAME` shows these lists.
Kotlin `.kt` output is `unchecked`; Maven sources are not checked.

The classpath of the check is the input itself: the archive, the other
archives of the same war, fat jar, or input folder, and their
`WEB-INF/classes` or `BOOT-INF/classes`. Missing dependencies usually make a
file `unchecked`, but syntax errors still flag it. Supplying the dependency
jars lets the checker examine more files.

decaf uses the `javac` of the `java` on PATH. It must be at least as new as
the class files: for newer class files, decaf prints the Java version that the
check needs.

The check makes a run longer. In one test on `jenkins.war`, a normal run took
7 minutes with the check and 5 minutes without it. Maven supplied the sources
of 46 of its 80 archives, and decaf does not check Maven sources. With
`--no-maven`, decaf decompiled and checked all 80 archives: 23 minutes with the
check, 7 minutes without it. A run on one library jar took 24 seconds with the
check and 12 seconds without it. The checker JVM runs in the job of its engine,
after the engine JVM stops, and gets the same heap. `--timeout` also limits
each checker JVM. `--no-check` turns the check off.

## Configuration

The config file is `~/.config/decaf/config.toml` on Linux,
`~/Library/Application Support/decaf/config.toml` on macOS, and
`%LOCALAPPDATA%\decaf\decaf\config.toml` on Windows (or `--config PATH`).
It is TOML, so it must be UTF-8. When `XDG_CONFIG_HOME` is set, the file is
`$XDG_CONFIG_HOME/decaf/config.toml` on Linux and macOS.

```toml
# Maven repositories, tried in order.
# Maven Central is appended automatically unless listed explicitly
# (as https://repo1.maven.org/maven2 or https://repo.maven.apache.org/maven2).
repositories = [
  "https://nexus.mycorp.com/repository/maven-public",
  "https://user:pass@private.repo/maven2",   # basic auth via URL userinfo
]
```

In a user name or a password, write `/`, `?`, `#`, and `@` as `%2F`, `%3F`,
`%23`, and `%40`: decaf refuses a repository URL with an `@` after its host.
`--repo URL` (repeatable) prepends ad-hoc repositories. SHA-1 lookup is
Central-only; jars built by Maven almost always embed `pom.properties`, which
works against any repository that publishes `.jar.sha1` files. A jar whose
SHA-1 differs from the published jar (patched, shaded, or re-signed) gets
no upstream sources: decaf decompiles it.

## Engines

These are the built-in pins and their minimum Java versions. Config
overrides can require newer Java; `decaf engines list` checks the active pins.

| Engine | Version | Min Java | License |
|---|---|---|---|
| [Vineflower](https://github.com/Vineflower/vineflower) | 1.12.0 | 17 | Apache-2.0 |
| [CFR](https://github.com/leibnitz27/cfr) | 0.152 | 11 | MIT |
| [Fernflower](https://github.com/JetBrains/intellij-community/tree/master/plugins/java-decompiler/engine) (JetBrains `java-decompiler-engine`) | 253.33813.25 | 21 | Apache-2.0 |
| [Procyon](https://github.com/mstrobel/procyon) | 0.6.0 | 11 | Apache-2.0 |
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
decaf engines clean --stale    # drop superseded files (bare: wipe the engines and the checker)
decaf engines update           # update pins to upstream latest
decaf engines update cfr --version 0.150   # pin an exact version
decaf engines update --reset   # back to built-in pins
```

`update` verifies downloads against upstream-published checksums (sha256, or
sha1 with a warning when that's all the repo offers). Without a checksum, it
keeps the current pin, prints a red ✗, and exits 1. New pins go under
`[engines.NAME]` in your config file; rewriting it drops hand-written comments.
Every later run verifies the cached jar against that pin, exactly like the
built-in ones. Like folders named `engines`, `cache`, or `report`, a folder literally
named `run` needs an explicit path (`decaf ./run`).

A new pin can need a newer Java than the old one. `engines list` and each run
read the Java that a cached engine jar needs from its class files. A fallback
engine that needs a newer Java is skipped with a warning, and `update` warns
when the java on PATH is too old for the new pin.

On Java 19+, engine JVMs keep class-data-sharing (CDS) archives (`.jsa`) next
to the engine jars so that later runs start faster. `clean --stale` keeps
the archives of active pins. With a read-only engine cache, decaf runs the
JVMs without these archives.

## Development

```bash
uv sync
uv run pytest                      # fast offline suite
uv run pytest -m "slow or network" # + real-engine and live-Maven integration
```

Regenerate the README's SVGs with `uv run python docs/render_media.py`.
Add `--gif` to record the demo with [VHS](https://github.com/charmbracelet/vhs).
The script compiles a sample WAR and runs the built-in Vineflower pin,
downloading it if needed.

## Credits

decaf is inspired by [@rskvp93](https://github.com/rskvp93)'s srctools.
