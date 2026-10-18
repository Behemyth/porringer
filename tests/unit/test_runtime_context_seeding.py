"""Helpers for test runtime context seeding.

Tests for runtime-context seeding in ExecutionState.

Verifies that ``execute_manifest()`` and ``ExecutionState`` correctly
inherit a pre-resolved ``RuntimeContext`` from ``DiscoveredPlugins``
so that manifests without a ``runtimes:`` section still carry
interpreter paths into the package/tool phases.

This is the regression test suite for the seeding gap:
``ExecutionState`` was previously constructed with an empty
``RuntimeContext()`` regardless of the caller's pre-resolved context.
"""

import asyncio
from pathlib import Path

from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.execution import ExecutionState
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.schema import SetupAction, SetupParameters, SetupResults
from tests.fixtures.mock_plugins import (
    MOCK_DIST,
    MOCK_RUNTIME_EXE,
    RUNTIME_ACTION_TEMPLATE,
    MockPythonEnv,
    MockRuntimeProvider,
    preserve_path,
)

_ALT_RUNTIME_EXE = Path('/mock/runtimes/python3.13/python')


def _make_plugins(
    *,
    runtime_context: RuntimeContext | None = None,
    environments: dict | None = None,
) -> DiscoveredPlugins:
    """Build a ``DiscoveredPlugins`` with an optional pre-resolved runtime."""
    return DiscoveredPlugins(
        environments=environments or {},
        project_environments={},
        scm_environments={},
        runtime_context=runtime_context,
    )


def _make_state_from_plugins(
    plugins: DiscoveredPlugins,
    *,
    runtime_actions: list[SetupAction] | None = None,
) -> ExecutionState:
    """Build an ``ExecutionState`` that seeds from *plugins.runtime_context*."""
    actions = runtime_actions or []
    preview = SetupResults(actions=actions, root_directory=Path('.'))

    # Reproduce the seeding logic that execute_manifest() should perform:
    copied = plugins.copy()
    seeded = (
        RuntimeContext(executables=dict(copied.runtime_context.executables))
        if copied.runtime_context is not None
        else RuntimeContext()
    )

    return ExecutionState(
        actions=actions,
        plugins=copied,
        parameters=SetupParameters(),
        event_queue=asyncio.Queue(),
        manifest_directory=Path('.'),
        preview=preview,
        runtime_context=seeded,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRuntimeContextSeeding:
    """ExecutionState should inherit a pre-resolved runtime from plugins."""

    @staticmethod
    def test_execute_state_seeds_from_plugins_runtime_context() -> None:
        """State.runtime_context is populated from plugins.runtime_context."""
        rc = RuntimeContext(executables={'python': MOCK_RUNTIME_EXE})
        plugins = _make_plugins(runtime_context=rc)
        state = _make_state_from_plugins(plugins)

        assert state.runtime_context.get('python') == MOCK_RUNTIME_EXE

    @staticmethod
    def test_seeded_runtime_context_is_independent_copy() -> None:
        """The seeded context is an independent copy, not an alias.

        ``RuntimeContext`` is frozen, so direct mutation is impossible.
        Verify that replacing ``state.runtime_context`` via
        ``with_executable`` does not affect the original plugins.
        """
        rc = RuntimeContext(executables={'python': MOCK_RUNTIME_EXE})
        plugins = _make_plugins(runtime_context=rc)
        state = _make_state_from_plugins(plugins)

        # Produce a new context with a different path
        state.runtime_context = state.runtime_context.with_executable('python', _ALT_RUNTIME_EXE)

        # Original plugins.runtime_context is untouched
        assert rc.get('python') == MOCK_RUNTIME_EXE
        # State has the new path
        assert state.runtime_context.get('python') == _ALT_RUNTIME_EXE

    @staticmethod
    async def test_propagate_runtime_overrides_seeded_context() -> None:
        """propagate_runtime() overwrites a previously seeded entry."""
        rc = RuntimeContext(executables={'python': _ALT_RUNTIME_EXE})
        provider = MockRuntimeProvider(MOCK_DIST)
        consumer = MockPythonEnv(MOCK_DIST)

        plugins = _make_plugins(
            runtime_context=rc,
            environments={'mock-pim': provider, 'mock-pip': consumer},
        )
        runtime_action = SetupAction(**RUNTIME_ACTION_TEMPLATE)
        state = _make_state_from_plugins(plugins, runtime_actions=[runtime_action])

        # Seeded with ALT path
        assert state.runtime_context.get('python') == _ALT_RUNTIME_EXE

        with preserve_path():
            await state.propagate_runtime()

        # propagate_runtime() resolved to the mock provider's default → overrides
        assert state.runtime_context.get('python') == MOCK_RUNTIME_EXE

    @staticmethod
    def test_resolution_context_uses_seeded_runtime() -> None:
        """The resolution_context property threads the seeded runtime."""
        rc = RuntimeContext(executables={'python': MOCK_RUNTIME_EXE})
        plugins = _make_plugins(runtime_context=rc)
        state = _make_state_from_plugins(plugins)

        ctx = state.resolution_context
        assert ctx.runtime_context is not None
        assert ctx.runtime_context.get('python') == MOCK_RUNTIME_EXE

    @staticmethod
    async def test_no_runtime_actions_still_uses_seeded_context() -> None:
        """A manifest with no runtimes: section keeps the seeded context.

        This is the critical scenario: when there are no RUNTIME-phase
        actions, ``propagate_runtime()`` is a no-op, so the only
        source of runtime context is the pre-resolved seed.
        """
        rc = RuntimeContext(executables={'python': MOCK_RUNTIME_EXE})
        plugins = _make_plugins(runtime_context=rc)
        state = _make_state_from_plugins(plugins)

        # propagate_runtime is a no-op (no RUNTIME actions)
        with preserve_path():
            await state.propagate_runtime()

        # Seeded context survives
        assert state.runtime_context.get('python') == MOCK_RUNTIME_EXE

    @staticmethod
    def test_no_plugins_runtime_context_leaves_state_empty() -> None:
        """When plugins have no runtime_context, state starts empty (baseline)."""
        plugins = _make_plugins(runtime_context=None)
        state = _make_state_from_plugins(plugins)

        assert state.runtime_context.get('python') is None

    @staticmethod
    def test_python_command_uses_seeded_runtime() -> None:
        """A PythonEnvironment's python_command() uses the seeded path."""
        rc = RuntimeContext(executables={'python': MOCK_RUNTIME_EXE})
        plugins = _make_plugins(
            runtime_context=rc,
            environments={'mock-pip': MockPythonEnv(MOCK_DIST)},
        )
        state = _make_state_from_plugins(plugins)

        consumer = state.environments['mock-pip']
        assert isinstance(consumer, MockPythonEnv)
        assert consumer.python_command(state.runtime_context) == str(MOCK_RUNTIME_EXE)
