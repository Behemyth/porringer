---
icon: lucide/braces
---

# API Surface

Porringer exposes public Python APIs for applications, downstream clients, and plugin authors. Prefer the imports documented here over console modules or internal backend helpers.

!!! note "Stability policy"

    Import paths documented on this page are intended for downstream applications and plugin authors. Modules under `porringer.backend.command.core`, `porringer.console`, and `porringer.utility` are implementation details unless a symbol is re-exported here or from `porringer.schema`.

## Top-Level Imports

The top-level package re-exports the common application entry points:

| Import | Use |
| --- | --- |
| `API` | Programmatic entry point for sync, package, extension, tool, and profile operations. |
| `DiscoveredPlugins` | Reusable plugin discovery result for repeated operations. |
| `LocalConfiguration` | Caller-provided cache and configuration paths. |
| `RuntimeContext` | Resolved runtime executable paths. |
| `SetupManifest` | Manifest model. |
| `SetupParameters` | Shared parameters for inspect and run operations. |
| `__version__` | Installed package version string. |
| `PorringerError` | Base exception for Porringer-raised errors. |
| `ManifestError` | Manifest load, parse, or validation failures. |
| `ManifestValidationCode` | Machine-readable manifest diagnostic codes. |
| `SetupError`, `ProcessError`, `PluginError`, `NotSupportedError`, `UpdateError`, `CommandTimeoutError`, `PluginDependencyError` | Specific exception subclasses. |

```python
import asyncio
from pathlib import Path

from porringer import API, SetupParameters


async def main() -> None:
    api = API()
    report = await api.sync.inspect(SetupParameters(paths=Path('.')))
    print(report.summary.actions)


asyncio.run(main())
```

## API Namespaces

`API()` creates namespace objects for backend operations. Pass a `LocalConfiguration` to override defaults:

| Namespace | Main operations |
| --- | --- |
| `api.sync` | Inspect manifests and run them with optional progress events. Use `inspect_paths([...])` for a stateless per-directory dashboard view. |
| `api.package` | Check package updates through environment plugins, and list runtimes and installed packages per location with `inventory()`. |
| `api.extension` | List installed Porringer plugins. |
| `api.tool` | Check, upgrade, or uninstall the packages and tools a manifest declares; `add`, `upgrade` and `remove` also edit the manifest (see [Managing entries](manage.md)). |
| `api.profile` | Resolve, inspect, and run HTTPS setup profiles. |

`api.global_manifest_path` is the user's global manifest (it may not exist yet). Pass it in `paths` like any other manifest; Porringer recognises it and infers nothing from its location.

Long-lived clients should discover plugins once and pass the result into repeated calls:

```python
import asyncio
from pathlib import Path

from porringer import API, SetupParameters
from porringer.schema import InspectionMode


async def main() -> None:
    api = API()
    plugins = await API.discover_plugins(resolve_runtime=False)

    report = await api.sync.inspect(
        SetupParameters(paths=Path('.'), inspection_mode=InspectionMode.FAST),
        plugins=plugins,
    )

    print(report.status, report.summary.actions)


asyncio.run(main())
```

### Action filters

`SetupParameters` narrows the action plan with `plugin_names`, `action_kinds`, `include_packages`, `action_ids`, and `entry_ids`. For every filter, `None` selects everything and an empty set selects nothing. Filters compose as an intersection.

`api.tool.check_updates`, `api.tool.upgrade_project`, and `api.tool.uninstall` accept `paths=` and only touch package, tool, and runtime actions. All three accept `action_ids` to target exact rows; IDs are `<manifest_index>:<action_index>` positions, so re-inspect after a manifest changes. They also accept `entry_ids`, which survive manifest edits and are the key to store for per-entry settings. `uninstall` requires `include_packages`, `action_ids` or `entry_ids`, removes tools before the packages and runtimes they may depend on, and skips packages that are not installed:

```python
report = await api.tool.uninstall(paths=Path('.'), include_packages={'ruff'})
```

### Execution and cancellation

Runs on one `API` instance are serialized, because execution changes the process-wide `PATH`. Starting a run from inside another run on the same instance, for example from `on_event`, raises `PorringerBusyError`. Separate instances and separate processes are not coordinated.

To cancel a run, cancel the task that awaits it. Porringer stops any active subprocess and re-raises `asyncio.CancelledError`.

`api.tool.remove` on a runtime entry raises `RuntimeInUseError` while entries or installed tools still depend on that runtime; `api.tool.runtime_dependents` reports them without changing anything (see [Managing entries](manage.md)).

### Plans: run what was previewed

`api.sync.plan(parameters)` inspects like `inspect` and returns a `SyncPlan`: the report (`plan.report`) plus the loaded actions, frozen. `api.sync.run(plan=plan)` runs those actions without loading the manifests again, so a confirmed preview is exactly what runs. Remote manifests are downloaded once, when the plan is made.

The plan fingerprints every file it was built from: each input file, each manifest, and the `pyproject.toml` at each manifest's root. If any changed, appeared, or vanished, `run` raises `PlanStaleError` with the paths in `changed` and runs nothing; make a new plan. A plan runs once, whatever the outcome; call `plan.discard()` to release one you won't run.

