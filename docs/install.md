---
icon: lucide/refresh-cw
---

# Install Command

Use `porringer install` to reconcile the current machine with a manifest, HTTPS setup profile, or `porringer://` install link. Install first builds the same plan exposed by `porringer preview`, asks for confirmation unless `--yes` is provided, then executes the selected actions.

Run preview first when you want to inspect the plan without changing the environment:

```shell
porringer preview ./my-project
porringer install ./my-project
```

## Manifest Locations

When the target is a directory, Porringer looks for manifests in this order:

1. `porringer.json`.
2. `[tool.porringer]` in `pyproject.toml`.

A target can also be a manifest file, an HTTPS setup profile, or a `porringer://` install link.

## Global Manifest

Each user also has a global manifest at `porringer.json` in Porringer's config directory (`porringer env info` shows where). It holds the packages and tools you want on this machine regardless of project. Pass `--global` to `install`, `preview` or `check` to use it. It can't be combined with a target or `--project-dir`.

Only its declared entries become actions, plus any installer they need first (for example `pipx`). Nothing is inferred from the folder it lives in: no project install, no runtime from a nearby `pyproject.toml`, no clone from `url`.

## Manifest Shape

| Field | Type | Description |
| --- | --- | --- |
| `version` | string | Manifest schema version. The current version is `"1"`. |
| `packages` | object | Packages to install per ecosystem, such as `{ "python": ["requests"] }`. |
| `tools` | object | CLI tools to install per ecosystem, such as `{ "python": ["pdm"] }`. |
| `runtimes` | object | Language runtimes per ecosystem, such as `{ "python": ["3.14"] }`. |
| `scm` | object | Source repositories to clone or update, such as `{ "git": ["https://github.com/org/repo"] }`. |
| `preferences` | object | Optional installer preference per ecosystem, such as `{ "python": "uv" }`. |
| `name` | string | Optional display name. |
| `description` | string | Optional short description. |
| `author` | string | Optional author or organization name. |
| `url` | string | Optional project URL. |

Manifest entries are grouped by kind, then by ecosystem. Ecosystem names are declared by plugins, so new ecosystems do not require core schema changes.

```json
{
  "version": "1",
  "packages": {
    "python": ["ruff", "pytest"],
    "node": ["typescript"]
  },
  "tools": {
    "python": ["pdm"]
  },
  "preferences": {
    "python": "uv"
  }
}
```

## Kinds and Installers

| Ecosystem | Kind | Typical installers |
| --- | --- | --- |
| `python` | `packages` | `uv`, `pip` |
| `python` | `tools` | `pipx` |
| `python` | `runtimes` | `pim`, `pyenv` |
| `python` | project install | `uv`, `pdm`, `poetry` |
| `system` | `packages` | `brew`, `apt`, `winget` |
| `node` | `packages` | `npm`, `pnpm` |
| `node` | project install | `npm`, `pnpm` |
| `git` | `scm` | `git` |

Project install is implicit. Project-install plugins inspect the repository for marker files, lock files, and tool-specific configuration. Porringer then chooses one project-install owner per ecosystem.

## Entry Fields

An entry is either a string (`"pdm>=2"`) or an object:

| Field | Description |
| --- | --- |
| `name` | Package name with an optional version constraint. Required. |
| `description` | Optional display text. |
| `platforms` | Optional list of platforms the entry applies to; empty means all. |
| `include_prereleases` | Consider pre-releases when checking for updates. |
| `installer` | Installer plugin for this entry only, such as `"pipx"`. It must install that kind for the entry's ecosystem, otherwise the manifest is rejected. It overrides `preferences` and is never swapped for another installer; if it isn't installed yet, its prerequisite is installed first (for example `pip` for `pipx`). |
| `runtime` | Runtime tag the installer runs under, such as `"3.14"`. Packages and tools only. If the tag doesn't resolve on this machine, that entry fails. Command previews still show the default interpreter. |

```json
{
  "tools": {
    "python": [
      { "name": "pdm", "installer": "pipx", "runtime": "3.14" }
    ]
  }
}
```

## Inference

Several manifest sections are optional because Porringer infers them from repository metadata and conventions. An explicit manifest entry always overrides the inferred value.

| Inferred | From | Notes |
| --- | --- | --- |
| Tool bootstrap (e.g. `pipx`) | The tool plugin itself | Synthesized only when the resolved tool installer is unavailable; declaring it via `packages` is never required. |
| Project's own tool (e.g. `pdm`) | Project evidence (lock files, `[tool.*]` tables) | Synthesized only when the tool's CLI isn't already available; an explicit `tools` entry pins a version instead. |
| SCM clone | `url`, when it points at a repo page on a known forge | A homepage or docs URL is never mistaken for a repository; declaring `scm` explicitly always wins. |
| Runtime version | `requires-python` in an already-local `pyproject.toml` | Only applies once the project exists on disk; a manifest that clones the project first should still declare `runtimes` explicitly. |

