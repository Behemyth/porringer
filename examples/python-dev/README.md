# Python Development Environment Example

This example shows a small Python development environment managed by Porringer. It installs common linting, formatting, type-checking, testing, and project-management tools.

## What the Manifest Declares

The `porringer.json` manifest groups entries by kind and ecosystem. Porringer resolves each ecosystem to an available installer at runtime, then detects project install from repository files when a project plugin has ownership evidence.

| Manifest section | Resolves to | Installs |
| --- | --- | --- |
| `packages.python` | `uv` or `pip` | `ruff`, `pyrefly`, `pytest`, `pytest-asyncio` |
| `tools.python` | `pipx` | `pdm` |
| `packages.system` (`maintainer` group) | `winget`, `brew`, or `apt` | GitHub CLI (`gh`), only with `--with maintainer` |

These packages mirror the repository's own `lint` and `test` dependency groups in `pyproject.toml`. If you run preview from a matching development environment, the package actions should report as satisfied.

The GitHub CLI entries belong to the optional `maintainer` group, so a plain install skips them and the preview lists the group as not included. Each entry pins the system installer for its platform. Porringer only installs `gh`; sign in afterwards with `gh auth login`.

## Usage

Preview the plan without changing the environment:

```shell
porringer preview examples/python-dev
```

Run the setup with confirmation:

```shell
porringer install examples/python-dev
```

Skip confirmation in a scripted flow:

```shell
porringer install examples/python-dev --yes
```

Include the optional maintainer tools:

```shell
porringer install examples/python-dev --with maintainer
```

Upgrade every package to the latest allowed version:

```shell
porringer install examples/python-dev --strategy latest
```

## Embedded pyproject Manifest

The same manifest can live in `pyproject.toml`:

```toml
[tool.porringer]
version = "1"

[tool.porringer.packages]
python = ["ruff", "pyrefly", "pytest", "pytest-asyncio"]

[tool.porringer.tools]
python = ["pdm"]

[tool.porringer.preferences]
python = "uv"
```