```python
plan = await api.sync.plan(SetupParameters(paths=Path('.')))
if confirm(plan.report):
    report = await api.sync.run(plan=plan)
else:
    plan.discard()
```

Pass `on_event` to `plan` (or `inspect`) to fill in a view while inspection runs. Events arrive in this order: one `PluginsDiscoveredEvent` with plugin availability, a `ManifestFailedEvent` for each input that could not be loaded, a `ManifestLoadedEvent` for each manifest (with its typed actions, before any checks run), then one `ActionInspectedEvent` per action, manifest by manifest. Each `ActionInspectedEvent` carries the typed `action` and `result` plus the JSON-stable `inspection`. Actions are reported as soon as their checks finish, so with complete inspection they can arrive out of source order within a manifest; match them by `event.action_ref`. The final report is always in source order.

## Schema Package

`porringer.schema` is the flat import surface for CLI, API, and downstream data contracts. It includes manifest models, setup parameters, inspection reports, run reports, progress events, observability envelopes, project/tool/profile reports, and update-check results.

Prefer importing schemas from `porringer.schema` instead of individual schema modules:

```python
from porringer.schema import (
    ActionProgress,
    DirectoryStatus,
    FollowUpAction,
    InspectionMode,
    ResultEnvelope,
    SetupParameters,
    SyncInspectionReport,
    SyncRunReport,
    progress_event_snapshot,
)
```

## Plugin Author Surface

Plugin authors should build on `porringer.core.plugin_schema` and `porringer.core.schema`.

| Surface | Use when implementing |
| --- | --- |
| `Environment` | Package manager plugins such as `pip`, `npm`, `apt`, or `winget`. |
| `ProjectInstaller` | The project-install capability. Mix into a package plugin (e.g. `uv`, `npm`, `pnpm`) so one plugin drives both phases, or subclass `ProjectEnvironment` for a project-only tool such as `pdm` or `poetry`. |
| `ScmEnvironment` | Source-control plugins such as `git`. |
| `RuntimeProvider` | Plugins that discover or install language runtimes. |
| `RuntimeConsumer` | Plugins that can target a resolved runtime executable. |
| `ToolBasedPlugin` | Shared base for plugins driven by command-line tools. |

```python
from porringer.core.plugin_schema import Environment
from porringer.core.plugin_schema.environment import CheckUpdatesParameters
from porringer.core.schema import Package


class MyEnvironment(Environment):
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        return []
```

Environment plugins must use async package queries and update checks. They should use the helper command methods provided by the base classes instead of blocking subprocess calls.

### Project command plans

`ProjectInstaller.command_plan()` is the single command-building entry point for a project plugin. Porringer uses the returned steps for both preview and execution, so the command a user confirms is the command that runs.

The default plan contains one command built from `tool_name()`, the install verb, and `runtime_selection_args()`. The default runtime-selection arguments are empty. A plugin must opt in only when its tool accepts an inline runtime flag. For example, uv can use `--python`; PDM cannot accept that flag on `pdm install`.

Override `command_plan()` when a tool needs multiple commands. Return every command in `ProjectCommandPlan.steps` in execution order and set `argv` to the primary or final command. Poetry, for example, can run `poetry env use <python>` before `poetry install`. Do not build a separate preview command: Porringer renders and executes the same plan.

Use the public `ProjectEnvironmentUnitTests` base in `porringer.test.pytest.tests` for project plugins. Its inherited tests verify that command plans are tokenized, safe without a runtime context, and only add runtime arguments declared by the plugin.

## Test Support

`porringer.test.*` is a public plugin-testing toolkit shipped with the package. Plugin packages should use it to verify conformance with Porringer's runtime contracts. It does not belong in production code.

| Module | Contents |
| --- | --- |
| `porringer.test.pytest.tests` | Abstract pytest base classes: `EnvironmentUnitTests`, `EnvironmentIntegrationTests`, `ProjectEnvironmentTests`, `ScmEnvironmentTests`, `RuntimeProviderTests`. |
| `porringer.test.pytest.shared` | Abstract base classes and session-scope fixtures. |
| `porringer.test.pytest.variants` | Parametrize helpers for environment, project-environment, and SCM variants. |
| `porringer.test.pytest.plugin` | pytest plugin that registers Porringer fixtures. |
| `porringer.test.mock.environment` | `MockEnvironment`, a lightweight in-process environment stub. |
| `porringer.test.mock.project_environment` | `MockProjectEnvironment` stub. |
| `porringer.test.mock.scm` | `MockScmEnvironment` stub. |
| `porringer.test.mock.subprocess` | `MockSubprocessEnvironment` for command-list output tests without subprocesses. |

A plugin package typically inherits from an abstract test base and supplies the plugin type through a fixture:

```python
# tests/test_my_plugin.py
import pytest
from porringer.test.pytest.tests import EnvironmentUnitTests

from my_package.plugin import MyEnvironment


class TestMyEnvironment(EnvironmentUnitTests[MyEnvironment]):
    @pytest.fixture(name='plugin_type', scope='session')
    def fixture_plugin_type(self) -> type[MyEnvironment]:
        return MyEnvironment
```

The base tests cover command construction, tool availability checks, non-empty command lists, shell-injection resistance, and protocol conformance.

Downstream applications should not depend on `porringer.console.*`, `porringer.backend.command.core.*`, or `porringer.test.*` at runtime.
