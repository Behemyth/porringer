"""Tests for manifest-driven uninstall (``api.tool.uninstall``)."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.execution import (
    execute_uninstall,
    uninstall_manifest,
)
from porringer.backend.command.tool import ToolCommands
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Package, PluginKind
from porringer.schema import (
    ActionCompletedEvent,
    ActionStartedEvent,
    BatchSetupResults,
    ProgressEvent,
    SetupParameters,
    SetupResults,
    SkipReason,
    SyncRunReport,
)
from tests.fixtures.factories import make_environment, setup_action


def _queue() -> asyncio.Queue[ProgressEvent | None]:
    return asyncio.Queue()


def _drain(queue: asyncio.Queue[ProgressEvent | None]) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    while not queue.empty():
        event = queue.get_nowait()
        if event is not None:
            events.append(event)
    return events


class TestExecuteUninstall:
    """Single-action uninstall behavior."""

    @staticmethod
    async def test_uninstalls_present_package() -> None:
        """A present package is uninstalled and reports its prior version."""
        env = make_environment(tool_name='pipx', installed=[Package(name='ruff', version='0.5.0')])
        env.uninstall = AsyncMock(return_value=Package(name='ruff', version='0.5.0'))
        action = setup_action('ruff', installer='pipx')

        result = await execute_uninstall(action, {'pipx': env}, _queue())

        assert result.success
        assert not result.skipped
        assert result.installed_version == '0.5.0'
        env.uninstall.assert_awaited_once()

    @staticmethod
    async def test_missing_package_is_skipped() -> None:
        """An absent package is skipped with NOT_INSTALLED and never uninstalled."""
        env = make_environment(tool_name='pipx', installed=[])
        env.uninstall = AsyncMock()
        action = setup_action('ruff', installer='pipx')

        result = await execute_uninstall(action, {'pipx': env}, _queue())

        assert result.skipped
        assert result.skip_reason == SkipReason.NOT_INSTALLED
        env.uninstall.assert_not_awaited()

    @staticmethod
    async def test_constraint_does_not_block_removal() -> None:
        """A version outside the manifest constraint is still removed."""
        env = make_environment(tool_name='pipx', installed=[Package(name='ruff', version='0.5.0')])
        env.uninstall = AsyncMock(return_value=Package(name='ruff', version='0.5.0'))
        action = setup_action('ruff', installer='pipx', constraint='>=9')

        result = await execute_uninstall(action, {'pipx': env}, _queue())

        assert result.success
        env.uninstall.assert_awaited_once()

    @staticmethod
    async def test_unavailable_installer_fails() -> None:
        """An installer that is not available yields a failed result."""
        result = await execute_uninstall(setup_action('ruff', installer='pipx'), {}, _queue())

        assert not result.success
        assert 'not available' in (result.message or '')

    @staticmethod
    async def test_teardown_once_when_plugin_is_empty() -> None:
        """teardown() runs once after the plugin reports no remaining packages."""
        env = make_environment(tool_name='pipx')
        env.packages = AsyncMock(side_effect=[[Package(name='ruff', version='1')], []])
        env.uninstall = AsyncMock(return_value=Package(name='ruff', version='1'))
        env.teardown = AsyncMock()
        torn_down: set[str] = set()

        await execute_uninstall(
            setup_action('ruff', installer='pipx'),
            {'pipx': env},
            _queue(),
            teardown_complete=torn_down,
        )

        env.teardown.assert_awaited_once()
        assert torn_down == {'pipx'}


class TestUninstallManifest:
    """Manifest-level uninstall ordering and identity."""

    @staticmethod
    async def test_reverse_phase_order_with_stable_refs(tmp_path: Path) -> None:
        """Tools are removed before packages, and refs keep source indices."""
        pip = make_environment(tool_name='pip', installed=[Package(name='requests', version='2')])
        pipx = make_environment(tool_name='pipx', installed=[Package(name='ruff', version='1')])
        for env in (pip, pipx):
            env.uninstall = AsyncMock(side_effect=lambda params: Package(name=params.package.name, version='1'))
        plugins = DiscoveredPlugins(
            environments={'pip': pip, 'pipx': pipx},
            project_environments={},
            scm_environments={},
            runtime_context=RuntimeContext(),
        )
        preview = SetupResults(
            actions=[
                setup_action('requests', kind=PluginKind.PACKAGE, installer='pip'),
                setup_action('ruff', kind=PluginKind.TOOL, installer='pipx'),
            ],
            action_indices=[0, 2],
            root_directory=tmp_path,
        )
        queue = _queue()

        results = await uninstall_manifest(preview, SetupParameters(), queue, plugins=plugins, manifest_index=1)

        started = [e for e in _drain(queue) if isinstance(e, ActionStartedEvent)]
        assert [e.action_ref.action_id for e in started if e.action_ref] == [
            '1:2',
            '1:0',
        ]
        assert [r.action.package.name for r in results.results if r.action.package] == [
            'ruff',
            'requests',
        ]
        assert all(a.description.startswith('Uninstall') for a in results.actions)
        assert all(r.success for r in results.results)

    @staticmethod
    async def test_emits_completed_for_each_action(tmp_path: Path) -> None:
        """Every action gets a completion event, including skips."""
        pip = make_environment(tool_name='pip', installed=[])
        plugins = DiscoveredPlugins(
            environments={'pip': pip},
            project_environments={},
            scm_environments={},
            runtime_context=RuntimeContext(),
        )
        preview = SetupResults(
            actions=[setup_action('requests', kind=PluginKind.PACKAGE, installer='pip')],
            root_directory=tmp_path,
        )
        queue = _queue()

        await uninstall_manifest(preview, SetupParameters(), queue, plugins=plugins)

        completed = [e for e in _drain(queue) if isinstance(e, ActionCompletedEvent)]
        assert len(completed) == 1
        assert completed[0].result.skip_reason == SkipReason.NOT_INSTALLED


class TestToolUninstall:
    """Public ``tool.uninstall`` parameter wiring."""

    @staticmethod
    @pytest.mark.mock_packages
    @pytest.mark.usefixtures('stub_runtime_context')
    async def test_api_end_to_end(session_api: API, tmp_path: Path) -> None:
        """api.tool.uninstall removes a declared, installed package through the run pipeline."""
        (tmp_path / 'porringer.json').write_text(
            json.dumps({
                'version': '1',
                'packages': {'python': ['pytest', 'not-a-real-package-xyz']},
            }),
            encoding='utf-8',
        )
        removed = AsyncMock(side_effect=lambda params: Package(name=params.package.name, version='1'))
        events: list[ProgressEvent] = []

        with (
            patch('porringer.plugin.pip.plugin.PIPEnvironment.uninstall', removed),
            patch('porringer.plugin.uv.plugin.UvEnvironment.uninstall', removed),
        ):
            report = await session_api.tool.uninstall(
                paths=tmp_path,
                include_packages={'pytest', 'not-a-real-package-xyz'},
                on_event=events.append,
            )

        by_package = {result.package: result for result in report.results}
        assert by_package['pytest'].success
        assert not by_package['pytest'].skipped
        assert by_package['not-a-real-package-xyz'].skip_reason == SkipReason.NOT_INSTALLED.name
        assert all(result.action_id is not None for result in report.results)
        removed.assert_awaited_once()
        assert any(isinstance(event, ActionCompletedEvent) for event in events)

    @staticmethod
    async def test_requires_a_selector() -> None:
        """Without include_packages, action_ids or entry_ids, nothing is removed."""
        with pytest.raises(ValueError, match='include_packages, action_ids or entry_ids'):
            await ToolCommands(cast(Any, object())).uninstall()

    @staticmethod
    async def test_scopes_to_managed_kinds() -> None:
        """Uninstall runs the uninstall executor over package/tool/runtime actions only."""
        captured: dict[str, Any] = {}

        class _FakeSync:
            @staticmethod
            async def _run_exclusive(params: SetupParameters, **kwargs: Any) -> SyncRunReport:
                captured['params'] = params
                captured['executor'] = kwargs['executor']
                return SyncRunReport(results=BatchSetupResults())

        report = await ToolCommands(cast(Any, _FakeSync())).uninstall(
            paths=['p'], include_packages={'ruff'}, plugin_names={'pipx'}
        )

        params = captured['params']
        assert report.operation == 'uninstall'
        assert captured['executor'] is uninstall_manifest
        assert params.include_packages == {'ruff'}
        assert params.plugin_names == {'pipx'}
        assert params.action_kinds == {
            PluginKind.PACKAGE,
            PluginKind.TOOL,
            PluginKind.RUNTIME,
        }
