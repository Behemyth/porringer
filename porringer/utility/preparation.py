"""Safe install result projection using the shared result envelope."""

from typing import Literal

from porringer.core.schema import PluginKind
from porringer.schema import BatchSetupResults, Diagnostic, DiagnosticSeverity, ResultEnvelope, ResultStatus
from porringer.schema.preparation import InstallPayload, PreparationStatus
from porringer.utility.observability import batch_result_summary


def install_envelope(
    results: BatchSetupResults | None,
    *,
    outcome: Literal['completed', 'failed', 'cancelled'],
) -> ResultEnvelope:
    """Exclude commands, intent, URLs, exception text and subprocess output from JSON."""
    diagnostics: list[Diagnostic] = []
    preparation: list[PreparationStatus] = []
    if results is not None:
        for manifest in results.manifest_results:
            for result in manifest.results:
                if not result.success:
                    diagnostics.append(
                        Diagnostic(
                            code='action.failed',
                            severity=DiagnosticSeverity.ERROR,
                            message='Install action failed. See stderr for details.',
                        )
                    )
                if result.action.kind != PluginKind.PROJECT:
                    continue
                requested = result.action.prepare_command is not None
                status = (
                    'not_requested'
                    if not requested
                    else 'skipped'
                    if result.skipped
                    else 'prepared'
                    if result.project_prepared
                    else 'failed'
                )
                preparation.append(
                    PreparationStatus(
                        action_id=result.ref.action_id if result.ref else None,
                        outcome=status,
                        metadata=result.preparation_metadata,
                        diagnostics=result.preparation_diagnostics,
                    )
                )
                diagnostics.extend(result.preparation_diagnostics)
        if results.failed_paths:
            diagnostics.append(
                Diagnostic(
                    code='manifest.load_failed',
                    severity=DiagnosticSeverity.ERROR,
                    message='One or more manifests could not be loaded. See stderr for details.',
                )
            )
    if outcome != 'completed':
        diagnostics.append(
            Diagnostic(
                code=f'install.{outcome}',
                severity=DiagnosticSeverity.INFO if outcome == 'cancelled' else DiagnosticSeverity.ERROR,
                message='Install cancelled.' if outcome == 'cancelled' else 'Install failed. See stderr for details.',
            )
        )
    status = ResultStatus.FAILED if outcome == 'failed' else ResultStatus.SUCCESS
    if status == ResultStatus.SUCCESS and any(item.severity == DiagnosticSeverity.WARNING for item in diagnostics):
        status = ResultStatus.PARTIAL
    envelope = ResultEnvelope(
        operation='install',
        status=status,
        diagnostics=tuple(diagnostics),
        payload=InstallPayload(outcome=outcome, preparation=tuple(preparation)).model_dump(mode='json'),
    )
    if results is not None:
        envelope.summary = batch_result_summary(results)
    envelope.summary.warnings = sum(item.severity == DiagnosticSeverity.WARNING for item in diagnostics)
    return envelope
