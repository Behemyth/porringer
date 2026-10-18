---
icon: lucide/package-check
---

# Check Command

Use `porringer check` to ask installed environment plugins whether their managed packages have newer versions available. Each plugin uses its native tooling or registry APIs. Unavailable plugins are skipped.

## Basic Usage

Check all plugins for available updates:

```shell
porringer check
```

## Filtering by Plugin

Check one or more plugins by name:

```shell
porringer check --plugin pip
porringer check --plugin pip --plugin pipx
```

## Include Pre-releases

Include pre-release versions in update results:

```shell
porringer check --prereleases
```

## Per Runtime

Check the packages installed in one interpreter, or in every interpreter plus global stores such as pipx:

```shell
porringer check --runtime 3.14
porringer check --all-runtimes
```

Each interpreter is labelled with its provider and whether it is the default and was installed by that provider. Global stores appear once, not once per interpreter. A location that can't be listed is reported as an error, never as "no packages".

## Global Manifest

Check only the entries of your global manifest:

```shell
porringer check --global
```

Pre-release handling then comes from each entry's `include_prereleases`, so `--prereleases` can't be combined with `--global`.

## Options

| Option | Short | Description |
| --- | --- | --- |
| `--plugin` | `-p` | Plugin names to check. Omit to check every available environment plugin. |
| `--prereleases` | | Include pre-release versions. |
| `--global` | | Check the entries of your global manifest. |
| `--runtime` | `-r` | Check packages installed in this runtime tag. Repeatable. |
| `--all-runtimes` | | Check packages in every runtime and every global store. |

## API Usage

```python
import asyncio

from porringer.api import API
from porringer.schema import CheckParameters, LocalConfiguration


async def main() -> None:
    api = API(LocalConfiguration())
    params = CheckParameters(plugin_names={'pip'}, include_prereleases=False)
    results = await api.package.check_updates(params)

    for result in results:
        if result.error:
            print(f'{result.plugin}: {result.error}')
            continue
        for package in result.packages:
            print(f'{package.name}: {package.current_version} -> {package.latest_version}')


asyncio.run(main())
```

Long-lived callers should discover plugins once and reuse the same plugin map across checks, inspection, and sync:

```python
import asyncio

from porringer.api import API
from porringer.schema import CheckParameters, LocalConfiguration


async def main() -> None:
    api = API(LocalConfiguration())
    plugins = await API.discover_plugins()
    results = await api.package.check_updates(CheckParameters(), plugins=plugins)

    print(sum(result.updates_available for result in results))


asyncio.run(main())
```

## Plugin Implementation

Every `Environment` plugin implements `check_updates` as an async method. A plugin that cannot check for updates should return an empty list.

```python
from porringer.core.plugin_schema.environment import (
    Environment,
    CheckUpdatesParameters,
)
from porringer.core.schema import Package


class MyPluginEnvironment(Environment):
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        # Use async native tooling or registry I/O.
        # Return packages whose version is the latest available version.
        return []
```
