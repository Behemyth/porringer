"""Helpers for test environment.

Tests for the winget environment plugin.
"""

from unittest.mock import AsyncMock, patch

import pytest
from packaging.version import Version

from porringer.backend.command.core.resolution import is_package_installed
from porringer.core.schema import Distribution, Package, PackageRef, PluginParameters
from porringer.plugin.winget.plugin import WingetEnvironment
from porringer.test.pytest.tests import EnvironmentUnitTests

_LIST_OUTPUT = (
    '   - \r   \\ \r'
    'Name                 Id                                 Version    Available Source\n'
    '-----------------------------------------------------------------------------------\n'
    'GitHub CLI           GitHub.cli                         2.103.3              winget\n'
    'Adobe Lightroom      ARP\\Machine\\X86\\LRCC_9_6           9.6                        \n'
    'Old Tool             Vendor.OldTool                     < 1.2.0    1.3.0     winget\n'
    'Half row             Vendor.Half\n'
)


def _make_env() -> WingetEnvironment:
    return WingetEnvironment(PluginParameters(distribution=Distribution(version=Version('0.0.0'))))


class TestEnvironment(EnvironmentUnitTests[WingetEnvironment]):
    """The tests for the winget environment plugin."""

    @staticmethod
    @pytest.fixture(name='plugin_type', scope='session')
    def fixture_plugin_type() -> type[WingetEnvironment]:
        """A required testing hook that allows type generation.

        Returns:
            The type of the Environment
        """
        return WingetEnvironment


class TestWingetPackages:
    """``WingetEnvironment.packages`` parses ``winget list`` output."""

    @staticmethod
    async def test_parses_installed_packages() -> None:
        """Rows become packages keyed by identifier; short rows are skipped."""
        env = _make_env()
        with patch.object(env, '_run_text_command', new_callable=AsyncMock, return_value=_LIST_OUTPUT) as run:
            result = await env.packages()

        assert result == [
            Package(name='GitHub.cli', version='2.103.3'),
            Package(name='ARP\\Machine\\X86\\LRCC_9_6', version='9.6'),
            Package(name='Vendor.OldTool', version='1.2.0'),
        ]
        assert run.await_args is not None
        assert run.await_args.args[0][:2] == ['winget', 'list']

    @staticmethod
    async def test_command_failure_returns_empty() -> None:
        """A failed ``winget list`` reports nothing installed."""
        env = _make_env()
        with patch.object(env, '_run_text_command', new_callable=AsyncMock, return_value=None):
            assert await env.packages() == []

    @staticmethod
    async def test_unparseable_output_returns_empty() -> None:
        """Output without a table header reports nothing installed."""
        env = _make_env()
        with patch.object(env, '_run_text_command', new_callable=AsyncMock, return_value='No installed package found'):
            assert await env.packages() == []

    @staticmethod
    async def test_installed_identifier_matches_case_insensitively() -> None:
        """A manifest ``GitHub.CLI`` entry is satisfied by the listed ``GitHub.cli``."""
        env = _make_env()
        with patch.object(env, '_run_text_command', new_callable=AsyncMock, return_value=_LIST_OUTPUT):
            installed = await env.packages()

        found, _, matched = is_package_installed(PackageRef(name='GitHub.CLI'), installed)

        assert found
        assert matched == Package(name='GitHub.cli', version='2.103.3')
