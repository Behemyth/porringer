"""Helpers for test concurrency and client.

Tests for concurrency limiting, shared aiohttp.ClientSession, and console check command.

Validates:
1. ``max_concurrency`` field on ``SetupParameters``
2. Semaphore bounding in ``inspect_actions``
3. Shared ``aiohttp.ClientSession`` is threaded through the update-check chain
4. Console ``check`` command properly awaits async methods
"""

import asyncio
import contextlib
import inspect
from pathlib import Path
from typing import override
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from packaging.version import Version

from porringer.backend.command.core import execution as execution_module
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.inspection import inspect_actions
from porringer.backend.command.core.resolution import (
    ResolutionContext,
    check_for_newer_version,
)
from porringer.backend.command.package import PackageCommands
from porringer.core.plugin_schema.environment import CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import (
    Distribution,
    Ecosystem,
    Package,
    PackageRef,
    PluginKind,
    PluginParameters,
)
from porringer.schema import (
    ActionCompletedEvent,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.0')))
_EXPECTED_WORKER_TASKS = 2
_MANY_ACTIONS = 6


class _StubEnv(Environment):
    """Minimal environment stub for concurrency tests."""

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        """Return ecosystem identifier."""
        return Ecosystem('python')

    @classmethod
    @override
    def tool_name(cls) -> str | None:
        """Return tool name."""
        return 'stub'

    @override
    def install_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        """Return install command."""
        return ['stub', 'install', package.name]

    @override
    def uninstall_command(self, package: PackageRef, *, runtime_context: RuntimeContext | None = None) -> list[str]:
        """Return uninstall command."""
        return ['stub', 'uninstall', package.name]

    @override
    def upgrade_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        """Return upgrade command."""
        return ['stub', 'upgrade', package.name]

    @override
    async def packages(
        self, *, project_path: Path | None = None, runtime_context: RuntimeContext | None = None
    ) -> list[Package]:
        """Return empty package list."""
        return [Package(name='existing', version='1.0.0')]

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        """Return no updates."""
        return []

    @classmethod
    @override
    def supports_parallel(cls) -> bool:
        """Support parallel execution."""
        return True


def _make_action(name: str, installer: str = 'stub') -> SetupAction:
    """Build a simple PACKAGE action for testing."""
    return SetupAction(
        description=f'Install {name}',
        kind=PluginKind.PACKAGE,
        ecosystem=Ecosystem('python'),
        installer=installer,
        package=PackageRef.model_validate(name),
    )


def _make_preview(actions: list[SetupAction]) -> SetupResults:
    """Build a manifest preview for inspection tests."""
    return SetupResults(actions=actions, root_directory=Path.cwd())


# =========================================================================
# CheckUpdatesParameters.http_client
# =========================================================================


class TestHttpClientField:
    """Tests for the ``http_client`` field on CheckUpdatesParameters."""

    @staticmethod
    def test_with_client() -> None:
        """Accepts an aiohttp.ClientSession instance."""
        client = MagicMock(spec=aiohttp.ClientSession)
        params = CheckUpdatesParameters(packages=[], http_client=client)
        assert params.http_client is client

    @staticmethod
    def test_excluded_from_serialization() -> None:
        """The http_client field is excluded from model dumps."""
        client = MagicMock(spec=aiohttp.ClientSession)
        params = CheckUpdatesParameters(packages=[], http_client=client)
        dumped = params.model_dump()
        assert 'http_client' not in dumped


# =========================================================================
# ResolutionContext.http_client
# =========================================================================


class TestResolutionContextHttpClient:
    """Tests for the ``http_client`` field on ResolutionContext."""

    @staticmethod
    def test_with_client() -> None:
        """Accepts an aiohttp.ClientSession instance."""
        client = MagicMock(spec=aiohttp.ClientSession)
        ctx = ResolutionContext(http_client=client)
        assert ctx.http_client is client


# =========================================================================
# Semaphore bounding in inspection
# =========================================================================


class TestInspectionConcurrencyBounding:
    """``inspect_actions`` respects ``max_concurrency``."""

    @staticmethod
    async def test_semaphore_limits_concurrency() -> None:
        """With max_concurrency=2, at most 2 tasks run concurrently."""
        environments: dict[str, Environment] = {'stub': _StubEnv(_MOCK_PARAMS)}
        plugins = DiscoveredPlugins(environments=environments, project_environments={}, scm_environments={})
        max_concurrent = 2
        params = SetupParameters(max_concurrency=max_concurrent)

        # Track peak concurrency
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def _counting_inspect(action, envs, **kwargs):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            # Simulate some async work
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1
            return SetupActionResult(action=action, success=True, skipped=True, message='already installed')

        num_actions = 6
        actions = [_make_action(f'pkg-{i}') for i in range(num_actions)]

        with patch(
            'porringer.backend.command.core.inspection.inspect_action',
            side_effect=_counting_inspect,
        ):
            results = await inspect_actions(_make_preview(actions), plugins, params)

        assert len(results) == num_actions
        assert all(r.success for r in results)
        assert peak <= max_concurrent, f'Peak concurrency was {peak}, expected <= {max_concurrent}'

    @staticmethod
    async def test_task_creation_is_bounded_by_max_concurrency() -> None:
        """With max_concurrency set, inspect creates worker tasks, not one task per action."""
        environments: dict[str, Environment] = {'stub': _StubEnv(_MOCK_PARAMS)}
        plugins = DiscoveredPlugins(environments=environments, project_environments={}, scm_environments={})
        params = SetupParameters(max_concurrency=_EXPECTED_WORKER_TASKS)
        actions = [_make_action(f'pkg-{i}') for i in range(_MANY_ACTIONS)]
        groups: list[object] = []

        class _CountingTaskGroup:
            """Minimal TaskGroup stand-in that records created tasks."""

            def __init__(self) -> None:
                self.created = 0
                self._tasks: list[asyncio.Task[None]] = []
                groups.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                if self._tasks:
                    await asyncio.gather(*self._tasks)
                return False

            def create_task(self, coro) -> asyncio.Task[None]:
                self.created += 1
                task = asyncio.create_task(coro)
                self._tasks.append(task)
                return task

        async def _mock_inspect(action, envs, **kwargs):
            return SetupActionResult(action=action, success=True, skipped=True, message='ok')

        with (
            patch('porringer.backend.command.core.inspection.inspect_action', side_effect=_mock_inspect),
            patch('porringer.backend.command.core.inspection.asyncio.TaskGroup', _CountingTaskGroup),
        ):
            results = await inspect_actions(_make_preview(actions), plugins, params)

        assert len(results) == len(actions)
        first_group = groups[0]
        assert isinstance(first_group, _CountingTaskGroup)
        assert first_group.created == _EXPECTED_WORKER_TASKS

    @staticmethod
    async def test_unlimited_concurrency() -> None:
        """With max_concurrency=0, all tasks run simultaneously.

        Each mocked inspection blocks until every task has started.  If the
        scheduler serialized work, the first task would block forever and the
        ``wait_for`` would time out, so reaching ``peak == num_actions`` proves
        the actions genuinely ran in parallel.
        """
        environments: dict[str, Environment] = {'stub': _StubEnv(_MOCK_PARAMS)}
        plugins = DiscoveredPlugins(environments=environments, project_environments={}, scm_environments={})
        params = SetupParameters(max_concurrency=0)

        num_actions = 5
        active = 0
        peak = 0
        all_started = asyncio.Event()

        async def _mock_inspect(action, envs, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active >= num_actions:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=5)
            active -= 1
            return SetupActionResult(action=action, success=True, skipped=True, message='ok')

        actions = [_make_action(f'pkg-{i}') for i in range(num_actions)]

        with patch(
            'porringer.backend.command.core.inspection.inspect_action',
            side_effect=_mock_inspect,
        ):
            results = await inspect_actions(_make_preview(actions), plugins, params)

        assert len(results) == num_actions
        assert peak == num_actions


# =========================================================================
# Shared aiohttp.ClientSession threading
# =========================================================================


class TestSharedHttpClient:
    """Shared client is forwarded through the update-check chain."""

    @staticmethod
    async def test_check_for_newer_version_passes_client() -> None:
        """``check_for_newer_version`` includes http_client in params."""
        env = _StubEnv(_MOCK_PARAMS)
        client = MagicMock(spec=aiohttp.ClientSession)

        captured_params: list[CheckUpdatesParameters] = []

        async def _spy_check_updates(params: CheckUpdatesParameters) -> list[Package]:
            captured_params.append(params)
            return [Package(name='ruff', version='0.9.0')]

        env.check_updates = _spy_check_updates

        result = await check_for_newer_version(
            env,
            PackageRef.model_validate('ruff'),
            '0.8.0',
            http_client=client,
        )
        assert result == '0.9.0'
        assert len(captured_params) == 1
        assert captured_params[0].http_client is client

    @staticmethod
    async def test_check_for_newer_version_none_client() -> None:
        """When no client is provided, http_client is None in params."""
        env = _StubEnv(_MOCK_PARAMS)

        captured_params: list[CheckUpdatesParameters] = []

        async def _spy_check_updates(params: CheckUpdatesParameters) -> list[Package]:
            captured_params.append(params)
            return [Package(name='ruff', version='0.9.0')]

        env.check_updates = _spy_check_updates

        await check_for_newer_version(
            env,
            PackageRef.model_validate('ruff'),
            '0.8.0',
        )
        assert captured_params[0].http_client is None

    @staticmethod
    async def test_inspection_creates_shared_client() -> None:
        """Inspection creates a shared aiohttp.ClientSession for all tasks."""
        environments: dict[str, Environment] = {'stub': _StubEnv(_MOCK_PARAMS)}
        plugins = DiscoveredPlugins(environments=environments, project_environments={}, scm_environments={})
        params = SetupParameters(max_concurrency=0)

        seen_clients: list[aiohttp.ClientSession | None] = []

        async def _capture_client(action, envs, **kwargs):
            ctx = kwargs.get('context')
            seen_clients.append(ctx.http_client if ctx else None)
            return SetupActionResult(action=action, success=True, skipped=True, message='ok')

        num_actions = 3
        actions = [_make_action(f'pkg-{i}') for i in range(num_actions)]

        with patch(
            'porringer.backend.command.core.inspection.inspect_action',
            side_effect=_capture_client,
        ):
            await inspect_actions(_make_preview(actions), plugins, params)

        # All tasks should have received a non-None client
        assert len(seen_clients) == num_actions
        assert all(c is not None for c in seen_clients)
        # All tasks should share the same client instance
        assert seen_clients[0] is seen_clients[1] is seen_clients[2]


# =========================================================================
# Console check command fix
# =========================================================================


class TestConsoleCheckAsync:
    """Console check command properly awaits async plugin methods."""

    @staticmethod
    def test_check_updates_is_async() -> None:
        """``PackageCommands.check_updates`` is a coroutine function."""
        assert inspect.iscoroutinefunction(PackageCommands.check_updates)


# =========================================================================
# Parallel package execution: fail_fast result shape
# =========================================================================


class TestParallelPackagesFailFast:
    """``_run_parallel_packages`` reports a stable result shape under fail_fast."""

    @staticmethod
    async def test_fail_fast_returns_failed_result_and_stops() -> None:
        """A failing action yields its result and signals ``should_continue=False``.

        Exercises the fail_fast + ``max_concurrency`` path: the failing
        action's result must be present (not silently dropped), and the
        function must report that execution should stop.
        """
        environments: dict[str, Environment] = {'stub': _StubEnv(_MOCK_PARAMS)}
        params = SetupParameters(max_concurrency=2, fail_fast=True)
        event_queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()

        actions = [_make_action(f'pkg-{i}') for i in range(4)]
        failing_package = actions[1].package.name if actions[1].package else ''

        async def _fake_execute_package(action, *_args, **_kwargs):
            success = (action.package.name if action.package else '') != failing_package
            return SetupActionResult(
                action=action,
                success=success,
                message='ok' if success else 'boom',
            )

        with patch.object(execution_module, 'execute_package', side_effect=_fake_execute_package):
            results, should_continue = await execution_module._run_parallel_packages(
                actions,
                environments,
                params,
                event_queue,
            )

        assert should_continue is False
        failed = [r for r in results if not r.success]
        assert len(failed) == 1
        assert failed[0].message == 'boom'
        assert (failed[0].action.package.name if failed[0].action.package else '') == failing_package


class TestPackageCancellation:
    """Package execution must preserve caller cancellation semantics."""

    @staticmethod
    async def test_attempt_operation_propagates_cancellation() -> None:
        """A cancelled plugin operation is not converted into a failed result."""
        action = _make_action('cancelled-package')
        event_queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()

        async def cancelled_operation(_params):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await execution_module._attempt_operation(
                action,
                spec=execution_module.OperationSpec(
                    execute=cancelled_operation,
                    verb='install',
                    verb_past='Installed',
                ),
                event_queue=event_queue,
            )

    @staticmethod
    @pytest.mark.parametrize('source', ['child', 'caller'])
    async def test_parallel_cancellation_cleans_up_siblings(source: str) -> None:
        """Cancellation settles siblings without emitting completed results."""
        cancelled_action = _make_action('cancelled-package')
        sibling_action = _make_action('sibling-package')
        sibling_started = asyncio.Event()
        sibling_finished = asyncio.Event()
        block = asyncio.Event()
        event_queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()

        async def execute_package(action: SetupAction, *_args, **_kwargs) -> None:
            if action is cancelled_action:
                await sibling_started.wait()
                if source == 'child':
                    raise asyncio.CancelledError()
                await block.wait()
            else:
                try:
                    sibling_started.set()
                    await block.wait()
                finally:
                    sibling_finished.set()

        with patch.object(execution_module, 'execute_package', side_effect=execute_package):
            owner = asyncio.create_task(
                execution_module._run_parallel_packages(
                    [cancelled_action, sibling_action],
                    {'stub': _StubEnv(_MOCK_PARAMS)},
                    SetupParameters(max_concurrency=2),
                    event_queue,
                )
            )
            try:
                await asyncio.wait_for(sibling_started.wait(), timeout=5)
                if source == 'caller':
                    owner.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(owner), timeout=5)
            finally:
                owner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(owner, timeout=5)

        assert sibling_finished.is_set()
        assert owner.cancelled()
        while not event_queue.empty():
            assert not isinstance(event_queue.get_nowait(), ActionCompletedEvent)