Preview always shows the full inferred plan, including any synthesized steps, so the manifest stays terse while the plan output stays transparent.

## Sync Strategies

| Strategy | Flag | Behavior |
| --- | --- | --- |
| `minimal` | default | Install missing packages and leave satisfied packages alone. |
| `latest` | `--strategy latest` | Upgrade every package to its latest allowed version. |

## Common Usage

```shell
porringer install
porringer install ./my-project
porringer install ./my-project --strategy latest
```

Use `--only-action` to run one or more stable action IDs from a previous preview:

```shell
porringer preview ./my-project --json
porringer install ./my-project --only-action 0:2
```

## Options

| Option | Description |
| --- | --- |
| `--project-dir`, `-d` | Working directory for project-install actions. |
| `--fail-fast / --no-fail-fast` | Stop on the first error, or continue and report all failures. |
| `--strategy`, `-s` | Use `minimal`, `latest`, or `exact` resolution. |
| `--plugin` | Include only actions for selected plugin names. Repeatable. |
| `--only-action` | Run only a stable action ID such as `0:2`. Repeatable. |
| `--jsonl` | Emit progress events and the final result as newline-delimited JSON. |
| `--record` | Write a replayable JSON run record to the given path. |
| `--yes`, `-y` | Skip the confirmation prompt. |
| `--global` | Install your global manifest instead of a project's. |

## Confirmation and Progress

`install` presents a compact execution plan before it changes the machine. The **Plan** section contains only actions that will run. Already-satisfied actions are collapsed to a count, and updates that `minimal` strategy will not apply appear separately under **Available updates**.

At the prompt, enter `y` or `yes` to continue. Enter `n`, `no`, or leave the response blank to cancel. Other input is rejected and the prompt is shown again, which prevents a pasted shell command from being treated as a cancellation.

During a long project sync, the default display shows the active command step, elapsed time, and its latest output line. Use `-v` to stream every subprocess line with its action and channel:

```shell
porringer install ./my-project -v
```

Press Ctrl-C to cancel a human-mode install. Porringer stops the active subprocess and reports the cancellation. The live progress display continues to report the active step and its latest output while a long-running build is active.

## Execution Order

Porringer installs runtimes before packages and tools, then clones source repositories before running project install. After package installation, it discovers plugins again. That lets tools installed earlier in the same run become installers for later actions — SCM clones run before project install for the same reason: a project referenced only by URL must exist locally before it can be synced.

This is called deferred tool resolution. A tool action whose installer is not available during preview can be created with `installer=None` and resolved just before execution.

```json
{
  "version": "1",
  "runtimes": { "python": ["3.14"] },
  "packages": { "python": [{ "name": "pipx" }] },
  "tools": { "python": [{ "name": "pdm" }] }
}
```

Execution order:

1. `pim` or `pyenv` installs Python 3.14.
2. `pip` or `uv` installs `pipx`.
3. Plugin discovery runs again and finds `pipx`.
4. `pipx` installs `pdm`.
5. The selected project plugin syncs dependencies from the discovered project root.

See `examples/python-bootstrap/` for a complete bootstrap example.

## Project Directory Resolution

Project-install backends discover their own project root by walking up from the manifest location and looking for ecosystem marker files.

| Ecosystem | Marker file |
| --- | --- |
| `python` | `pyproject.toml` |
| `node` | `package.json` |

This lets a manifest live below the project root:

```text
my-project/
  package.json
  pyproject.toml
  .porringer/
    porringer.json
```

```shell
porringer install my-project/.porringer
```

Each ecosystem resolves independently, so Python and Node project plugins can choose different roots from the same manifest. If no marker is found, the manifest directory is used and a warning is reported.

Use `--project-dir` to override auto-discovery for all ecosystems:

```shell
porringer install manifest.json --project-dir ./my-project
```

## Standalone Manifests

When a manifest has no associated project, pass `project_directory=False` through the API. Package, tool, runtime, and SCM actions still run. Project-install actions are reported as skipped instead of failed.

```python
import asyncio
from pathlib import Path

from porringer.api import API
from porringer.schema import LocalConfiguration, SetupParameters


async def main() -> None:
    api = API(LocalConfiguration())
    params = SetupParameters(
        paths=Path('downloaded-profile/porringer.json'),
        project_directory=False,
    )
    report = await api.sync.run(params)

    for skip in report.results.skips:
        print(f'Skipped: {skip.action.description} - {skip.message}')


asyncio.run(main())
```

## API Usage

```python
import asyncio
from pathlib import Path

from porringer.api import API
from porringer.schema import LocalConfiguration, SetupParameters


async def main() -> None:
    api = API(LocalConfiguration())
    project_path = Path('./my-project')
    manifest_path = Path('./my-project/.porringer/porringer.json')

    await api.sync.run(SetupParameters(paths=project_path))

    await api.sync.run(SetupParameters(paths=manifest_path, project_directory=False))

    await api.sync.run(SetupParameters(paths=manifest_path, project_directory=project_path))

    def on_event(event):
        description = getattr(event, 'action', None) and event.action.description or 'manifest'
        print(type(event).__name__, description)

    report = await api.sync.run(SetupParameters(paths=project_path), on_event=on_event)
    print(report.status, report.results.total_actions)


asyncio.run(main())
```

