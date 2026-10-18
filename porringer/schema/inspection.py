"""Data models and schemas for inspection.

Inspection report schemas for manifest previews and diagnostics.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import Field

from porringer.core.schema import PorringerModel
from porringer.schema.execution import InspectionMode
from porringer.schema.observability import (
    SCHEMA_VERSION,
    ActionRef,
    Diagnostic,
    FollowUpAction,
    ResultStatus,
)


class InspectionStatus(StrEnum):
    """Frontend-oriented status for an inspected setup action."""

    NEEDED = 'needed'
    SATISFIED = 'satisfied'
    UPDATE_AVAILABLE = 'update_available'
    UNAVAILABLE = 'unavailable'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    UNKNOWN = 'unknown'
    """Presence/update was never probed (``InspectionMode.FAST``). Distinct
    from :attr:`NEEDED`, which means presence was checked and the action is
    genuinely absent. Never rely on ``UNKNOWN`` implying an action should run.
    """


class ActionSnapshot(PorringerModel):
    """JSON-stable projection of a setup action."""

    index: int
    ref: ActionRef | None = None
    action_id: str | None = None
    manifest_index: int | None = None
    action_index: int | None = None
    description: str
    kind: str | None = None
    ecosystem: str | None = None
    installer: str | None = None
    package: str | None = None
    package_name: str | None = None
    package_constraint: str | None = None
    package_description: str | None = None
    include_prereleases: bool = False
    runtime_tag: str | None = None
    entry_id: str | None = None


class ActionInspection(PorringerModel):
    """Inspection result for one setup action."""

    index: int
    ref: ActionRef
    action_id: str
    manifest_index: int
    action_index: int
    action: ActionSnapshot
    status: InspectionStatus
    cli_command: tuple[str, ...] = ()
    cli_steps: tuple[tuple[str, ...], ...] = ()
    """Every command step execution will run, in order.

    Most actions run one command, so this is a one-element tuple
    matching `cli_command`. Multi-step actions (e.g. Poetry's separate
    `poetry env use` before `poetry install`) list every step, so a
    renderer can show the full plan instead of only the final command.
    """
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    message: str | None = None
    installed_version: str | None = None
    available_version: str | None = None


class ManifestDiagnosticSnapshot(PorringerModel):
    """JSON-stable projection of a manifest diagnostic."""

    field: str
    message: str
    code: str
    severity: str


class ManifestMetadataSnapshot(PorringerModel):
    """JSON-stable projection of manifest display metadata."""

    name: str | None = None
    description: str | None = None
    author: str | None = None
    url: str | None = None


class ManifestInspection(PorringerModel):
    """Inspection report for one manifest."""

    index: int
    manifest_path: Path | None = None
    root_directory: Path | None = None
    metadata: ManifestMetadataSnapshot | None = None
    preferences: dict[str, str] = Field(default_factory=dict)
    diagnostics: tuple[ManifestDiagnosticSnapshot, ...] = Field(default_factory=tuple)
    actions: tuple[ActionInspection, ...] = Field(default_factory=tuple)


class FailedPathInspection(PorringerModel):
    """Manifest path that could not be inspected."""

    path: Path
    error: str
    manifest_index: int | None = None


class DiscoveredPluginSnapshot(PorringerModel):
    """JSON-stable projection of a discovered plugin entry."""

    name: str
    available: bool
    capabilities: tuple[str, ...] = Field(default_factory=tuple)
    kind: str


class InspectionSummary(PorringerModel):
    """Aggregate counts for an inspection report."""

    manifests: int = 0
    failed_paths: int = 0
    actions: int = 0
    needed: int = 0
    satisfied: int = 0
    update_available: int = 0
    unavailable: int = 0
    failed: int = 0
    skipped: int = 0
    unknown: int = 0


class SyncInspectionReport(PorringerModel):
    """Structured read-only report for a sync inspection."""

    schema_version: str = SCHEMA_VERSION
    operation: str = 'sync.inspect'
    status: ResultStatus = ResultStatus.SUCCESS
    inspection_mode: InspectionMode = InspectionMode.COMPLETE
    manifests: tuple[ManifestInspection, ...] = Field(default_factory=tuple)
    failed_paths: tuple[FailedPathInspection, ...] = Field(default_factory=tuple)
    plugins: tuple[DiscoveredPluginSnapshot, ...] = Field(default_factory=tuple)
    summary: InspectionSummary = Field(default_factory=InspectionSummary)
    diagnostics: tuple[Diagnostic, ...] = Field(default_factory=tuple)
    follow_up_actions: tuple[FollowUpAction, ...] = Field(default_factory=tuple)

    @property
    def success(self) -> bool:
        """Whether inspection completed without failed paths or failed actions."""
        return self.summary.failed_paths == 0 and self.summary.failed == 0 and self.summary.unavailable == 0
