"""The install JSON contract reports preparation, not native build or test success."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from porringer.console.entry import app
from porringer.core.schema import PluginKind
from porringer.schema import (
    BatchSetupResults,
    Diagnostic,
    DiagnosticSeverity,
    PreparationMetadata,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SyncInspectionReport,
    SyncPlan,
)
from porringer.utility.exception import PlanStaleError

_EXIT_USAGE = 2
_EXIT_INTERRUPT = 130


def _batch(*, success: bool = True, prepared: bool = True, native: bool = True) -> BatchSetupResults:
    action = SetupAction(
        description='secret-token',
        kind=PluginKind.PROJECT,
        prepare_command=('secret-command',) if native else None,
        native_intent='{"password":"secret-token"}',
    )
    result = SetupActionResult(
        action=action,
        success=success,
        project_prepared=prepared,
        message='https://user:secret-token@example.com',
        preparation_metadata=PreparationMetadata(
            schema_version='1',
            identity='selected-explicitly',
            backend='cmake',
            provider='conan',
            dependency_outputs=(Path.cwd() / 'actual-output',),
        )
        if prepared
        else None,
    )
    return BatchSetupResults(manifest_results=[SetupResults(actions=[action], results=[result])])


@pytest.mark.parametrize(('success', 'expected_exit'), [(True, 0), (False, 1)])
def test_install_json_preparation_not_build(test_config, success: bool, expected_exit: int) -> None:
    """A single safe envelope preserves execution and exit codes."""
    batch = _batch(success=success, prepared=success)
    plan = SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())
    with (
        patch('porringer.backend.command.sync.SyncCommands.plan', AsyncMock(return_value=plan)),
        patch('porringer.console.command.sync._execute_with_progress', return_value=batch) as execute,
    ):
        result = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)

    assert result.exit_code == expected_exit, result.output
    data = json.loads(result.stdout)
    assert data['schema_version'] == '2'
    assert data['operation'] == 'install'
    payload = data['payload']
    assert payload['schema_version'] == '1'
    assert payload['outcome'] == ('completed' if success else 'failed')
    preparation = payload['preparation'][0]
    assert preparation['outcome'] == ('prepared' if success else 'failed')
    assert preparation['native_build'] == preparation['native_test'] == 'not_verified'
    if success:
        assert preparation['metadata']['identity'] == 'selected-explicitly'
        assert preparation['metadata']['backend'] == 'cmake'
        assert preparation['metadata']['provider'] == 'conan'
        assert preparation['metadata']['dependency_outputs'] == [str(Path.cwd() / 'actual-output')]
    else:
        assert data['diagnostics'][0]['code'] == 'action.failed'
    assert 'secret-token' not in result.stdout
    assert 'secret-command' not in result.stdout
    execute.assert_called_once()


@pytest.mark.parametrize('has_project', [True, False])
def test_install_json_without_native_preparation(test_config, has_project: bool) -> None:
    """Non-native and empty installs never invent preparation metadata."""
    batch = _batch(prepared=False, native=False) if has_project else BatchSetupResults()
    with (
        patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            AsyncMock(return_value=SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())),
        ),
        patch('porringer.console.command.sync._execute_with_progress', return_value=batch),
    ):
        result = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)['payload']
    if has_project:
        assert payload['preparation'][0]['outcome'] == 'not_requested'
        assert payload['preparation'][0]['metadata'] is None
    else:
        assert payload['preparation'] == []
    assert payload['native_build'] == payload['native_test'] == 'not_verified'


@pytest.mark.parametrize(('answer', 'exit_code', 'outcome'), [('n\n', 0, 'cancelled'), ('', 1, 'failed')])
def test_install_json_confirmation(test_config, answer: str, exit_code: int, outcome: str) -> None:
    """JSON neither bypasses confirmation nor changes cancellation semantics."""
    with (
        patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            AsyncMock(return_value=SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())),
        ),
        patch('porringer.console.command.sync._execute_with_progress') as execute,
    ):
        result = CliRunner().invoke(app, ['install', '--json'], obj=test_config, input=answer)
    assert result.exit_code == exit_code, result.output
    data = json.loads(result.stdout)
    assert data['payload']['outcome'] == outcome
    assert data['diagnostics'][0]['code'] == f'install.{outcome}'
    execute.assert_not_called()


def test_install_json_unexpected_errors_propagate_without_secrets(test_config) -> None:
    """Unexpected exceptions are not swallowed or exposed in the JSON document."""
    error = RuntimeError('secret-token')
    with patch('porringer.backend.command.sync.SyncCommands.plan', AsyncMock(side_effect=error)):
        result = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)
    assert result.exit_code == 1
    assert result.exception is error
    assert json.loads(result.stdout)['payload']['outcome'] == 'failed'
    assert 'secret-token' not in result.stdout


@pytest.mark.parametrize(('error', 'exit_code'), [(KeyboardInterrupt(), _EXIT_INTERRUPT), (SystemExit(1), 1)])
def test_install_json_interruption_keeps_exit_semantics(test_config, error: BaseException, exit_code: int) -> None:
    """Interruption cannot accidentally emit a successful preparation result."""
    with patch('porringer.backend.command.sync.SyncCommands.plan', AsyncMock(side_effect=error)):
        result = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)
    assert result.exit_code == exit_code
    expected = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'failed'
    assert json.loads(result.stdout)['payload']['outcome'] == expected


def test_install_json_stale_plan_is_still_failure(test_config) -> None:
    """A stale confirmed plan remains rejected rather than being rerun."""
    with (
        patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            AsyncMock(return_value=SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())),
        ),
        patch(
            'porringer.console.command.sync._execute_with_progress',
            side_effect=PlanStaleError([Path('private-details')]),
        ),
    ):
        result = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)
    assert result.exit_code == 1
    assert json.loads(result.stdout)['payload']['outcome'] == 'failed'
    assert 'private-details' not in result.stdout


def test_install_json_rejects_jsonl(test_config) -> None:
    """Two incompatible stdout formats fail rather than mixing documents."""
    result = CliRunner().invoke(app, ['install', '--json', '--jsonl', '--yes'], obj=test_config)
    assert result.exit_code == _EXIT_USAGE
    assert json.loads(result.stdout)['payload']['outcome'] == 'failed'


def test_install_json_metadata_warning_does_not_change_success(test_config) -> None:
    """Preparation can succeed without an authoritative metadata report."""
    batch = _batch()
    result = batch.manifest_results[0].results[0]
    result.preparation_metadata = None
    result.preparation_diagnostics = (
        Diagnostic(
            code='preparation.metadata_unavailable',
            severity=DiagnosticSeverity.WARNING,
            message='Authoritative native metadata is unavailable.',
        ),
    )
    with (
        patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            AsyncMock(return_value=SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())),
        ),
        patch('porringer.console.command.sync._execute_with_progress', return_value=batch),
    ):
        invocation = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)
    assert invocation.exit_code == 0, invocation.output
    data = json.loads(invocation.stdout)
    assert data['status'] == 'partial'
    assert data['diagnostics'][0]['code'] == 'preparation.metadata_unavailable'
    assert data['payload']['preparation'][0]['outcome'] == 'prepared'
    assert data['payload']['preparation'][0]['metadata'] is None


@pytest.mark.parametrize('code', ['preparation.metadata_invalid', 'preparation.metadata_read_failed'])
def test_install_json_metadata_errors_are_explicit_failures(test_config, code: str) -> None:
    """Report errors fail installation without misrepresenting the successful preparation subprocess."""
    batch = _batch(success=False)
    result = batch.manifest_results[0].results[0]
    result.preparation_metadata = None
    result.preparation_diagnostics = (
        Diagnostic(code=code, severity=DiagnosticSeverity.ERROR, message='Preparation report failed.'),
    )
    with (
        patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            AsyncMock(return_value=SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())),
        ),
        patch('porringer.console.command.sync._execute_with_progress', return_value=batch),
    ):
        invocation = CliRunner().invoke(app, ['install', '--json', '--yes'], obj=test_config)
    assert invocation.exit_code == 1, invocation.output
    data = json.loads(invocation.stdout)
    assert data['status'] == 'failed'
    assert any(diagnostic['code'] == code and diagnostic['severity'] == 'error' for diagnostic in data['diagnostics'])
    assert data['payload']['preparation'][0]['outcome'] == 'prepared'
    assert data['payload']['preparation'][0]['metadata'] is None


def test_install_json_profile_uses_existing_execution(test_config) -> None:
    """Profiles keep their inspection, hash and execution path."""
    with (
        patch('porringer.backend.command.profile.ProfileCommands.inspect', AsyncMock()),
        patch('porringer.console.command.install._display_report'),
        patch(
            'porringer.backend.command.profile.ProfileCommands.run',
            AsyncMock(return_value=SimpleNamespace(results=_batch())),
        ) as run,
    ):
        result = CliRunner().invoke(
            app,
            ['install', 'porringer://profile?url=https://example.com/profile.json', '--json', '--yes'],
            obj=test_config,
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['payload']['preparation'][0]['outcome'] == 'prepared'
    run.assert_awaited_once()
