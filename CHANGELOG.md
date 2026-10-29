# Changelog

All notable changes to decaf are documented here.

## [Unreleased]

### Changed

- The live display shows the compile check of each engine output, for
  example `vineflower · check 1,234 files` (#101). Before, it showed the last
  engine event while the check ran.
- `decaf engines clean` also removes the compiled checker. With `--stale`,
  it keeps the checker of the current decaf version (#101).
- The `--timeout` help and the README say that the value also limits each
  compile check (#101).
- A class from Groovy gets no more retries when two engines in a row flag
  its file (#101). No engine writes compilable Java for the call-site and
  meta-class code of groovyc, and each engine gives a different first error
  for it, so the stop rule of #108 never applied. On groovy-all-2.4.21, 111
  such classes got four retries each, and CFR and Procyon start one JVM for
  each class: the retries took 85% of a 496 s run. Now the Fernflower,
  Procyon, and JD retries get 11, 7, and 3 classes instead of 137, 113, and
  113, and they take 27 s instead of 261 s. The flagged files rose from 309
  to 336: for 27 classes, the file of Fernflower passed as `noted`, and now
  the file of Vineflower stays.
- CFR and Procyon decompile the classes of a folder in one JVM, not in one
  JVM for each class (#101). They take class files as positional arguments.
  When that JVM stops with an error, out of memory, or at the timeout, each
  class of its group gets a JVM of its own, as before. A group holds the
  class paths that fit one command line of Windows. The class retries of the
  compile check and a run with `--engine cfr` or `--engine procyon` on a
  class folder use this path. On groovy-all-2.4.21, the CFR retry of 445
  classes took 43 s instead of 206 s, and the run 177 s instead of 331 s,
  with the same files and flags. The heap estimate of a class folder now
  uses the whole folder for every engine.

### Fixed

- A batch of jars lost the classes that have `$` in the simple name, or gave
  them to another jar of the batch, and the report still showed ✓ with
  `missing_classes: 0` (#106). For guice-5.1.0, a batch wrote 243 of its 275
  files. A class is now an inner class only when the class file of its outer
  class exists. Thus decaf now counts and retries classes such as
  `$Gson$Types` and inner classes without their outer class. A jar with
  such an inner class no longer shares a batch with a jar that has its outer
  class, because the engine then puts both classes into one file. CFR and
  Procyon now also decompile these classes in class folders.
- An error such as a broken pipe stopped a run, but the report said
  `completed` and the engine JVMs continued (#112). Now decaf kills the
  JVMs and writes the report with the new status `aborted` before it stops.
  The report has a row for each job that ended before the stop, also for
  each jar of a batch. A full disk at the start of a batch now fails only
  the jars of that batch. SIGTERM and SIGHUP now end a run like Ctrl-C:
  decaf writes the report with the status `interrupted` and deletes its
  temp folder. A signal that the parent process ignores, for example SIGHUP
  under `nohup`, stays ignored. An interrupt or an error before the first
  job now also writes the report, for example in the javac build of the
  compile check. This build now leaves no temp folder in the cache. After
  Ctrl-C, decaf starts no new network request, and a sources download
  stops. After that, no file of a killed engine and no Maven sources go to
  the output. The scheduler now waits with a timeout, so Ctrl-C on Windows
  does not wait for the next engine to finish.
- A status line that stdout could not encode stopped the run, for example
  ✓ on a redirected Windows stdout, or a file name that is not UTF-8 on
  Linux (#109). Now such a character prints as an escape (`\udcff`), and
  the marks fall back to `+` and `x`.
- Markup in a name or an error text (for example `[/x]`) could stop decaf
  with a traceback in a usage error, in the heartbeat line, and in
  `decaf report --network-fallbacks`. Now these texts are escaped too.
- In the `json` and `ndjson` formats, stderr now shows the same notes as the
  human format: the found artifacts, the engine memory, and the engine
  downloads. An error with exit code 2 now comes before the first ndjson
  line, so stdout stays empty. Each ndjson line is now one write, so the
  lines of two threads cannot mix.
- Without a terminal, long lines were wrapped at 80 columns, so a log file
  split long paths. Now each line stays whole.
- `decaf report` showed a traceback for a file that is not a JSON object,
  not UTF-8, or nested too deep. Now it shows an error and exits with code 2.
  A value of a wrong type, for example a text in a number field, and an
  engine attempt with a missing key also gave a traceback. Now such a value
  gets its default.
- One damaged entry stopped more than that entry (#109). An entry name that
  is not UTF-8 stopped the whole scan; now its archive fails as
  `unreadable archive`. A damaged resource or class entry failed its
  artifact, and a damaged library failed its war with all the other
  libraries. Now decaf skips only the damaged entry: a damaged library fails
  alone, a damaged class stays a missing class, and a damaged source entry
  is a missing file. A Maven sources jar with a damaged entry now goes to
  the engines.
- An error of the Maven sources lookup failed the artifact, and no engine
  ran (#111). Examples are a read-only or full sources cache, and a damaged
  `pom.properties` entry. Now the error is the sources miss
  (`sources lookup failed: ...`), and the engines decompile. A downloaded
  sources jar now goes to the cache only when decaf can read each of its
  entries. Such a download, and a download that is not a zip (for example
  an HTML page with HTTP 200), gave the miss `no -sources.jar in any repo`
  and a verdict for a week. Now the miss names the repository and the
  damaged entry, and decaf keeps no verdict. A cached sources jar with a
  damaged entry is deleted, so the next run downloads it again.
- One weak signal could pick the wrong source for an archive (#110). A jar
  with one `pom.properties` got the sources of that artifact with no check,
  so a fat, shaded, or patched jar could get the wrong sources and the
  report said ok. Now decaf uses the `pom.properties` only when a
  repository's `.jar.sha1` matches the jar, and the verdict cache keeps the
  result. An archive with source entries and no class entries (for example
  a war with one stray `.java`) lost its nested jars; now decaf decompiles
  them too. HTTP 408 is now retried, and an answer other than 200, 404, or
  410 (for example 400 or 407) no longer counts as missing sources. A
  cached "no sources" verdict now counts only for the same file-name hints,
  so a renamed jar is looked up again. Each set of hints keeps its own
  verdict, so one jar under two names is not looked up again in each run.
- The compile check lost results and hid its failures (#108). The checker
  wrote the results of a job only at its end, so a kill at the timeout
  made all its files `unchecked: timeout`, also the files with flags. Now
  each result is written when its file is done. A file that the check could
  not finish now shows its cause in `first` (for example the javac
  exception, or the exception that stopped the checker), and the ending
  counts such files. An unreadable jar on the classpath now costs only its
  own classes. A class folder such as `build/classes/java/main` now goes on
  the classpath at its package root, so its classes find each other. The
  checker classes now run from a jar, so the CDS archive of the checker
  works, and a property dump of `java` that is not UTF-8 no longer stops
  the run.
- The compile check hid some engine errors and flagged some correct files
  (#108). A parse error (for example CFR's `** GOTO`) now flags the file
  also next to an absent class, and the first error of such a file is an
  engine error, not the absent class. An unqualified name now counts as
  absent only when an import, an absent supertype, or an absent class of the
  same package can supply it. The absent supertype can also be the type of
  an anonymous class, a qualified name such as `org.absent.Base`, or a
  nested type such as `Outer.Inner`. A class of the same package counts only
  when the class file names it, for example in a split package or in a
  `-tests.jar` without its main jar. A missing member of another class
  counts as absent only when that class has two versions in the classpath. A
  file gets a second try with javac `--release` from its class-file version
  (at least 8) when the JDK version can cause one of its errors. Thus old
  code may use JDK APIs that newer Java removed, such as `javax.xml.bind`,
  and names that newer Java restricts, such as `var`. Four more javac flow
  errors of engines now get a retry.
- The compile check flagged correct Kotlin, Groovy, and Scala code, and each
  flagged class went through all the engines of the chain (#108). The
  bytecode of these languages can throw undeclared checked exceptions and
  can have two methods that differ only in their return type, but javac
  rejects both in a source file. For a class with the Kotlin or Scala
  annotation, the Groovy interface `GroovyObject`, or the source file name
  of such a language in the class file (for example a Groovy script), the
  check now notes these two errors and does not flag them. Other classes can
  have such bytecode too, for example a Lombok class with `@SneakyThrows` or
  a class that a tool generated. Their retries now stop when two engines in
  a row give the same first error of these two kinds. An undeclared
  `Throwable` or `Exception` does not stop the retries, because Vineflower
  and CFR drop the cast of `throw (T) t`, but JD keeps it.
- decaf read and wrote the config file with the codec of the locale, for
  example cp1252 on Windows. Now it uses UTF-8, as TOML specifies, and a
  config that is not UTF-8 gives an error, not a traceback. Maven Central
  under its second name (`repo.maven.apache.org`) counted as one more
  repository, so decaf asked Central two times for each artifact. The README
  now also gives the config path for macOS and Windows (#115).
- A class retry could write the wrong copy of a class (#107). The retry of
  a class decompiles its inner classes again. The copy of such an inner
  class could then replace the file of an earlier engine, and in merge mode
  this gave a collision with itself. Now the file of the earlier engine
  stays. A retry tree held a class and its `META-INF/versions/N` twin
  together. Then Fernflower wrote the versioned body as the base class, and
  Vineflower wrote no base class. Now a retry tree holds one version of
  each class: the base class, or the class of the lowest version folder
  when no base class exists. With `--merge`, a versioned twin that CFR or
  JD writes replaced its base class. Now the base class wins. The README
  now says which engines write the versioned classes.
- A Fernflower run that the timeout stopped ended the fallback chain
  (#113). The cut-off archive of sources gave an error in the unpack, so no
  other engine ran. Now decaf deletes such an archive, and the next engine
  runs. A full disk in the unpack still fails the artifact. `--timeout`
  accepted 0, negative values, NaN, infinity, and values
  above the longest wait of the platform. These values failed each artifact,
  wrote a report that is not valid JSON, gave a shorter limit, or let the
  JVMs run with no limit. Now `--timeout` must be a number of seconds above
  0 and at most 4294967 (about 49 days). When the wait for a JVM fails,
  decaf now kills the JVM. CFR and Procyon run one JVM for each class of a
  class folder, and each JVM got the full timeout. Now the timeout is for
  the whole folder. An input path that is not ASCII could fail on all
  engines, because Java decodes a path with the codec of the locale: UTF-8,
  or ASCII under `LC_ALL=C`. Examples are a Latin-1 name on Linux, and
  `café.jar` under `LC_ALL=C`. The compile check could not read such a jar
  either, also on the class path of the other inputs. Now such an input goes
  to the engines and to the check through a link in the temp folder. The
  link keeps an ASCII file name, so the Maven lookup keeps the hints of the
  file name.
- The engine setup failed with no message, or with a traceback (#113). A
  fallback engine that failed to download left the chain with no warning.
  An error of the engine cache, for example a read-only cache, gave a
  traceback and exit code 1. Now the warning `fallback chain: skipped ...`
  gives the cause for a fallback engine, and for the primary engine the run
  stops with the cause (exit code 2). `decaf engines fetch`, `list`, and
  `update` now also give the cause. Two runs that downloaded one engine at
  the same time used the same temp files; now each download has its own,
  also in `decaf engines update`.
- Some Java versions stopped each run (#113). An early-access Java such as
  `26-ea` counted as Java 0. Now decaf reads such versions. A `java -version`
  that gives no answer in 30 s, or no version that decaf can read, now stops
  the run with a clear error. A pin can need a newer Java than the built-in
  spec says: for example, the newest Fernflower needs Java 25. Then
  `engines list` showed `ok`, and the engine failed on each artifact. Now
  `engines list` and each run read the Java that an engine jar needs from
  its class files, so a fallback engine that needs a newer Java is skipped.
  `engines update` now warns when the java on PATH is too old for a new pin.
  When the Java is too old for the primary engine, the error now gives a
  hint. The README no longer gives the Java version of each engine, because
  these versions change; `decaf engines list` shows them.
- The engine runs had smaller faults (#113). With a read-only engine cache,
  each JVM exited with code 1 after its work, because it could not write its
  CDS archive. Thus each batch ran two times. Now decaf turns CDS off when it
  cannot write to the cache. A pinned engine got the CDS archive name of the
  built-in version, and `engines clean --stale` deleted the CDS archives of
  the active pins and of the compile check. Now the archive name has the
  version of the pin, and `--stale` keeps these archives. A symlink or a
  Windows junction in the engine cache stopped `engines clean` with a
  traceback. Now decaf deletes the link and keeps its target. On a small heap, Procyon prints only
  `Java heap space`, so the report did not count the out-of-heap failure.
  Now decaf also finds this text and `GC overhead limit exceeded`. On Linux,
  the peak memory of a JVM that stopped at start was the memory of decaf,
  because a forked child keeps the pages of decaf in `ru_maxrss`. Now a peak
  that is not more than the memory of decaf plus 16 MB is unknown. This rule
  also hides the peak of some small JVMs that ran normally.
- The CPU budget and the scheduler ignored limits of the host (#114). The
  default budget counted all the cores of the host, also when decaf could
  use only some of them, for example under `taskset` or in a container with
  a cpuset. Then each JVM got more threads than CPUs. Now the default counts
  the CPUs of decaf's affinity. While a whale waited for its second free
  slot, each freed slot went to a small jar, so the whale could wait until
  no small jars were left. Now the whale gets each slot that a small jar
  frees. A small jar still takes a free slot when only whales run, because
  the waiting whale needs the end of one of them.
- The memory notes had gaps (#114). A jar under 3000 classes, which goes
  into a batch, got no heap estimate. CFR and Procyon run one JVM for each
  class of a class folder, but the estimate used the size of the whole
  folder, so it gave a false warning. Now each jar gets the estimate, and
  for one JVM for each class, the estimate uses the largest class. One batch
  JVM that ran out of heap counted as one attempt for each of its jars; now
  it counts once. `decaf report --memory` also listed a batch JVM one time
  for each of its jars. Now it lists the JVM once, with its first jar and
  the number of its other jars. A very large `--memory` value gave a traceback; now it
  gives an error.
- A usage error of a command line without a command name, such as
  `decaf -o out`, named the first token as the command:
  `Usage: decaf -o [OPTIONS]` and `Try 'decaf -o -h' for help`. Now the
  text names `decaf run`.
- With `--format ndjson`, when the reader of the output stopped (for
  example `| head`), a Maven warning of a fetch thread got the broken pipe,
  and the error became the sources miss of its artifact. Now the fetch
  thread drops that line, and its lookup continues. The main thread stops
  the run at its next line, as before.
- An archive with two entries of one name, for example two
  `WEB-INF/lib/a.jar` from Ant `duplicate="add"`, gave two equal rows for
  one file. The scan line counted two nested archives, and decaf
  decompiled the file two times. A damaged first entry
  also failed the name when the last entry was good. Now the name gets one
  row for the last entry, which Java reads, and a warning names the entry
  when the entries are not the same. The last entry now also decides for a
  source file of a sources jar: a damaged earlier entry is no missing file,
  and decaf uses a Maven sources jar with such an entry.
- On a case-insensitive output, decaf found the names that share one file
  with `str.casefold`. APFS and ext4 casefold also ignore the Unicode normal
  form, so decaf missed two names such as `é` (NFC) and `e` + U+0301 (NFD),
  and one file overwrote the other. Now the key is NFD, then full case
  folding, and it joins at least the names that NTFS, APFS, or ext4 casefold
  join. The key also joins some names that NTFS keeps apart, for example
  `ß` and `ss`: decaf refuses them, but no file overwrites another.
- The temp folder of a run grew until the run ended, because decaf deleted
  its temp files only at the end (#97). On a tmpfs, these files used RAM
  next to the engine JVMs, and a large input could fill the disk. Now decaf
  deletes each temp folder with one owner after its last use: the output of
  each engine run and each class retry, the retry tree, the held files, an
  extracted sources jar, the copy of a class folder, the work files of each
  checker JVM, and the folders of a batch. The nested archives and the
  extracted classes folder of a container go when each row below the
  container is final, because the compile check of a member reads the
  archives of its family. Thus a single fat jar keeps its nested archives
  until its last member is done.
- decaf read the depth and the container of an archive from its row name.
  But a folder name can end with "!" (`lib!/x.jar`), and an entry name in
  an archive can hold "!/". Such an archive counted as one level deeper:
  its nested archives became skipped rows one level early, and its compile
  check did not get the other inputs or the archives of its family. A
  skipped archive with "!/" in its entry name also got no copy in the
  output. Now each archive records its container and its depth.
- The mirror output dropped each "!" of a row name. Thus `in/lib!/x.jar`
  and `in/lib/x.jar` wrote into one output folder: their sources mixed,
  and a resource file of one replaced the file of the other. Now a
  top-level archive keeps its input path as its folder, and a nested
  archive gets the folder of its container plus its entry name. A name
  keeps its "!".
- After Ctrl-C, the files of a stopped compile check had the reason
  "javac crash". Now their reason is "interrupted", and the end of the run
  counts them as files that the check did not finish (#101).
- Two workers could extract the classes folder of one archive two times.
  Now the second worker waits for the first one (#101).
- For old bytecode, Vineflower writes some inner classes to files of their
  own, for example `XStream$1.java`. Such a file names `this$0`, `val$x`,
  `access$300()`, or `class$()`: names that javac makes for a nested class.
  Thus it did not compile alone, the check flagged it, and the jar stayed
  partial (#101). Now these errors of such a file are noted, and its other
  errors still flag it. On commons-collections-3.2.2, xpp3-1.1.4c, and
  xstream-1.4.21, the flagged files fell from 70 to 11, and xpp3 is not
  partial now.
- A flagged file of such an inner class now gets a retry of its outer class
  (#101). Until the retry ends, decaf holds the files of the outer class and
  of its inner classes from that engine. The next engine can nest the inner
  classes in the file of the outer class. Its files replace the held files
  only as a whole. The file of the outer class must be one of them, and each
  file must pass the check. Before, when the engine wrote no file of the
  outer class, the retry of the outer class gave the inner class two times.
  On the three jars, the flagged files fell from 11 to 0, and no jar is
  partial now.

### Security

- The compile check gave the jars of the input to javac as class path
  entries, so javac followed their `Class-Path` manifests, also to files
  outside the input. Now each jar goes in as the root of its zip file
  system, and javac reads no manifest of an input jar.
- Archive names and engine output could put terminal control codes on the
  screen, because decaf escaped only the rich markup. Now each control
  character (C0, DEL, and C1) prints as `\xNN`. Engine output keeps its
  tabs and line ends, also the CRLF of a Windows JVM. `decaf report` printed
  the number fields of a report file with no check. Now each value of the
  report file must have the type of its field.
- Maven coordinates are now checked before decaf uses them in a cache file
  name or a URL. Many coordinates come from the input archive
  (`pom.properties`, the manifest, the file name). Before, such text could
  make decaf write a sources jar outside its cache. Now a group, artifact, or
  version with a character that Maven coordinates do not use (for example a
  path separator or a control character) is not used, and the lookup goes on
  as for an archive without these coordinates.
- Repository credentials (`user:pass@` in a repository URL) still showed in
  one text: the miss of an artifact that a repository verified but that has
  no sources jar (#115). This text goes into the report, the `-v` output, and
  the ndjson events. The error for a bad repository URL showed them too. Now
  decaf removes the credentials from these texts. decaf now also refuses a
  repository URL in which the user part is not clear: a URL that httpx
  cannot read, a URL with no host, or a URL with an `@` after its host. The
  error shows no text up to the last `@`. A pin write of
  `decaf engines update` gave the config file the mode of the umask, so a
  config with credentials could become readable to other users. The write
  also replaced a symlink with a file. Now the config file keeps its mode,
  and decaf writes through a symlink.
- A file name in the input folder could act as an engine option, because
  decaf gave the engines relative paths. Now each engine gets absolute
  paths.
- decaf read some entries of an input archive into its memory with no size
  limit: the class files that the compile check reads, and the
  `pom.properties` and the manifest. Thus one small archive could fill the
  memory. Now decaf reads no class file above 16 MiB and no such text entry
  above 1 MiB.

## [1.11.0] - 2026-10-21

### Added

- Engine memory is measured and budgeted (#98). Each engine attempt records
  its peak memory and its heap limit in the report; the ending shows the
  largest peak and counts the attempts that ran out of heap, with a hint; and
  `decaf report --memory` lists the ten largest peaks. Before the decompile
  starts, decaf prints how the budget is split and warns when an artifact
  probably needs more heap than its share. `--memory SIZE` sets the budget
  for all engine JVMs together.
- Compile check of decompiled sources (#99). After an engine writes its
  output, decaf compiles each `.java` file alone with `javac`, with the
  classes of the input on the classpath. A class with a parse, name, or flow
  error (for example a variable that the engine renamed in its uses but not
  in its declaration) goes to the next engine. A class that stays wrong is
  listed under `flagged`, and its artifact is partial. A file that refers to
  a class outside the input is not checked, so an absent dependency never
  causes a false flag. The check and the stub scan (#96) share one hold
  step: a retry file replaces a held file only when it has no stub and
  passes the check. The report gets `check` counts for each artifact,
  `attempts[].flagged`, and the totals `checked_files`, `flagged_files`,
  `noted_files`, and `unchecked_files`. For each artifact, it also lists the
  `flagged` and `noted` files with their javac errors (at most 100 for each
  file), and the `unchecked` files with their reasons; `decaf report
  --artifact` shows these lists. When files are flagged, the end of the run
  points to `decaf report OUTPUT --problems`. The checker JVM gets the same
  heap as the engine JVM of its job (#98). The check needs a JDK and is on by
  default. `--no-check` turns it off.

### Changed

- Engine JVMs now get `-Xmx` from a budget of 80% of the RAM by default (or
  of a lower memory limit of decaf's cgroup or a parent cgroup), split in
  equal shares (a whale gets two, and `-j` is lowered when a share
  would fall below 1 GiB). Each heap is its share less 300 MB plus 2% of the
  share for the JVM itself. With the default budget, no JVM gets more heap
  than the JVM default (25% of the RAM). Before, each JVM could take 25% of
  the RAM, so four jobs could fill it. An `-Xms` in `JAVA_TOOL_OPTIONS` or
  `JDK_JAVA_OPTIONS` above a heap is lowered to it, and under `ulimit -v` each
  heap is at most half the limit, so the JVMs still start. `--memory 0`
  restores the JVM default (#98).

### Fixed

- Method stubs no longer pass as complete output. When an engine cannot
  decompile a method, it writes a comment in place of the body
  (`// $VF: Couldn't be decompiled` and the like) and still writes the class
  file, so the artifact got a green ✓ with `missing_classes: 0`. decaf now
  finds the stub markers of all five engines, retries the stubbed classes
  with the next engines of the chain, and writes a retry file only when it
  has no stub. A class that stays stubbed keeps the first engine's file, is
  listed under the new `stubs` report key (path, engine, method count,
  exception names), and makes the artifact partial (`!`). The ending counts
  the stubbed classes, and `decaf report --artifact` lists them (#96).

## [1.10.0] - 2026-09-11

### Added

- Windows: decaf sets the NTFS per-directory case-sensitivity flag on its
  output and temp folders (needs WSL installed and a local NTFS volume), so
  archives whose classes differ only by case decompile completely (#93).

### Fixed

- Case-insensitive output trees (default macOS, Windows without the flag,
  FAT/exFAT) silently collapsed classes that differ only by case: the file kept
  the first name and the last content, and the missing-class retry then
  reported a clean run. Such artifacts are now refused before any work with a
  `case collision` failure and a one-line hint; nested archives whose names
  differ only by case are refused the same way instead of extracting onto one
  file; small jars whose classes differ only by case never share a batch; and
  merge mode records cross-jar case-only clashes as collisions (`dropped_path`)
  instead of overwriting (#93).

## [1.9.1] - 2026-07-27

### Fixed

- Artifact names, GAVs, and failure text with `[bracketed]` content render
  verbatim in status lines and `decaf report --artifact` detail instead of
  being swallowed or crashing rendering; `:id:`-style artifactIds no longer
  turn into emoji (#89).
- `decaf report` tolerates reports with `null`/mistyped `settings`, `totals`,
  `artifacts`, or `duration_seconds` instead of crashing (#89).
- `decaf report --artifact` with overlapping globs prints each matching
  artifact once (#89).
- HTTP 401/403 during a sources download or a candidate probe no longer
  records a 7-day negative verdict: the miss is tagged network-degraded
  (`!` marker) and resolution is re-attempted next run — bad credentials
  can't poison the verdict cache through either path (#89).
- Uppercase `.JAVA`/`.KT` entries in sources jars are now counted and
  written by both output writers (extraction already kept them) (#89).
- Decode and redirect anomalies during candidate probes taint the miss like
  other network failures instead of being silently swallowed, so they no
  longer feed negative verdicts (#89).

## [1.9.0] - 2026-07-27

### Added

- `decaf report` — offline inspector over `decaf-report.json`: default
  outcome view, `--problems`, `--artifact GLOB`, `--network-fallbacks`.
  Reads reports from any decaf version. A folder literally named `report`
  now needs `decaf ./report` (#84).
- Report schema v1: additive `schema_version`, `decaf_version`, `status`,
  and `started_at`/`ended_at` timestamps; the report file is written
  atomically (#84).
- `--format json|ndjson`: machine data on stdout (the final report, or
  streamed per-artifact events), human narration on stderr; exit codes
  unchanged (#84).
- Non-TTY runs print a plain progress heartbeat every 30s instead of going
  silent (#84).

### Changed

- The end-of-run summary table is replaced by an outcome-led ending:
  verdict line first, failures grouped by cause, output/report paths
  last (#84).

### Fixed

- Partial results are marked honestly: a yellow `!` (instead of a green `✓`)
  flags artifacts with missing classes or sources lost to network failures;
  the summary and report gain a `partial` count (#83).
- The failure recap says `…and N more failures (see report)` instead of
  silently truncating at 20 (#83).
- Every run prints its output and `decaf-report.json` paths, and interrupted
  runs report `completed/discovered` artifacts — the report gains an additive
  `discovered` field so a partial run can't pass for a complete one (#83).
- Fallback engines dropped for Java incompatibility are named in a warning
  instead of vanishing silently (#83).

### Security

- Maven repository credentials (`https://user:pass@…` config URLs) are
  stripped from everything decaf writes or shows: report settings and
  per-artifact `repo` fields, `-v` output, sources-cache `.repo` markers
  (legacy markers sanitized on read), and lookup-verdict files (#83).

## [1.8.0] - 2026-07-27

### Added

- Batched decompiles report live per-member progress: batch rows are labeled
  `batch of N` and update in place while the batch JVM runs — `87/123 classes`
  for tree-writing engines, `queued in batch`/`decompiling`/`done, pending split`
  for fernflower — without resetting the per-attempt elapsed clock (#71).
- Raw engine options pass through after `--`: everything after the separator
  is handed verbatim to the engine invocation, solo and batched alike
  (`decaf ./libs --engine cfr --no-fallback -- --renameillegalidents true`).
  Requires `--no-fallback`, since option dialects are engine-specific.
  Tokens are user-owned — decaf does not validate them, and colliding with
  decaf-managed flags is unsupported. Echoed as `engine_args` in the report
  settings (#70).

### Changed

- Exhausted-retry network warnings name the artifact being resolved, full
  nested path included (`maven: search.maven.org: timeout persisted after
  3 attempts (while resolving app.jar!/lib/dep.jar); …`) (#68).
- All five engines now batch small jars (cfr and procyon join vineflower,
  fernflower, and jd), and batches got bigger: archives below 3,000 classes
  are batch-eligible (was 800), up to 32 jars / 8,000 classes per batch
  (was 16 / 2,000) (#74).
- Exhausted-retry network warnings name the resolution step that failed
  (`index lookup`, `sha1 lookup`, `candidate probe`, `sources download`) and
  explain that affected artifacts may still resolve via other lookups, so a
  host-scoped failure no longer reads as an artifact-scoped one (#73).

### Fixed

- Batched decompiles no longer drop `package-info.java` / `module-info.java`
  that solo runs preserve: the post-batch split now attributes metadata
  sources to the member whose archive contains the matching class, and two
  members claiming the same metadata file (two modular jars, or two jars
  sharing a package) never share a batch — batch output is now identical to
  solo output (#75).

## [1.7.0] - 2026-07-24

### Added

- Decompiling rows in the live display show the engine, class count, and a
  live per-attempt elapsed time (`vineflower · 13,271 classes · 1m42s`);
  the clock resets when a fallback engine or per-class retry starts, so it
  tracks progress toward `--timeout` (#65).

### Changed

- Live stage-1 rows now show truthful sub-states — `scanning`,
  `copying resources`, `resolving`, `downloading`, `extracting`,
  `copying classes` — instead of a blanket `fetching`; `downloading`
  appears only while a network GET is actually running, so warm cached
  runs no longer imply network traffic (#65).
- Routine cached-maven ✓ lines render dim, so decompiled, extracted, and
  failed artifacts stand out in scrollback. Emphasis only — still exactly
  one line per jar (#65).

## [1.6.0] - 2026-07-23

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
- The `OK` summary row gains a `resource-only N` part when resource-only jars
  were mirrored, so its breakdown keeps summing to the total (#57).
- Maven lookup verdicts are cached on disk (`<user cache>/decaf/verdicts/`):
  positive sha1→GAV matches are kept forever, negative verdicts ("not in
  the index", "no sources published") for 7 days, and only network-clean
  misses are ever recorded. Warm re-runs skip all index and probe traffic,
  and network failures can no longer force decompilation of artifacts
  whose sources are already cached on disk (#52).
- `--fresh-maven` re-derives Maven lookup verdicts, ignoring cached ones,
  and `decaf cache clean` deletes cached sources jars and verdicts (#52).

### Changed

- Mirror mode is now faithful to the input: resources always come from the
  original archive on every path. Maven hits and sources-jar artifacts
  include the original's resources (previously sources only), solo
  decompiles no longer inherit whatever extra files the engine emitted, and
  artifacts whose engines all failed still carry their resources (#57).
- `resources_skipped` in decaf-report.json now means original-archive
  resources counted but not written (merge mode, or mirror with
  `--no-resource`) instead of its previous path-dependent meanings (#57).

### Fixed

- Kotlin sources are no longer invisible on the input side: a jar of `.kt`
  sources now classifies as a sources jar (previously resource-only, and
  nothing in merge mode), Maven sources extraction keeps `.kt` files
  (previously a Kotlin library's fetched `-sources.jar` yielded nothing
  and decaf fell back to decompiling the binary), and a sources jar
  shipping both `.java` and `.kt` keeps both (#59).

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

[1.8.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.8.0
[1.7.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.7.0
[1.6.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.6.0
[1.5.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.5.0
[1.4.1]: https://github.com/vcth4nh/decaf/releases/tag/v1.4.1
[1.4.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.4.0
[1.3.1]: https://github.com/vcth4nh/decaf/releases/tag/v1.3.1
[1.3.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.3.0
[1.2.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.2.0
[1.1.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.1.0
[1.0.0]: https://github.com/vcth4nh/decaf/releases/tag/v1.0.0
