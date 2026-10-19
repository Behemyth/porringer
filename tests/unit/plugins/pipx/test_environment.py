"""Helpers for test environment.

Tests plugin schemas.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from porringer.core.plugin_schema.python_environment import default_python
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.plugin.pipx.plugin import PIPXEnvironment
from porringer.test.pytest.tests import EnvironmentUnitTests


class TestEnvironment(EnvironmentUnitTests[PIPXEnvironment]):
    """The tests for the pipx environment plugin."""

    @staticmethod
    @pytest.fixture(name="plugin_type", scope="session")
    def fixture_plugin_type() -> type[PIPXEnvironment]:
        """A required testing hook that allows type generation.

        Returns:
            The type of the Environment
        """
        return PIPXEnvironment


# ---------------------------------------------------------------------------
# is_available_for() tests
# ---------------------------------------------------------------------------


class TestIsAvailableFor:
    """Runtime-aware availability checks for the pipx plugin.

    pipx is a Python module (``python -m pipx``), so availability
    should be probed via the resolved interpreter — not just PATH.
    """

    @staticmethod
    def test_returns_true_when_module_importable(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """is_available_for() returns True when the target interpreter has pipx."""
        python = tmp_path / "python.exe"
        python.touch()
        ctx = RuntimeContext(executables={"python": python})

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=0),
        )
        assert PIPXEnvironment.is_available_for(ctx) is True

    @staticmethod
    def test_returns_false_when_module_missing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """is_available_for() returns False when the target interpreter lacks pipx."""
        python = tmp_path / "python.exe"
        python.touch()
        ctx = RuntimeContext(executables={"python": python})

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=1),
        )
        assert PIPXEnvironment.is_available_for(ctx) is False

    @staticmethod
    def test_returns_false_on_subprocess_error(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """is_available_for() returns False when the subprocess fails to start."""
        python = tmp_path / "python.exe"
        python.touch()
        ctx = RuntimeContext(executables={"python": python})

        def _raise(*a: Any, **kw: Any) -> None:
            raise OSError("not found")

        monkeypatch.setattr(subprocess, "run", _raise)
        assert PIPXEnvironment.is_available_for(ctx) is False

    @staticmethod
    def test_probes_runtime_interpreter(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """is_available_for() probes the runtime context interpreter, not sys.executable."""
        python = tmp_path / "special-python"
        python.touch()
        ctx = RuntimeContext(executables={"python": python})

        captured_args: list[Any] = []

        def _capture_run(args: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
            captured_args.append(args)
            return subprocess.CompletedProcess(args, returncode=0)

        monkeypatch.setattr(subprocess, "run", _capture_run)
        PIPXEnvironment.is_available_for(ctx)

        assert len(captured_args) == 1
        assert captured_args[0][0] == str(python)

    @staticmethod
    def test_falls_back_to_default_python(monkeypatch: pytest.MonkeyPatch) -> None:
        """is_available_for() probes the default interpreter when runtime_context has no Python."""
        ctx = RuntimeContext()  # empty — no resolved runtime

        captured_args: list[Any] = []

        def _capture_run(args: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
            captured_args.append(args)
            return subprocess.CompletedProcess(args, returncode=0)

        monkeypatch.setattr(subprocess, "run", _capture_run)
        PIPXEnvironment.is_available_for(ctx)

        assert len(captured_args) == 1
        assert captured_args[0][0] == default_python()
