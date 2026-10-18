---
icon: lucide/search-check
---

# Preview Command

Use `porringer preview` to inspect one or more manifests without executing setup actions. Preview loads manifests, resolves plugins, checks package and project state, and reports what Porringer would do.

Preview also works for HTTPS setup profiles and `porringer://` install links. It is the read-only path that UIs, agents, and humans should use before execution.

```shell
porringer preview
porringer preview ./my-project
porringer preview ./my-project --json
porringer preview ./my-project --mode fast --json
porringer preview ./my-project --explain
porringer preview ./my-project --envelope
```

Use complete mode for final diagnostics. Use fast mode for live refresh loops where manifest shape, plugin availability, command previews, and stable action IDs matter more than package presence or update metadata.

## Options

| Option | Description |
| --- | --- |
| `--path`, `-p` | Manifest file or directory containing a manifest. |
| `--project-dir`, `-d` | Working directory for project-install actions. |
| `--strategy`, `-s` | Use `minimal`, `latest`, or `exact` resolution. |
| `--mode` | Use `complete` or `fast` inspection. |
| `--plugin` | Include only actions for selected plugin names. Repeatable. |
| `--only-action` | Include only a stable action ID such as `0:2`. Repeatable. |
| `--json` | Emit machine-readable JSON. |
| `--envelope` | Emit a common result envelope as JSON. |
| `--explain` | Render diagnostics and follow-up actions for humans. |
| `--global` | Preview your global manifest instead of a project's. |

## Inspection Modes

| Mode | Use when | What it does |
| --- | --- | --- |
| `complete` | You need final validation before execution. | Checks package presence, update status, extras, SCM presence, diagnostics, and command previews. |
| `fast` | You need low-latency UI or agent refreshes. | Skips slower probes while preserving the same report shape, action IDs, plugin availability, and command previews. |

Fast reports and complete reports share the same JSON shape. A client can render fast previews during editing, then run complete preview when the user asks for evidence or is about to execute.

Fast mode never probes presence, so it cannot tell "needed" apart from "not checked". Actions that would be `needed` under complete inspection instead report `unknown`. Treat `unknown` as "not yet known", not as "run this". Never present a fast report as the basis for a confirmation prompt; always run `complete` inspection immediately before asking a human (or executing) to act.

## JSON Output

`--json` emits a `SyncInspectionReport`:

```shell
porringer preview ./my-project --json
```

The report includes:

- `schema_version` and `operation`.
- Top-level `status` and `success` fields.
- Manifest results and failed paths.
- Discovered plugin availability.
- Summary counts.
- Typed `diagnostics`.
- `follow_up_actions` for safe next steps.

Each action includes:

| Field | Meaning |
| --- | --- |
| `index` | Display order in the filtered report. |
| `ref` | Stable identity with manifest index, action index, and action ID. |
| `action_id` | Compact correlation key such as `0:3`. |
| `status` | `needed`, `satisfied`, `update_available`, `unavailable`, `failed`, `skipped`, or `unknown`. |
| `action` | JSON-stable action data such as kind, ecosystem, installer, package, and command. Actions from a manifest entry also carry `entry_id` (see below). |
| `cli_command` | Native command preview when one can be rendered. |
| `cli_steps` | Every command step that execution will run, in order. For a one-step action this contains `cli_command`; multi-step project tools can include setup steps before the final install command. |
| `message` | Presence, update, skip, or error context. |

The report `success` property is false when manifest loading fails, an action fails inspection, or an action is unavailable.

`action_id` is a position, so it changes when the manifest changes. `entry_id` identifies the manifest entry itself: it is derived from the manifest file, kind, ecosystem, package name, `installer` and `runtime`, so it stays the same when the constraint or description changes or entries are reordered. Actions Porringer adds on its own (bootstraps, inferred project installs) have no `entry_id`.

## Diagnostics and Follow-up Actions

Diagnostics use stable codes such as:

- `manifest.load_failed`
- `manifest.unknown_plugin`
- `action.installer_unavailable`
- `action.failed`
- `package.update_available`

Each diagnostic can identify a target manifest, action, plugin, or package, and may include a remediation.

Follow-up actions are safe affordances for humans and machines. They carry a label, kind, target, optional command preview, and optional `action_id`. A GUI can render them as buttons. A CLI or agent can ask for confirmation and then run a selected action ID.

## Envelopes and Explanation

`--envelope` wraps the inspection report in a `ResultEnvelope` with a correlation ID, timestamps, summary, diagnostics, follow-up actions, and the raw report payload. Use it when a caller wants one consistent final-result shape across inspect, sync, project, tool, and profile operations.

`--explain` renders the same structured diagnostics and follow-up actions as a short human explanation.

## Relation to Install

`preview` is read-only. `install` executes.

Downstream tools should prefer `porringer preview --json` when they need to render a plan, validate a manifest, or explain why an action cannot run. Runtime progress events from `api.sync.run(..., on_event=...)` use the same action identity contract as preview reports.

The same `action_id` appears in preview reports, progress event snapshots, and command trace artifacts. Downstream tools can correlate inspect rows, live execution updates, and trace evidence without matching on descriptions or native command strings.

See the [downstream client pattern](install.md#downstream-client-pattern) for the recommended long-lived client loop.
