# Usage

[README](../README.md)

## Common commands

```bash
decaf ./libs                        # mirror archives under ./decaf-out
decaf app.war -o out --merge         # one package tree under out/src
decaf ./libs --no-resource           # mirrored layout, sources only
decaf ./libs --engine cfr --no-fallback
decaf ./libs --no-maven -j 8 --timeout 120
decaf ./libs --cpus 8                # total CPU budget
decaf ./libs --memory 8g             # total engine heap budget
decaf ./libs --max-depth 2           # unpack archives nested two levels deep
```

- Use a new or empty output directory, or pass `--force` to write into an
  existing one. `--force` does not remove files left by earlier runs.
- Folder names that match commands need an explicit path: `decaf ./run`,
  `decaf ./engines`, `decaf ./cache`, or `decaf ./report`.
- See [output layouts](output-layouts.md) for resources and merge behavior,
  and [resource limits](resource-limits.md) for CPU and memory settings.
- Use [reports](reports.md) to inspect failures and partial results. Exit
  code `0` does not guarantee that every source file is complete.

## Nested archives

- `--max-depth` defaults to `1`: jars inside a WAR or fat JAR are processed,
  but archives inside those jars are not unpacked.
- Deeper archives are listed as skipped in the report. In mirror output,
  they are copied through as archive files.
- Folder recursion is unlimited; the depth setting applies only to archives.
- If an archive has two entries with the same name, decaf uses the last one,
  as Java does. It warns when their contents differ.

## Engine options

```bash
decaf ./libs --engine cfr --no-fallback -- --renameillegalidents true
```

- Tokens after `--` pass verbatim to the engine.
- This requires `--no-fallback`, because each engine has its own option syntax.
- decaf does not validate those tokens. Options that conflict with flags
  decaf manages, such as CFR's `--outputdir`, are unsupported.

See [sources and engines](sources-and-engines.md) for pins and engine management.

## Case-sensitive output

Obfuscated archives can contain classes whose names differ only by case.
They need separate files in both the output and temporary directories.

- On Windows with WSL installed, decaf makes those directories case-sensitive
  automatically.
- On a case-insensitive filesystem, decaf refuses colliding names with a
  `case collision` failure and a hint. This includes default macOS volumes,
  Windows without WSL, and FAT/exFAT volumes.
- Use a case-sensitive volume for the output and point `TMPDIR` at it too.
- Name comparison uses NFD Unicode normalization and full case folding.
  It also catches names that differ only in Unicode normalization and some
  names NTFS keeps distinct, such as `ß` and `ss`.
