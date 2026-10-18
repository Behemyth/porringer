"""Shared observability schemas for reports, events, and traces."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from porringer.core.schema import PorringerModel

SCHEMA_VERSION = '2'


def action_id_for(manifest_index: int, action_index: int) -> str:
    """Return the stable action id for a manifest/action position."""
    return f'{manifest_index}:{action_index}'


class ActionRef(PorringerModel):
    """Stable identity for one setup action within a manifest batch.

    Manifest indexes refer to the resolved input path position before
    failed paths are removed. Action indexes refer to the original manifest
    action position before plugin, package, action-kind, or action-id filters
    can narrow the action list.
    """

    manifest_index: int = Field(ge=0, description='Resolved input path index for the manifest.')
    action_index: int = Field(ge=0, description='Original source action index within the manifest.')
    action_id: str = Field(description='Compact ``<manifest-index>:<action-index>`` selector.')

    @model_validator(mode='after')
    def _validate_action_id(self) -> Self:
        """Ensure the compact id matches the structured indexes."""
        expected = action_id_for(self.manifest_index, self.action_index)
        if self.action_id != expected:
            msg = f'action_id must be {expected!r}'
            raise ValueError(msg)
        return self

    @classmethod
    def from_indices(cls, manifest_index: int, action_index: int) -> ActionRef:
        """Build an action reference from manifest/action indexes."""
        return cls(
            manifest_index=manifest_index,
            action_index=action_index,
            action_id=action_id_for(manifest_index, action_index),
        )


class ResultStatus(StrEnum):
    """High-level result status shared by reports and envelopes."""

    SUCCESS = 'success'
    PARTIAL = 'partial'
    FAILED = 'failed'


class DiagnosticSeverity(StrEnum):
    """Machine-readable diagnostic severity."""

    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'


class ActionRisk(StrEnum):
    """Risk category for an available follow-up action."""

    LOW = 'low'
    NORMAL = 'normal'
    ELEVATED = 'elevated'
    DESTRUCTIVE = 'destructive'


class DiagnosticTarget(PorringerModel):
    """Stable target for a diagnostic or follow-up action."""

    kind: Literal['batch', 'manifest', 'project', 'action', 'plugin', 'package', 'profile']
    path: Path | None = None
    manifest_index: int | None = None
    action_ref: ActionRef | None = None
    action_id: str | None = None
    plugin: str | None = None
    package: str | None = None


class Remediation(PorringerModel):
    """Suggested remediation for a diagnostic."""

    label: str
    command: tuple[str, ...] | None = None
    action_id: str | None = None


class Diagnostic(PorringerModel):
    """Typed diagnostic suitable for humans, GUIs, CLIs, and agents."""

    code: str
    severity: DiagnosticSeverity
    message: str
    target: DiagnosticTarget | None = None
    cause: str | None = None
    remediation: Remediation | None = None


class FollowUpAction(PorringerModel):
    """A concrete follow-up action exposed by a report."""

    label: str
    kind: str
    target: DiagnosticTarget | None = None
    action_id: str | None = None
    command: tuple[str, ...] | None = None
    requires_confirmation: bool = False
    risk: ActionRisk = ActionRisk.NORMAL


class ResultSummary(PorringerModel):
    """Common summary counts for operation envelopes."""

    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    needs_user_action: int = 0
    warnings: int = 0
    counts: dict[str, int] = Field(default_factory=dict)


class ResultEnvelope(PorringerModel):
    """Stable envelope for final operation results."""

    schema_version: str = SCHEMA_VERSION
    event_type: Literal['result'] = 'result'
    operation: str
    status: ResultStatus
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    started_at: datetime | None = None
    ended_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    summary: ResultSummary = Field(default_factory=ResultSummary)
    diagnostics: tuple[Diagnostic, ...] = Field(default_factory=tuple)
    follow_up_actions: tuple[FollowUpAction, ...] = Field(default_factory=tuple)
    payload: dict[str, Any] = Field(default_factory=dict)


class ReplayRecord(PorringerModel):
    """Replayable record of an observable operation."""

    schema_version: str = SCHEMA_VERSION
    operation: str
    correlation_id: str
    started_at: datetime
    ended_at: datetime
    parameters: dict[str, Any] = Field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    result: ResultEnvelope
    trace_paths: tuple[Path, ...] = Field(default_factory=tuple)
