"""Behavioral matrix for ``Environment`` command-construction methods.

Complements :mod:`tests.unit.test_plugin_protocol` (which only checks
shape) with cross-plugin invariants every command-builder must obey:

* Idempotent — calling twice produces equal argv (no hidden state).
* Constraint-tolerant — a constrained ``PackageRef`` does not raise.
  (Whether the constraint reaches argv is plugin-specific: some
  wrappers like brew / pyenv intentionally ignore version pins.)
* Pre-release flag is honoured deterministically (same argv for
  repeated calls with the same flag value).
* Uninstall references the package name verbatim.

Together with the protocol matrix this replaces the per-plugin
"smoke test that the install command is well-formed" duplication.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from porringer.backend.command.core.action_builder import get_cli_steps
from porringer.backend.command.core.discovery import (
    DiscoveredPlugins,
    discover_all_plugins,
)
from porringer.core.plugin_schema.environment import Environment, PackageParameters
from porringer.core.schema import Ecosystem, Package, PackageRef, PluginKind
from porringer.plugin.apt.plugin import APTEnvironment
from porringer.plugin.winget.plugin import WingetEnvironment
from porringer.schema import SetupAction, SyncStrategy
from tests.fixtures.command_process import CommandProcess


def _env_pairs() -> list[tuple[str, Environment]]:
    discovered = discover_all_plugins(use_cache=False)
    return sorted(discovered.environments.items())


_ENV = _env_pairs()
_IDS = [n for n, _ in _ENV]

_BARE = PackageRef(name='example')
_CONSTRAINED = PackageRef(name='example', constraint='>=1.2.3')


@pytest.mark.parametrize(('name', 'plugin'), _ENV, ids=_IDS)
class TestEnvironmentCommandBehaviour:
    """Per-plugin invariants for install / upgrade / uninstall command builders."""

    @staticmethod
    def test_install_is_idempotent(name: str, plugin: Environment) -> None:
        """Repeated calls to ``install_command`` with the same ref return equal argv."""
        del name
        assert plugin.install_command(_BARE) == plugin.install_command(_BARE)

    @staticmethod
    def test_upgrade_is_idempotent(name: str, plugin: Environment) -> None:
        """Repeated calls to ``upgrade_command`` with the same ref return equal argv."""
        del name
        assert plugin.upgrade_command(_BARE) == plugin.upgrade_command(_BARE)

    @staticmethod
    def test_uninstall_is_idempotent(name: str, plugin: Environment) -> None:
        """Repeated calls to ``uninstall_command`` with the same ref return equal argv."""
        del name
        assert plugin.uninstall_command(_BARE) == plugin.uninstall_command(_BARE)

    @staticmethod
    def test_install_accepts_constraint(name: str, plugin: Environment) -> None:
        """Building an install command with a constrained ref does not raise.

        Whether the constraint surfaces in argv is plugin-specific —
        some wrappers (brew, pyenv, ...) deliberately ignore version
        pins — so we only assert non-failure plus shape.
        """
        del name
        argv = plugin.install_command(_CONSTRAINED)
        assert isinstance(argv, list)
        assert argv

    @staticmethod
    def test_upgrade_accepts_constraint(name: str, plugin: Environment) -> None:
        """Building an upgrade command with a constrained ref does not raise."""
        del name
        argv = plugin.upgrade_command(_CONSTRAINED)
        assert isinstance(argv, list)
        assert argv

    @staticmethod
    def test_uninstall_references_package_name(name: str, plugin: Environment) -> None:
        """The uninstall argv must mention the package name in some form."""
        del name
        argv = plugin.uninstall_command(_BARE)
        assert any('example' in a for a in argv), f'package name missing from {argv!r}'

    @staticmethod
    def test_prerelease_flag_is_deterministic(name: str, plugin: Environment) -> None:
        """Repeated calls with the same ``include_prereleases`` value produce equal argv."""
        del name
        a = plugin.install_command(_BARE, include_prereleases=True)
        b = plugin.install_command(_BARE, include_prereleases=True)
        assert a == b
        c = plugin.install_command(_BARE, include_prereleases=False)
        d = plugin.install_command(_BARE, include_prereleases=False)
        assert c == d


@pytest.mark.parametrize('plugin_type', [APTEnvironment, WingetEnvironment], ids=['apt', 'winget'])
@pytest.mark.parametrize('operation', ['install', 'upgrade'])
@pytest.mark.parametrize('constraint', [None, '1.2.3'], ids=['unconstrained', 'pinned'])
@pytest.mark.parametrize('returncode', [0, 1], ids=['success', 'failure'])
async def test_system_package_command_contract(
    plugin_type: type[Environment],
    operation: str,
    constraint: str | None,
    returncode: int,
    command_process: CommandProcess,
) -> None:
    """Preview and execution use the shared hook and independently specified argv."""
    plugin = plugin_type.__new__(plugin_type)
    package = PackageRef(name='example', constraint=constraint)
    installer = 'apt' if plugin_type is APTEnvironment else 'winget'
    if installer == 'apt':
        expected = ['apt', 'install', '-y']
        if operation == 'upgrade':
            expected.append('--only-upgrade')
        expected.append('example=1.2.3' if constraint else 'example')
    else:
        expected = [
            'winget',
            operation,
            '--id',
            'example',
            '--accept-source-agreements',
            '--accept-package-agreements',
            '--disable-interactivity',
            '-e',
        ]
        if constraint:
            expected.extend(['--version', '1.2.3'])

    action = SetupAction(
        description=f'{operation} example',
        kind=PluginKind.PACKAGE,
        ecosystem=Ecosystem('system'),
        installer=installer,
        package=package,
    )
    plugins = DiscoveredPlugins(environments={installer: plugin}, project_environments={}, scm_environments={})
    strategy = SyncStrategy.MINIMAL if operation == 'install' else SyncStrategy.LATEST
    params = PackageParameters(package=package)
    command_process.script(expected, returncode=returncode)
    version_query = ['dpkg-query', '-W', '-f', '${Version}', 'example']
    command_process.script(version_query, stdout='1.2.3')

    with patch.object(plugin, 'package_command', wraps=plugin.package_command) as build_command:
        assert get_cli_steps(action, plugins, strategy) == (tuple(expected),)
        command_process.assert_no_calls()
        build_command.assert_called_once_with(operation, params)
        build_command.reset_mock()
        result = await plugin.install(params) if operation == 'install' else await plugin.upgrade(params)
        build_command.assert_called_once_with(operation, params)

    expected_calls = [expected]
    version = None
    if installer == 'apt' and returncode == 0:
        expected_calls.append(version_query)
        version = '1.2.3'
    assert command_process.argv_list == expected_calls
    assert result == (Package(name='example', version=version) if returncode == 0 else None)
