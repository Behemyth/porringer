"""Helpers for test runtime propagation.

Tests for runtime propagation across phase transitions and venv scoping.
"""

import asyncio
import inspect
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import override
from unittest.mock import patch

from packaging.version import Version
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.execution import (
    ExecutionState,
    invalidate_runtime_cache_after_mutation,
)
from porringer.backend.command.core.phase import PackagePhase, ToolPhase
from porringer.core.plugin_schema.environment import CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.project_environment import (
    ProjectEnvironment,
    ProjectInstaller,
)
from porringer.core.plugin_schema.python_environment import (
    PythonEnvironment,
    default_python,
)
from porringer.core.plugin_schema.runtime import (
    RuntimeConsumer,
    RuntimeContext,
    RuntimeProvider,
)
from porringer.core.schema import (
    Distribution,
    Ecosystem,
    Package,
    PackageRef,
    PluginKind,
    PluginParameters,
)
from porringer.schema import (
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_DIST = PluginParameters(distribution=Distribution(version=Version("0.0.0")))
_MOCK_RUNTIME_EXE = Path("/mock/runtimes/python3.14/python")
EXPECTED_RUNTIME_CACHE_INVALIDATIONS = 1

_RUNTIME_ACTION = SetupAction(
    description="Install python 3.14",
    kind=PluginKind.RUNTIME,
    installer="mock-pim",
    package=PackageRef(name="3.14"),
)


@contextmanager
def _preserve_path() -> Generator[None]:
    """Save and restore the PATH environment variable."""
    original = os.environ.get("PATH", "")
    try:
        yield
    finally:
        os.environ["PATH"] = original


class _MockRuntimeProvider(Environment, RuntimeProvider):
    """An environment that provides a Python runtime."""

    _resolved: Path | None = _MOCK_RUNTIME_EXE
    invalidations: int = 0

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        return Ecosystem("python")

    @classmethod
    @override
    def tool_name(cls) -> str:
        return "mock-pim"

    @classmethod
    @override
    def provided_runtime_kind(cls) -> str:
        """Return the kind of runtime this provider supplies."""
        return "python"

    @override
    async def resolve_executable(self, tag: str) -> Path | None:
        """Resolve a tagged runtime to a mock path."""
        return self._resolved

    @override
    async def available_tags(self) -> list[str]:
        """Return a canned tag list."""
        return ["3.14"] if self._resolved is not None else []

    @override
    def install_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        return ["mock-pim", "install", str(package)]

    @override
    def uninstall_command(
        self, package: PackageRef, *, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        return ["mock-pim", "uninstall", package.name]

    @override
    def upgrade_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        return ["mock-pim", "upgrade", str(package)]

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        return []

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        return []

    def invalidate_runtime_cache(self) -> None:
        """Track runtime cache invalidation calls for tests."""
        self.invalidations += 1


# Register as RuntimeProvider via structural subtyping check
assert isinstance(_MockRuntimeProvider(_MOCK_DIST), RuntimeProvider)


class TestRuntimeCacheInvalidation:
    """Runtime-provider caches are invalidated after successful mutations."""

    @staticmethod
    def test_successful_runtime_result_invalidates_provider_cache() -> None:
        """A successful runtime install/upgrade/uninstall invalidates the provider cache."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        provider.invalidations = 0
        result = SetupActionResult(action=_RUNTIME_ACTION, success=True)

        invalidate_runtime_cache_after_mutation(
            _RUNTIME_ACTION, {"mock-pim": provider}, result
        )

        assert provider.invalidations == EXPECTED_RUNTIME_CACHE_INVALIDATIONS

    @staticmethod
    def test_skipped_runtime_result_does_not_invalidate_provider_cache() -> None:
        """Skipped results do not invalidate runtime provider caches."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        provider.invalidations = 0
        result = SetupActionResult(action=_RUNTIME_ACTION, success=True, skipped=True)

        invalidate_runtime_cache_after_mutation(
            _RUNTIME_ACTION, {"mock-pim": provider}, result
        )

        assert provider.invalidations == 0


class _MockPythonEnv(PythonEnvironment):
    """A minimal RuntimeConsumer environment (like pip or uv)."""

    @classmethod
    @override
    def tool_name(cls) -> str | None:
        return "mock-pip"

    @override
    def install_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        return ["mock-pip", "install", str(package)]

    @override
    def uninstall_command(
        self, package: PackageRef, *, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        return ["mock-pip", "uninstall", package.name]

    @override
    def upgrade_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        return ["mock-pip", "upgrade", str(package)]

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        return []


class _MockProjectEnv(ProjectEnvironment):
    """A minimal RuntimeConsumer project environment."""

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        return Ecosystem("python")

    @classmethod
    @override
    def consumed_runtime_kind(cls) -> str:
        return "python"

    @classmethod
    @override
    def tool_name(cls) -> str:
        return "mock-pdm"


class _MockNodeConsumer(Environment, RuntimeConsumer):
    """An environment that consumes a Node runtime, not Python."""

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        return Ecosystem("node")

    @classmethod
    def consumed_runtime_kind(cls) -> str:
        """Return the kind of runtime this consumer requires."""
        return "node"

    @classmethod
    @override
    def tool_name(cls) -> str | None:
        return None

    @override
    def install_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        return []

    @override
    def uninstall_command(
        self, package: PackageRef, *, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        return []

    @override
    def upgrade_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        return []

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        return []

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        return []


def _make_state(
    *,
    environments: dict[str, Environment] | None = None,
    project_environments: dict[str, ProjectInstaller] | None = None,
    runtime_actions: list[SetupAction] | None = None,
) -> ExecutionState:
    """Build a minimal ExecutionState for testing."""
    actions = runtime_actions or []

    plugins = DiscoveredPlugins(
        environments=environments or {},
        project_environments=project_environments or {},
        scm_environments={},
    )
    preview = SetupResults(
        actions=actions,
        root_directory=Path("."),
    )

    return ExecutionState(
        actions=actions,
        plugins=plugins,
        parameters=SetupParameters(),
        event_queue=asyncio.Queue(),
        manifest_directory=Path("."),
        preview=preview,
    )


# ---------------------------------------------------------------------------
# Bug 1: Runtime propagation lost after phase transition
# ---------------------------------------------------------------------------


class TestRuntimePropagationAfterPluginRefresh:
    """Verify cached runtime is re-applied after plugin re-discovery."""

    @staticmethod
    async def test_refresh_all_plugins_re_propagates_runtime() -> None:
        """Runtime context persists across plugin refresh — no per-plugin mutation needed."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        consumer = _MockPythonEnv(_MOCK_DIST)
        state = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
            runtime_actions=[_RUNTIME_ACTION],
        )

        with _preserve_path():
            await state.propagate_runtime()

        # Runtime stored on the execution state, not on the plugin
        assert state.runtime_context.get("python") == _MOCK_RUNTIME_EXE

        # refresh_all_plugins replaces environments with new instances
        new_consumer = _MockPythonEnv(_MOCK_DIST)

        with (
            patch(
                "porringer.backend.command.core.execution.discover_all_plugins"
            ) as mock_discover,
            patch("porringer.backend.command.core.execution.refresh_path"),
            patch("porringer.backend.command.core.execution.invalidate_plugin_cache"),
        ):
            mock_discover.return_value = DiscoveredPlugins(
                environments={"mock-pip": new_consumer},
                project_environments={},
                scm_environments={},
            )
            state.refresh_all_plugins()

        assert state.environments["mock-pip"] is new_consumer
        # The runtime context survives the refresh — it lives on the state
        assert state.runtime_context.get("python") == _MOCK_RUNTIME_EXE

    @staticmethod
    def test_refresh_all_plugins_noop_without_runtime() -> None:
        """Without a resolved runtime, runtime_context remains empty."""
        state = _make_state(environments={"mock-pip": _MockPythonEnv(_MOCK_DIST)})
        assert state.runtime_context.get("python") is None

        new_consumer = _MockPythonEnv(_MOCK_DIST)
        with (
            patch(
                "porringer.backend.command.core.execution.discover_all_plugins"
            ) as mock_discover,
            patch("porringer.backend.command.core.execution.refresh_path"),
            patch("porringer.backend.command.core.execution.invalidate_plugin_cache"),
        ):
            mock_discover.return_value = DiscoveredPlugins(
                environments={"mock-pip": new_consumer},
                project_environments={},
                scm_environments={},
            )
            state.refresh_all_plugins()

        assert state.runtime_context.get("python") is None

    @staticmethod
    async def test_refresh_all_plugins_re_propagates_to_project_environments() -> None:
        """Runtime context persists across refresh — project environments get it too."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        consumer = _MockPythonEnv(_MOCK_DIST)
        proj_env = _MockProjectEnv(_MOCK_DIST)
        state = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
            project_environments={"mock-pdm": proj_env},
            runtime_actions=[_RUNTIME_ACTION],
        )

        with _preserve_path():
            await state.propagate_runtime()
        assert state.runtime_context.get("python") == _MOCK_RUNTIME_EXE

        new_proj_env = _MockProjectEnv(_MOCK_DIST)

        with (
            patch(
                "porringer.backend.command.core.execution.discover_all_plugins"
            ) as mock_discover,
            patch("porringer.backend.command.core.execution.refresh_path"),
            patch("porringer.backend.command.core.execution.invalidate_plugin_cache"),
        ):
            mock_discover.return_value = DiscoveredPlugins(
                environments={"mock-pim": provider, "mock-pip": consumer},
                project_environments={"mock-pdm": new_proj_env},
                scm_environments={},
            )
            state.refresh_all_plugins()

        assert state.project_environments is not None
        assert state.project_environments["mock-pdm"] is new_proj_env
        # The runtime context is on the state, not the plugin — it survives refresh
        assert state.runtime_context.get("python") == _MOCK_RUNTIME_EXE

    @staticmethod
    async def test_non_matching_runtime_kind_not_propagated() -> None:
        """A Node consumer does not receive the Python runtime."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        node_env = _MockNodeConsumer(_MOCK_DIST)
        state = _make_state(
            environments={"mock-pim": provider, "mock-npm": node_env},
            runtime_actions=[_RUNTIME_ACTION],
        )

        with _preserve_path():
            await state.propagate_runtime()
        # Python runtime is stored, but node runtime is not
        assert state.runtime_context.get("python") == _MOCK_RUNTIME_EXE
        assert state.runtime_context.get("node") is None

        new_node_env = _MockNodeConsumer(_MOCK_DIST)
        with (
            patch(
                "porringer.backend.command.core.execution.discover_all_plugins"
            ) as mock_discover,
            patch("porringer.backend.command.core.execution.refresh_path"),
            patch("porringer.backend.command.core.execution.invalidate_plugin_cache"),
        ):
            mock_discover.return_value = DiscoveredPlugins(
                environments={"mock-npm": new_node_env},
                project_environments={},
                scm_environments={},
            )
            state.refresh_all_plugins()

        # Node runtime still absent from context
        assert state.runtime_context.get("node") is None


# ---------------------------------------------------------------------------
# Bug 1b: Cache mutation — shared plugins must not carry runtime state
# ---------------------------------------------------------------------------


class TestCacheMutationRegression:
    """Regression test for the cache mutation bug.

    Previously ``_propagate_runtime`` wrote ``runtime_executable``
    directly onto plugin instances.  Because ``DiscoveredPlugins`` was
    cached at module level and ``copy()`` was shallow, a second
    ``ExecutionState`` that re-used the cached plugins would inherit
    stale runtime state.

    The bug is now prevented at two levels:

    1. **RuntimeContext refactor** — runtime state lives exclusively
       on ``ExecutionState.runtime_context``, never on plugin objects.
    2. **Factory pattern** — the plugin scan cache stores only
       lightweight ``PluginInformation`` metadata;
       ``DiscoveredPlugins.copy()`` creates fresh plugin instances,
       so accidental mutable state on a plugin can never leak
       between execution runs.

    These tests prove both guarantees hold.
    """

    @staticmethod
    async def test_shared_plugin_instance_not_mutated() -> None:
        """Plugin instances remain clean after propagate_runtime."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        consumer = _MockPythonEnv(_MOCK_DIST)

        state = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
            runtime_actions=[_RUNTIME_ACTION],
        )

        with _preserve_path():
            await state.propagate_runtime()

        # Runtime is recorded on the state's context …
        assert state.runtime_context.get("python") == _MOCK_RUNTIME_EXE
        # … but the plugin instance itself has no mutable runtime state.
        assert (
            not hasattr(consumer, "runtime_executable")
            or consumer.runtime_executable is None
        )

    @staticmethod
    async def test_second_state_with_same_plugins_gets_clean_context() -> None:
        """A fresh ExecutionState sharing the same plugins starts with empty context."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        consumer = _MockPythonEnv(_MOCK_DIST)

        state1 = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
            runtime_actions=[_RUNTIME_ACTION],
        )
        with _preserve_path():
            await state1.propagate_runtime()
        assert state1.runtime_context.get("python") == _MOCK_RUNTIME_EXE

        # A second state sharing the *exact same* plugin instances
        state2 = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
        )
        # Its context is unpolluted — the original bug would have failed here
        assert state2.runtime_context.get("python") is None

    @staticmethod
    async def test_python_command_isolation_between_states() -> None:
        """python_command() reflects only the calling state's context."""
        provider = _MockRuntimeProvider(_MOCK_DIST)
        consumer = _MockPythonEnv(_MOCK_DIST)

        state1 = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
            runtime_actions=[_RUNTIME_ACTION],
        )
        with _preserve_path():
            await state1.propagate_runtime()

        rc1 = state1.runtime_context
        assert consumer.python_command(rc1) == str(_MOCK_RUNTIME_EXE)

        # A second state with no runtime — same consumer instance
        state2 = _make_state(
            environments={"mock-pim": provider, "mock-pip": consumer},
        )
        rc2 = state2.runtime_context
        # Should fall back to the default interpreter, not the first state's runtime
        assert consumer.python_command(rc2) == default_python()


# ---------------------------------------------------------------------------
# Bug 2: PACKAGE/TOOL phases should not pass project_path
# ---------------------------------------------------------------------------


class TestPackagePhaseNoProjectPath:
    """Verify Phase 2a/2b do not scope presence checks to a project venv."""

    @staticmethod
    def test_python_command_uses_global_without_project_path() -> None:
        """Without project_path, python_command returns the runtime executable."""
        env = _MockPythonEnv(_MOCK_DIST)
        rc = RuntimeContext(executables={"python": _MOCK_RUNTIME_EXE})
        assert env.python_command(rc) == str(_MOCK_RUNTIME_EXE)

    @staticmethod
    def test_venv_discovered_only_with_project_path(tmp_path: Path) -> None:
        """_discover_venv_python returns a path only when a .venv exists."""
        if os.name == "nt":
            venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        assert PythonEnvironment._discover_venv_python(tmp_path) == venv_python
        assert PythonEnvironment._discover_venv_python(Path("/nonexistent")) is None

    @staticmethod
    def test_package_and_tool_phases_no_project_path() -> None:
        """PackagePhase and ToolPhase execute methods must not pass project_path."""
        package_source = inspect.getsource(PackagePhase.execute)
        tool_source = inspect.getsource(ToolPhase.execute)

        assert "project_path" not in package_source, (
            "PackagePhase.execute should not pass project_path"
        )
        assert "project_path" not in tool_source, (
            "ToolPhase.execute should not pass project_path"
        )
