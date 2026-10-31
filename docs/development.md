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
uv run python docs/render_media.py                  # regenerate SVGs
uv run python docs/render_media.py --gif            # also record both GIFs and save demo.jar
uv run python docs/render_media.py --demo workflow  # record just the main workflow
```

- A JDK is required to compile the sample classes and run the checker.
- GIF recording needs Linux, [VHS](https://github.com/charmbracelet/vhs), ttyd,
  ffmpeg, and Chromium. Shared terminal settings live in
  [demos/common.tape](demos/common.tape): 60 ms per character, normal playback,
  and a steady cursor. Commands are separated by two-second pauses, with a
  five-second hold at the end of each GIF.
- The renderer uses this checkout and built-in engine pins, downloading any
  missing engines into decaf's cache before recording.
- Inputs and output directories are temporary. Recordings use isolated
  `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` directories, with update notices disabled
  and Maven Central configured. The visible commands use the default CPU and
  memory settings. Terminal output and SVG reports come from actual decaf runs.

Each GIF has a separate tape. `--demo` accepts the names below and can be
repeated to record several GIFs without regenerating the SVGs.

| Demo | Recording | Shows |
| --- | --- | --- |
| [workflow](demos/workflow.gif) | [workflow.tape](demos/workflow.tape) | Maven sources, decompilation, compile checks, and a successful CFR retry in one run |
| [reports](demos/reports.gif) | [reports.tape](demos/reports.tape) | Valid archives alongside a failure, problem summary, and artifact details |

### Combined demo JAR

[demo.jar](demos/demo.jar) contains `demo/Calculator.class`, a nested
`slf4j-api-2.0.13.jar`, and a `settings.xml` resource. The renderer downloads the
pinned `org.slf4j:slf4j-api:2.0.13` binary from Maven Central and verifies its
SHA-256 before adding it to the sample.

The application class has a parameter named `super` in its debug metadata. This
is valid JVM bytecode, but Vineflower preserves the Java keyword in its output
and `javac` flags it. CFR renames the parameter during the automatic retry, and
the recovered source passes the check. The renderer also compiles that source
with `javac` before saving the GIF and downloadable JAR.

A real Maven lookup for the generated application runs before recording and
caches its miss. The nested SLF4J dependency starts without cached sources or a
lookup result, so the visible run downloads its published sources. Recording
requires network access and stops if these outcomes cannot be verified.

### Failure and layout inputs

| Input | Purpose |
| --- | --- |
| `app.war` | A valid app, a nested `greeter.jar`, and a `web.xml` resource |
| `broken.jar` | Deliberately invalid ZIP data, reported as an unreadable archive |
| `bytecode.jar` | A valid JVM class that throws `IOException` without declaring it, triggering a compile-check retry |

To build `bytecode.jar`, the renderer compiles a Java class, then renames its
`Exceptions` attribute to an unknown attribute. The JVM permits this bytecode,
but the decompiled Java needs a `throws` declaration. The fixture exposes a
bytecode limitation that remains flagged after Vineflower and CFR both try it.

The renderer verifies matching sources in both layouts, preserved resources,
the failed archive, retained partial sources, compiler diagnostics, and fallback
attempts. The reports GIF uses `--no-maven` to focus on failures and diagnostics.
Static images:

- [output-layouts.svg](output-layouts.svg): mirror and merged trees from the valid WAR.
- [report.svg](report.svg): run summary and failed or partial artifacts.
- [compile-check.svg](compile-check.svg): compiler diagnostics and engine attempts.
