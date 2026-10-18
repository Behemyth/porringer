<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/synodic/porringer/development/docs/images/porringer-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/synodic/porringer/development/docs/images/porringer-light.svg">
    <img width=25% alt="Porringer Logo" src="https://raw.githubusercontent.com/synodic/porringer/development/docs/images/porringer-light.svg">
  </picture>
  <br>
  <em>A meta-package manager for all porridges</em>
</p>

# Porringer

A CLI and Python API for synchronizing developer environments from a declarative manifest. Porringer reads the packages, tools, runtimes, repositories, and project install work you want, then delegates the actual work to installed package managers through plugins.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.md)
[![PyPI version](https://img.shields.io/pypi/v/porringer.svg)](https://pypi.org/project/porringer/)

## Features

- Manifest-driven setup from `porringer.json` or `[tool.porringer]` in `pyproject.toml`.
- Plugin-based installers for Python, Node, system packages, project install, runtimes, and source repositories.
- Read-only previews with stable action IDs, command previews, diagnostics, and JSON output.
- Sync strategies for minimal installs or latest allowed versions.
- JSONL progress streams, replay records, and trace artifacts for downstream clients.
- Python APIs for inspection, execution, profiles, tools, and progress events.

## Quick Start

Install Porringer into an isolated CLI environment, then preview a manifest before running it:

```shell
pipx install porringer
porringer preview examples/python-dev
porringer install examples/python-dev
```

See [Setup](https://synodic.github.io/porringer/setup/) for `uv`, `pip`, and development checkout instructions.

## Development

Porringer uses [PDM](https://pdm-project.org/en/latest/) for local development. Common tasks are defined in `pyproject.toml` under `[tool.pdm.scripts]`:

```shell
pdm install
pdm run test
pdm run generate
```

See [Development](https://synodic.github.io/porringer/development/) for test lanes, traces, smoke tests, and diagnostics.

For contribution guidelines, see [CONTRIBUTING.md](https://github.com/synodic/.github/blob/stable/CONTRIBUTING.md).

## Documentation

- [Documentation home](https://synodic.github.io/porringer/)
- [Setup](https://synodic.github.io/porringer/setup/)
- [Install links](https://synodic.github.io/porringer/links/)
- [Install command](https://synodic.github.io/porringer/install/)
- [Preview command](https://synodic.github.io/porringer/preview/)
- [Check command](https://synodic.github.io/porringer/check/)
- [Download API](https://synodic.github.io/porringer/download/)
- [API surface](https://synodic.github.io/porringer/api/)
- [Development](https://synodic.github.io/porringer/development/)

## License

This project is licensed under the MIT License. See [LICENSE.md](LICENSE.md) for details.

Copyright © 2025 Synodic Software
