"""Helpers for test environment.

Unit tests for the PIMEnvironment plugin.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from packaging.version import Version

from porringer.core.plugin_schema.runtime import RuntimeProvider
from porringer.core.schema import Distribution, PackageRef, PluginParameters
from porringer.plugin.pim.plugin import PIMEnvironment
from porringer.test.mock.subprocess import fake_proc as _fake_proc
from porringer.test.pytest.tests import RuntimeProviderUnitTests

_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.1')))
EXPECTED_PROBES_AFTER_INVALIDATION = 2


@pytest.fixture
def environment() -> PIMEnvironment:
    """Provide a PIMEnvironment instance."""
    PIMEnvironment.invalidate_runtime_cache()
    return PIMEnvironment(_PARAMS)


class TestPIMBasics:
    """Basic property tests."""

    @staticmethod
    def test_implements_runtime_provider(environment: PIMEnvironment) -> None:
        """PIMEnvironment implements RuntimeProvider."""
        assert isinstance(environment, RuntimeProvider)


class TestResolveExecutable:
    """Tests for resolve_executable."""

    @staticmethod
    async def test_resolve_success(environment: PIMEnvironment) -> None:
        """Successful resolution returns the executable path."""
        exe_path = r'C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe'
        with (
            patch(
                'asyncio.create_subprocess_exec',
                return_value=_fake_proc(stdout=f'{exe_path}\n'),
            ),
            patch.object(Path, 'exists', return_value=True),
        ):
            result = await environment.resolve_executable('3.14')
            assert result == Path(exe_path)

    @staticmethod
    async def test_resolve_not_installed(environment: PIMEnvironment) -> None:
        """Returns None when version is not installed."""
        with patch(
            'asyncio.create_subprocess_exec',
            return_value=_fake_proc(returncode=1, stderr='not found'),
        ):
            result = await environment.resolve_executable('3.99')
            assert result is None

    @staticmethod
    async def test_resolve_py_missing(environment: PIMEnvironment) -> None:
        """Returns None when py launcher is missing."""
        with patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError):
            result = await environment.resolve_executable('3.14')
            assert result is None

    @staticmethod
    async def test_resolve_path_does_not_exist(environment: PIMEnvironment) -> None:
        """Returns None when resolved path does not exist."""
        with (
            patch(
                'asyncio.create_subprocess_exec',
                return_value=_fake_proc(stdout='/bad/path/python\n'),
            ),
            patch.object(Path, 'exists', return_value=False),
        ):
            result = await environment.resolve_executable('3.14')
            assert result is None


class TestDefaultExecutable:
    """Tests for default_executable."""

    @staticmethod
    async def test_default_executable_success_is_cached(
        environment: PIMEnvironment,
    ) -> None:
        """Successful default executable resolution is cached for the process."""
        exe_path = r'C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe'
        stdout = f'3.14\n{exe_path}\n'

        with (
            patch('asyncio.create_subprocess_exec', return_value=_fake_proc(stdout=stdout)) as mock_exec,
            patch.object(Path, 'exists', return_value=True),
        ):
            first = await environment.default_executable()
            second = await environment.default_executable()

        assert first == Path(exe_path)
        assert second == Path(exe_path)
        mock_exec.assert_called_once()

    @staticmethod
    async def test_default_executable_invalidation_reprobes(
        environment: PIMEnvironment,
    ) -> None:
        """Invalidating the runtime cache causes the next lookup to run py again."""
        exe_path = r'C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe'
        stdout = f'3.14\n{exe_path}\n'

        with (
            patch('asyncio.create_subprocess_exec', return_value=_fake_proc(stdout=stdout)) as mock_exec,
            patch.object(Path, 'exists', return_value=True),
        ):
            await environment.default_executable()
            environment.invalidate_runtime_cache()
            await environment.default_executable()

        assert mock_exec.call_count == EXPECTED_PROBES_AFTER_INVALIDATION

    @staticmethod
    async def test_default_executable_unexpected_output(
        environment: PIMEnvironment,
    ) -> None:
        """Malformed py output returns None instead of caching a bad path."""
        with patch('asyncio.create_subprocess_exec', return_value=_fake_proc(stdout='3.14\n')):
            assert await environment.default_executable() is None


class TestSetup:
    """Tests for the ``setup()`` lifecycle hook."""

    @staticmethod
    async def test_skips_when_bin_already_populated(
        environment: PIMEnvironment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Short-circuits without invoking py when ``python.exe`` already exists in bin."""
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setenv('LOCALAPPDATA', r'C:\\Users\\u\\AppData\\Local')
        with (
            patch.object(Path, 'exists', return_value=True),
            patch('asyncio.create_subprocess_exec', new=AsyncMock()) as mock_exec,
        ):
            await environment.setup()
            mock_exec.assert_not_called()

    @staticmethod
    async def test_runs_configure_when_bin_missing(
        environment: PIMEnvironment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invokes ``py install --configure --yes`` when bin dir is not populated."""
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setenv('LOCALAPPDATA', r'C:\\Users\\u\\AppData\\Local')
        with (
            patch.object(Path, 'exists', return_value=False),
            patch('asyncio.create_subprocess_exec', return_value=_fake_proc()) as mock_exec,
        ):
            await environment.setup()
            mock_exec.assert_called_once()
            args = mock_exec.call_args.args
            assert args[:4] == ('py', 'install', '--configure', '--yes')

    @staticmethod
    async def test_noop_on_non_windows(environment: PIMEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
        """Does nothing on non-Windows platforms."""
        monkeypatch.setattr(sys, 'platform', 'linux')
        with patch('asyncio.create_subprocess_exec', new=AsyncMock()) as mock_exec:
            await environment.setup()
            mock_exec.assert_not_called()

    @staticmethod
    async def test_handles_py_missing(environment: PIMEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
        """Swallows FileNotFoundError when py launcher is unavailable."""
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setenv('LOCALAPPDATA', r'C:\\Users\\u\\AppData\\Local')
        with (
            patch.object(Path, 'exists', return_value=False),
            patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError),
        ):
            # Must not raise.
            await environment.setup()

    @staticmethod
    async def test_handles_nonzero_exit(environment: PIMEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
        """Logs and returns when py exits non-zero; does not raise."""
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setenv('LOCALAPPDATA', r'C:\\Users\\u\\AppData\\Local')
        with (
            patch.object(Path, 'exists', return_value=False),
            patch(
                'asyncio.create_subprocess_exec',
                return_value=_fake_proc(returncode=1, stderr='boom'),
            ),
        ):
            await environment.setup()


class TestUninstallCommand:
    """Tests for the ``uninstall_command`` flag additions."""

    @staticmethod
    def test_uninstall_targets_only_the_tag(environment: PIMEnvironment) -> None:
        """Uninstall removes just the tagged runtime; ``--purge`` would reject the tag or remove them all."""
        cmd = environment.uninstall_command(PackageRef(name='3.14'))
        assert cmd == ['py', 'uninstall', '-y', '3.14']


class TestPIMEnvironmentUnit(RuntimeProviderUnitTests[PIMEnvironment]):
    """Runs the standard environment and runtime-provider unit test suite."""

    @staticmethod
    @pytest.fixture(name='plugin_type', scope='session')
    def fixture_plugin_type() -> type[PIMEnvironment]:
        """A required testing hook that allows type generation.

        Returns:
            The type of the Environment
        """
        return PIMEnvironment
