"""Helpers for test bootstrap cross platform.

Cross-platform tests for the python-bootstrap example manifest.

Validates that deferred resolution works correctly when platform-
specific plugins are unavailable:

- `pim` requires the Windows `py` launcher
- `pyenv` requires the Unix `pyenv` CLI

When neither is available, a RUNTIME action should still appear in
the preview with `installer=None` (deferred), rather than being
silently dropped.

Additionally validates that PACKAGE actions are deferred (not dropped)
when no Python package installer (pip, uv) is on PATH — a situation
common on fresh Windows machines bootstrapped with pim, where only
the ``py`` launcher provides access to Python/pip.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from porringer.backend.command.core.action_builder import parse_manifest
from porringer.backend.command.core.execution import inject_runtime_path
from porringer.core.schema import PluginKind
from porringer.schema import SetupResults

_BOOTSTRAP_DIR = Path(__file__).resolve().parents[2] / 'examples' / 'python-bootstrap'


class TestBootstrapDeferredRuntime:
    """Verify preview behaviour when no runtime provider is available."""

    @staticmethod
    @pytest.fixture
    def preview_no_runtime() -> SetupResults:
        """Preview the bootstrap manifest with both py and pyenv unavailable."""
        original_which = __import__('shutil').which

        def _which_no_runtime(cmd: str) -> str | None:
            if cmd in {'py', 'pyenv'}:
                return None
            return original_which(cmd)

        with patch('shutil.which', side_effect=_which_no_runtime):
            return parse_manifest(_BOOTSTRAP_DIR)

    @staticmethod
    def test_runtime_action_deferred(preview_no_runtime: SetupResults) -> None:
        """A RUNTIME action is generated with installer=None (deferred).

        Previously, the engine silently dropped the section when no
        runtime provider was available.  Now it defers, matching the
        existing TOOL behaviour.
        """
        runtime_actions = [a for a in preview_no_runtime.actions if a.kind == PluginKind.RUNTIME]
        assert len(runtime_actions) == 1
        assert runtime_actions[0].installer is None, 'Expected deferred (installer=None)'
        assert runtime_actions[0].package is not None
        assert runtime_actions[0].package.name == '3.15'


class TestBootstrapDeferredPackage:
    """Verify preview behaviour when no Python package installer is on PATH.

    Simulates a fresh Windows machine where only the ``py`` launcher
    is available (pim installs runtimes), but neither ``pip``,
    ``python``, ``uv``, nor ``pipx`` are on PATH.  In this scenario the
    pdm TOOL action defers (pipx unavailable) and its synthesized pipx
    bootstrap PACKAGE action must also be **deferred** (not silently
    dropped), so that the phase-transition after runtime installation
    can resolve them.
    """

    @staticmethod
    @pytest.fixture
    def preview_no_pip() -> SetupResults:
        """Preview the bootstrap manifest with pip, python, uv, and pipx unavailable."""
        original_which = __import__('shutil').which

        def _which_no_pip(cmd: str) -> str | None:
            if cmd in {'pip', 'python', 'uv', 'pipx'}:
                return None
            return original_which(cmd)

        with patch('shutil.which', side_effect=_which_no_pip):
            return parse_manifest(_BOOTSTRAP_DIR)

    @staticmethod
    def test_package_actions_deferred(preview_no_pip: SetupResults) -> None:
        """PACKAGE actions should be deferred, not dropped.

        The synthesized pipx-via-pip bootstrap action must appear with
        ``installer=None`` when pip itself is also unavailable.
        """
        package_actions = [a for a in preview_no_pip.actions if a.kind == PluginKind.PACKAGE]
        assert len(package_actions) >= 1, 'PACKAGE actions must not be silently skipped'
        for action in package_actions:
            assert action.installer is None, f'Expected deferred (installer=None), got {action.installer}'


class TestBootstrapFullyDeferred:
    """Verify preview when *both* runtime and package providers are missing.

    This extreme scenario (no py, pyenv, pip, python, uv, pipx on PATH)
    validates that the entire inter-phase deferral chain works: both
    RUNTIME and PACKAGE sections produce deferred actions, allowing the
    execution engine to resolve them sequentially at each phase boundary.
    """

    @staticmethod
    @pytest.fixture
    def preview_nothing() -> SetupResults:
        """Preview with all Python ecosystem tools unavailable."""
        original_which = __import__('shutil').which

        def _which_nothing(cmd: str) -> str | None:
            if cmd in {'py', 'pyenv', 'pip', 'python', 'uv', 'pipx'}:
                return None
            return original_which(cmd)

        with patch('shutil.which', side_effect=_which_nothing):
            return parse_manifest(_BOOTSTRAP_DIR)

    @staticmethod
    def test_runtime_and_package_both_deferred(preview_nothing: SetupResults) -> None:
        """Both RUNTIME and PACKAGE actions should be deferred."""
        runtime_actions = [a for a in preview_nothing.actions if a.kind == PluginKind.RUNTIME]
        package_actions = [a for a in preview_nothing.actions if a.kind == PluginKind.PACKAGE]

        assert len(runtime_actions) >= 1
        assert len(package_actions) >= 1

        for action in runtime_actions:
            assert action.installer is None
        for action in package_actions:
            assert action.installer is None


class TestInjectRuntimePath:
    """Unit tests for ``inject_runtime_path()``."""

    @staticmethod
    def test_injects_parent_directory(tmp_path: Path) -> None:
        """The executable's parent directory should be added to PATH."""
        exe = tmp_path / 'python.exe'
        exe.touch()

        original_path = os.environ.get('PATH', '')
        try:
            inject_runtime_path(exe)
            current = os.environ['PATH']
            assert str(tmp_path) in current.split(os.pathsep)
        finally:
            os.environ['PATH'] = original_path

    @staticmethod
    def test_injects_scripts_directory(tmp_path: Path) -> None:
        """The scripts sibling directory should be added to PATH."""
        exe = tmp_path / 'python.exe'
        exe.touch()

        expected_scripts = 'Scripts' if os.name == 'nt' else 'bin'
        scripts_dir = str(tmp_path / expected_scripts)

        original_path = os.environ.get('PATH', '')
        try:
            inject_runtime_path(exe)
            current = os.environ['PATH']
            assert scripts_dir in current.split(os.pathsep)
        finally:
            os.environ['PATH'] = original_path

    @staticmethod
    def test_no_duplicates(tmp_path: Path) -> None:
        """Calling twice should not add duplicate entries."""
        exe = tmp_path / 'python.exe'
        exe.touch()

        original_path = os.environ.get('PATH', '')
        try:
            inject_runtime_path(exe)
            path_after_first = os.environ['PATH']
            inject_runtime_path(exe)
            path_after_second = os.environ['PATH']
            assert path_after_first == path_after_second
        finally:
            os.environ['PATH'] = original_path
