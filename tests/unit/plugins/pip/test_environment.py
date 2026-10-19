"""Helpers for test environment.

Tests for the pip environment plugin.
"""

import asyncio
import inspect
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from packaging.version import Version
from porringer.core.plugin_schema.python_environment import default_python
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Distribution, Package, PluginParameters
from porringer.plugin.pip.plugin import PIPEnvironment
from porringer.test.mock.subprocess import fake_proc as _fake_proc
from porringer.test.pytest.tests import EnvironmentUnitTests


class TestEnvironment(EnvironmentUnitTests[PIPEnvironment]):
    """The tests for the pip environment plugin."""

    @staticmethod
    @pytest.fixture(name="plugin_type", scope="session")
    def fixture_plugin_type() -> type[PIPEnvironment]:
        """A required testing hook that allows type generation.

        Returns:
            The type of the Environment
        """
        return PIPEnvironment


# ---------------------------------------------------------------------------
# is_available_for() tests
# ---------------------------------------------------------------------------


class TestIsAvailableFor:
    """Runtime-aware availability checks for the pip plugin."""

    @staticmethod
    def test_returns_true_when_module_importable(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """is_available_for() returns True when the target interpreter has pip."""
        python = tmp_path / "python.exe"
        python.touch()
        ctx = RuntimeContext(executables={"python": python})

        # Simulate a successful `python -c "import pip"` subprocess
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=0),
        )
        assert PIPEnvironment.is_available_for(ctx) is True

    @staticmethod
    def test_returns_false_when_module_missing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """is_available_for() returns False when the target interpreter lacks pip."""
        python = tmp_path / "python.exe"
        python.touch()
        ctx = RuntimeContext(executables={"python": python})

        # Simulate a failed `python -c "import pip"` subprocess
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=1),
        )
        assert PIPEnvironment.is_available_for(ctx) is False

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
        assert PIPEnvironment.is_available_for(ctx) is False

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
        PIPEnvironment.is_available_for(ctx)

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
        PIPEnvironment.is_available_for(ctx)

        assert len(captured_args) == 1
        assert captured_args[0][0] == default_python()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_PACKAGES = [
    {"name": "ruff", "version": "0.15.0"},
    {"name": "pytest", "version": "9.0.2"},
    {"name": "pydantic", "version": "2.12.5"},
]
"""Simulated package list shared across scenarios."""


def _make_env() -> PIPEnvironment:
    """Create a fresh PIPEnvironment for testing (no cached packages)."""
    params = PluginParameters(distribution=Distribution(version=Version("0.0.0")))
    return PIPEnvironment(params)


def _mock_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[..., AsyncMock],
) -> None:
    """Replace asyncio.create_subprocess_exec with *handler*.

    *handler* may be either a regular callable (lambda) or an async function.
    We always wrap it in an ``AsyncMock`` so that the ``await`` on the call-site
    resolves correctly.
    """
    if inspect.iscoroutinefunction(handler):
        monkeypatch.setattr(asyncio, "create_subprocess_exec", handler)
    else:
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(side_effect=handler)
        )


# ---------------------------------------------------------------------------
# Scenario: venv with pip installed (standard venv / virtualenv)
# ---------------------------------------------------------------------------


class TestVenvWithPip:
    """Simulate a virtual environment where `python -m pip` works normally."""

    @staticmethod
    async def test_lists_packages(monkeypatch: pytest.MonkeyPatch) -> None:
        """Pip list succeeds — packages returned directly, no fallback needed."""
        pip_json = json.dumps(_SAMPLE_PACKAGES)

        async def run(*a: Any, **kw: Any) -> AsyncMock:
            return _fake_proc(stdout=pip_json)

        _mock_subprocess(monkeypatch, run)
        result = await _make_env().packages()

        assert result == [
            Package(name=p["name"], version=p["version"]) for p in _SAMPLE_PACKAGES
        ]

    @staticmethod
    async def test_skips_entries_without_name(monkeypatch: pytest.MonkeyPatch) -> None:
        """Entries missing a `name` key are silently dropped."""
        pip_json = json.dumps(
            [
                {"name": "ruff", "version": "0.15.0"},
                {"version": "1.0.0"},
                {"name": None, "version": "2.0.0"},
            ]
        )
        _mock_subprocess(monkeypatch, lambda *a, **kw: _fake_proc(stdout=pip_json))

        result = await _make_env().packages()
        assert len(result) == 1
        assert result[0].name == "ruff"

    @staticmethod
    async def test_empty_environment(monkeypatch: pytest.MonkeyPatch) -> None:
        """A venv with nothing installed returns an empty list."""
        _mock_subprocess(monkeypatch, lambda *a, **kw: _fake_proc(stdout="[]"))

        assert await _make_env().packages() == []


# ---------------------------------------------------------------------------
# Scenario: uv-created venv (no pip module, pip list fails)
# ---------------------------------------------------------------------------


