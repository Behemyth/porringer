"""Progress event schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from porringer.core.schema import PluginKind, PorringerModel
from porringer.schema.execution import SetupAction, SetupActionResult, SetupResults
from porringer.schema.inspection import (
    ActionInspection,
    ActionSnapshot,
    DiscoveredPluginSnapshot,
)
from porringer.schema.observability import SCHEMA_VERSION, ActionRef
from porringer.schema.plugin import PluginCapability


@dataclass(slots=True)
class ActionProgress:
    """Fine-grained progress update from within a plugin operation.

    Plugins emit these to report phases and percentages during long-running
    operations (e.g., downloading a wheel, verifying checksums).

    Args:
        action: The parent setup action this progress belongs to.
        phase: Current phase (e.g. `"downloading"`, `"installing"`, `"verifying"`).
        progress: 0.0–1.0 completion fraction, or `None` if indeterminate.
        message: Human-readable status line (e.g. `"Downloading ruff-0.8.0.whl (2.1 MB)"`).
        output: Raw output line from the subprocess, for log panel display.
        channel: Which subprocess channel the output came from (`"stdout"` or `"stderr"`).
            step_index: One-based project command step number, when applicable.
            step_total: Total project command steps, when applicable.
            command: The argv for the active project command step, when applicable.
    """

    action: SetupAction
    phase: str
    progress: float | None = None
    message: str | None = None
    output: str | None = None
    channel: Literal['stdout', 'stderr'] | None = None
    step_index: int | None = None
    step_total: int | None = None
    command: tuple[str, ...] | None = None


@dataclass(slots=True, frozen=True)
class DiscoveredPluginEntry:
    """A single discovered plugin with availability and capabilities.

    Args:
        name: The canonical plugin name.
        available: Whether the plugin's tool binary is on PATH.
        capabilities: Protocol capabilities the plugin implements.
        kind: The plugin kind (package, tool, runtime, etc.).
    """

    name: str
    available: bool
    capabilities: frozenset[PluginCapability]
    kind: PluginKind


# ---------------------------------------------------------------------------
# Discriminated progress-event union
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ManifestLoadedEvent:
    """Emitted when a manifest has been fully resolved and is ready for execution."""

    manifest: SetupResults
    manifest_index: int | None = None


@dataclass(slots=True)
class ManifestFailedEvent:
    """Emitted when a manifest path could not be loaded."""

    failed_path: tuple[Path, str]
    manifest_index: int | None = None


@dataclass(slots=True)
class PluginsDiscoveredEvent:
    """Emitted once per batch with the full plugin availability map."""

    discovered_plugins: tuple[DiscoveredPluginEntry, ...]


@dataclass(slots=True)
class ActionStartedEvent:
    """Emitted when an action begins execution."""

    action: SetupAction
    action_ref: ActionRef | None = None
    action_index: int | None = None

    def __post_init__(self) -> None:
        """Keep legacy action_index populated when an action ref is available."""
        if self.action_index is None and self.action_ref is not None:
            self.action_index = self.action_ref.action_index


@dataclass(slots=True)
class ActionCompletedEvent:
    """Emitted when an action finishes execution."""

    action: SetupAction
    result: SetupActionResult
    action_ref: ActionRef | None = None
    action_index: int | None = None

    def __post_init__(self) -> None:
        """Keep legacy action_index populated when an action ref is available."""
        if self.action_index is None and self.action_ref is not None:
            self.action_index = self.action_ref.action_index


@dataclass(slots=True)
class ActionProgressEvent:
    """Emitted for fine-grained progress within an action."""

    action: SetupAction
    progress: ActionProgress
    action_ref: ActionRef | None = None


@dataclass(slots=True)
class ActionInspectedEvent:
    """Emitted during inspection as soon as one action's checks finish.

    Carries the typed action and result alongside the stable inspection
    record, so a client can fill in a row without waiting for the whole
    report.
    """

    action: SetupAction
    result: SetupActionResult
    inspection: ActionInspection

    @property
    def action_ref(self) -> ActionRef:
        """Stable reference of the inspected action."""
        return self.inspection.ref


class SetupActionResultSnapshot(PorringerModel):
    """JSON-stable projection of an action result."""

    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    message: str | None = None
    installed_version: str | None = None
    available_version: str | None = None
    cli_command: tuple[str, ...] = Field(default_factory=tuple)
    duration_seconds: float | None = None
    cli_steps: tuple[tuple[str, ...], ...] = Field(default_factory=tuple)
    failed_step_index: int | None = None


class ActionProgressSnapshot(PorringerModel):
    """JSON-stable projection of fine-grained action progress."""

    phase: str
    progress: float | None = None
    message: str | None = None
    output: str | None = None
    channel: Literal['stdout', 'stderr'] | None = None
    step_index: int | None = None
    step_total: int | None = None
    command: tuple[str, ...] | None = None


class ManifestProgressSnapshot(PorringerModel):
    """JSON-stable projection of a loaded manifest progress event."""

    manifest_index: int | None = None
    manifest_path: Path | None = None
    root_directory: Path | None = None
    actions: int = 0


class FailedPathProgressSnapshot(PorringerModel):
    """JSON-stable projection of a failed manifest path event."""

    manifest_index: int | None = None
    path: Path
    error: str


class ProgressEventSnapshot(PorringerModel):
    """Stable JSON envelope for progress events."""

    schema_version: str = SCHEMA_VERSION
    event_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = None
    action_ref: ActionRef | None = None
    action_id: str | None = None
    action: ActionSnapshot | None = None
    result: SetupActionResultSnapshot | None = None
    action_progress: ActionProgressSnapshot | None = None
    manifest: ManifestProgressSnapshot | None = None
    failed_path: FailedPathProgressSnapshot | None = None
    discovered_plugins: tuple[DiscoveredPluginSnapshot, ...] = Field(default_factory=tuple)
    inspection: ActionInspection | None = None


def _snapshot_action(action: SetupAction, action_ref: ActionRef | None, action_index: int | None) -> ActionSnapshot:
    """Project a setup action into the shared JSON action shape."""
    package = str(action.package) if action.package is not None else None
    index = action_ref.action_index if action_ref is not None else action_index or 0
    return ActionSnapshot(
        index=index,
        ref=action_ref,
        action_id=action_ref.action_id if action_ref is not None else None,
        manifest_index=action_ref.manifest_index if action_ref is not None else None,
        action_index=action_ref.action_index if action_ref is not None else action_index,
        description=action.description,
        kind=action.kind.value if action.kind is not None else None,
        ecosystem=str(action.ecosystem) if action.ecosystem is not None else None,
        installer=action.installer,
        package=package,
        package_name=action.package.name if action.package is not None else None,
        package_constraint=action.package.constraint if action.package is not None else None,
        package_description=action.package_description,
        include_prereleases=action.include_prereleases,
        runtime_tag=action.runtime_tag,
        entry_id=action.entry_id,
    )


def _snapshot_result(result: SetupActionResult) -> SetupActionResultSnapshot:
    """Project an action result into the stable JSON result shape."""
    return SetupActionResultSnapshot(
        success=result.success,
        skipped=result.skipped,
        skip_reason=result.skip_reason.name if result.skip_reason is not None else None,
        message=result.message,
        installed_version=result.installed_version,
        available_version=result.available_version,
        cli_command=result.cli_command or (),
        duration_seconds=result.duration_seconds,
        cli_steps=result.cli_steps or (),
        failed_step_index=result.failed_step_index,
    )


def progress_event_snapshot(event: ProgressEvent, *, correlation_id: str | None = None) -> ProgressEventSnapshot:
    """Convert a runtime progress event to a stable JSON snapshot."""
    if isinstance(event, ManifestLoadedEvent):
        return ProgressEventSnapshot(
            event_type='manifest_loaded',
            correlation_id=correlation_id,
            manifest=ManifestProgressSnapshot(
                manifest_index=event.manifest_index,
                manifest_path=event.manifest.manifest_path,
                root_directory=event.manifest.root_directory,
                actions=len(event.manifest.actions),
            ),
        )
    if isinstance(event, ManifestFailedEvent):
        path, error = event.failed_path
        return ProgressEventSnapshot(
            event_type='manifest_failed',
            correlation_id=correlation_id,
            failed_path=FailedPathProgressSnapshot(manifest_index=event.manifest_index, path=path, error=error),
        )
    if isinstance(event, PluginsDiscoveredEvent):
        return ProgressEventSnapshot(
            event_type='plugins_discovered',
            correlation_id=correlation_id,
            discovered_plugins=tuple(
                DiscoveredPluginSnapshot(
                    name=entry.name,
                    available=entry.available,
                    capabilities=tuple(sorted(capability.name.lower() for capability in entry.capabilities)),
                    kind=entry.kind.value,
                )
                for entry in event.discovered_plugins
            ),
        )
    return _action_event_snapshot(event, correlation_id)


def _action_event_snapshot(
    event: ActionStartedEvent | ActionCompletedEvent | ActionInspectedEvent | ActionProgressEvent,
    correlation_id: str | None,
) -> ProgressEventSnapshot:
    """Convert a per-action progress event to a stable JSON snapshot."""
    if isinstance(event, ActionStartedEvent):
        return ProgressEventSnapshot(
            event_type='action_started',
            correlation_id=correlation_id,
            action_ref=event.action_ref,
            action_id=event.action_ref.action_id if event.action_ref is not None else None,
            action=_snapshot_action(event.action, event.action_ref, event.action_index),
        )
    if isinstance(event, ActionCompletedEvent):
        return ProgressEventSnapshot(
            event_type='action_completed',
            correlation_id=correlation_id,
            action_ref=event.action_ref,
            action_id=event.action_ref.action_id if event.action_ref is not None else None,
            action=_snapshot_action(event.action, event.action_ref, event.action_index),
            result=_snapshot_result(event.result),
        )
    if isinstance(event, ActionInspectedEvent):
        return ProgressEventSnapshot(
            event_type='action_inspected',
            correlation_id=correlation_id,
            action_ref=event.action_ref,
            action_id=event.action_ref.action_id,
            action=event.inspection.action,
            inspection=event.inspection,
        )
    return ProgressEventSnapshot(
        event_type='action_progress',
        correlation_id=correlation_id,
        action_ref=event.action_ref,
        action_id=event.action_ref.action_id if event.action_ref is not None else None,
        action=_snapshot_action(event.action, event.action_ref, None),
        action_progress=ActionProgressSnapshot(
            phase=event.progress.phase,
            progress=event.progress.progress,
            message=event.progress.message,
            output=event.progress.output,
            channel=event.progress.channel,
            step_index=event.progress.step_index,
            step_total=event.progress.step_total,
            command=event.progress.command,
        ),
    )


type ProgressEvent = (
    ManifestLoadedEvent
    | ManifestFailedEvent
    | PluginsDiscoveredEvent
    | ActionStartedEvent
    | ActionCompletedEvent
    | ActionProgressEvent
    | ActionInspectedEvent
)
