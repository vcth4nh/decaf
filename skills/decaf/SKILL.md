---
name: decaf
description: Use when a task needs the Java source of a .jar, .war, .ear, .aar, or .class file and the decaf CLI is available - reading a dependency or vendor jar, reviewing a deployed app, grepping an app and its libraries, or recovering lost sources. Tells which decaf command fits which task, what a run writes and how long it takes, what the exit codes and the report mean, and what to trust without re-checking the bytecode.
compatibility: Requires decaf on PATH (uv tool install git+https://github.com/vcth4nh/decaf) and a JDK on PATH (Java 17+ for the default engine, javac for the compile check). Network access to Maven Central and GitHub is optional - it gives original sources and the first engine download.
---

# decaf

All-in-one Java decompiler CLI. `decaf INPUT -o OUT` turns every archive and class file
under INPUT into a source tree under OUT, plus `OUT/decaf-report.json`. `decaf run --help`
lists the flags; this skill tells you what to expect.

Typical flow:

1. `decaf INPUT -o out` (add `--merge` for one tree to grep, see the trade-off below).
2. Read the exit code and the ending block (both below).
3. Open `out/<archive>/<package path>/<Class>.java`. A jar inside a war sits at
   `out/app.war/WEB-INF/lib/dep.jar/<package path>/<Class>.java`; with `--merge`, everything is
   under `out/src/<package path>/`.
4. For the detail of one artifact, or on `✗`, `!`, or exit 1: `decaf report out --problems`,
   then `decaf report out --artifact '*NAME*'`.

## What decaf does

- **Sources first.** A jar with Maven coordinates gets the real `-sources.jar` from Maven
  Central instead of a decompile. decaf proves the match: the SHA-1 of the jar must equal
  the SHA-1 of the published jar. A patched, shaded, or private jar never matches and is
  decompiled. The lookup sends the SHA-1 of each unknown jar to Maven Central; `--no-maven`
  keeps a run offline and decompiles everything.
- **Five engines, automatic fallback.** Vineflower, then CFR, Fernflower, Procyon, JD. An
  engine that crashes, times out, misses classes, or writes a stub hands the archive or the
  class to the next one. `--engine cfr` only changes the first engine; the fallback stays on
  unless you add `--no-fallback`.
- **Compile check.** decaf compiles each decompiled file alone with `javac` (no class files
  are written). A file with a parse, name, or flow error goes to the next engine; the first
  version without such errors stays. Maven sources are not checked.
- **Inputs.** One archive, one `.class` file, or a folder, scanned recursively. Archives
  inside archives are unpacked one level deep by default: the jars under `WEB-INF/lib` of a
  war are level 1. `--max-depth 2` goes one level deeper. A deeper archive is copied through
  as a file and listed as `skipped` in the report.

## Pick the command

| Task | Command | The sources land in |
|---|---|---|
| Read one jar | `decaf lib.jar -o out` | `out/lib.jar/<package path>/<Class>.java` |
| Grep or open an app with its libraries as one tree | `decaf app.war -o out --merge` | `out/src/<package path>/<Class>.java` |
| A folder of jars, sources only | `decaf ./libs -o out --no-resource` | `out/<path relative to libs>/<archive>/...` |
| Private or vendor jar, offline, or the deployed bytecode rather than upstream sources | `decaf lib.jar -o out --no-maven` | as above |
| Faster and rougher | add `--no-check` (about half the time) | |
| Shared machine | add `--cpus 4 --memory 8g` | |
| One engine with its own options | `decaf lib.jar -o out --engine cfr --no-fallback -- --renameillegalidents true` | |
| Script around it | `decaf lib.jar -o out --format json > report.json`, or `--format ndjson` | |
| Prepare an offline machine | `decaf engines fetch` once, with network | |

- The default output is `./decaf-out`, relative to the current folder. Pass `-o`.
- A non-empty OUT stops the run with exit 2 and `error: output out is not empty (use --force
  to write anyway)`. Use a new folder, or `--force`.
- The mirror layout (default) copies every resource byte-for-byte (manifests, `web.xml`,
  licenses, `pom.xml`) and turns a nested archive into a folder named after it. A failed
  artifact gets no folder. `--no-resource` keeps the layout and drops the resources.
- `--merge` writes `.java` and `.kt` files only, under `out/src/`. You lose which archive a
  file came from; duplicate classes are deduped, first wins by input path order, and the
  report lists the conflicts; container prefixes such as `WEB-INF/classes/` are stripped. For
  a review that must name the archive, keep the mirror and use `grep -r --include='*.java'`.
- Options after `--` go to the engine unchanged and need `--no-fallback`; each engine has
  its own option names.
- In the default output mode, everything (progress, ending, errors) goes to stdout and
  stderr stays empty. With `--format json` or `ndjson`, stdout carries only the JSON and the
  narration moves to stderr.

## What to expect from a run

- **Time.** One small jar: seconds. An app of 80 archives (jenkins.war): 7 minutes by
  default, 23 minutes with `--no-maven` (everything decompiled and checked), 7 minutes with
  `--no-maven --no-check`. Run a big job in the background and read the report at the end.
  `--timeout 600` is the per-archive engine limit, in seconds.
- **The first run** downloads the five engines (sha256-verified) into the user cache dir.
  Later runs need the network only for Maven lookups.
- **Writes outside OUT, always.** The user cache dir (`~/.cache/decaf` or `$XDG_CACHE_HOME/decaf`
  on Linux, `~/Library/Caches/decaf` on macOS; `decaf engines list` prints its `engines`
  subfolder) holds the engines, the downloaded sources jars, and the lookup verdicts: a SHA-1
  match is kept forever, a "no sources" answer for a week. Archives are unpacked in a
  `decaf-*` folder under the system temp dir (`TMPDIR`) and removed at the end, and the JVMs
  write `hsperfdata_<user>` there. No flag turns these writes off. `decaf cache clean` wipes
  the Maven part of the cache; `--fresh-maven` ignores the verdicts once.
- **CPU and memory.** By default decaf uses all cores but one and gives the engine JVMs 80%
  of the RAM as one heap budget. The line `engine memory: up to 25 GB (4 jobs, 5.8 GB heap
  each; a whale gets 7.8 GB)` is a budget, not a reservation; a whale is an archive with
  3000 or more classes. The ending line `Memory  budget … · largest peak 0.2 GB` shows the
  real use.
- **The ending block.** One line per artifact: `✓` complete, `!` partial, `✗` failed, with
  the source of its code: `(maven sources, org.jsoup:jsoup:1.17.2, cached)` or `(vineflower,
  4 classes)`. `cached` appears only on a Maven line and means the sources jar came from the
  cache, not the network; a cached "no sources" verdict shows only in the report. Then
  `Artifacts N processed · N complete`, plus `· N partial` and `· N failed` when they are not
  zero, `Sources N Maven · N decompiled · N extracted`, the failures grouped by cause, and
  the paths of OUT and the report. The ending shows no compile-check counts; the report does.
  When stdout is not a terminal, lines wrap at 80 columns, and a long path can break in two:
  use the path you gave `-o`.
- **Counts.** `N classes` counts top-level classes. Inner and anonymous classes (`Outer$1`,
  `Outer$Config`) are nested inside `Outer.java`; there is no `Outer$1.java`. `java_files`
  also counts `package-info.java` files, so it can exceed `classes`. For Maven sources,
  `classes` is 0. A jar built without debug info gives names such as `var1`; that is the
  bytecode, not a defect.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | no artifact failed; `!` partial artifacts are possible | read the ending for `!`, then the sources |
| 1 | at least one artifact failed (`✗`); the other artifacts are complete or partial, and their sources are usable | `decaf report out --problems`, then go on |
| 2 | usage or environment error: bad flag, non-empty OUT, Java too old | read the one `error:` line |
| 130 | interrupted (Ctrl-C, SIGTERM, SIGHUP); the report is still written with status `interrupted` | re-run with `--force` or into a new folder |

## Read the result

- `decaf report out` reprints the ending. `--problems` lists the failed artifacts grouped
  by cause, then the partial ones. `--artifact '*NAME*'` shows one artifact in full: how it
  resolved, each engine attempt, the check counts, and the flagged, noted, and unchecked
  files, each with its error count and first javac error (the full list is in the JSON).
  The glob matches the whole artifact name, which is `app.war!/WEB-INF/lib/NAME` for a
  nested jar, so start the pattern with `*`; `no artifacts match` exits 0. `--problems` and
  `--network-fallbacks` print only the header line when the list is empty; that means none.
- **Failure causes** (`✗`): `unreadable archive` (not a zip, or truncated: get a new copy,
  decaf cannot help), `all engines failed` (read the attempts in `--artifact`), `case
  collision` (the output tree ignores case; use a case-sensitive volume for OUT and `TMPDIR`),
  `interrupted`.
- **`✓` means complete, not compiled.** The check result is in the report, not in the mark:
  - **flagged**: a parse, name, or flow error that no engine fixed. The file of the first
    engine stays and the artifact is partial (`!`), so a `✓` artifact has no flagged file.
    Read a flagged file with care; verify its logic against the bytecode (`javap -c -p`) when
    a decision depends on it.
  - **noted**: type errors only, for example a lost generic argument (`List var2` for
    `List<String>`, which also gives errors at the uses of `var2`), or a checked exception
    that javac rejects in source. The logic is intact and the artifact stays `✓`. Classes
    from Kotlin, Scala, Groovy, and Lombok land here often.
  - **unchecked**: the file refers to a class outside the input, so javac could not compile
    it. No defect is known. A single library jar without its dependencies is mostly
    unchecked; a war or a fat jar is mostly checked.
  - **stubbed**: an engine wrote a placeholder in place of a method body. decaf retries the
    class with the next engine and counts the stubs that remain; they make the artifact
    partial.
- **Maven sources or decompiled?** The artifact line says it. In the JSON, `method` is
  `maven` or the engine name, and `gav`, `resolved_by`, and `sources_miss` say how the
  lookup went. Vineflower files carry no header comment; CFR files start with `Decompiled
  with CFR`. Maven sources are the upstream sources of that exact version, with javadoc and
  real names.
- **Multi-release jars.** The classes under `META-INF/versions/N` are variants for newer
  Javas. A sources jar can lack them, the default engines write only the base version, the
  mirror does not copy their `.class` files, and `missing_classes` does not count them. No
  engine writes `module-info`. When completeness matters, run `unzip -l lib.jar | grep
  META-INF/versions`; `--engine cfr --no-maven` writes the variants to
  `out/lib.jar/META-INF/versions/N/<package path>/<Class>.java`.
- **Trust.** A `✓` artifact needs no javap, recompile, or hash check from you: the check
  compiled the files, and the Maven match compared the hash. Spend verification on flagged
  files and on partial or failed artifacts. One thing a source grep cannot give you: an
  exhaustive search for the users of an API, because a chained call such as
  `x.getDigest().digest(b)` has no type name in the source; grep the class files too
  (`grep -rl 'java/security/MessageDigest'` over the unpacked jar).

## Report fields for scripts

`decaf-report.json` (also `--format json` on stdout) has `status` (`completed`,
`completed_with_failures`, `interrupted`, `aborted`), `totals` (`artifacts`, `ok`,
`partial`, `failed`, `skipped`, `maven_sources`, `decompiled`, `java_files`,
`flagged_files`, `noted_files`, `unchecked_files`, `network_misses`), and `artifacts[]`
with `rel`, `kind`, `outcome` (`ok`, `failed`, `skipped`; partial is `ok` with missing
classes, stubs, flagged files, or a network miss), `method`, `gav`, `repo`, `resolved_by`,
`sources_miss`, `sources_cached`, `classes`, `java_files`, `missing_classes`, `failure`,
`attempts[]` (`engine`, `level`, `returncode`, `timed_out`, `stderr_tail`, `peak_rss_mb`),
`check` (`checked`, `flagged`, `noted`, `unchecked`, `unchecked_reasons`), and the lists
`flagged[]`, `noted[]`, `unchecked[]` (`path`, `engine`, `first`, `messages`).
`--format ndjson` streams `scan`, `artifact`, `warning`, and one final `summary` line.

## Common mistakes

- Reading `decaf --help`, `decaf run --help`, `decaf engines list`, and `unzip -l` before
  the first run. Run `decaf INPUT -o out` first; the ending tells you what happened.
- Searching `out/` with `find` for a class. It is at `out/<archive>/<package path>/<Class>.java`
  (mirror) or `out/src/<package path>/<Class>.java` (merge).
- Treating exit 1 as a failed run, or a `!` artifact as unusable.
- Verifying a `✓` artifact again with javap, a recompile, or a SHA-1 comparison.
- Expecting `Outer$1.java`, or expecting `N classes` to equal the number of `.class` files.
- `--artifact 'NAME*'` for a jar inside a war: the name starts with the war, use `'*NAME*'`.
- Editing the output tree by hand: a later `--force` run rewrites it.
- Expecting `--engine cfr` alone to give pure CFR output, or passing engine options without
  `--no-fallback`.
