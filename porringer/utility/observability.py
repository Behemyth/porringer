"""Helpers for machine-readable and human-readable operation results."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from porringer.schema import (
    ActionInspection,
    ActionRef,
    ActionRisk,
    BatchSetupResults,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticTarget,
    FollowUpAction,
    InspectionStatus,
    Remediation,
    ReplayRecord,
    ResultEnvelope,
    ResultStatus,
    ResultSummary,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SyncInspectionReport,
)

logger = logging.getLogger(__name__)


def result_status(success: bool, diagnostics: tuple[Diagnostic, ...] = ()) -> ResultStatus:
    """Return an envelope status from success and diagnostic severity."""
    if not success or any(diagnostic.severity == DiagnosticSeverity.ERROR for diagnostic in diagnostics):
        return ResultStatus.FAILED
    if any(diagnostic.severity == DiagnosticSeverity.WARNING for diagnostic in diagnostics):
        return ResultStatus.PARTIAL
    return ResultStatus.SUCCESS


def inspection_diagnostics(report: SyncInspectionReport) -> tuple[Diagnostic, ...]:
    """Build typed diagnostics for an inspection report."""
    diagnostics: list[Diagnostic] = []

    for failed in report.failed_paths:
        diagnostics.append(
            Diagnostic(
                code='manifest.load_failed',
                severity=DiagnosticSeverity.ERROR,
                message=f'Failed to load manifest: {failed.error}',
                target=DiagnosticTarget(
                    kind='manifest',
                    path=failed.path,
                    manifest_index=failed.manifest_index,
                ),
                cause=failed.error,
            )
        )

    for manifest in report.manifests:
        for manifest_diagnostic in manifest.diagnostics:
            severity = DiagnosticSeverity(manifest_diagnostic.severity)
            diagnostics.append(
                Diagnostic(
                    code=f'manifest.{manifest_diagnostic.code.lower()}',
                    severity=severity,
                    message=manifest_diagnostic.message,
                    target=DiagnosticTarget(
                        kind='manifest',
                        path=manifest.manifest_path,
                        manifest_index=manifest.index,
                    ),
                )
            )

        for action in manifest.actions:
            diagnostics.extend(_action_diagnostics(action))

    return tuple(diagnostics)


def inspection_follow_up_actions(
    report: SyncInspectionReport,
) -> tuple[FollowUpAction, ...]:
    """Build follow-up actions for an inspection report."""
    actions: list[FollowUpAction] = []
    for manifest in report.manifests:
        for action in manifest.actions:
            if action.status == InspectionStatus.NEEDED:
                actions.append(_follow_up_run_action(action, 'Run action', 'run_action'))
            elif action.status == InspectionStatus.UPDATE_AVAILABLE:
                actions.append(_follow_up_run_action(action, 'Upgrade package', 'upgrade_action'))
            elif action.status == InspectionStatus.FAILED:
                actions.append(_follow_up_run_action(action, 'Retry action', 'retry_action'))
    return tuple(actions)


def inspection_result_summary(report: SyncInspectionReport) -> ResultSummary:
    """Convert an inspection summary to the shared envelope summary."""
    summary = report.summary
    return ResultSummary(
        total=summary.actions,
        succeeded=summary.satisfied,
        skipped=summary.skipped,
        failed=summary.failed + summary.failed_paths + summary.unavailable,
        needs_user_action=summary.needed + summary.update_available,
        warnings=summary.skipped,
        counts={
            'manifests': summary.manifests,
            'failed_paths': summary.failed_paths,
            'needed': summary.needed,
            'satisfied': summary.satisfied,
            'update_available': summary.update_available,
            'unavailable': summary.unavailable,
            'skipped': summary.skipped,
            'unknown': summary.unknown,
        },
    )


def inspection_envelope(
    report: SyncInspectionReport,
    *,
    operation: str = 'sync.inspect',
    correlation_id: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> ResultEnvelope:
    """Wrap an inspection report in a result envelope."""
    diagnostics = report.diagnostics or inspection_diagnostics(report)
    follow_up_actions = report.follow_up_actions or inspection_follow_up_actions(report)
    return ResultEnvelope(
        operation=operation,
        status=result_status(report.success, diagnostics),
        correlation_id=correlation_id or str(uuid4()),
        started_at=started_at,
        ended_at=ended_at or datetime.now(UTC),
        summary=inspection_result_summary(report),
        diagnostics=diagnostics,
        follow_up_actions=follow_up_actions,
        payload=report.model_dump(mode='json'),
    )


def batch_diagnostics(batch: BatchSetupResults) -> tuple[Diagnostic, ...]:
    """Build typed diagnostics for collected execution results."""
    diagnostics: list[Diagnostic] = []
    for path, error in batch.failed_paths:
        diagnostics.append(
            Diagnostic(
                code='manifest.load_failed',
                severity=DiagnosticSeverity.ERROR,
                message=f'Failed to load manifest: {error}',
                target=DiagnosticTarget(kind='manifest', path=path),
                cause=error,
            )
        )

    for fallback_index, manifest in enumerate(batch.manifest_results):
        manifest_index = _manifest_index(manifest, fallback_index)
        action_refs = _action_refs(manifest.actions, manifest.action_indices, manifest_index)
        ref_by_action_id = {id(action): ref for action, ref in action_refs}
        for result in manifest.results:
            if result.success:
                continue
            ref = _ref_for_result(result, action_refs, ref_by_action_id)
            diagnostics.append(
                Diagnostic(
                    code='action.failed',
                    severity=DiagnosticSeverity.ERROR,
                    message=result.message or result.action.description,
                    target=_target_for_result(result, ref),
                    cause=result.message,
                    remediation=Remediation(
                        label='Retry this action',
                        action_id=ref.action_id if ref else None,
                    ),
                )
            )
    return tuple(diagnostics)


def batch_follow_up_actions(batch: BatchSetupResults) -> tuple[FollowUpAction, ...]:
    """Build follow-up actions for collected execution results."""
    actions: list[FollowUpAction] = []
    for fallback_index, manifest in enumerate(batch.manifest_results):
        manifest_index = _manifest_index(manifest, fallback_index)
        action_refs = _action_refs(manifest.actions, manifest.action_indices, manifest_index)
        ref_by_action_id = {id(action): ref for action, ref in action_refs}
        for result in manifest.results:
            if result.success:
                continue
            ref = _ref_for_result(result, action_refs, ref_by_action_id)
            actions.append(
                FollowUpAction(
                    label='Retry action',
                    kind='retry_action',
                    target=_target_for_result(result, ref),
                    action_id=ref.action_id if ref else None,
                    command=_command_for_result(result),
                )
            )
    return tuple(actions)


def batch_result_summary(batch: BatchSetupResults) -> ResultSummary:
    """Convert collected execution results to a shared envelope summary."""
    return ResultSummary(
        total=batch.total_actions,
        succeeded=batch.total_succeeded,
        skipped=batch.total_skipped,
        failed=batch.total_failed + len(batch.failed_paths),
        needs_user_action=batch.total_failed + len(batch.failed_paths),
        counts={
            'manifests': len(batch.manifest_results),
            'failed_paths': len(batch.failed_paths),
            'actions': batch.total_actions,
        },
    )


def batch_envelope(
    batch: BatchSetupResults,
    *,
    operation: str = 'sync.run',
    correlation_id: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> ResultEnvelope:
    """Wrap collected execution results in a result envelope."""
    diagnostics = batch_diagnostics(batch)
    follow_up_actions = batch_follow_up_actions(batch)
    return ResultEnvelope(
        operation=operation,
        status=result_status(batch.success, diagnostics),
        correlation_id=correlation_id or str(uuid4()),
        started_at=started_at,
        ended_at=ended_at or datetime.now(UTC),
        summary=batch_result_summary(batch),
        diagnostics=diagnostics,
        follow_up_actions=follow_up_actions,
        payload=_batch_payload(batch),
    )


def replay_record(
    *,
    operation: str,
    correlation_id: str,
    parameters: SetupParameters,
    events: list[dict[str, Any]],
    result: ResultEnvelope,
    started_at: datetime,
    ended_at: datetime,
    trace_paths: tuple[Path, ...] = (),
) -> ReplayRecord:
    """Build a replayable record for an observable operation."""
    return ReplayRecord(
        operation=operation,
        correlation_id=correlation_id,
        started_at=started_at,
        ended_at=ended_at,
        # Replay records intentionally preserve setup parameters for diagnostics;
        # redact future sensitive parameters before serializing them here.
        parameters=parameters.model_dump(mode='json'),
        events=tuple(events),
        result=result,
        trace_paths=trace_paths,
    )


def explain_inspection_report(report: SyncInspectionReport) -> tuple[str, ...]:
    """Render a concise human explanation for an inspection report."""
    summary = report.summary
    lines = [
        (
            f'{summary.actions} action(s): {summary.needed} needed, {summary.satisfied} satisfied, '
            f'{summary.update_available} update available, {summary.unavailable} unavailable, '
            f'{summary.failed} failed.'
        )
    ]
    diagnostics = report.diagnostics or inspection_diagnostics(report)
    if diagnostics:
        lines.append('Diagnostics:')
        lines.extend(
            f'- {diagnostic.severity.value}: {diagnostic.code}: {diagnostic.message}' for diagnostic in diagnostics
        )
    follow_up_actions = report.follow_up_actions or inspection_follow_up_actions(report)
    if follow_up_actions:
        lines.append('Follow-up actions:')
        lines.extend(
            f'- {action.action_id or "batch"}: {action.label}'
            + (f' ({" ".join(action.command)})' if action.command else '')
            for action in follow_up_actions
        )
    return tuple(lines)


def _action_diagnostics(action: ActionInspection) -> tuple[Diagnostic, ...]:
    target = DiagnosticTarget(
        kind='action',
        manifest_index=action.manifest_index,
        action_ref=action.ref,
        action_id=action.action_id,
        plugin=action.action.installer,
        package=action.action.package_name,
    )
    if action.status == InspectionStatus.UNAVAILABLE:
        installer = action.action.installer or '(unresolved)'
        return (
            Diagnostic(
                code='action.installer_unavailable',
                severity=DiagnosticSeverity.ERROR,
                message=action.message or f"Installer '{installer}' is not available",
                target=target,
                remediation=Remediation(label='Install or enable the missing plugin'),
            ),
        )
    if action.status == InspectionStatus.FAILED:
        return (
            Diagnostic(
                code='action.failed',
                severity=DiagnosticSeverity.ERROR,
                message=action.message or action.action.description,
                target=target,
                cause=action.message,
                remediation=Remediation(label='Retry this action', action_id=action.action_id),
            ),
        )
    if action.status == InspectionStatus.SKIPPED:
        return (
            Diagnostic(
                code='action.skipped',
                severity=DiagnosticSeverity.WARNING,
                message=action.message or 'Action was skipped',
                target=target,
            ),
        )
    if action.status == InspectionStatus.UPDATE_AVAILABLE:
        return (
            Diagnostic(
                code='package.update_available',
                severity=DiagnosticSeverity.INFO,
                message=action.message or 'A newer package version is available',
                target=target,
                remediation=Remediation(
                    label='Upgrade package',
                    action_id=action.action_id,
                    command=action.cli_command,
                ),
            ),
        )
    return ()


def _follow_up_run_action(
    action: ActionInspection,
    label: str,
    kind: str,
    *,
    risk: ActionRisk = ActionRisk.NORMAL,
    requires_confirmation: bool = False,
) -> FollowUpAction:
    return FollowUpAction(
        label=label,
        kind=kind,
        target=DiagnosticTarget(
            kind='action',
            manifest_index=action.manifest_index,
            action_ref=action.ref,
            action_id=action.action_id,
            plugin=action.action.installer,
            package=action.action.package_name,
        ),
        action_id=action.action_id,
        command=action.cli_command,
        requires_confirmation=requires_confirmation,
        risk=risk,
    )


def _manifest_index(manifest: SetupResults, fallback_index: int) -> int:
    return manifest.manifest_index if manifest.manifest_index is not None else fallback_index


def _action_refs(
    actions: list[SetupAction],
    action_indices: list[int],
    manifest_index: int,
) -> tuple[tuple[SetupAction, ActionRef], ...]:
    indices = action_indices if len(action_indices) == len(actions) else list(range(len(actions)))
    return tuple(
        (action, ActionRef.from_indices(manifest_index, index)) for action, index in zip(actions, indices, strict=True)
    )


def _ref_for_result(
    result: SetupActionResult,
    action_refs: tuple[tuple[SetupAction, ActionRef], ...],
    ref_by_action_id: dict[int, ActionRef],
) -> ActionRef | None:
    if result.ref is not None:
        return result.ref
    ref = ref_by_action_id.get(id(result.action))
    if ref is not None:
        return ref

    matches = [ref for action, ref in action_refs if action == result.action]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.debug(
            'Ambiguous action reference for copied result action %r matched %d actions',
            result.action.description,
            len(matches),
        )
    return None


def _target_for_result(result: SetupActionResult, ref: ActionRef | None) -> DiagnosticTarget:
    action = result.action
    return DiagnosticTarget(
        kind='action',
        manifest_index=ref.manifest_index if ref is not None else None,
        action_ref=ref,
        action_id=ref.action_id if ref is not None else None,
        plugin=action.installer,
        package=action.package.name if action.package is not None else None,
    )


def _command_for_result(result: SetupActionResult) -> tuple[str, ...] | None:
    if result.cli_command:
        return result.cli_command
    return None


def _batch_payload(batch: BatchSetupResults) -> dict[str, Any]:
    return {
        'success': batch.success,
        'failed_paths': [{'path': path, 'error': error} for path, error in batch.failed_paths],
        'manifest_results': [
            {
                'manifest_index': _manifest_index(manifest, fallback_index),
                'manifest_path': manifest.manifest_path,
                'root_directory': manifest.root_directory,
                'action_indices': manifest.action_indices,
                'actions': len(manifest.actions),
                'results': [
                    {
                        'success': result.success,
                        'skipped': result.skipped,
                        'message': result.message,
                        'skip_reason': result.skip_reason.name if result.skip_reason is not None else None,
                        'installed_version': result.installed_version,
                        'available_version': result.available_version,
                        'cli_command': result.cli_command,
                    }
                    for result in manifest.results
                ],
            }
            for fallback_index, manifest in enumerate(batch.manifest_results)
        ],
    }
