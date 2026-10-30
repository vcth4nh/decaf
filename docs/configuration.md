# Configuration

[README](../README.md) · [Sources and engines](sources-and-engines.md)

## Config file

| Platform | Default path |
| --- | --- |
| Linux | `~/.config/decaf/config.toml` |
| macOS | `~/Library/Application Support/decaf/config.toml` |
| Windows | `%LOCALAPPDATA%\decaf\decaf\config.toml` |

- `--config PATH` selects a different file.
- On Linux and macOS, `XDG_CONFIG_HOME` changes the path to
  `$XDG_CONFIG_HOME/decaf/config.toml`.
- The file is TOML and must be UTF-8.

## Maven repositories

The domains and credentials below are examples; replace them with your own.

```toml
repositories = [
  "https://repo.example.com/repository/maven-public",
  "https://user:pass@private.example.com/maven2",
]

update_check = false  # disable the notice about newer releases
```

- Repositories are tried in order. Maven Central is appended unless already
  listed as `https://repo1.maven.org/maven2` or
  `https://repo.maven.apache.org/maven2`.
- `--repo URL` prepends a repository for one run and can be repeated.
- Basic authentication uses the URL's username and password.
- Percent-encode `/`, `?`, `#`, and `@` in credentials as `%2F`, `%3F`,
  `%23`, and `%40`. decaf rejects a repository URL with an `@` after its host.

```bash
decaf ./libs --repo https://repo.example.com/repository/maven-public
```

See [Maven sources](sources-and-engines.md#maven-sources) for checksum matching
and [installation and updates](installation.md#update-notices) for update notices.

## Engine pins

`decaf engines update` writes overrides under `[engines.NAME]`. Use the
[engine management commands](sources-and-engines.md#managing-engines) to update,
inspect, or reset them.
