# Output layouts

[README](../README.md) · [Usage](usage.md)

![A sample WAR in mirror and merged layouts, with Java sources in green and the original web.xml resource in yellow](output-layouts.svg)

## Mirror

```bash
decaf ./in -o out
```

- Output follows the input tree, with one directory per archive. For example,
  `in/libs/app.war` becomes `out/libs/app.war/`, including directories for
  nested jars such as `WEB-INF/lib/dep.jar/`.
- `.class` files are replaced with `.java` or `.kt` sources.
- Non-class resources are copied byte-for-byte, even when Maven supplies
  the sources or every engine fails.
- Archives beyond `--max-depth` remain archive files.
- `--no-resource` keeps the layout but writes sources only.

Some inputs need special handling:

- Corrupt archives are reported but not copied; damaged entries are skipped.
- Stray `.java` or `.kt` files inside a binary jar are dropped in favor of
  decompiler output.

## Merged

```bash
decaf app.war -o out --merge
```

- Sources from all artifacts go into `out/src/`, arranged by package.
- Container prefixes such as `WEB-INF/classes/` are removed.
- Identical duplicate classes are deduplicated.
- Conflicting duplicates use the first file in input path order. The report
  records each collision.
- Resources are skipped and counted in the report.

See [reports](reports.md) to check collisions and other incomplete results.