## Downstream Client Pattern

Long-lived clients should keep expensive discovery work out of tight refresh loops:

1. Discover plugins once per process or workspace refresh.
2. Use `InspectionMode.FAST` for live UI refreshes.
3. Run complete inspection before final validation or execution.
4. Resolve runtimes once before operations that need runtime-aware package managers.
5. Serialize progress events with `progress_event_snapshot(event)` when a machine needs live updates.
6. Enable `PORRINGER_TRACE_DIR` only for diagnostics.

```python
from pathlib import Path

from porringer.api import API
from porringer.schema import (
    InspectionMode,
    LocalConfiguration,
    SetupParameters,
    progress_event_snapshot,
)


api = API(LocalConfiguration())
project_path = Path('./my-project')
plugins = await API.discover_plugins(resolve_runtime=False)

fast_report = await api.sync.inspect(
    SetupParameters(paths=project_path, inspection_mode=InspectionMode.FAST),
    plugins=plugins,
)

await API.resolve_runtime_context(plugins)
complete_report = await api.sync.inspect(SetupParameters(paths=project_path), plugins=plugins)


def on_event(event):
    send_json(progress_event_snapshot(event).model_dump(mode='json'))


run_report = await api.sync.run(
    SetupParameters(paths=project_path),
    plugins=plugins,
    on_event=on_event,
)
```

The same `action_id` values appear in fast inspect reports, complete inspect reports, progress snapshots, and trace artifacts. Use action IDs instead of descriptions, labels, or native command strings for correlation.

Action IDs use `<manifest-index>:<action-index>`. They are stable for the same resolved input path list and manifest contents. Adding, removing, or reordering input paths or manifest actions intentionally changes them.

## Observable Results

Downstream-facing reports use a shared observability vocabulary:

- `status` summarizes the operation as `success`, `partial`, or `failed`.
- `diagnostics` are typed findings with stable codes, targets, and optional remediations.
- `follow_up_actions` describe safe next actions such as run, upgrade, or retry.
- `ResultEnvelope` wraps final results with operation metadata, correlation ID, timestamps, summary counts, diagnostics, follow-up actions, and the raw payload.

Use raw Python reports when the caller already knows the domain-specific schema. Use CLI envelopes, JSONL, or records when a machine wants one consistent shape across operations.

```shell
porringer preview ./my-project --explain
porringer preview ./my-project --envelope
porringer preview ./my-project --only-action 0:2 --json
porringer install ./my-project --jsonl
porringer install ./my-project --only-action 0:2 --record ./porringer-run.json
```

`--jsonl` writes one compact JSON object per progress event and a final `ResultEnvelope` line. `--record` writes a replayable JSON artifact containing setup parameters, captured event snapshots, the final envelope, and timing metadata.

For `action_progress` events, `action_progress.step_index`, `action_progress.step_total`, and `action_progress.command` identify the active command in a multi-step project plan. Completed action results may include `duration_seconds`, `cli_steps`, and `failed_step_index`. These fields let a client identify the failed command without parsing human output.

Treat replay files as local diagnostic artifacts. They include the exact setup parameters used for a run, including local paths and selectors.

### JSONL Event Types

Consumers should use `event_type` as the JSONL discriminator.

| `event_type` | Payload field | Meaning |
| --- | --- | --- |
| `plugins_discovered` | `discovered_plugins` | Plugin availability and capability snapshot. |
| `manifest_loaded` | `manifest` | Manifest resolved and actions are ready. |
| `manifest_failed` | `failed_path` | Manifest path failed to load. |
| `action_started` | `action` | Action execution began. |
| `action_progress` | `action`, `action_progress` | Plugin or subprocess progress. |
| `action_completed` | `action`, `result` | Action execution finished. |
| `action_inspected` | `action`, `inspection` | One action's checks finished during `plan` or `inspect`. |
| `result` | result envelope fields | Final `ResultEnvelope` for the operation. |

### Downstream API Boundaries

Porringer owns reusable backend contracts for CLIs, GUIs, and agents:

- `api.sync.inspect(...)` for manifest inspection.
- `api.sync.plan(...)` and `api.sync.run(plan=...)` to run exactly what was previewed (see [API](api.md)).
- `api.sync.run(..., on_event=...)` for execution with live progress.
- `SetupParameters.action_ids` for stable action selectors.
- `api.tool.check_updates(...)` and `api.tool.upgrade_project(...)` for managed package and tool operations.
- `api.profile.resolve(...)`, `api.profile.inspect(...)`, and `api.profile.run(...)` for portable HTTPS setup profiles.

Downstream applications should own application-specific behavior such as saved profile lists, tray menus, URI confirmation, update schedules, sorting, and display labels.
