"""Guard: command-builder methods spawn no subprocesses.

The dry-run / preview path of every plugin operation is built on the
``*_command()`` family.  These methods must be **pure** \u2014 they may
read configuration but must not invoke the wrapped CLI.  Any plugin
that shells out from inside ``install_command`` / ``upgrade_command``
/ ``uninstall_command`` / ``sync_command`` would silently break dry
runs and execute side effects during preview.

This module relies on pytest-subprocess's default strict mode and
calls every plugin's command builders, asserting **zero** subprocess
calls were observed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from porringer.backend.command.core.discovery import discover_all_plugins
from porringer.core.plugin_schema.environment import Environment, PackageParameters
from porringer.core.plugin_schema.project_environment import ProjectInstaller
from porringer.core.schema import Package, PackageRef
from porringer.plugin.apt.plugin import APTEnvironment
from porringer.plugin.winget.plugin import WingetEnvironment
from tests.fixtures.command_process import CommandProcess


def _env_pairs() -> list[tuple[str, Environment]]:
    return sorted(discover_all_plugins(use_cache=False).environments.items())


def _proj_pairs() -> list[tuple[str, ProjectInstaller]]:
    return sorted(discover_all_plugins(use_cache=False).project_environments.items())


_ENV = _env_pairs()
_ENV_IDS = [n for n, _ in _ENV]
_PROJ = _proj_pairs()
_PROJ_IDS = [n for n, _ in _PROJ]

_PKG = PackageRef(name='example', constraint='>=1.0')


@pytest.mark.parametrize(('name', 'plugin'), _ENV, ids=_ENV_IDS)
def test_environment_command_builders_perform_no_subprocess(
    name: str,
    plugin: Environment,
    command_process: CommandProcess,
) -> None:
    """``install_command`` / ``upgrade_command`` / ``uninstall_command`` are pure."""
    del name
    plugin.install_command(_PKG)
    plugin.install_command(_PKG, include_prereleases=True)
    plugin.upgrade_command(_PKG)
    plugin.upgrade_command(_PKG, include_prereleases=True)
    plugin.uninstall_command(_PKG)
    command_process.assert_no_calls()


@pytest.mark.parametrize(('name', 'plugin'), _PROJ, ids=_PROJ_IDS)
def test_project_sync_command_performs_no_subprocess(
    name: str,
    plugin: ProjectInstaller,
    command_process: CommandProcess,
    tmp_path: Path,
) -> None:
    """``command_plan`` is pure."""
    del name
    type(plugin).command_plan(tmp_path)
    command_process.assert_no_calls()


@pytest.mark.parametrize('operation', ['install', 'upgrade'])
@pytest.mark.parametrize('plugin_type', [APTEnvironment, WingetEnvironment], ids=['apt', 'winget'])
async def test_log_only_dry_run_preserves_result_without_execution(
    operation: str,
    plugin_type: type[Environment],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rehearsal logs the command but skips subprocesses and execution hooks."""
    plugin = plugin_type.__new__(plugin_type)
    spawn = AsyncMock(side_effect=AssertionError('Dry runs must not spawn processes'))
    post_action = AsyncMock()
    monkeypatch.setattr('asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr(plugin, 'post_action', post_action)
    package = PackageRef(name='example', constraint='1.2.3')
    params = PackageParameters(package=package, dry=True)
    if plugin_type is APTEnvironment:
        expected = ['apt', 'install', '--simulate', '-y']
        if operation == 'upgrade':
            expected.append('--only-upgrade')
        expected.append('example=1.2.3')
    else:
        expected = [
            'winget',
            operation,
            '--id',
            'example',
            '--accept-source-agreements',
            '--accept-package-agreements',
            '-e',
            '--version',
            '1.2.3',
        ]

    with caplog.at_level(logging.INFO, logger=f'porringer.{plugin.tool_name()}.{operation}'):
        result = await plugin.install(params) if operation == 'install' else await plugin.upgrade(params)

    assert result == Package(name='example', version=None)
    spawn.assert_not_called()
    post_action.assert_not_called()
    assert f'[dry-run] Would run: {" ".join(expected)}' in caplog.messages
