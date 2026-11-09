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
| `groups` | object | Optional entry groups that callers opt into, such as `{ "maintainer": { "description": "Release tools" } }`. See [Optional Groups](#optional-groups). |
| `native` | object | Optional CPPython local-source intent, applied after the Python project sync. See [Native Local Sources](#native-local-sources). |
| `project` | object | Optional request for the selected project installer's preparation step. See [Project Preparation](#project-preparation). |
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
| `group` | Optional group the entry belongs to. The group must be declared under `groups`. The entry is installed only when the group is selected with `--with`. |

```json
{
  "tools": {
    "python": [
      { "name": "pdm", "installer": "pipx", "runtime": "3.14" }
    ]
  }
}
```

## Optional Groups

Use groups for tools that only some people need, such as maintainer or release tools. Declare each group under `groups`, then add `group` to its entries. A plain install skips them, and the preview lists each group with whether it's included and how many of its entries apply to this platform.

```json
{
  "groups": {
    "maintainer": { "description": "Repository administration and release tools" }
  },
  "packages": {
    "system": [
      { "name": "GitHub.cli", "installer": "winget", "platforms": ["win32"], "group": "maintainer" },
      { "name": "gh", "installer": "brew", "platforms": ["darwin"], "group": "maintainer" },
      { "name": "gh", "installer": "apt", "platforms": ["linux"], "group": "maintainer" }
    ]
  }
}
```

```shell
porringer preview --with maintainer
porringer install --with maintainer
```

Group names use lowercase letters, digits, and hyphens. When an entry names a group that isn't declared, the manifest is rejected. When you pass `--with` with a group that no loaded manifest declares, the command fails. Once selected, a group's entries are planned, confirmed, and reported like any other entry, and each action records its `group`. Setup profiles and install links don't accept `--with`.

Porringer installs tools; it doesn't sign you in to them. After installing the GitHub CLI, run `gh auth login`.

## Native Local Sources

A C++ project that uses [CPPython](https://github.com/Synodic-Software/CPPython) can choose which of its native dependencies build from local checkouts instead of published packages. Put that choice in a `native` section, and Porringer passes it to the project's own CPPython after the Python project sync:

```json
{
  "native": {
    "schema_version": 1,
    "root": "..",
    "default": "local",
    "packages": { "periapsis": {}, "honesty": {} }
  }
}
```

The project sync gets one more step, which runs the project's locked CPPython through the project tool:

```shell
pdm run cppython local apply --clone --inline '{"default":"local",...}' --json
```

- CPPython clones each local package whose checkout is missing from the root, from the package's `url`. A missing checkout without a `url` fails the step before the selection changes.
- CPPython owns the format. Porringer doesn't validate the section beyond requiring an object; CPPython validates the whole document before it changes anything. Run `cppython local schema intent` for the schema.
- A relative `root` resolves against the manifest's directory, and Porringer passes it to CPPython as an absolute path.
- The selection is stored per user, not in the project. Applying a document merges it into the current selection, so running the install again changes nothing.
- The step requires one Python project sync whose installer supports the handoff. Porringer reports an error during planning if no such sync exists.
- Without a `native` section, Porringer leaves the selection alone.

## Project Preparation

A project can request its preparation entry point after its environment is
synchronized and any native local-source intent is applied:

```json
{
  "project": {
    "ecosystem": "python",
    "prepare": true
  }
}
```

The selected project installer supplies the command. For a PDM project this is
`pdm run prepare`; the project defines what its `prepare` entry point does.
Manifests cannot supply arbitrary command arguments. The command runs in the
project root without a shell, and `porr preview` shows it before execution.
The ecosystem must match exactly one project sync, and its installer must
support preparation. Missing or ambiguous ownership is an error. A failed
sync, native handoff, or preparation step fails the project action. Project
scripts can execute code, so review their origin before confirming setup.
Successful results report whether project preparation ran; they do not imply
that the project was built or tested.

Project sync, native handoff, and preparation commands have no time limit by
default. To set a separate limit for each command, pass for example
`porr install --project-timeout 1800`, or set `PORRINGER_PROJECT_TIMEOUT` to a
positive number of seconds. API callers can set
`SetupParameters(project_timeout_seconds=1800)`. If a command times out,
Porringer reports the failed step and does not run later project steps. This
option does not change the timeouts for package, runtime, or SCM actions.

## Inference

Several manifest sections are optional because Porringer infers them from repository metadata and conventions. An explicit manifest entry always overrides the inferred value.

| Inferred | From | Notes |
| --- | --- | --- |
| Tool bootstrap (e.g. `pipx`) | The tool plugin itself | Synthesized only when the resolved tool installer is unavailable; declaring it via `packages` is never required. |
| Project's own tool (e.g. `pdm`) | Project evidence (lock files, `[tool.*]` tables) | Synthesized only when the tool's CLI isn't already available; an explicit `tools` entry pins a version instead. |
| SCM clone | `url`, when it points at a repo page on a known forge | A homepage or docs URL is never mistaken for a repository; declaring `scm` explicitly always wins. |
| Runtime version | `requires-python` in an already-local `pyproject.toml` | Only applies once the project exists on disk; a manifest that clones the project first should still declare `runtimes` explicitly. |

Preview always shows the full inferred plan, including any synthesized steps, so the manifest stays terse while the plan output stays transparent.

When a tool installer is unavailable during validation, Porringer reports a warning if an earlier package phase can schedule a registered bootstrap for its prerequisite. That package installer may itself be deferred; if no bootstrap action can be planned, validation reports an error.

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
| `--with` | Include an optional manifest group, such as `maintainer`. Repeatable. |
| `--json` | Emit one versioned install result on stdout, with preparation metadata when reported. Human output goes to stderr. |
| `--jsonl` | Emit progress events and the final result as newline-delimited JSON. |
| `--record` | Write a replayable JSON run record to the given path. |
| `--yes`, `-y` | Skip the confirmation prompt. |
| `--global` | Install your global manifest instead of a project's. |

## Machine-readable preparation status

Use `porr install --json --yes` for unattended consumers. `--json` does not
change the install steps or their exit codes. Without `--yes`, confirmation
is still required. Human preview, progress, and errors go to stderr; stdout
contains one JSON document. `--json` and `--jsonl` cannot be combined.

The result uses the public `ResultEnvelope` (`schema_version: "2"`,
`operation: "install"`) with summary counts and structured diagnostics.
Its `payload` follows `InstallPayload` version `"1"`:

| Payload field | Meaning |
| --- | --- |
| `schema_version` | `"1"` for this install payload. |
| `outcome` | `completed`, `failed`, or `cancelled`. A declined confirmation remains exit code 0; EOF without confirmation remains a failure. |
| `preparation` | One `PreparationStatus` per returned project action result. Empty when there are no project results, including cancellation before execution. |
| `native_build`, `native_test` | Always `not_verified`. Preparation success is not native build or test success. |

Each preparation entry has `action_id` when available, an `outcome`
(`prepared`, `failed`, `skipped`, or `not_requested`), optional `metadata`,
structured `diagnostics`, and the same explicit native verification limits.
An install without requested preparation uses `not_requested` and null metadata.

Metadata is accepted only from the project-owned preparation step, never
inferred from a manifest, identity name, or directory layout. CPPython's
opt-in preparation report contains:

- `schema_version: "1"`.
- `identity`: the identity actually selected by CPPython, or null for a
  project without an identity.
- `backend`: the selected native build-system/generator, such as `cmake` or
  `meson`, not a Python packaging backend inferred from it.
- `provider`: the selected dependency provider's name, such as `conan`.
- `dependency_outputs`: the provider's actual installation output directories,
  or null when the provider does not expose them. Conan reports its own
  resolved output folders, including identity-specific folders.

Porringer sets `CPPYTHON_PREPARATION_REPORT` to a unique, absolute JSON report
filepath in the project directory, scoped to the preparation subprocess only.
CPPython exclusively creates that file after successful installation.
Porringer reads it only when the preparation subprocess succeeds, then removes
it on success, failure, timeout, or cancellation. Normal stdout and stderr logs
continue streaming unchanged; log contents are never interpreted as metadata.
The project still runs its existing preparation entry point, with its dependency
groups and other options intact.

Older CPPython versions and other preparation scripts can succeed without
creating the report. In that case, metadata is null and a
`preparation.metadata_unavailable` warning is returned. The envelope status is
`partial`, but a successful install still exits 0.

Malformed reports produce `preparation.metadata_invalid`; unreadable reports
produce `preparation.metadata_read_failed`. These are explicit errors that make
the install fail. A preparation entry can still say `prepared` when its
subprocess succeeded but reading or validating the report failed. CPPython
report-write failures propagate as command failures. No report is accepted from
a failed preparation command, and cleanup failures are surfaced rather than
silently leaving a report behind.

The JSON projection excludes raw commands, native intent, subprocess output,
and exception text to avoid exposing credentials. Error codes remain structured;
details remain on stderr. Existing `--record` and `--jsonl` formats are unchanged.

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
