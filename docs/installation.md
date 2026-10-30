# Installation and updates

[README](../README.md)

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- Java on `PATH` that can run your chosen engines. The default, Vineflower,
  needs Java 17+. `decaf engines list` shows compatibility with your Java.
- A JDK with `javac` for [compile checks](compile-check.md). With only a JRE,
  decaf skips the check and prints a note.
- Linux, macOS, or Windows. CPU limits are enforced on Linux and passed as
  JVM hints elsewhere; see [resource limits](resource-limits.md).
- A [case-sensitive output and temporary directory](usage.md#case-sensitive-output)
  for archives containing class names that differ only by case.

If Java is too old for the primary engine, the run stops with a hint.

## Install

```bash
uv tool install git+https://github.com/vcth4nh/decaf
```

From a checkout:

```bash
uv tool install .          # put decaf on PATH
uvx --from . decaf --help  # or run without installing
```

## Update

```bash
decaf self-update          # install the latest release
decaf self-update --check  # check without installing
```

- decaf reads the latest GitHub release and, if it is newer, runs
  `uv tool install git+https://github.com/vcth4nh/decaf@vX.Y.Z` with that
  release's version. The uv output is shown in the terminal.
- Only a `uv tool install` installation can update itself. An install from
  a local folder also moves to the GitHub release.
- For pip, pipx, or uvx, decaf prints the command to run. For an editable
  install, it tells you to update the checkout with `git pull`.
- Updates and decompilation runs cannot overlap. Starting either while the
  other is active exits with code `2`.
- On Windows, an update renames the running `decaf.exe` to `decaf.exe.old`.
  The next update that installs a release removes the old file.

## Update notices

- At the end of a terminal run, decaf prints a line if a newer release exists.
- GitHub is queried at most once a day; the run does not wait for the answer.
- Set `update_check = false` in the [config file](configuration.md) to disable
  the notice.
