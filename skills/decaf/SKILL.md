---
name: decaf
description: Use when a task needs the Java source of a .jar, .war, .ear, .aar, or .class file and the decaf CLI is available - reading a dependency or vendor jar, reviewing a deployed app, grepping an app and its libraries, or recovering lost sources. Tells which decaf command fits which task, what a run writes and how long it takes, what the exit codes and the report mean, and what to trust without re-checking the bytecode.
compatibility: Requires decaf on PATH (uv tool install git+https://github.com/vcth4nh/decaf) and a JDK on PATH (Java 17+ for the default engine, javac for the compile check). Network access to Maven Central and GitHub is optional - it gives original sources and the first engine download.
---

# decaf

All-in-one Java decompiler CLI. `decaf INPUT -o OUT` (short for `decaf run INPUT -o OUT`)
turns every archive and class file under INPUT into a source tree under OUT, plus
`OUT/decaf-report.json`. `decaf run --help` lists the flags; this skill tells you what to
expect.

Typical flow:

1. `decaf INPUT -o out` (add `--merge` for one tree to grep; it loses the archive of each
   file, see below).
2. Read the exit code and the ending block (both below).
3. Open `out/<archive>/<package path>/<Class>.java`. A war's own classes sit at
   `out/app.war/WEB-INF/classes/<package path>/<Class>.java`, a jar inside it at
   `out/app.war/WEB-INF/lib/dep.jar/<package path>/<Class>.java`; with `--merge`, everything
   is under `out/src/<package path>/`. When you do not know the archive of a class, use
   `find out -name '<Class>.java'`.
4. On `✗`, `!`, or exit 1: `decaf report out --problems`.
5. For the detail of one artifact (engine attempts, check counts, flagged and noted files):
   `decaf report out --artifact '*NAME*'`, with NAME as the ending prints it.

## What decaf does

- **Sources first.** A jar with Maven coordinates gets the real `-sources.jar` from Maven
  Central instead of a decompile. decaf proves the match: the SHA-1 of the jar must equal
  the SHA-1 of the published jar. A patched, shaded, or private jar never matches and is
  decompiled. The lookup sends the SHA-1 of each jar without `pom.properties` to Maven
  Central; `--no-maven` makes no lookup and decompiles everything.
- **Five engines, automatic fallback.** Vineflower, then CFR, Fernflower, Procyon, JD. An
  engine that crashes, times out, misses classes, or writes a stub hands the archive or the
  class to the next one. `--engine cfr` only changes the first engine; the fallback stays on
  unless you add `--no-fallback`.
- **Compile check.** decaf compiles each decompiled file with `javac`, one file at a time,
  with the input archives on the classpath, and writes no class files. A file with a parse,
  name, or flow error goes to the next engine; the first version without such errors stays.
  Maven sources are not checked.
- **Inputs.** One archive, one `.class` file, or a folder, scanned recursively. Archives
  inside archives are unpacked one level deep by default: the jars under `WEB-INF/lib` of a
  war are level 1, the jars of a war inside an `.ear` are level 2 (`--max-depth 2`). A deeper
  archive is copied through as a file and listed as `skipped` in the report.

## Pick the command

| Task | Command | The sources land in |
|---|---|---|
| Read one jar | `decaf lib.jar -o out` | `out/lib.jar/<package path>/<Class>.java` |
| Grep or open an app with its libraries as one tree (loses the archive of each file) | `decaf app.war -o out --merge` | `out/src/<package path>/<Class>.java` |
| A folder of jars, without the resource files | `decaf ./libs -o out --no-resource` | `out/<path relative to libs>/<archive>/...` |
| Private or vendor jar, offline, or the deployed bytecode rather than upstream sources | `decaf lib.jar -o out --no-maven` | the mirror paths above |
| Faster and rougher | add `--no-check`: the run takes about half the time, and a file with decompiler errors is not retried, so such errors can stay in the output | |
| Shared machine | add `--cpus 4 --memory 8g` | |
| One engine with its own options | `decaf lib.jar -o out --engine cfr --no-fallback -- --renameillegalidents true` | |
| Script around it | `decaf lib.jar -o out --format json > report.json`, or `--format ndjson` | |
| Prepare an offline machine | `decaf engines fetch` once, with network | |

- The default output is `./decaf-out`, relative to the current folder. Pass `-o`.
- A non-empty OUT stops the run with exit 2 and `error: output out is not empty (use --force
  to write anyway)`. Use a new folder, or `--force`, which writes into the folder as it is.
- The mirror layout (default) copies every resource byte-for-byte (manifests, `web.xml`,
  licenses, `pom.xml`) and turns a nested archive into a folder named after it. A failed
  artifact gets no folder. `--no-resource` keeps the layout and drops the resources.
- `--merge` writes `.java` and `.kt` files only, under `out/src/`. You lose which archive a
  file came from; duplicate classes are deduped, the first in input order wins, and the
  report counts the conflicts (`collisions`); container prefixes such as `WEB-INF/classes/`
  are stripped. For a review that must name the archive, keep the mirror and use
  `grep -r --include='*.java' --include='*.kt'`.
- Options after `--` go to the first engine unchanged; without `--no-fallback`, decaf stops
  with exit 2. Each engine has its own option names.
- In the default output mode, everything (progress, ending, errors) goes to stdout and
  stderr stays empty. With `--format json` or `ndjson`, stdout carries only the JSON and the
  narration moves to stderr.

## What to expect from a run

- **Time.** One small jar: seconds. An app of 80 archives (jenkins.war): 7 minutes by
  default, 23 minutes with `--no-maven` (everything decompiled and checked), 7 minutes with
  `--no-maven --no-check`. Run a big job in the background and read the report at the end.
  `--timeout N` limits each engine attempt and each check on one archive (default 600 s).
- **The first run** downloads the five engines (sha256-verified) into the user cache dir.
  Later runs need the network only for Maven lookups. A machine that never has network
  needs the engines cache from a machine that ran `decaf engines fetch`.
- **Writes outside OUT, always.** The user cache dir (`~/.cache/decaf` or
  `$XDG_CACHE_HOME/decaf` on Linux, `~/Library/Caches/decaf` on macOS,
  `%LOCALAPPDATA%\decaf\decaf\Cache` on Windows; `decaf engines list` prints its `engines`
  subfolder) holds the engines, the downloaded sources jars, and the lookup verdicts: a
  SHA-1 match is kept forever, a "no sources" answer for a week. Archives are unpacked in a
  `decaf-*` folder under the system temp dir (`TMPDIR`) and removed at the end; the JVMs
  write `hsperfdata_<user>` in that temp dir. No flag turns these writes off; `--no-maven`
  only adds no verdicts and no sources jars. `decaf cache clean` wipes the Maven part of
  the cache; `--fresh-maven` ignores the verdicts once.
- **CPU and memory.** By default decaf uses all cores but one and gives the engine JVMs 80%
  of the total RAM (or of a cgroup limit) as one heap budget; `--memory 8g` sets that
  budget. The line `engine memory: up to 25 GB (4 jobs, 5.8 GB heap each; a whale gets 7.8
  GB)` is a budget, not a reservation; a whale is an archive with 3000 or more classes. The
  ending line `Memory  budget … · largest peak 0.2 GB` shows the real use.
- **The ending block.** One line per artifact: `✓` complete, `!` partial, `✗` failed, with
  the source of its code: `(maven sources, org.jsoup:jsoup:1.17.2, cached)` or `(vineflower,
  4 classes)`. `cached` appears only on a Maven line and means the sources jar came from the
  cache, not the network; a cached "no sources" verdict shows only in the report, as
  `sources_miss=cached verdict: …`. Then `Artifacts N processed · N complete`, plus
  `· N partial` and `· N failed` when they are not zero, `Sources N Maven · N decompiled ·
  N extracted` (extracted: an input archive that already held source files), the failures
  grouped by cause, and the paths of OUT and the report. The ending shows no compile-check
  counts; `decaf report out --artifact` and the JSON do. When stdout is not a terminal,
  lines wrap at 80 columns, and a long path can break in two: use the path you gave `-o`.
- **Counts.** `N classes` counts top-level classes. Inner and anonymous classes (`Outer$1`,
  `Outer$Config`) are nested inside `Outer.java`; there is no `Outer$1.java`. `java_files`
  also counts `package-info.java` files, so it can exceed `classes`. For Maven sources,
  `classes` is 0. A jar built without debug info gives names such as `var1`; that is the
  bytecode, not a defect.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | no artifact failed; `!` partial artifacts are possible | read the ending for `!`, then the sources |
| 1 | at least one artifact failed (`✗`); the other artifacts are complete or partial, and their sources are usable | `decaf report out --problems`, then go on with the task |
| 2 | usage or environment error: bad flag, non-empty OUT, Java too old | read the one `error:` line |
| 130 | interrupted (Ctrl-C, SIGTERM, SIGHUP); the report is still written with status `interrupted` | re-run with `--force` or into a new folder |

## Read the result

- `decaf report out` reprints the ending after a header line (`out/decaf-report.json ·
  schema 1 · decaf 1.11.0 · <time>`). `--problems` lists the failed artifacts grouped by
  cause, then the partial ones. `--artifact '*NAME*'` shows each matching artifact in full:
  how it resolved, each engine attempt, the check counts, and the flagged, noted, and
  unchecked files, each with its error count and first javac error (`13:` is the line); the
  JSON holds every message of a file. The glob matches the whole artifact name, which is
  `app.war!/WEB-INF/lib/NAME` for a nested jar, so start the pattern with `*`; `no artifacts
  match` exits 0. `--problems` and `--network-fallbacks` print only the header line when
  the list is empty; that means none.
- **Failure causes** (`✗`): `unreadable archive` (not a zip, or truncated: get a new copy,
  decaf cannot help), `all engines failed` (read the attempts in `--artifact`: `timed_out`
  asks for a higher `--timeout`, `out_of_memory` for a higher `--memory`), `case collision`
  (the output tree ignores case; use a case-sensitive volume for OUT and `TMPDIR`),
  `interrupted`.
- **`✓` reports completeness, not the check.** A `✓` artifact has every class and no
  flagged file; the check result is in the report:
  - **flagged**: a parse, name, or flow error that no engine fixed. The file of the first
    engine stays and the artifact is partial (`!`). Read a flagged file with care; verify
    its logic against the bytecode (`javap -c -p` on the class in the original archive) when
    a decision depends on it.
  - **noted**: type errors only, for example a lost generic argument (`List var2` for
    `List<String>`, which also gives errors at the uses of `var2`), or a checked exception
    that javac rejects in source. The logic is intact and the artifact stays `✓`. Classes
    from Kotlin, Scala, Groovy, and Lombok land here often.
  - **unchecked**: the file refers to a class outside the input, so javac gave no verdict.
    No defect is known. A single library jar without its dependencies is mostly unchecked;
    put its dependencies in the same input folder to get a fuller check. A war or a fat jar
    is mostly checked.
  - **stubbed**: an engine wrote a placeholder in place of a method body. decaf retries the
    class with the next engine and lists the stubs that remain (`stubs[]`); they make the
    artifact partial.
- **Maven sources or decompiled?** The artifact line says it. In the JSON, `method` is
  `maven`, `extracted`, or the engine name, and `gav`, `resolved_by`, and `sources_miss`
  say how the lookup went. Vineflower files carry no header comment; CFR files start with
  `Decompiled with CFR`. Maven sources are the upstream sources of that exact version, with
  javadoc and real names.
- **Multi-release jars.** The classes under `META-INF/versions/N` are variants for newer
  Javas. A sources jar can lack them, the default chain writes only the base version, the
  mirror does not carry their `.class` files, and `missing_classes` does not count them. No
  engine writes `module-info`. When completeness matters, run `unzip -l lib.jar | grep
  META-INF/versions`; `--engine cfr --no-maven` (or `--engine jd`) writes the variants to
  `out/lib.jar/META-INF/versions/N/<package path>/<Class>.java`.
- **Trust.** A `✓` artifact needs no javap, recompile, or hash check from you: the check
  compiled every checked file, and the Maven match compared the hash. An unchecked file got
  no verdict; read it like any decompiled source and verify a line against the bytecode
  only when a decision depends on it. Spend verification on flagged files and partial
  artifacts; a failed artifact has no sources to verify. The output holds no class files:
  for `javap` or a bytecode grep, unpack the original archive. One thing a source grep
  cannot give you: an exhaustive search for the users of an API, because a chained call such
  as `x.getDigest().digest(b)` has no type name in the source; grep the class files too
  (`grep -rl 'java/security/MessageDigest'` over the unpacked jar).

## Report fields for scripts

`decaf-report.json` (also `--format json` on stdout) has `status` (`completed`,
`completed_with_failures`, `interrupted`, `aborted` when an error stopped the run),
`totals` (`artifacts`, `ok`, `partial`, `failed`, `skipped`, `maven_sources`, `decompiled`,
`extracted`, `java_files`, `collisions`, `flagged_files`, `noted_files`, `unchecked_files`,
`network_misses`; `ok` includes the partial ones), and `artifacts[]` with `rel`, `kind`,
`outcome` (`ok`, `failed`, `skipped`; partial is `ok` with `missing_classes` > 0, `stubs[]`,
`flagged[]`, or a `sources_miss` that starts with `network:`, which is a network miss:
the lookup failed and the jar was decompiled), `method`, `gav`, `repo`, `resolved_by`,
`sources_miss`, `sources_cached`, `classes`, `java_files`, `missing_classes`, `collisions`,
`failure`, `attempts[]` (`engine`, `level` = `archive` or `class`, `returncode`,
`timed_out`, `out_of_memory`, `stderr_tail`, `peak_rss_mb`), `check` (`checked`,
`flagged`, `noted`, `unchecked`, `unchecked_reasons`), and the lists `flagged[]`,
`noted[]`, `unchecked[]` (`path` under the artifact folder, `engine`, `first`,
`messages`). `--format ndjson` streams one object per line with `event` = `scan`,
`artifact`, `warning`, and one final `summary`.

## Common mistakes

- Reading `decaf --help`, `decaf run --help`, `decaf engines list`, and `unzip -l` before
  the first run. Run `decaf INPUT -o out` first; the ending tells you what happened.
- Searching `out/` with `find` for a class whose archive you know. It is at
  `out/<archive>/<package path>/<Class>.java` (mirror) or `out/src/<package path>/<Class>.java`
  (merge).
- Treating exit 1 as a failed run, or a `!` artifact as unusable.
- Verifying a `✓` artifact again with javap, a recompile, or a SHA-1 comparison.
- Expecting `Outer$1.java`, or expecting `N classes` to equal the number of `.class` files.
- `--artifact 'NAME*'` for a jar inside a war: the name starts with the war, use `'*NAME*'`.
- Editing the output tree by hand: a later `--force` run rewrites it.
- Expecting `--engine cfr` alone to give pure CFR output, or passing engine options without
  `--no-fallback`.
