"""Command-contract tests for bootstrapping pipx before using it."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

import pytest
from dirty_equals import IsPartialDict
from packaging.version import Version

from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.execution import execute_manifest
from porringer.core.plugin_schema.environment import Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Distribution, Ecosystem, PackageRef, PluginKind, PluginParameters
from porringer.plugin.pip.plugin import PIPEnvironment
from porringer.plugin.pipx.plugin import PIPXEnvironment
from porringer.schema import SetupAction, SetupParameters, SetupResults
from tests.fixtures.command_process import CommandProcess

_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.0')))


def _plugins(*, target_python: Path, include_pipx: bool) -> DiscoveredPlugins:
    """Build the discovered plugin set for one bootstrap phase."""
    environments: dict[str, Environment] = {'pip': PIPEnvironment(_PARAMS)}
    if include_pipx:
        environments['pipx'] = PIPXEnvironment(_PARAMS)
    return DiscoveredPlugins(
        environments=environments,
        project_environments={},
        scm_environments={},
        runtime_context=RuntimeContext(executables={'python': target_python}),
    )


def _preview(root: Path) -> SetupResults:
    """Create a preview whose tool action must be resolved after the package phase."""
    return SetupResults(
        actions=[
            SetupAction(
                description="Install 'pipx' via pip",
                kind=PluginKind.PACKAGE,
                ecosystem=Ecosystem('python'),
                installer='pip',
                package=PackageRef(name='pipx'),
            ),
            SetupAction(
                description="Install 'porringer-smoke-python' (deferred)",
                kind=PluginKind.TOOL,
                ecosystem=Ecosystem('python'),
                installer=None,
                package=PackageRef(name='porringer-smoke-python'),
            ),
        ],
        root_directory=root,
    )


@pytest.mark.fresh_plugins
async def test_pipx_installed_by_package_phase_is_used_by_tool_phase(
    command_process: CommandProcess,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Porringer refreshes tools after pip installs pipx and uses the target Python."""
    target_python = tmp_path / 'runtime' / 'Scripts' / 'python.exe'
    target_python_text = str(target_python)

    command_process.script([target_python_text, '-m', 'pip', 'list', '--format=json'], stdout='[]')
    command_process.script([target_python_text, '-m', 'pip', 'install', 'pipx'], stdout='installed pipx')
    command_process.script([target_python_text, '-c', 'import pipx'])
    command_process.script(
        [target_python_text, '-m', 'pipx', 'install', 'porringer-smoke-python'],
        stdout='installed porringer-smoke-python',
    )

    refreshed = _plugins(target_python=target_python, include_pipx=True)
    monkeypatch.setattr('porringer.backend.command.core.execution.refresh_path', lambda: None)
    monkeypatch.setattr('porringer.backend.command.core.execution.discover_all_plugins', lambda: refreshed)
    monkeypatch.setattr('porringer.plugin.pip.plugin.shutil.which', lambda _name: None)
    monkeypatch.setenv('PIPX_HOME', str(tmp_path / 'pipx-home'))

    result = await execute_manifest(
        _preview(tmp_path),
        SetupParameters(project_directory=False),
        asyncio.Queue(),
        plugins=_plugins(target_python=target_python, include_pipx=False),
    )

    assert [action_result.success for action_result in result.results] == [True, True]
    assert [action_result.action.installer for action_result in result.results] == ['pip', 'pipx']

    commands = command_process.argv_list
    pip_install = [target_python_text, '-m', 'pip', 'install', 'pipx']
    pipx_probe = [target_python_text, '-c', 'import pipx']
    pipx_install = [target_python_text, '-m', 'pipx', 'install', 'porringer-smoke-python']
    assert commands.index(pip_install) < commands.index(pipx_probe) < commands.index(pipx_install)
    assert all(command[0] == target_python_text for command in commands)
    assert not any(command[0] == 'pipx' for command in commands)
    assert [asdict(call) for call in command_process.calls] == [
        IsPartialDict(argv=(target_python_text, '-m', 'pip', 'list', '--format=json'), returncode=0),
        IsPartialDict(argv=tuple(pip_install), returncode=0),
        IsPartialDict(argv=tuple(pipx_probe), returncode=0),
        IsPartialDict(argv=tuple(pipx_install), returncode=0),
    ]
