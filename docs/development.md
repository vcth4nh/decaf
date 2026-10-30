# Development

[README](../README.md)

## Tests

```bash
uv sync
uv run pytest                      # fast offline suite
uv run pytest -m "slow or network"  # real engines and live Maven
```

## README media

```bash
uv run python docs/render_media.py                 # regenerate SVGs
uv run python docs/render_media.py --gif           # also record all four GIFs
uv run python docs/render_media.py --demo sources  # record just one GIF
```

- A JDK is required to compile the sample classes and run the checker.
- GIF recording also needs [VHS](https://github.com/charmbracelet/vhs), ttyd,
  ffmpeg, and Chromium. Shared terminal settings live in
  [demos/common.tape](demos/common.tape): 60 ms per character, normal playback,
  and a steady cursor. Each demo pauses before executing commands and holds
  results for 8–12 seconds.
- The renderer uses this checkout and built-in engine pins, downloading any
  missing engines into decaf's cache.
- Inputs and output directories are created in a temporary directory.
  Terminal output and SVG reports come from actual decaf runs.
- The Maven demo downloads the pinned `org.slf4j:slf4j-api:2.0.13` binary from
  Maven Central and verifies its SHA-256 before recording. It needs network
  access for the first source lookup and download. Its temporary `XDG_CACHE_HOME`
  isolates the source cache on Linux, so the first run downloads sources and the
  second reuses them. Engine JARs are prepared before recording; source lookups
  and downloaded sources start uncached.

Each GIF has a separate tape. `--demo` accepts the names below and can be
repeated to record several GIFs without regenerating the SVGs.

| Demo | Recording | Shows |
| --- | --- | --- |
| [sources](demos/sources.gif) | [sources.tape](demos/sources.tape) | Published Maven sources, then a cached run |
| [layouts](demos/layouts.gif) | [layouts.tape](demos/layouts.tape) | A batch with nested dependencies, mirrored and merged files |
| [checks](demos/checks.gif) | [checks.tape](demos/checks.tape) | Compile-check diagnostics and fallback attempts |
| [reports](demos/reports.gif) | [reports.tape](demos/reports.tape) | Valid archives alongside a failure, problem summary, and artifact details |

The generated fixtures contain these inputs:

| Input | Purpose |
| --- | --- |
| `app.war` | A valid app, a nested `greeter.jar`, and a `web.xml` resource |
| `libs/` | The valid WAR plus `tools.jar`, a separate library for the batch and layout demo |
| `broken.jar` | Deliberately invalid ZIP data, reported as an unreadable archive |
| `bytecode.jar` | A valid JVM class that throws `IOException` without declaring it, triggering a compile-check retry |

To build `bytecode.jar`, the renderer compiles a Java class, then renames its
`Exceptions` attribute to an unknown attribute. The JVM permits this bytecode,
but the decompiled Java needs a `throws` declaration. The fixture exposes a
bytecode limitation that remains flagged after Vineflower and CFR both try it.

The renderer verifies Maven retrieval and cache reuse, matching sources in both
layouts, preserved resources, the failed archive, retained partial sources,
compiler diagnostics, and fallback attempts before saving the corresponding GIF.
The SVGs remain available as static examples:

- [report.svg](report.svg): run summary and failed or partial artifacts.
- [compile-check.svg](compile-check.svg): compiler diagnostics and engine attempts.
- [output-layouts.svg](output-layouts.svg): mirror and merged trees from the valid WAR.
