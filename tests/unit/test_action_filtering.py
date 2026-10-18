"""Helpers for test action filtering.

Tests for action-plan filtering via SetupParameters.

Covers ``include_packages`` (filter by package name) and ``plugin_names`` (filter by
resolved installer), plus their composition.  These filters are applied
uniformly by ``sync.inspect`` and ``sync.run``.
"""

import json
from pathlib import Path

import pytest

from porringer.api import API
from porringer.backend.command.core.action_builder import parse_manifest
from porringer.core.schema import PluginKind
from porringer.schema import SetupParameters

FIRST_ACTION = 0
EXPECTED_COMMANDS = 2


def _write_manifest(tmp_path: Path, packages: list[str]) -> Path:
    """Write a minimal python-package manifest and return its directory."""
    data: dict = {'version': '1', 'packages': {'python': packages}}
    (tmp_path / 'porringer.json').write_text(json.dumps(data))
    return tmp_path


def _package_names(report) -> set[str]:
    """Lower-cased names of every surviving package action in a report."""
    return {
        a.action.package_name.lower() for m in report.manifests for a in m.actions if a.action.package_name is not None
    }


def _resolved_installer(tmp_path: Path) -> str:
    """The installer the builder resolves for the manifest's package actions."""
    full = parse_manifest(tmp_path)
    pkg_actions = [a for a in full.actions if a.kind == PluginKind.PACKAGE]
    assert pkg_actions, 'manifest produced no package actions'
    installer = pkg_actions[FIRST_ACTION].installer
    assert installer is not None
    return installer


@pytest.mark.mock_packages
class TestIncludePackagesFilter:
    """SetupParameters.include_packages filters the action plan by package name."""

    @staticmethod
    async def test_none_preserves_all_actions(session_api: API, tmp_path: Path) -> None:
        """include_packages=None keeps the full action plan."""
        _write_manifest(tmp_path, ['requests', 'flask'])
        full_count = len(parse_manifest(tmp_path).actions)
        assert full_count > 0

        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, include_packages=None))
        assert report.summary.actions == full_count

    @staticmethod
    @pytest.mark.parametrize('selector', ['requests', 'REQUESTS'])
    async def test_filters_to_named_package(session_api: API, tmp_path: Path, selector: str) -> None:
        """Only the named package survives (case-insensitively); siblings are dropped."""
        _write_manifest(tmp_path, ['requests', 'flask'])
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, include_packages={selector}))
        assert _package_names(report) == {'requests'}

    @staticmethod
    async def test_nonexistent_removes_all_package_actions(session_api: API, tmp_path: Path) -> None:
        """A nonexistent package name drops every package action."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(
            SetupParameters(paths=tmp_path, include_packages={'nonexistent_package_xyz'})
        )
        assert _package_names(report) == set()

    @staticmethod
    async def test_no_matches_leaves_no_package_actions(session_api: API, tmp_path: Path) -> None:
        """Filtering to a nonexistent package removes all package actions."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(
            SetupParameters(paths=tmp_path, include_packages={'nonexistent_package_xyz'})
        )
        assert _package_names(report) == set()
        assert report.summary.actions == 0


@pytest.mark.mock_packages
class TestPluginsFilter:
    """SetupParameters.plugin_names filters the action plan by resolved installer."""

    @staticmethod
    async def test_none_preserves_all_actions(session_api: API, tmp_path: Path) -> None:
        """plugin_names=None keeps the full action plan."""
        _write_manifest(tmp_path, ['requests', 'flask'])
        full_count = len(parse_manifest(tmp_path).actions)
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, plugin_names=None))
        assert report.summary.actions == full_count

    @staticmethod
    async def test_filter_by_installer_keeps_matching_package_actions(session_api: API, tmp_path: Path) -> None:
        """Filtering to the resolved installer keeps its package actions."""
        _write_manifest(tmp_path, ['requests'])
        installer = _resolved_installer(tmp_path)
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, plugin_names={installer}))
        assert _package_names(report) == {'requests'}
        for m in report.manifests:
            for a in m.actions:
                assert a.action.installer == installer

    @staticmethod
    async def test_nonexistent_plugin_drops_all_package_actions(session_api: API, tmp_path: Path) -> None:
        """A nonexistent installer drops all installer-backed actions."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(
            SetupParameters(paths=tmp_path, plugin_names={'nonexistent_plugin_xyz'})
        )
        assert _package_names(report) == set()
        assert report.summary.actions == 0

    @staticmethod
    async def test_filter_applies_in_run(session_api: API, tmp_path: Path) -> None:
        """The plugin_names filter also applies when executing via run()."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.run(SetupParameters(paths=tmp_path, plugin_names={'nonexistent_plugin_xyz'}))
        assert report.results.total_actions == 0


@pytest.mark.mock_packages
class TestFilterComposition:
    """include_packages and plugin_names compose as an intersection."""

    @staticmethod
    async def test_plugins_and_packages_intersect(session_api: API, tmp_path: Path) -> None:
        """Combining both filters keeps only actions matching name AND installer."""
        _write_manifest(tmp_path, ['requests', 'flask'])
        installer = _resolved_installer(tmp_path)
        report = await session_api.sync.inspect(
            SetupParameters(paths=tmp_path, plugin_names={installer}, include_packages={'requests'})
        )
        assert _package_names(report) == {'requests'}
        for m in report.manifests:
            for a in m.actions:
                if a.action.package_name is not None:
                    assert a.action.installer == installer


@pytest.mark.mock_packages
class TestEmptySetFilters:
    """An empty set selects nothing, unlike ``None`` which selects everything."""

    @staticmethod
    async def test_empty_plugin_names_drops_installer_actions(session_api: API, tmp_path: Path) -> None:
        """plugin_names=set() keeps no installer-backed actions."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, plugin_names=set()))
        assert _package_names(report) == set()

    @staticmethod
    async def test_empty_include_packages_drops_package_actions(session_api: API, tmp_path: Path) -> None:
        """include_packages=set() keeps no package actions."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, include_packages=set()))
        assert _package_names(report) == set()

    @staticmethod
    async def test_empty_action_ids_drops_everything(session_api: API, tmp_path: Path) -> None:
        """action_ids=set() keeps no actions."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, action_ids=set()))
        assert report.summary.actions == 0


@pytest.mark.mock_packages
class TestActionKindsFilter:
    """SetupParameters.action_kinds filters the action plan by kind."""

    @staticmethod
    async def test_matching_kind_keeps_actions(session_api: API, tmp_path: Path) -> None:
        """Package actions survive a PACKAGE filter."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, action_kinds={PluginKind.PACKAGE}))
        assert _package_names(report) == {'requests'}
        assert all(a.action.kind == PluginKind.PACKAGE.value for m in report.manifests for a in m.actions)

    @staticmethod
    async def test_other_kind_drops_actions(session_api: API, tmp_path: Path) -> None:
        """Package actions are dropped by an SCM-only filter."""
        _write_manifest(tmp_path, ['requests'])
        report = await session_api.sync.inspect(SetupParameters(paths=tmp_path, action_kinds={PluginKind.SCM}))
        assert report.summary.actions == 0
