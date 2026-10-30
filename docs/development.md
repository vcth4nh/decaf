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
uv run python docs/render_media.py        # regenerate SVGs
uv run python docs/render_media.py --gif  # also record the terminal demo
```

- A JDK is required to compile the sample classes and run the checker.
- GIF recording also needs [VHS](https://github.com/charmbracelet/vhs), ttyd,
  ffmpeg, and Chromium. Timing and terminal settings live in [demo.tape](demo.tape).
- The renderer uses this checkout and built-in engine pins, downloading any
  missing engines into decaf's cache.
- Inputs and output directories are created in a temporary directory.
  Terminal output and SVG reports come from actual decaf runs.

The fixture contains three inputs:

| Input | Purpose |
| --- | --- |
| `app.war` | A valid app, a nested `greeter.jar`, and a `web.xml` resource |
| `broken.jar` | Deliberately invalid ZIP data, reported as an unreadable archive |
| `bytecode.jar` | A valid JVM class that throws `IOException` without declaring it, triggering a compile-check retry |

To build `bytecode.jar`, the renderer compiles a Java class, then renames its
`Exceptions` attribute to an unknown attribute. The JVM permits this bytecode,
but the decompiled Java needs a `throws` declaration. The fixture exposes a
bytecode limitation that remains flagged after Vineflower and CFR both try it.

The renderer verifies the failed archive, retained sources, compiler diagnostic,
and fallback attempts before saving media. Generated files are:

- [demo.gif](demo.gif): the run, problem summary, and compile-check details.
- [report.svg](report.svg): run summary and failed or partial artifacts.
- [compile-check.svg](compile-check.svg): compiler diagnostics and engine attempts.
- [output-layouts.svg](output-layouts.svg): mirror and merged trees from the valid WAR.
