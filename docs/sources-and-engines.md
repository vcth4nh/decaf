# Sources and engines

[README](../README.md)

## Maven sources

decaf prefers published `-sources.jar` files when it can match your binary
to Maven coordinates: group, artifact, and version.

- Coordinates from `pom.properties`, filenames, or manifests must match
  the repository's `.jar.sha1` checksum.
- A SHA-1 lookup on Maven Central can also resolve coordinates.
- `pom.properties` works with any configured repository that publishes
  `.jar.sha1` files; SHA-1 search itself is Central-only.
- Patched, shaded, or re-signed jars whose SHA-1 differs from the published
  binary are decompiled instead of receiving upstream sources.
- The report records each match or miss. `--no-maven` skips source lookup.

See [configuration](configuration.md) to add private or public repositories.

## Network failures and caching

- Network errors are retried with backoff.
- If a network failure forces decompilation, decaf warns and records
  `sources_miss: "network: …"` and `totals.network_misses`.
- `decaf report out --network-fallbacks` lists those artifacts.
- Verified SHA-1 matches are cached indefinitely; verified misses are cached
  for a week. Later runs can reuse cached sources without a lookup.
- `--fresh-maven` rechecks cached verdicts.
- `decaf cache clean` removes cached sources and verdicts.

## Fallback chain

Vineflower → CFR → Fernflower → Procyon → JD-CLI.

- A crash, timeout, missing class, or stubbed method triggers the next engine.
- Retries can cover the whole archive or individual classes.
- [Compile checks](compile-check.md) also retry classes whose Java output
  has parse, name, or flow errors.
- Engines download automatically on first use into the user cache.
  Built-in versions are pinned and verified with SHA-256.

## Built-in versions

These are the pins shipped in this checkout. Overrides can need newer Java;
`decaf engines list` checks the active pins against your runtime.

| Engine | Version | Min Java | License |
| --- | --- | --- | --- |
| [Vineflower](https://github.com/Vineflower/vineflower) | 1.12.0 | 17 | Apache-2.0 |
| [CFR](https://github.com/leibnitz27/cfr) | 0.152 | 11 | MIT |
| [Fernflower](https://github.com/JetBrains/intellij-community/tree/master/plugins/java-decompiler/engine) | 253.33813.25 | 21 | Apache-2.0 |
| [Procyon](https://github.com/mstrobel/procyon) | 0.6.0 | 11 | Apache-2.0 |
| [JD-CLI](https://github.com/intoolswetrust/jd-cli) | 1.2.0 | 11 | GPL-3.0 |

## Managing engines

```bash
decaf engines list             # pins, cache state, Java compatibility
decaf engines fetch            # pre-download all engines
decaf engines clean --stale    # remove superseded files
decaf engines update           # update pins to upstream latest
decaf engines update cfr --version 0.150
decaf engines update --reset   # restore built-in pins
```

- `engines clean` without `--stale` removes all cached engines and the checker.
- Updates verify upstream checksums: SHA-256, or SHA-1 with a warning.
  Without a checksum, the current pin stays, a red ✗ is printed, and the
  command exits `1`.
- New pins go into `[engines.NAME]` in your config. Rewriting the file drops
  handwritten comments. Each later run verifies the cached jar against its pin.
- A fallback engine requiring newer Java is skipped with a warning. Engine
  updates also warn if the new pin needs a newer runtime.
- On Java 19+, engine JVMs cache class-data-sharing (`.jsa`) archives beside
  their jars to speed up later starts. `clean --stale` keeps active pins'
  archives. A read-only cache runs without them.

## Multi-release jars

A multi-release jar can contain extra versions of a class under
`META-INF/versions/N/`.

- Vineflower, Fernflower, and Procyon write the base version. The other
  versions are not counted as missing.
- CFR and JD-CLI also write the extra versions. Select them with
  `--engine cfr` or `--engine jd` when you need those files.
- Merged output keeps the base version and records other versions as collisions.
- If a class has no base version and the first engine misses it, fallback
  decompiles the class from its lowest version folder.
