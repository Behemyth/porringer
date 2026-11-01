"""Tests for optional manifest groups (``groups`` / entry ``group`` / ``--with``)."""

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from porringer.api import API
from porringer.console.entry import app
from porringer.core.schema import Ecosystem
from porringer.schema import ManifestGroupSnapshot, SetupManifest, SetupParameters, SyncInspectionReport
from porringer.utility.exception import ManifestValidationCode

_PYTHON = Ecosystem('python')

_GROUPED = {
    'version': '1',
    'groups': {'lint': {'description': 'Linters'}},
    'packages': {'python': ['requests', {'name': 'ruff', 'group': 'lint'}]},
}


def _write_manifest(path: Path, data: dict) -> Path:
    """Write a manifest into *path* and return the file path."""
    manifest = path / 'porringer.json'
    manifest.write_text(json.dumps(data), encoding='utf-8')
    return manifest


def _package_names(report: SyncInspectionReport) -> list[str | None]:
    return [action.action.package_name for action in report.manifests[0].actions]


def _groups(report: SyncInspectionReport) -> tuple[ManifestGroupSnapshot, ...]:
    metadata = report.manifests[0].metadata
    assert metadata is not None
    return metadata.groups


class TestSchema:
    """Manifest model behavior for groups."""

    @staticmethod
    def test_undeclared_group_is_rejected() -> None:
        """An entry can only name a group declared under ``groups``."""
        with pytest.raises(ValidationError, match="group 'lint' is not declared"):
            SetupManifest.model_validate({'packages': {'python': [{'name': 'ruff', 'group': 'lint'}]}})

    @staticmethod
    def test_group_names_are_restricted() -> None:
        """Group names must be usable as ``--with`` values."""
        with pytest.raises(ValidationError):
            SetupManifest.model_validate({'groups': {'Not Valid': {}}})

    @staticmethod
    def test_select_groups_keeps_required_and_selected_entries() -> None:
        """Unselected group entries are dropped; empty ecosystems disappear."""
        manifest = SetupManifest.model_validate({
            'groups': {'lint': {}, 'release': {}},
            'packages': {'python': ['requests', {'name': 'ruff', 'group': 'lint'}]},
            'tools': {'python': [{'name': 'twine', 'group': 'release'}]},
        })

        default = manifest.select_groups(())
        assert [str(spec.name) for spec in default.packages[_PYTHON]] == ['requests']
        assert default.tools == {}

        selected = manifest.select_groups({'lint'})
        assert [str(spec.name) for spec in selected.packages[_PYTHON]] == ['requests', 'ruff']
        assert selected.tools == {}

    @staticmethod
    def test_group_entries_respect_platforms() -> None:
        """Only entries that apply to this platform are counted."""
        other = 'darwin' if sys.platform != 'darwin' else 'win32'
        manifest = SetupManifest.model_validate({
            'groups': {'maintainer': {}},
            'packages': {
                'system': [
                    {'name': 'gh', 'platforms': [sys.platform], 'group': 'maintainer'},
                    {'name': 'gh', 'platforms': [other], 'group': 'maintainer'},
                ]
            },
        })

        assert len(manifest.group_entries('maintainer')) == 1


@pytest.mark.mock_packages
class TestInspection:
    """API-level group selection."""

    @staticmethod
    async def test_unselected_group_is_left_out(session_api: API, tmp_path: Path) -> None:
        """Group entries are not planned by default, and the group is reported as not selected."""
        _write_manifest(tmp_path, _GROUPED)

        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path))

        assert _package_names(report) == ['requests']
        assert [(group.name, group.description, group.selected, group.entries) for group in _groups(report)] == [
            ('lint', 'Linters', False, 1)
        ]

    @staticmethod
    async def test_selected_group_is_planned(session_api: API, tmp_path: Path) -> None:
        """Selecting a group plans its entries and tags their actions."""
        _write_manifest(tmp_path, _GROUPED)

        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, groups={'lint'}))

        assert _package_names(report) == ['requests', 'ruff']
        assert [action.action.group for action in report.manifests[0].actions] == [None, 'lint']
        assert _groups(report)[0].selected is True

    @staticmethod
    async def test_unknown_group_is_an_error(session_api: API, tmp_path: Path) -> None:
        """Requesting a group no manifest declares fails instead of silently doing nothing."""
        _write_manifest(tmp_path, _GROUPED)

        with pytest.raises(ValueError, match=r'Unknown optional group\(s\): release \(available: lint\)'):
            await session_api.sync.inspect(SetupParameters(paths=tmp_path, groups={'release'}))


def test_platform_split_duplicates_do_not_warn(session_api: API, tmp_path: Path) -> None:
    """The same package for disjoint platforms isn't a duplicate."""
    _write_manifest(
        tmp_path,
        {
            'version': '1',
            'packages': {
                'system': [
                    {'name': 'gh', 'installer': 'brew', 'platforms': ['darwin']},
                    {'name': 'gh', 'installer': 'apt', 'platforms': ['linux']},
                ]
            },
        },
    )

    result = session_api.sync.validate_manifest(tmp_path)

    assert not [w for w in result.warnings if w.code == ManifestValidationCode.DUPLICATE_PACKAGE]


def test_overlapping_platform_duplicates_warn(session_api: API, tmp_path: Path) -> None:
    """The same package listed twice for one platform still warns."""
    _write_manifest(
        tmp_path,
        {
            'version': '1',
            'packages': {
                'system': [
                    {'name': 'gh', 'platforms': ['linux']},
                    {'name': 'gh'},
                ]
            },
        },
    )

    result = session_api.sync.validate_manifest(tmp_path)

    assert [w for w in result.warnings if w.code == ManifestValidationCode.DUPLICATE_PACKAGE]


@pytest.mark.mock_packages
class TestCLI:
    """``--with`` on preview and install."""

    @staticmethod
    def test_preview_with_includes_group(tmp_path: Path, test_config) -> None:
        """``preview --with`` plans the group's entries."""
        _write_manifest(tmp_path, _GROUPED)

        result = CliRunner().invoke(app, ['preview', str(tmp_path), '--with', 'lint', '--json'], obj=test_config)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        actions = payload['manifests'][0]['actions']
        assert [action['action']['package_name'] for action in actions] == ['requests', 'ruff']
        assert payload['manifests'][0]['metadata']['groups'][0]['selected'] is True

    @staticmethod
    def test_preview_lists_unselected_group(tmp_path: Path, test_config) -> None:
        """The human preview tells the user how to include an unselected group."""
        _write_manifest(tmp_path, _GROUPED)

        result = CliRunner().invoke(app, ['preview', str(tmp_path)], obj=test_config)

        assert result.exit_code == 0, result.output
        assert 'Optional group lint: not included' in result.output
        assert '--with lint' in result.output

    @staticmethod
    def test_preview_unknown_group_fails(tmp_path: Path, test_config) -> None:
        """An unknown ``--with`` group is reported and exits non-zero."""
        _write_manifest(tmp_path, _GROUPED)

        result = CliRunner().invoke(app, ['preview', str(tmp_path), '--with', 'release'], obj=test_config)

        assert result.exit_code != 0
        assert 'Unknown optional group' in result.output

    @staticmethod
    def test_install_unknown_group_fails_before_prompt(tmp_path: Path, test_config) -> None:
        """Install rejects an unknown group cleanly instead of with a traceback."""
        _write_manifest(tmp_path, _GROUPED)

        result = CliRunner().invoke(app, ['install', str(tmp_path), '--with', 'release', '--yes'], obj=test_config)

        assert result.exit_code != 0
        assert 'Unknown optional group' in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