class TestVenvWithoutPip:
    """Simulate a venv where `python -m pip list` fails.

    When pip is unavailable the plugin falls back to
    ``importlib.metadata`` to list installed packages so that
    presence detection still works in pip-less environments
    (e.g. PDM-managed or uv-created venvs).
    """

    @staticmethod
    async def test_pip_failure_falls_back_to_importlib(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pip list fails → importlib.metadata fallback used."""
        importlib_json = json.dumps(
            [
                {"name": "packaging", "version": "24.0"},
                {"name": "pytest", "version": "9.0.2"},
            ]
        )
        call_count = 0

        async def run(*args: Any, **kw: Any) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            # First call is `python -m pip list`; second is `python -c ...`
            if call_count == 1:
                return _fake_proc(returncode=1, stderr="pip error")
            return _fake_proc(stdout=importlib_json)

        _mock_subprocess(monkeypatch, run)
        result = await _make_env().packages()

        expected_calls = 2
        assert call_count == expected_calls
        assert len(result) == expected_calls
        assert result[0] == Package(name="packaging", version="24.0")

    @staticmethod
    async def test_both_pip_and_importlib_fail(monkeypatch: pytest.MonkeyPatch) -> None:
        """When both pip and importlib.metadata fail, return empty."""

        async def run(*args: Any, **kw: Any) -> AsyncMock:
            return _fake_proc(returncode=1, stderr="error")

        _mock_subprocess(monkeypatch, run)
        result = await _make_env().packages()

        assert result == []


# ---------------------------------------------------------------------------
# Scenario: global Python (no venv active, python not on PATH, etc.)
# ---------------------------------------------------------------------------


class TestGlobalEnvironment:
    """Simulate global (system-wide) Python and edge cases around PATH."""

    @staticmethod
    async def test_python_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
        """When `python` is not found, return empty without crashing."""

        async def run(*a: Any, **kw: Any) -> AsyncMock:
            raise FileNotFoundError

        _mock_subprocess(monkeypatch, run)
        assert await _make_env().packages() == []

    @staticmethod
    async def test_pip_returns_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
        """Corrupt pip output is handled gracefully (returns empty, no fallback)."""
        _mock_subprocess(monkeypatch, lambda *a, **kw: _fake_proc(stdout="not json"))

        assert await _make_env().packages() == []

    @staticmethod
    async def test_pip_returns_global_packages(monkeypatch: pytest.MonkeyPatch) -> None:
        """A global Python with pip works the same as a venv with pip."""
        global_pkgs = [
            {"name": "setuptools", "version": "75.0.0"},
            {"name": "wheel", "version": "0.45.0"},
        ]
        _mock_subprocess(
            monkeypatch, lambda *a, **kw: _fake_proc(stdout=json.dumps(global_pkgs))
        )

        result = await _make_env().packages()
        expected_package_count = 2
        assert len(result) == expected_package_count
        assert result[0] == Package(name="setuptools", version="75.0.0")


# ---------------------------------------------------------------------------
# Caching behaviour
# ---------------------------------------------------------------------------


class TestCaching:
    """Verify that packages() calls through to the subprocess each time.

    Per-instance caching has been removed — caching is now handled by
    the resolution-layer ``PackageCache``.  Each ``packages()`` call
    invokes the underlying subprocess so the cache layer can control
    freshness.
    """

    @staticmethod
    async def test_each_call_queries_subprocess(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each packages() call invokes the subprocess (no internal memoisation)."""
        call_count = 0

        async def run(*a: Any, **kw: Any) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            return _fake_proc(
                stdout=json.dumps([{"name": "ruff", "version": "0.15.0"}])
            )

        _mock_subprocess(monkeypatch, run)
        env = _make_env()

        first = await env.packages()
        second = await env.packages()

        assert first == second
        _expected_subprocess_calls = 2
        assert call_count == _expected_subprocess_calls

    @staticmethod
    async def test_separate_instances_independent(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each PIPEnvironment instance queries independently."""
        call_count = 0

        async def run(*a: Any, **kw: Any) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            return _fake_proc(
                stdout=json.dumps([{"name": "ruff", "version": "0.15.0"}])
            )

        _mock_subprocess(monkeypatch, run)
        env1 = _make_env()
        env2 = _make_env()

        await env1.packages()
        await env2.packages()

        expected_calls = 2
        assert call_count == expected_calls


# ---------------------------------------------------------------------------
# Guard: python_command must target the user's default interpreter
# ---------------------------------------------------------------------------


class TestPythonCommand:
    """Verify `python_command` resolves to the user's default interpreter.

    Never a bare `'python'` from PATH and never the venv the suite runs in:
    packages belong to the interpreter that venv was created from.
    """

    @staticmethod
    def test_default_is_default_python() -> None:
        """Without a runtime provider the command is the user's default interpreter."""
        env = _make_env()
        assert env.python_command() == default_python()

    @staticmethod
    def test_runtime_override_takes_precedence() -> None:
        """When a runtime provider resolves a path, that path wins."""
        env = _make_env()
        custom = Path("/custom/python3")
        rc = RuntimeContext(executables={"python": custom})
        assert env.python_command(rc) == str(custom)


# ---------------------------------------------------------------------------
# Smoke test: unmocked packages() against the live environment
# ---------------------------------------------------------------------------


class TestLivePackages:
    """Run `packages()` against the real interpreter (no mocking).

    This catches the scenario where `python_command` resolves to a Python
    that does *not* have the project's dependencies — the exact failure mode
    observed on Windows and Linux CI.
    """

    @staticmethod
    async def test_known_dev_dependencies_are_visible() -> None:
        """At least the known dev-dependencies must be discoverable."""
        env = _make_env()
        running = RuntimeContext(executables={"python": Path(sys.executable)})
        installed = {
            p.name.lower() for p in await env.packages(runtime_context=running)
        }

        # These are always present in the dev/test environment
        expected = {"pytest", "packaging"}
        missing = expected - installed
        assert not missing, (
            f"Dev-dependencies not visible to PIPEnvironment.packages(): {missing}. "
            f"python_command={env.python_command()!r}"
        )
