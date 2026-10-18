"""Helpers for test install cli.

CLI tests for the install and open commands.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from porringer.api import API
from porringer.console.entry import app
from porringer.schema import (
    DownloadResult,
    InspectionMode,
    SetupParameters,
    SyncInspectionReport,
    SyncPlan,
)
from porringer.utility.exception import PlanStaleError

_EXPECTED_CONFIRMATION_PROMPTS_AFTER_ONE_RETRY = 2


def _empty_plan() -> SyncPlan:
    """A plan with nothing to do."""
    return SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())


def _write_manifest(path: Path, data: dict) -> Path:
    """Write a porringer manifest into *path*."""
    manifest = path / 'porringer.json'
    manifest.write_text(json.dumps(data), encoding='utf-8')
    return manifest


def _fake_inspection() -> SimpleNamespace:
    """Build a minimal stand-in for a profile inspection result."""
    summary = SimpleNamespace(
        actions=0,
        needed=0,
        satisfied=0,
        update_available=0,
        unavailable=0,
        failed=0,
        skipped=0,
        unknown=0,
    )
    report = SimpleNamespace(
        manifests=[],
        failed_paths=[],
        summary=summary,
        success=True,
        inspection_mode=InspectionMode.COMPLETE,
    )
    profile = SimpleNamespace(name='Test Profile')
    return SimpleNamespace(profile=profile, inspection=report)


@pytest.mark.mock_packages
class TestInstallCLI:
    """Install command behavior tests."""

    @staticmethod
    def test_non_tty_refuses_without_yes(tmp_path: Path, test_config) -> None:
        """Non-interactive install without --yes aborts with an actionable hint."""
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})
        runner = CliRunner()

        result = runner.invoke(app, ['install', str(tmp_path)], obj=test_config)

        assert result.exit_code == 1
        assert 'PORRINGER_ASSUME_YES' in result.output

    @staticmethod
    def test_redirected_output_refuses_instead_of_hidden_prompt(tmp_path: Path, test_config) -> None:
        """An interactive stdin with redirected output fails fast rather than waiting on an invisible prompt."""
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})
        runner = CliRunner()

        with (
            patch(
                'porringer.backend.command.sync.SyncCommands.plan',
                new_callable=AsyncMock,
                return_value=_empty_plan(),
            ),
            patch('porringer.console.common._stdin_is_interactive', return_value=True),
        ):
            result = runner.invoke(app, ['install', str(tmp_path)], obj=test_config, input='y\n')

        assert result.exit_code == 1
        assert 'output is redirected' in result.output
        assert 'Apply this setup plan?' not in result.output

    @staticmethod
    def test_preview_inspects_in_complete_mode(tmp_path: Path, test_config) -> None:
        """The confirmation preview always probes real presence (COMPLETE inspection).

        Showing a human an unprobed FAST report before the confirmation
        prompt would be misleading, since FAST reports every action as
        unknown rather than checking whether it is actually needed. The
        preview must inspect in COMPLETE mode regardless of what the
        execution parameters carry.
        """
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})
        runner = CliRunner()

        with patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            new_callable=AsyncMock,
            return_value=_empty_plan(),
        ) as plan_mock:
            result = runner.invoke(app, ['install', str(tmp_path)], obj=test_config, input='n\n')

        assert result.exit_code == 0
        plan_mock.assert_awaited_once()
        assert plan_mock.await_args is not None
        (params,) = plan_mock.await_args.args
        assert params.inspection_mode == InspectionMode.COMPLETE

    @staticmethod
    def test_stale_plan_is_refused(tmp_path: Path, test_config) -> None:
        """The confirmed plan is what runs; if its files changed, install stops with a hint."""
        manifest = _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})
        plan = _empty_plan()
        runner = CliRunner()

        with (
            patch(
                'porringer.backend.command.sync.SyncCommands.plan',
                new_callable=AsyncMock,
                return_value=plan,
            ),
            patch(
                'porringer.backend.command.sync.SyncCommands.run',
                new_callable=AsyncMock,
                side_effect=PlanStaleError([manifest]),
            ) as run_mock,
        ):
            result = runner.invoke(app, ['install', str(tmp_path), '--yes'], obj=test_config)

        assert result.exit_code == 1
        assert run_mock.await_args is not None
        assert run_mock.await_args.kwargs['plan'] is plan
        assert 'Run the install again' in result.output

    @staticmethod
    def test_invalid_confirmation_input_reprompts(tmp_path: Path, test_config) -> None:
        """A pasted command is rejected instead of being treated as "no"."""
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})
        runner = CliRunner()

        with patch(
            'porringer.backend.command.sync.SyncCommands.plan',
            new_callable=AsyncMock,
            return_value=_empty_plan(),
        ):
            result = runner.invoke(
                app,
                ['install', str(tmp_path)],
                obj=test_config,
                input='d:\\projects\\periapsis\\.venv\\Scripts\\activate.bat\nn\n',
            )

        assert result.exit_code == 0
        assert 'Please answer y or n.' in result.output
        assert result.output.count('Apply this setup plan? [y/N]:') == _EXPECTED_CONFIRMATION_PROMPTS_AFTER_ONE_RETRY
        assert 'Aborted.' in result.output

    @staticmethod
    def test_removed_command_hook_option_is_not_advertised(tmp_path: Path, test_config) -> None:
        """The install command no longer advertises removed command-hook options."""
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})
        runner = CliRunner()

        result = runner.invoke(app, ['install', str(tmp_path)], obj=test_config)

        removed_hook_flag = ''.join(['--with-', 'post', '-', 'sync'])
        assert removed_hook_flag not in result.output
        assert '--timeout' not in result.output

    @staticmethod
    def test_missing_path_fails(tmp_path: Path, test_config) -> None:
        """A nonexistent path target exits with an error."""
        runner = CliRunner()

        result = runner.invoke(app, ['install', str(tmp_path / 'missing')], obj=test_config)

        assert result.exit_code == 1
        assert 'does not exist' in result.output


class TestOpenCLI:
    """Open command (link handler) behavior tests."""

    @staticmethod
    def test_open_previews_without_executing(test_config) -> None:
        """``open`` inspects the linked profile and never executes it."""
        runner = CliRunner()

        with (
            patch(
                'porringer.backend.command.profile.ProfileCommands.inspect',
                new_callable=AsyncMock,
                return_value=_fake_inspection(),
            ) as mock_inspect,
            patch(
                'porringer.backend.command.profile.ProfileCommands.run',
                new_callable=AsyncMock,
            ) as mock_run,
        ):
            result = runner.invoke(
                app,
                [
                    'open',
                    'porringer://profile?url=https://example.com/profile.json&sha256=abc123',
                ],
                obj=test_config,
            )

        assert result.exit_code == 0
        mock_run.assert_not_awaited()
        mock_inspect.assert_awaited_once()
        assert mock_inspect.await_args is not None
        assert mock_inspect.await_args.kwargs['expected_hash'] == 'sha256:abc123'
        assert 'Preview only' in result.output

    @staticmethod
    def test_open_rejects_non_link(test_config) -> None:
        """``open`` only accepts porringer:// links."""
        runner = CliRunner()

        result = runner.invoke(app, ['open', 'https://example.com/profile.json'], obj=test_config)

        assert result.exit_code == 1
        assert 'Unsupported scheme' in result.output


class TestProfileHashEnforcement:
    """Link-level hash verification failure tests."""

    @staticmethod
    async def test_resolve_fails_on_hash_mismatch(test_api: API) -> None:
        """A failed hash verification aborts profile resolution."""

        async def _fake_download(params) -> DownloadResult:
            return DownloadResult(success=False, message='Hash mismatch: expected deadbeef, got abc123')

        with (
            patch(
                'porringer.backend.command.profile.download_file',
                side_effect=_fake_download,
            ),
            pytest.raises(ValueError, match='Hash mismatch'),
        ):
            await test_api.profile.resolve('https://example.com/profile.json', expected_hash='sha256:deadbeef')
