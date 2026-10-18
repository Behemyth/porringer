# Python Bootstrap Example

This example bootstraps a Python development toolchain from an almost empty environment. It demonstrates runtime installation, deferred tool resolution, native tool plugins, and project install in one manifest.

## Bootstrap Chain

The manifest runs in ordered phases:

1. `runtimes.python` installs Python 3.15 through `pim` on Windows or `pyenv` on macOS and Linux. The resolved interpreter path is passed to runtime-aware package and tool operations.
2. `packages.python` installs `pipx` into the current Python environment through `pip` or `uv`.
3. `tools.python` installs `pdm` as an isolated CLI tool through `pipx`. This backend may be deferred during preview because `pipx` is installed earlier in the same run.
4. The `plugins` list runs `pdm self add cppython` through PDM's native plugin management.
5. Project install runs `pdm install` from the discovered project root.

## Manifest Overview

```text
runtimes.python  ->  pim / pyenv   ->  Python 3.15
packages.python  ->  pip / uv      ->  pipx
tools.python     ->  pipx          ->  pdm          (deferred resolution)
                 ->  pdm self add  ->  cppython     (native plugin management)
project install  ->  pdm install                   (plugin-owned project install)
```

## Usage

Preview the plan without changing the environment:

```shell
porringer preview examples/python-bootstrap
```

Run the setup with confirmation:

```shell
porringer install examples/python-bootstrap
```

Skip confirmation in a scripted flow:

```shell
porringer install examples/python-bootstrap --yes
```

## How Deferred Resolution Works

Porringer installs runtimes before package and tool actions. It forwards the resolved interpreter to `RuntimeConsumer` plugins so later `pip` or `uv` commands target the intended Python.

After package installation, Porringer discovers plugins again. If `pipx` was just installed, `tools.python` can resolve to the `pipx` backend and install `pdm`. Project install runs after the toolchain is available and is owned by the selected project plugin.

If a tool backend is unavailable at preview time, the action is created with a deferred installer. Resolution happens later in the run, after earlier package actions have completed.
