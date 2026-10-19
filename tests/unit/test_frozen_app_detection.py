"""Helpers for test frozen app detection.

Tests for which interpreter python_command() targets when Porringer runs
from a frozen (PyInstaller) binary or from inside a virtual environment.

In a frozen app ``sys.executable`` is the packaged binary, so the
``python`` on ``PATH`` is used. Inside a venv (a project ``.venv`` or
Porringer's own pipx venv) the interpreter the venv was created from is
used, so packages never land in the environment Porringer runs from.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from porringer.core.plugin_schema import python_environment as _python_environment
from porringer.core.plugin_schema.runtime import RuntimeContext

from tests.conftest import FROZEN_EXE, frozen_context
from tests.fixtures.mock_plugins import MOCK_DIST, MOCK_RUNTIME_EXE, MockPythonEnv

_SYSTEM_PYTHON = r"C:\Python314\python.exe"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPythonCommandFrozenApp:
    """python_command() must handle sys.frozen gracefully."""

    @staticmethod
    def test_frozen_no_runtime_uses_which() -> None:
        """In a frozen app with no runtime override, prefer shutil.which('python')."""
        env = MockPythonEnv(MOCK_DIST)
        empty_ctx = RuntimeContext()

        with frozen_context(which_result=_SYSTEM_PYTHON):
            result = env.python_command(empty_ctx)

        assert result == _SYSTEM_PYTHON

    @staticmethod
    def test_frozen_with_runtime_context_uses_override() -> None:
        """Runtime context override takes priority even in frozen apps."""
        env = MockPythonEnv(MOCK_DIST)
        rc = RuntimeContext(executables={"python": MOCK_RUNTIME_EXE})

        with frozen_context():
            result = env.python_command(rc)

        assert result == str(MOCK_RUNTIME_EXE)

    @staticmethod
    def test_frozen_no_which_falls_back_to_sys_executable() -> None:
        """When shutil.which also fails, gracefully degrade to sys.executable."""
        env = MockPythonEnv(MOCK_DIST)
        empty_ctx = RuntimeContext()

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", FROZEN_EXE),
            patch(
                "porringer.core.plugin_schema.python_environment.shutil.which",
                return_value=None,
            ),
        ):
            result = env.python_command(empty_ctx)

        assert result == FROZEN_EXE

    @staticmethod
    def test_not_frozen_ignores_which() -> None:
        """When not frozen, python_command() returns sys.executable as before."""
        env = MockPythonEnv(MOCK_DIST)
        empty_ctx = RuntimeContext()

        original_exe = sys.executable
        # Ensure sys.frozen is absent (the normal case) and we are not in a venv
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(sys, "prefix", sys.base_prefix),
            patch(
                "porringer.core.plugin_schema.python_environment.shutil.which",
                return_value=_SYSTEM_PYTHON,
            ),
        ):
            result = env.python_command(empty_ctx)

        assert result == original_exe

    @staticmethod
    def test_frozen_none_runtime_context_uses_which() -> None:
        """With runtime_context=None (not just empty), still applies frozen fallback."""
        env = MockPythonEnv(MOCK_DIST)

        with frozen_context(which_result=_SYSTEM_PYTHON):
            result = env.python_command(None)

        assert result == _SYSTEM_PYTHON


class TestVirtualEnvironment:
    """Porringer running inside a venv targets the interpreter the venv was made from.

    pdm, pipx and other packages belong to the user's interpreter, never to a
    project ``.venv`` or Porringer's own pipx venv.
    """

    @staticmethod
    def _in_venv(tmp_path: Path, base: Path | None) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(sys, "frozen", False, create=True))
        stack.enter_context(patch.object(sys, "prefix", str(tmp_path / "venv")))
        stack.enter_context(patch.object(sys, "base_prefix", str(tmp_path / "base")))
        stack.enter_context(
            patch.object(
                sys, "_base_executable", str(base) if base else "", create=True
            )
        )
        return stack

    def test_venv_targets_base_interpreter(self, tmp_path: Path) -> None:
        """Inside any venv, the default interpreter is the venv's base interpreter."""
        base = tmp_path / "python.exe"
        base.write_text("", encoding="utf-8")

        with self._in_venv(tmp_path, base):
            assert _python_environment.default_python() == str(base)

    def test_missing_base_falls_back_to_running_interpreter(
        self, tmp_path: Path
    ) -> None:
        """When the base interpreter is gone, degrade to sys.executable."""
        with self._in_venv(tmp_path, tmp_path / "missing.exe"):
            assert _python_environment.default_python() == sys.executable

    @staticmethod
    def test_outside_venv_keeps_sys_executable(tmp_path: Path) -> None:
        """Outside a venv, the running interpreter is the user's interpreter."""
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(sys, "prefix", str(tmp_path)),
            patch.object(sys, "base_prefix", str(tmp_path)),
        ):
            assert _python_environment.default_python() == sys.executable

    def test_python_command_uses_base_interpreter(self, tmp_path: Path) -> None:
        """python_command() routes through the venv fallback."""
        env = MockPythonEnv(MOCK_DIST)
        base = tmp_path / "python.exe"
        base.write_text("", encoding="utf-8")

        with self._in_venv(tmp_path, base):
            assert env.python_command(RuntimeContext()) == str(base)

    def test_runtime_context_overrides_venv_fallback(self, tmp_path: Path) -> None:
        """A resolved runtime context wins over the venv fallback."""
        env = MockPythonEnv(MOCK_DIST)
        rc = RuntimeContext(executables={"python": MOCK_RUNTIME_EXE})

        with self._in_venv(tmp_path, tmp_path / "python.exe"):
            assert env.python_command(rc) == str(MOCK_RUNTIME_EXE)
