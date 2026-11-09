"""Round-trip serialization tests for machine-output schemas.

These tests verify that porringer's public JSON outputs (ResultEnvelope,
SyncInspectionReport, progress snapshots) can be round-tripped through
model_dump(mode='json') → model_validate() without data loss or type
errors.  This forms the downstream serialization stability contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from porringer.schema.inspection import (
    ActionInspection,
    ActionSnapshot,
    FailedPathInspection,
    InspectionStatus,
    InspectionSummary,
    ManifestInspection,
    SyncInspectionReport,
)
from porringer.schema.observability import (
    ActionRef,
    ActionRisk,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticTarget,
    FollowUpAction,
    Remediation,
    ResultEnvelope,
    ResultStatus,
    ResultSummary,
)
from porringer.schema.progress import (
    FailedPathProgressSnapshot,
    ManifestProgressSnapshot,
    ProgressEventSnapshot,
    SetupActionResultSnapshot,
)

_TOTAL_ACTIONS = 3
_MANIFEST_INDEX = 2
_ACTION_INDEX = 7
_MANIFEST_ACTIONS = 5


def _roundtrip[T: BaseModel](model: T) -> T:
    """Serialize to JSON dict and back through model_validate."""
    raw = model.model_dump(mode='json')
    # Verify the dump is JSON-encodable (no non-serializable objects).
    json.dumps(raw)
    return model.model_validate(raw)


class TestResultEnvelopeRoundTrip:
    """ResultEnvelope is the primary machine-readable output contract."""

    @staticmethod
    def test_minimal_envelope() -> None:
        """Empty success envelope survives a round-trip."""
        envelope = ResultEnvelope(
            operation='sync.preview',
            status=ResultStatus.SUCCESS,
        )
        rt = _roundtrip(envelope)
        assert rt.operation == 'sync.preview'
        assert rt.status == ResultStatus.SUCCESS
        assert rt.schema_version == envelope.schema_version
        assert rt.correlation_id == envelope.correlation_id

    @staticmethod
    def test_envelope_with_diagnostics() -> None:
        """Diagnostics with all optional fields survive a round-trip."""
        diagnostic = Diagnostic(
            code='unknown_plugin',
            severity=DiagnosticSeverity.WARNING,
            message='Plugin xyz not found',
            target=DiagnosticTarget(kind='plugin', plugin='xyz'),
            cause='ImportError: no module named xyz',
            remediation=Remediation(label='Install xyz', command=('pip', 'install', 'xyz')),
        )
        envelope = ResultEnvelope(
            operation='sync.run',
            status=ResultStatus.PARTIAL,
            summary=ResultSummary(total=3, succeeded=2, failed=1),
            diagnostics=(diagnostic,),
            follow_up_actions=(
                FollowUpAction(
                    label='Retry failed action',
                    kind='retry',
                    action_id='0:2',
                    risk=ActionRisk.NORMAL,
                    requires_confirmation=False,
                ),
            ),
            payload={'manifest_count': 1},
        )
        rt = _roundtrip(envelope)
        assert len(rt.diagnostics) == 1
        assert rt.diagnostics[0].code == 'unknown_plugin'
        assert rt.diagnostics[0].remediation is not None
        assert rt.diagnostics[0].remediation.command == ('pip', 'install', 'xyz')
        assert len(rt.follow_up_actions) == 1
        assert rt.summary.total == _TOTAL_ACTIONS

    @staticmethod
    def test_envelope_timestamps_are_timezone_aware() -> None:
        """Timestamps survive round-trip as timezone-aware datetimes."""
        envelope = ResultEnvelope(
            operation='sync.preview',
            status=ResultStatus.SUCCESS,
            started_at=datetime.now(UTC),
        )
        rt = _roundtrip(envelope)
        assert rt.ended_at.tzinfo is not None
        assert rt.started_at is not None
        assert rt.started_at.tzinfo is not None


class TestActionRefRoundTrip:
    """ActionRef is embedded in almost every output schema."""

    @staticmethod
    def test_from_indices_round_trips() -> None:
        """ActionRef built from indices is stable through serialization."""
        ref = ActionRef.from_indices(_MANIFEST_INDEX, _ACTION_INDEX)
        rt = _roundtrip(ref)
        assert rt.manifest_index == _MANIFEST_INDEX
        assert rt.action_index == _ACTION_INDEX
        assert rt.action_id == f'{_MANIFEST_INDEX}:{_ACTION_INDEX}'


class TestSyncInspectionReportRoundTrip:
    """SyncInspectionReport is the primary output of sync.inspect."""

    @staticmethod
    def test_empty_report() -> None:
        """A minimal inspection report without actions survives a round-trip."""
        report = SyncInspectionReport()
        rt = _roundtrip(report)
        assert rt.operation == 'sync.inspect'
        assert rt.summary.actions == 0

    @staticmethod
    def test_report_with_actions() -> None:
        """Action inspections with all status variants survive a round-trip."""
        ref = ActionRef.from_indices(0, 0)
        action = ActionSnapshot(
            index=0,
            ref=ref,
            action_id='0:0',
            manifest_index=0,
            action_index=0,
            description='Install requests',
            kind='package',
            ecosystem='python',
            installer='uv',
            package='requests',
            package_name='requests',
        )
        inspection = ActionInspection(
            index=0,
            ref=ref,
            action_id='0:0',
            manifest_index=0,
            action_index=0,
            action=action,
            status=InspectionStatus.NEEDED,
            cli_command=('uv', 'pip', 'install', 'requests'),
            success=True,
        )
        manifest = ManifestInspection(
            index=0,
            actions=(inspection,),
        )
        report = SyncInspectionReport(
            summary=InspectionSummary(manifests=1, actions=1, needed=1),
            manifests=(manifest,),
        )
        rt = _roundtrip(report)
        assert rt.manifests[0].actions[0].action_id == '0:0'
        assert rt.manifests[0].actions[0].status == InspectionStatus.NEEDED
        assert rt.manifests[0].actions[0].action.ecosystem == 'python'

    @staticmethod
    def test_failed_path_survives_round_trip() -> None:
        """FailedPathInspection preserves the error message."""
        report = SyncInspectionReport(
            failed_paths=(FailedPathInspection(path=Path('/tmp/missing'), error='no manifest found'),),
            summary=InspectionSummary(failed_paths=1),
        )
        rt = _roundtrip(report)
        assert rt.failed_paths[0].error == 'no manifest found'


class TestProgressSnapshotRoundTrip:
    """Progress snapshots are emitted during streaming operations."""

    @staticmethod
    def test_action_result_snapshot() -> None:
        """SetupActionResultSnapshot survives a round-trip."""
        snapshot = SetupActionResultSnapshot(
            success=True,
            project_prepared=True,
        )
        rt = _roundtrip(snapshot)
        assert rt.success is True
        assert rt.project_prepared is True

    @staticmethod
    def test_progress_event_snapshot() -> None:
        """ProgressEventSnapshot with a manifest snapshot survives a round-trip."""
        manifest_snapshot = ManifestProgressSnapshot(manifest_index=0, actions=_MANIFEST_ACTIONS)
        event = ProgressEventSnapshot(
            event_type='manifest_loaded',
            manifest=manifest_snapshot,
        )
        rt = _roundtrip(event)
        assert rt.manifest is not None
        assert rt.manifest.actions == _MANIFEST_ACTIONS

    @staticmethod
    def test_failed_path_snapshot() -> None:
        """FailedPathProgressSnapshot preserves the error message."""
        snapshot = FailedPathProgressSnapshot(path=Path('/missing'), error='manifest not found')
        rt = _roundtrip(snapshot)
        assert rt.error == 'manifest not found'

    @staticmethod
    def test_action_progress_snapshot() -> None:
        """ActionProgressSnapshot with a result survives a round-trip."""
        result = SetupActionResultSnapshot(success=False, message='network error')
        ref = ActionRef.from_indices(0, 1)
        event = ProgressEventSnapshot(
            event_type='action_completed',
            action_ref=ref,
            action_id='0:1',
            result=result,
        )
        rt = _roundtrip(event)
        assert rt.action_id == '0:1'
        assert rt.result is not None
        assert rt.result.message == 'network error'
