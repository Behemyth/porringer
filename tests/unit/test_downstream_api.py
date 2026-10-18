"""Helpers for test downstream api.

Tests for downstream-oriented API helpers.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from porringer.api import API
from porringer.backend.command.tool import ToolCommands
from porringer.core.schema import PackageRef, PluginKind
from porringer.schema import (
    ActionRef,
    BatchSetupResults,
    DirectoryState,
    DownloadResult,
    InspectionMode,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SyncRunReport,
    SyncStrategy,
)
from porringer.utility.observability import batch_diagnostics, batch_follow_up_actions


def _write_manifest(path: Path, data: dict) -> Path:
    """Write a porringer manifest into *path*."""
    manifest = path / 'porringer.json'
    manifest.write_text(json.dumps(data), encoding='utf-8')
    return manifest


@pytest.mark.mock_packages
class TestDownstreamSync:
    """Sync preview helpers."""

    @staticmethod
    async def test_inspect_uses_requested_fast_inspection(session_api: API, tmp_path: Path) -> None:
        """sync.inspect supports fast inspection through SetupParameters."""
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests']}})

        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, inspection_mode=InspectionMode.FAST))

        assert report.inspection_mode == InspectionMode.FAST
        assert report.summary.actions == 1

    @staticmethod
    async def test_action_id_selector_preserves_source_identity(session_api: API, tmp_path: Path) -> None:
        """action_ids select original manifest positions without renumbering refs."""
        _write_manifest(tmp_path, {'version': '1', 'packages': {'python': ['requests', 'rich']}})

        report = await session_api.sync.inspect(
            SetupParameters(
                paths=tmp_path,
                inspection_mode=InspectionMode.FAST,
                action_ids={'0:1'},
            )
        )

        assert report.summary.actions == 1
        assert report.manifests[0].actions[0].index == 0
        assert report.manifests[0].actions[0].action_index == 1
        assert report.manifests[0].actions[0].action_id == '0:1'
        assert report.manifests[0].actions[0].action.package_name == 'rich'

    @staticmethod
    async def test_action_id_selector_preserves_input_manifest_index(session_api: API, tmp_path: Path) -> None:
        """Failed earlier paths do not renumber later manifest action ids."""
        missing = tmp_path / 'missing'
        project = tmp_path / 'project'
        project.mkdir()
        _write_manifest(project, {'version': '1', 'packages': {'python': ['requests']}})

        report = await session_api.sync.inspect(
            SetupParameters(
                paths=[missing, project],
                fail_fast=False,
                inspection_mode=InspectionMode.FAST,
            )
        )

        assert report.summary.failed_paths == 1
        assert report.manifests[0].index == 1
        assert report.manifests[0].actions[0].action_id == '1:0'

        selected = await session_api.sync.inspect(
            SetupParameters(
                paths=[missing, project],
                fail_fast=False,
                inspection_mode=InspectionMode.FAST,
                action_ids={'1:0'},
            )
        )
        renumbered = await session_api.sync.inspect(
            SetupParameters(
                paths=[missing, project],
                fail_fast=False,
                inspection_mode=InspectionMode.FAST,
                action_ids={'0:0'},
            )
        )

        assert selected.summary.actions == 1
        assert selected.manifests[0].actions[0].action_id == '1:0'
        assert renumbered.summary.actions == 0


class TestObservableResults:
    """Observable result helper behavior."""

    @staticmethod
    def test_batch_diagnostics_preserve_ref_for_unique_copied_action() -> None:
        """Diagnostics recover refs when a result carries an equal copied action."""
        action = SetupAction(
            description='Install requests',
            installer='pip',
            package=PackageRef.model_validate('requests'),
        )
        result = SetupActionResult(action=replace(action), success=False, message='boom')
        batch = BatchSetupResults(
            manifest_results=[
                SetupResults(
                    actions=[action],
                    action_indices=[2],
                    results=[result],
                    manifest_index=4,
                )
            ]
        )

        diagnostics = batch_diagnostics(batch)
        follow_up_actions = batch_follow_up_actions(batch)

        assert diagnostics[0].target is not None
        assert diagnostics[0].target.action_id == '4:2'
        assert follow_up_actions[0].action_id == '4:2'

    @staticmethod
    def test_batch_diagnostics_omits_ambiguous_copied_action_ref() -> None:
        """Ambiguous copied actions are observable without assigning an unstable ref."""
        action = SetupAction(
            description='Install requests',
            installer='pip',
            package=PackageRef.model_validate('requests'),
        )
        result = SetupActionResult(action=replace(action), success=False, message='boom')
        batch = BatchSetupResults(
            manifest_results=[
                SetupResults(
                    actions=[action, replace(action)],
                    action_indices=[0, 1],
                    results=[result],
                    manifest_index=2,
                )
            ]
        )

        diagnostics = batch_diagnostics(batch)

        assert diagnostics[0].target is not None
        assert diagnostics[0].target.action_id is None


class TestToolCommands:
    """Managed tool command behavior."""

    @staticmethod
    async def test_upgrade_project_uses_latest_strategy() -> None:
        """Bulk cached tool updates use the LATEST strategy."""
        captured: list[SetupParameters] = []

        class _FakeSync:
            @staticmethod
            async def run(params: SetupParameters, **_kwargs: object) -> SyncRunReport:
                captured.append(params)
                return SyncRunReport(results=BatchSetupResults())

        commands = ToolCommands(cast(Any, _FakeSync()))

        report = await commands.upgrade_project(
            paths=['manifest.toml'],
            plugin_names={'pip'},
            include_packages={'requests'},
            action_ids={'0:1'},
        )

        assert report.operation == 'upgrade_project'
        assert captured[0].strategy == SyncStrategy.LATEST
        assert captured[0].paths == ['manifest.toml']
        assert captured[0].plugin_names == {'pip'}
        assert captured[0].action_kinds == {
            PluginKind.PACKAGE,
            PluginKind.TOOL,
            PluginKind.RUNTIME,
        }
        assert captured[0].include_packages == {'requests'}
        assert captured[0].action_ids == {'0:1'}

    @staticmethod
    async def test_upgrade_project_reports_action_ids() -> None:
        """Upgrade results carry the stable action id from the run."""
        action = SetupAction(
            description='Install requests',
            installer='pip',
            package=PackageRef.model_validate('requests'),
        )
        result = SetupActionResult(action=action, success=True, ref=ActionRef.from_indices(1, 3))

        class _FakeSync:
            @staticmethod
            async def run(_params: SetupParameters, **_kwargs: object) -> SyncRunReport:
                batch = BatchSetupResults(
                    manifest_results=[
                        SetupResults(
                            actions=[action],
                            action_indices=[3],
                            results=[result],
                            manifest_index=1,
                        )
                    ]
                )
                return SyncRunReport(results=batch)

        report = await ToolCommands(cast(Any, _FakeSync())).upgrade_project()

        assert report.results[0].action_id == '1:3'


class TestProfileCommands:
    """Setup profile resolution tests."""

    @staticmethod
    async def test_resolve_profile_downloads_and_validates(test_api: API) -> None:
        """profile.resolve downloads a portable setup profile."""
        profile_data = {
            'version': '1',
            'name': 'Workstation',
            'manifests': ['https://example.com/porringer.json'],
        }

        async def _fake_download(params) -> DownloadResult:
            params.destination.write_text(json.dumps(profile_data), encoding='utf-8')
            return DownloadResult(success=True, path=params.destination)

        with patch(
            'porringer.backend.command.profile.download_file',
            side_effect=_fake_download,
        ):
            profile = await test_api.profile.resolve('https://example.com/profile.json')

        assert profile.name == 'Workstation'
        assert [manifest.url for manifest in profile.manifests] == ['https://example.com/porringer.json']

    @staticmethod
    async def test_resolve_profile_manifest_hash_entries(test_api: API) -> None:
        """Profile manifest objects carry optional expected_hash values."""
        profile_data = {
            'version': '1',
            'name': 'Pinned Workstation',
            'manifests': [
                {
                    'url': 'https://example.com/porringer.json',
                    'expected_hash': 'sha256:abc123',
                }
            ],
        }

        async def _fake_download(params) -> DownloadResult:
            params.destination.write_text(json.dumps(profile_data), encoding='utf-8')
            return DownloadResult(success=True, path=params.destination)

        with patch(
            'porringer.backend.command.profile.download_file',
            side_effect=_fake_download,
        ):
            profile = await test_api.profile.resolve('https://example.com/profile.json')

        assert profile.manifests[0].url == 'https://example.com/porringer.json'
        assert profile.manifests[0].expected_hash == 'sha256:abc123'

    @staticmethod
    async def test_resolve_profile_threads_expected_hash(test_api: API) -> None:
        """profile.resolve forwards expected_hash to the profile download call."""
        profile_data = {
            'version': '1',
            'name': 'Pinned Workstation',
            'manifests': ['https://example.com/porringer.json'],
        }
        captured_hashes: list[str | None] = []

        async def _fake_download(params) -> DownloadResult:
            captured_hashes.append(params.expected_hash)
            params.destination.write_text(json.dumps(profile_data), encoding='utf-8')
            return DownloadResult(success=True, path=params.destination)

        with patch(
            'porringer.backend.command.profile.download_file',
            side_effect=_fake_download,
        ):
            await test_api.profile.resolve('https://example.com/profile.json', expected_hash='sha256:deadbeef')

        assert captured_hashes[0] == 'sha256:deadbeef'

    @staticmethod
    async def test_resolve_profile_rejects_http(test_api: API) -> None:
        """Profiles intentionally require HTTPS URLs."""
        with pytest.raises(ValueError, match='Only HTTPS URLs'):
            await test_api.profile.resolve('http://example.com/profile.json')


@pytest.mark.mock_packages
class TestInspectPaths:
    """Stateless multi-directory inspection for GUI clients."""

    @staticmethod
    async def test_inspect_paths_classifies_each_directory(session_api: API, tmp_path: Path) -> None:
        """sync.inspect_paths reports per-path state without persisting a registry."""
        valid = tmp_path / 'valid'
        valid.mkdir()
        _write_manifest(valid, {'version': '1', 'packages': {'python': ['requests']}})
        no_manifest = tmp_path / 'empty'
        no_manifest.mkdir()
        missing = tmp_path / 'missing'

        statuses = await session_api.sync.inspect_paths([valid, no_manifest, missing])

        by_name = {status.name: status for status in statuses}
        assert by_name['valid'].state == DirectoryState.INSPECTED
        assert by_name['valid'].has_manifest is True
        assert by_name['valid'].summary is not None
        assert by_name['valid'].summary.actions == 1
        assert by_name['empty'].state == DirectoryState.NO_MANIFEST
        assert by_name['missing'].state == DirectoryState.MISSING
        assert by_name['missing'].exists is False
