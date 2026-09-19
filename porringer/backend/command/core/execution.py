"""CLI command implementation for execution.

Phased execution engine.

Orchestrates the multi-phase setup flow: runtime → packages → tools →
SCM → project-install.  Each phase ensures its prerequisites are met before
proceeding.
"""

import asyncio
import logging
import os
import sysconfig
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import aiohttp

from porringer.backend.backend import BackendResolver
from porringer.core.path import ensure_system_path, reset_sync_state
from porringer.core.plugin_schema.environment import Environment, PackageParameters
from porringer.core.plugin_schema.project_environment import (
    ProjectInstaller,
)
from porringer.core.plugin_schema.runtime import RuntimeContext, RuntimeProvider
from porringer.core.plugin_schema.scm import CloneParameters, ScmEnvironment
from porringer.core.plugin_schema.tool_based import ToolBasedPlugin
from porringer.core.schema import Ecosystem, Package, PluginKind
from porringer.schema import (
    ActionCompletedEvent,
    ActionProgress,
    ActionProgressEvent,
    ActionRef,
    ActionStartedEvent,
    Install,
    InstallReason,
    ManifestLoadedEvent,
    ManifestMetadata,
    Operation,
    PluginsDiscoveredEvent,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    Skip,
    SkipReason,
    SyncStrategy,
    Uninstall,
    Upgrade,
)
from porringer.schema.progress import DiscoveredPluginEntry
from porringer.utility import HTTP_TIMEOUT
from porringer.utility.exception import PluginError
from porringer.utility.trace import TraceContext, current_trace_context, use_trace_context
from porringer.utility.utility import CommandProgress, run_command

from .action_builder import (
    PHASE_ORDER,
    STRATEGY_VERB,
    action_description,
    get_cli_command,
)
from .discovery import (
    DiscoveredPlugins,
    discover_all_plugins,
    invalidate_plugin_cache,
)
from .phase import run_phases
from .presence import clone_status_to_result
from .resolution import (
    PackageCache,
    ResolutionContext,
    resolve_operation,
    resolved_to_result,
)

logger = logging.getLogger(__name__)


def _action_ref(action: SetupAction, ref_map: dict[int, ActionRef] | None) -> ActionRef | None:
    """Look up the stable action reference for *action* via its ``id()``."""
    if ref_map is None:
        return None
    return ref_map.get(id(action))


def _action_trace_context(
    mode: str,
    action: SetupAction,
    ref: ActionRef | None,
    *,
    operation: str | None = None,
) -> TraceContext:
    """Build compact trace metadata for an action."""
    return TraceContext(
        mode=mode,
        action_ref=ref,
        action_description=action.description,
        action_kind=action.kind.value if action.kind is not None else None,
        installer=action.installer,
        package_name=action.package.name if action.package is not None else None,
        operation=operation,
    )


def _current_action_ref() -> ActionRef | None:
    """Return the action ref from the active trace context, if any."""
    context = current_trace_context()
    return context.action_ref if context is not None else None


def _make_progress_callback(
    action: SetupAction, event_queue: asyncio.Queue[ProgressEvent | None]
) -> Callable[[ActionProgress], None]:
    """Return a callback that emits ``ActionProgressEvent`` for *action*."""

    def _callback(update: ActionProgress) -> None:
        event_queue.put_nowait(ActionProgressEvent(action=action, progress=update, action_ref=_current_action_ref()))

    return _callback


def _emit_started(
    action: SetupAction,
    ref: ActionRef | None,
    event_queue: asyncio.Queue[ProgressEvent | None],
) -> float:
    """Emit an ``ActionStartedEvent`` and return its monotonic start time."""
    event_queue.put_nowait(ActionStartedEvent(action=action, action_ref=ref))
    return time.monotonic()


def _emit_completed(
    action: SetupAction,
    result: SetupActionResult,
    ref: ActionRef | None,
    event_queue: asyncio.Queue[ProgressEvent | None],
    started_at: float | None = None,
) -> None:
    """Emit an ``ActionCompletedEvent`` for *action*."""
    if started_at is not None:
        result.duration_seconds = max(0.0, time.monotonic() - started_at)
    event_queue.put_nowait(ActionCompletedEvent(action=action, result=result, action_ref=ref))


@dataclass(slots=True)
class ExecutionState:
    """Mutable state for a single phased execution run.

    Owns the discovered plugin container, grouped phase actions, and
    common parameters that every phase needs.  Provides
    :meth:`refresh_all_plugins` and :meth:`propagate_runtime` as
    methods so callers don't need to pass a half-dozen arguments.

    Metadata fields (``manifest_path``, ``metadata``, ``preferences``)
    are accessed via the stored :attr:`preview`.
    """

    actions: list[SetupAction]
    plugins: DiscoveredPlugins
    parameters: SetupParameters
    event_queue: asyncio.Queue[ProgressEvent | None]
    manifest_directory: Path
    preview: SetupResults
    manifest_index: int = 0
    results: list[SetupActionResult] = field(default_factory=list)
    runtime_context: RuntimeContext = field(default_factory=RuntimeContext)
    """Accumulated runtime context for this execution run.  Populated
    by :meth:`propagate_runtime` and threaded through to every
    operation that needs an interpreter path."""
    _setup_complete: set[str] = field(default_factory=set)
    """Installer names whose ``setup()`` has been called this sync.
    Ensures each plugin's ``setup()`` runs at most once (G1)."""
    _teardown_complete: set[str] = field(default_factory=set)
    """Installer names whose ``teardown()`` has been called this sync.
    Ensures each plugin's ``teardown()`` runs at most once."""
    _cached_action_ref_map: dict[int, ActionRef] | None = field(default=None, init=False, repr=False)

    # Convenience accessors that delegate to the discovered plugins and preview data.

    @property
    def environments(self) -> dict[str, Environment]:
        """Environment plugins from the current discovery."""
        return self.plugins.environments

    @property
    def phases(self) -> dict[PluginKind, list[SetupAction]]:
        """Group actions into phase buckets on access."""
        return group_actions_by_phase(self.actions)

    @property
    def project_environments(self) -> dict[str, ProjectInstaller] | None:
        """Project-install plugins (may be empty dict)."""
        return self.plugins.project_environments or None

    @property
    def scm_environments(self) -> dict[str, ScmEnvironment] | None:
        """SCM-environment plugins (may be empty dict)."""
        return self.plugins.scm_environments or None

    @property
    def manifest_path(self) -> Path | None:
        """Path to the manifest file (from preview)."""
        return self.preview.manifest_path

    @property
    def metadata(self) -> ManifestMetadata | None:
        """Display metadata from the manifest."""
        return self.preview.metadata

    @property
    def preferences(self) -> dict[Ecosystem, str]:
        """Ecosystem → plugin-name preferences from the manifest."""
        return dict(self.preview.preferences)

    @property
    def skip_project(self) -> bool:
        """Whether project-install actions should be skipped."""
        return self.parameters.project_directory is False

    @property
    def fallback_dir(self) -> Path:
        """Working directory for SCM phases."""
        return determine_fallback_dir(self.parameters, self.manifest_directory)

    # Convenience properties for common execution state.

    @property
    def strategy(self) -> SyncStrategy:
        """The sync strategy from the current parameters."""
        return self.parameters.strategy

    @property
    def _action_ref_map(self) -> dict[int, ActionRef]:
        """Map ``id(action)`` to stable action references in :attr:`actions`."""
        if self._cached_action_ref_map is None:
            source_indices = self.preview.action_indices
            if len(source_indices) != len(self.actions):
                source_indices = list(range(len(self.actions)))
            self._cached_action_ref_map = {
                id(action): ActionRef.from_indices(self.manifest_index, source_index)
                for action, source_index in zip(self.actions, source_indices, strict=True)
            }
        return self._cached_action_ref_map

    # Refresh plugins and runtime state for a new execution run.

    def refresh_all_plugins(self) -> None:
        """Refresh PATH and re-discover all plugin types.

        Invalidates the plugin cache, re-discovers environments,
        project environments, and SCM environments.

        ``discover_all_plugins()`` always returns fresh instances
        (scan metadata is cached, instances are not), and ``.copy()``
        mints another fresh set for this execution state.
        ``runtime_context`` is *not* baked into plugin instances —
        it is threaded explicitly through every operation.
        """
        refresh_path()
        invalidate_plugin_cache()
        self.plugins = discover_all_plugins().copy()

    async def propagate_runtime(self) -> None:
        """Resolve interpreter paths from completed runtime actions.

        Finds the first ``RuntimeProvider`` among the RUNTIME-phase
        actions, resolves its executable, injects the directory onto
        ``PATH``, and stores the result in :attr:`runtime_context`
        so it can be threaded through subsequent operations.

        Plugin instances are **not** mutated.
        """
        result = await _propagate_runtime(
            self.phases[PluginKind.RUNTIME],
            self.plugins,
        )
        if result is not None:
            kind, executable = result
            self.runtime_context = self.runtime_context.with_executable(kind, executable)

    # Helpers for emitting and packaging execution results.

    def emit(self, event: ProgressEvent) -> None:
        """Put *event* on the event queue."""
        self.event_queue.put_nowait(event)

    @property
    def resolution_context(self) -> ResolutionContext:
        """Resolution context for this execution run.

        Bundles the project-environment references and runtime context
        that flow through every resolution and execution helper.
        """
        return ResolutionContext(
            project_environments=self.project_environments,
            runtime_context=self.runtime_context,
        )

    # Ensure plugin lifecycle hooks run once per sync.

    async def _ensure_plugins_setup(self, actions: list[SetupAction]) -> set[str]:
        """Call ``setup()`` on plugins before their first action in this sync.

        Each plugin's ``setup()`` is called at most once per sync
        (G1 idempotency gating).  Failures are logged at WARNING and
        the failing plugin name is returned so the caller can skip its
        actions (G2 soft-fail isolation).

        Returns:
            Set of installer names whose ``setup()`` raised.
        """
        failed: set[str] = set()
        for installer_name in dict.fromkeys(a.installer for a in actions if a.installer):
            if installer_name in self._setup_complete:
                continue
            env = self.environments.get(installer_name)
            if env is None:
                continue
            self._setup_complete.add(installer_name)
            try:
                await env.setup()
            except Exception:
                logger.warning(
                    "setup() failed for plugin '%s'; its actions will be skipped",
                    installer_name,
                    exc_info=True,
                )
                failed.add(installer_name)
        return failed

    # Delegates for the package, project, and SCM execution phases.

    async def run_package_actions(self, actions: list[SetupAction]) -> tuple[list[SetupActionResult], bool]:
        """Execute package/tool/runtime actions.

        Calls ``setup()`` on each plugin before its first action in
        this sync (G1 idempotency gating).

        Returns:
            Tuple of (results, should_continue).
        """
        setup_skip_results: list[SetupActionResult] = []
        failed = await self._ensure_plugins_setup(actions)
        if failed:
            remaining: list[SetupAction] = []
            action_ref_map = self._action_ref_map
            for a in actions:
                if a.installer in failed:
                    result = SetupActionResult(
                        action=a,
                        success=False,
                        message=f"Skipped: setup() failed for '{a.installer}'",
                    )
                    setup_skip_results.append(result)
                    ref = _action_ref(a, action_ref_map)
                    started_at = _emit_started(a, ref, self.event_queue)
                    _emit_completed(a, result, ref, self.event_queue, started_at)
                else:
                    remaining.append(a)
            actions = remaining

        results, ok = await execute_package_actions(
            actions,
            self.environments,
            self.parameters,
            self.event_queue,
            self.resolution_context,
            action_ref_map=self._action_ref_map,
        )
        return setup_skip_results + results, ok

    async def run_project_phase(self, actions: list[SetupAction]) -> list[SetupActionResult]:
        """Execute or skip project-install actions."""
        return await handle_project_phase(actions, self)

    async def run_scm_actions(self, actions: list[SetupAction]) -> list[SetupActionResult]:
        """Execute SCM clone actions."""
        return await _execute_scm_actions(
            actions,
            self.scm_environments,
            self.fallback_dir,
            self.parameters,
            self.event_queue,
            action_ref_map=self._action_ref_map,
        )

    def resolve_deferred(self, actions: list[SetupAction]) -> None:
        """Resolve deferred actions via ``replace()`` on frozen ``SetupAction``."""
        action_ids = {id(action) for action in actions}
        indices = [index for index, action in enumerate(self.actions) if id(action) in action_ids]
        phase_actions = [self.actions[index] for index in indices]
        resolve_deferred_actions(
            phase_actions,
            self.plugins,
            self.strategy,
            preferences=self.preferences,
            runtime_context=self.runtime_context,
        )
        for index, action in zip(indices, phase_actions, strict=True):
            self.actions[index] = action
        self._cached_action_ref_map = None

    def early_return(self) -> SetupResults:
        """Create a ``SetupResults`` from the results accumulated so far."""
        return SetupResults(
            actions=self.actions,
            action_indices=self.preview.action_indices,
            results=self.results,
            manifest_index=self.manifest_index,
            manifest_path=self.manifest_path,
            root_directory=self.manifest_directory,
            metadata=self.metadata,
            preferences=self.preview.preferences,
        )


# ---------------------------------------------------------------------------
# PATH refresh
# ---------------------------------------------------------------------------

_path_lock = threading.Lock()


def _prepend_to_path(dirs: list[str], *, require_exists: bool = False) -> None:
    """Prepend directories to ``os.environ['PATH']`` if not already present.

    Mutations are **process-global** by design — ``shutil.which()``
    and ``asyncio.create_subprocess_exec()`` inherit the process
    environment, so every concurrent ``execute_single()`` run
    benefits from directories added by earlier runtime resolution.
    Additions are idempotent and append-only (directories already
    present are skipped).

    Uses a lock to prevent concurrent mutations from interleaving.

    .. warning::
        Because ``PATH`` is shared process state, **two sync runs must
        not execute concurrently within the same process.**  The lock
        only serializes individual mutations; it does not isolate one
        run's ``PATH`` from another's.  A runtime installed by one run
        becomes visible to a sibling run's tool resolution, which can
        produce non-deterministic results.  Embedders (GUI, server)
        that need parallelism should run each sync in a separate
        process.  The CLI is unaffected (one sync per process).

    Args:
        dirs: Directory paths to prepend (in order).
        require_exists: When ``True``, skip directories that do not
            exist on disk.  Useful for ``sysconfig`` directories that
            may not have been created yet.
    """
    with _path_lock:
        current_path = os.environ.get('PATH', '')
        current_entries = set(current_path.split(os.pathsep))
        new_entries = [d for d in dirs if d not in current_entries and (not require_exists or Path(d).is_dir())]
        if new_entries:
            os.environ['PATH'] = os.pathsep.join(new_entries) + os.pathsep + current_path
            logger.debug('PATH updated with: %s', ', '.join(new_entries))


def refresh_path() -> None:
    """Prepend common script/binary directories to ``PATH``.

    First, re-synchronizes the process ``PATH`` with the operating
    system (e.g. registry on Windows) so that tools installed during
    the current session become visible.  Then prepends Python-specific
    ``sysconfig`` script directories so that executables such as
    ``pipx`` are discoverable after installation.
    """
    # Refresh the process PATH so tools installed during the current session are visible.
    reset_sync_state()
    ensure_system_path()

    dirs: list[str] = []

    scripts_dir = sysconfig.get_path('scripts')
    if scripts_dir:
        dirs.append(scripts_dir)

    user_scripts = sysconfig.get_path('scripts', 'posix_user' if os.name != 'nt' else 'nt_user')
    if user_scripts:
        dirs.append(user_scripts)

    _prepend_to_path(dirs, require_exists=True)


def inject_runtime_path(executable: Path) -> None:
    """Prepend a resolved runtime's directories to ``PATH``.

    Makes the interpreter's directory and its platform-appropriate
    scripts sibling (``Scripts/`` on Windows, ``bin/`` on Unix)
    available to ``shutil.which()`` so that ``python`` and ``pip``
    are discoverable during the next plugin re-discovery.

    Args:
        executable: Absolute path to the interpreter.
    """
    parent = str(executable.parent)
    scripts = str(executable.parent / ('Scripts' if os.name == 'nt' else 'bin'))
    _prepend_to_path([parent, scripts])


# ---------------------------------------------------------------------------
# Package execution helpers
# ---------------------------------------------------------------------------


async def resolve_runtime_tag_override(
    tag: str,
    ecosystem: str | None,
    environments: dict[str, Environment],
) -> RuntimeContext | None:
    """Build a one-off :class:`RuntimeContext` for a specific *tag*.

    Scans *environments* for a :class:`RuntimeProvider` whose
    ``provided_runtime_kind()`` matches *ecosystem*, then resolves
    *tag* to a concrete executable path.

    Returns ``None`` when no matching provider exists or when the tag
    cannot be resolved to an executable.
    """
    if ecosystem is None:
        return None

    for env in environments.values():
        if isinstance(env, RuntimeProvider) and env.provided_runtime_kind() == ecosystem:
            executable = await env.resolve_executable(tag)
            if executable is not None:
                return RuntimeContext(executables={ecosystem: executable})
            return None

    return None


def forward_version_metadata(result: SetupActionResult, operation: Operation) -> None:
    """Copy version fields from a resolved *operation* into *result*.

    After a real install/upgrade/uninstall the low-level helpers return a
    bare ``SetupActionResult`` without version information.  This merges
    the metadata that was captured during resolution so that callers
    always see it.
    """
    if isinstance(operation, Install | Upgrade | Uninstall):
        result.installed_version = result.installed_version or operation.installed_version
    if isinstance(operation, Install | Upgrade):
        result.available_version = result.available_version or operation.available_version


def invalidate_runtime_cache_after_mutation(
    action: SetupAction,
    environments: dict[str, Environment],
    result: SetupActionResult,
) -> None:
    """Clear runtime-provider caches after successful runtime install/upgrade/uninstall."""
    if not result.success or result.skipped or action.kind is not PluginKind.RUNTIME or action.installer is None:
        return

    environment = environments.get(action.installer)
    if environment is None:
        return

    invalidator = getattr(environment, 'invalidate_runtime_cache', None)
    if callable(invalidator):
        invalidator()


def _prepare_action_context(
    action: SetupAction,
    environments: dict[str, Environment],
    context: ResolutionContext | None,
    package_cache: PackageCache | None,
) -> tuple[dict[str, Environment], ResolutionContext]:
    """Apply per-action WSL routing and cache/runtime-context merges.

    Shared by :func:`execute_package` and other execution helpers.
    Merges a caller-provided ``package_cache`` into the resolution
    context.

    Callers must validate ``action.installer`` / ``action.package``
    before calling.

    Args:
        action: The package action being executed.
        environments: Dict of instantiated environment plugins.
        context: Optional resolution context.
        package_cache: Optional shared cache for ``packages()`` results.

    Returns:
        A tuple of the (possibly overlaid) environments and the
        prepared resolution context.
    """
    ctx = context or ResolutionContext()
    # Merge any caller-provided package cache into the resolution context.
    if package_cache is not None:
        ctx = replace(ctx, package_cache=package_cache)

    return environments, ctx


async def execute_package(
    action: SetupAction,
    environments: dict[str, Environment],
    strategy: SyncStrategy,
    event_queue: asyncio.Queue[ProgressEvent | None],
    context: ResolutionContext | None = None,
    *,
    package_cache: PackageCache | None = None,
) -> SetupActionResult:
    """Execute a package install or upgrade based on the strategy.

    Delegates to :func:`resolve_operation` to determine the correct
    operation (install, upgrade, or skip), then dispatches to the
    appropriate execution helper.

    Args:
        action: The package action.
        environments: Dict of instantiated environment plugins.
        strategy: The sync strategy.
        event_queue: Queue to emit action progress events into.
        context: Optional resolution context providing runtime paths,
            project-environment references, and package cache.
        package_cache: Optional shared cache for ``packages()`` results.
            When provided, presence checks share a single query per
            installer.  Invalidated after successful installs/upgrades
            so subsequent actions see fresh state.

    Returns:
        The result of the operation.
    """
    if action.installer is None or action.package is None:
        return SetupActionResult(action=action, success=False, message='Installer or package not specified')

    environments, ctx = _prepare_action_context(action, environments, context, package_cache)

    # Apply any per-action runtime override before resolving the operation.
    if action.runtime_tag is not None:
        override_ctx = await resolve_runtime_tag_override(action.runtime_tag, action.ecosystem, environments)
        if override_ctx is None:
            return SetupActionResult(
                action=action,
                success=False,
                message=(f"Could not resolve runtime tag '{action.runtime_tag}' for ecosystem '{action.ecosystem}'"),
            )
        ctx = replace(ctx, runtime_context=override_ctx)

    resolved = await resolve_operation(
        action,
        environments,
        strategy,
        ctx,
    )

    # Skip the action when the resolver reports that it should be skipped.
    if isinstance(resolved.operation, Skip):
        logger.info("Skipping '%s': %s", action.package, resolved.message)
        return resolved_to_result(resolved)

    # --- Normal package actions -------------------------------------------
    if action.installer not in environments:
        msg = f"Installer '{action.installer}' is not available"
        return SetupActionResult(action=action, success=False, message=msg)

    environment = environments[action.installer]
    match resolved.operation:
        case Install(reason=InstallReason.ENSURE_EXTRAS):
            verb = 'Ensuring'
        case Install():
            verb = 'Installing'
        case _:
            verb = 'Upgrading'
    logger.info(f"{verb} '{action.package}' via {action.installer}")
    result = await _attempt_package_operation(
        action, environment, resolved.operation, event_queue, runtime_context=ctx.runtime_context
    )
    forward_version_metadata(result, resolved.operation)
    invalidate_runtime_cache_after_mutation(action, environments, result)
    return result


async def _attempt_package_operation(
    action: SetupAction,
    environment: Environment,
    operation: Operation,
    event_queue: asyncio.Queue[ProgressEvent | None],
    *,
    runtime_context: RuntimeContext | None = None,
) -> SetupActionResult:
    """Attempt to install or upgrade a package via the given environment plugin.

    Args:
        action: The package action.
        environment: The environment plugin to use.
        operation: The resolved operation (Install or Upgrade).
        event_queue: Queue to emit action progress events into.
        runtime_context: Resolved runtime paths for this execution run.

    Returns:
        The result of the attempt.
    """
    if isinstance(operation, Install):
        execute: Callable[[PackageParameters], Awaitable[Package | None]] = environment.install
        verb, verb_past = 'install', 'Installed'
    else:
        execute = environment.upgrade
        verb, verb_past = 'upgrade', 'Upgraded'

    return await _attempt_operation(
        action,
        spec=OperationSpec(
            execute=execute,
            verb=verb,
            verb_past=verb_past,
        ),
        event_queue=event_queue,
        runtime_context=runtime_context,
    )


@dataclass(frozen=True)
class OperationSpec:
    """Specification for a single package operation.

    Bundles the async callable together with the human-readable verb
    forms used in log / result messages.
    """

    execute: Callable[[PackageParameters], Awaitable[Package | None]]
    verb: str
    verb_past: str
    success_suffix: str = ''


async def _attempt_operation(
    action: SetupAction,
    *,
    spec: OperationSpec,
    event_queue: asyncio.Queue[ProgressEvent | None],
    runtime_context: RuntimeContext | None = None,
) -> SetupActionResult:
    """Core helper that runs an async package operation with standard error handling.

    Builds the progress callback, constructs `PackageParameters`,
    calls *spec.execute*, and catches the standard exception set.

    Args:
        action: The action being executed.
        spec: The operation specification (callable + verb forms).
        event_queue: Queue to emit action progress events into.
        runtime_context: Resolved runtime paths for this execution run.

    Returns:
        The result of the attempt.
    """
    success = False
    message = ''

    action_progress_cb = _make_progress_callback(action, event_queue)

    if action.package is None:
        return SetupActionResult(action=action, success=False, message='No package specified')
    params = PackageParameters(
        package=action.package,
        dry=False,
        include_prereleases=action.include_prereleases,
        progress_callback=action_progress_cb,
        runtime_context=runtime_context,
    )

    try:
        result = await spec.execute(params)

        success = result is not None
        message = (
            f'{spec.verb_past} {result.name}{spec.success_suffix}'
            if result is not None
            else f"Failed to {spec.verb} '{action.package}'{spec.success_suffix}"
        )
    except PluginError as e:
        logger.error(f'Plugin error {spec.verb}ing {action.package}: {e}')
        message = str(e)
    except asyncio.CancelledError:
        logger.error(f'{spec.verb.capitalize()} cancelled for {action.package}')
        raise
    except TimeoutError as e:
        logger.error(f'Timeout {spec.verb}ing {action.package}: {e}')
        message = str(e)
    except Exception as e:
        message = str(e)

    return SetupActionResult(action=action, success=success, message=message)


# ---------------------------------------------------------------------------
# Parallel / sequential orchestration
# ---------------------------------------------------------------------------


async def execute_package_actions(
    package_actions: list[SetupAction],
    environments: dict[str, Environment],
    parameters: SetupParameters,
    event_queue: asyncio.Queue[ProgressEvent | None],
    context: ResolutionContext | None = None,
    *,
    action_ref_map: dict[int, ActionRef] | None = None,
) -> tuple[list[SetupActionResult], bool]:
    """Execute PACKAGE actions with parallel support.

    Returns:
        Tuple of (results, should_continue). should_continue is False if fail_fast triggered.
    """
    parallel_actions, sequential_actions = _group_actions_by_parallelism(package_actions, environments)

    results: list[SetupActionResult] = []

    # Shared cache — presence checks share a single packages() call per
    # installer.  Invalidated by execute_package after successful mutations.
    cache = PackageCache()

    ctx = context or ResolutionContext()

    # Shared HTTP client — resolution now always checks for upstream
    # updates, so a pooled connection avoids per-action TCP overhead.
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as shared_client:
        enriched = ResolutionContext(
            project_path=ctx.project_path,
            project_environments=ctx.project_environments,
            http_client=shared_client,
            package_cache=cache,
            runtime_context=ctx.runtime_context,
        )

        # Execute parallel actions concurrently
        if parallel_actions:
            parallel_results, should_continue = await _run_parallel_packages(
                parallel_actions,
                environments,
                parameters,
                event_queue,
                enriched,
                package_cache=cache,
                action_ref_map=action_ref_map,
            )
            results.extend(parallel_results)
            if not should_continue:
                cache.log_debug_stats('execute_package_actions')
                return results, False

        # Execute sequential actions one at a time
        sequential_results, should_continue = await _run_sequential_packages(
            sequential_actions,
            environments,
            parameters,
            event_queue,
            enriched,
            package_cache=cache,
            action_ref_map=action_ref_map,
        )
        results.extend(sequential_results)

    cache.log_debug_stats('execute_package_actions')
    return results, should_continue


def _group_actions_by_parallelism(
    install_actions: list[SetupAction],
    environments: dict[str, Environment],
) -> tuple[list[SetupAction], list[SetupAction]]:
    """Group actions into parallel and sequential based on plugin support."""
    parallel_actions: list[SetupAction] = []
    sequential_actions: list[SetupAction] = []

    for action in install_actions:
        supports = (
            action.installer and action.installer in environments and environments[action.installer].supports_parallel()
        )
        if supports:
            parallel_actions.append(action)
        else:
            sequential_actions.append(action)

    return parallel_actions, sequential_actions


async def _run_sequential_packages(
    sequential_actions: list[SetupAction],
    environments: dict[str, Environment],
    parameters: SetupParameters,
    event_queue: asyncio.Queue[ProgressEvent | None],
    context: ResolutionContext | None = None,
    *,
    package_cache: PackageCache | None = None,
    action_ref_map: dict[int, ActionRef] | None = None,
) -> tuple[list[SetupActionResult], bool]:
    """Run package actions sequentially."""
    ctx = context or ResolutionContext()
    results: list[SetupActionResult] = []
    for action in sequential_actions:
        ref = _action_ref(action, action_ref_map)
        started_at = _emit_started(action, ref, event_queue)
        with use_trace_context(_action_trace_context('execute', action, ref, operation='package')):
            result = await execute_package(
                action,
                environments,
                parameters.strategy,
                event_queue,
                context,
                package_cache=package_cache,
            )
        results.append(result)
        # Invalidate cache after successful install/upgrade so the next
        # action sees fresh state for the same installer.
        if result.success and not result.skipped and package_cache is not None and action.installer:
            package_cache.invalidate_packages(action.installer, ctx.project_path)
        _emit_completed(action, result, ref, event_queue, started_at)
        if not result.success and not result.skipped and parameters.fail_fast:
            logger.error(f'Action failed: {action.description} - {result.message}')
            return results, False
    return results, True


async def _run_parallel_packages(
    parallel_actions: list[SetupAction],
    environments: dict[str, Environment],
    parameters: SetupParameters,
    event_queue: asyncio.Queue[ProgressEvent | None],
    context: ResolutionContext | None = None,
    *,
    package_cache: PackageCache | None = None,
    action_ref_map: dict[int, ActionRef] | None = None,
) -> tuple[list[SetupActionResult], bool]:
    """Run package actions in parallel using TaskGroup.

    Uses asyncio.TaskGroup (Python 3.11+) for structured concurrency.
    All tasks are automatically cancelled if any raises an unhandled exception.

    Returns:
        Tuple of (results, should_continue). should_continue is False if fail_fast triggered.
    """
    results: dict[int, SetupActionResult] = {}
    max_concurrency = parameters.max_concurrency
    semaphore: asyncio.Semaphore | None = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
    owner_task = asyncio.current_task()

    async def package_with_event(index: int, action: SetupAction) -> None:
        if semaphore is not None:
            await semaphore.acquire()
        try:
            ref = _action_ref(action, action_ref_map)
            started_at = _emit_started(action, ref, event_queue)
            try:
                with use_trace_context(_action_trace_context('execute', action, ref, operation='package')):
                    result = await execute_package(
                        action,
                        environments,
                        parameters.strategy,
                        event_queue,
                        context,
                        package_cache=package_cache,
                    )
            except asyncio.CancelledError:
                if owner_task is not None and not owner_task.cancelling():
                    owner_task.cancel()
                raise
            except Exception as e:
                result = SetupActionResult(action=action, success=False, message=str(e))
            _emit_completed(action, result, ref, event_queue, started_at)
            results[index] = result
        finally:
            if semaphore is not None:
                semaphore.release()

    try:
        async with asyncio.TaskGroup() as tg:
            for i, action in enumerate(parallel_actions):
                tg.create_task(package_with_event(i, action))
    except BaseExceptionGroup as eg:
        # ``package_with_event`` converts ordinary task failures into result
        # objects, so a plain ``Exception`` never escapes a worker.  Reaching
        # this handler therefore means the TaskGroup propagated a
        # ``BaseException`` (typically a cancellation) or, defensively, an
        # unexpected exception that slipped past the worker.  Note the old
        # ``except ExceptionGroup`` could not even catch cancellation, since a
        # ``CancelledError`` group is a ``BaseExceptionGroup`` (not an
        # ``ExceptionGroup``).  Split the two: log unexpected exceptions and
        # fall through to report partial results, but re-raise cancellation so
        # the caller's cancellation semantics are preserved.
        cancellations, unexpected = eg.split(asyncio.CancelledError)
        if unexpected is not None:
            logger.error('Parallel package operation failed with exceptions: %s', unexpected.exceptions)
        if cancellations is not None:
            logger.warning('Parallel package operation cancelled (%d task(s))', len(cancellations.exceptions))
            raise cancellations from None

    # Convert dict to ordered list
    result_list = [results.get(i) for i in range(len(parallel_actions))]
    final_results: list[SetupActionResult] = []

    for action, maybe_result in zip(parallel_actions, result_list, strict=False):
        action_result: SetupActionResult
        if maybe_result is None:
            # Task was cancelled before completing
            action_result = SetupActionResult(action=action, success=False, message='Task cancelled')
        else:
            action_result = maybe_result
        final_results.append(action_result)
        if not action_result.success and not action_result.skipped and parameters.fail_fast:
            logger.error(f'Action failed: {action.description} - {action_result.message}')
            return final_results, False

    return final_results, True


# ---------------------------------------------------------------------------
# Project / SCM action execution
# ---------------------------------------------------------------------------


def determine_fallback_dir(parameters: SetupParameters, root_directory: Path) -> Path:
    """Determine the fallback working directory for SCM actions.

    Project-install actions use per-plugin auto-discovery instead of
    this method.  This fallback is used by SCM clone actions.

    Args:
        parameters: Setup parameters that may specify a project directory.
        root_directory: The logical root directory for this manifest.

    Returns:
        The working directory to use.
    """
    if isinstance(parameters.project_directory, Path):
        return parameters.project_directory
    return root_directory


async def handle_project_phase(
    project_actions: list[SetupAction],
    state: ExecutionState,
) -> list[SetupActionResult]:
    """Execute or skip project-install actions depending on context.

    Args:
        project_actions: The project-kind actions to process.
        state: Execution state with parameters, flags, and directories.

    Returns:
        Results for each project action.
    """
    action_ref_map = state._action_ref_map
    if not state.skip_project:
        return await _execute_project_install_actions(
            project_actions,
            state.project_environments,
            state.manifest_directory,
            state.parameters,
            state.event_queue,
            runtime_context=state.runtime_context,
            action_ref_map=action_ref_map,
        )
    return skip_actions(
        project_actions,
        SkipReason.NO_PROJECT_DIRECTORY,
        'No project directory provided',
        state.event_queue,
        action_ref_map=action_ref_map,
    )


# ---------------------------------------------------------------------------
# Phased execution core
# ---------------------------------------------------------------------------


def discovered_plugin_entries(
    plugins: DiscoveredPlugins,
    runtime_context: RuntimeContext | None = None,
) -> tuple[DiscoveredPluginEntry, ...]:
    """Collect availability entries for discovered plugins.

    This pure data helper is shared by inspection reports and the
    ``PLUGINS_DISCOVERED`` progress event so callers see the same
    availability map in both preview and execution paths.

    When *runtime_context* is provided, ``RuntimeConsumer`` plugins are
    probed via the runtime-aware ``query_availability()`` path.
    """
    all_plugins: dict[str, ToolBasedPlugin] = {
        **plugins.environments,
        **plugins.project_environments,
        **plugins.scm_environments,
    }

    entries: list[DiscoveredPluginEntry] = []
    for name, plugin in sorted(all_plugins.items()):
        available = plugin.query_availability(runtime_context)
        caps = plugins.capabilities(name)
        entries.append(
            DiscoveredPluginEntry(
                name=name,
                available=available,
                capabilities=frozenset(caps),
                kind=plugin.plugin_kind(),
            )
        )

    return tuple(entries)


def _plugins_discovered_event(
    plugins: DiscoveredPlugins,
    runtime_context: RuntimeContext | None = None,
) -> ProgressEvent:
    """Build a ``PLUGINS_DISCOVERED`` progress event."""
    discovered = discovered_plugin_entries(plugins, runtime_context)
    logger.info('Plugins discovered — %d plugin(s)', len(discovered))
    return PluginsDiscoveredEvent(discovered_plugins=discovered)


async def execute_single(
    preview: SetupResults,
    parameters: SetupParameters,
    event_queue: asyncio.Queue[ProgressEvent | None],
    *,
    plugins: DiscoveredPlugins | None = None,
    manifest_index: int = 0,
) -> SetupResults:
    """Execute setup actions for a single path with parallel support.

    Execution is **phased** so that each layer's prerequisite tools
    are available before they are needed:

    1. **Runtime** — install/resolve language runtimes (pim, pyenv).
    2. **Package** — install packages into the current environment
       (pip, uv).  This may install tool prerequisites such as pipx.
    3. **Tool** — install isolated CLI tools (pipx).  Plugins are
       re-discovered after Phase 2 so that newly-installed backends
       are available.  Deferred actions whose ``installer`` was
       ``None`` at preview time are resolved here.
    4. **SCM clone** — clone source-control repositories, so a
       project referenced only by URL exists locally before it is
       synced.
    5. **Project sync** — run ``pdm install`` / ``uv sync`` in the
       manifest directory.

    Args:
        preview: The parsed manifest preview containing actions,
            ``root_directory``, ``manifest_path``, and ``metadata``.
        parameters: The setup parameters.
        event_queue: Queue to emit ``ProgressEvent`` items into.
        plugins: Pre-discovered plugins.  When provided, plugin
            discovery is skipped entirely (useful when the caller has
            already discovered plugins for a batch of manifests).
        manifest_index: Stable index of this manifest within the current batch.

    Returns:
        SetupResults containing the results of each action.
    """
    actions = preview.actions
    assert preview.root_directory is not None  # guaranteed by parse_manifest
    root_directory = preview.root_directory

    logger.info('Executing %d setup action(s) async', len(actions))

    # Use pre-discovered plugins when available; otherwise discover.
    plugins_discovered_here = plugins is None
    if plugins is None:
        invalidate_plugin_cache()
        plugins = discover_all_plugins()

    # Emit PLUGINS_DISCOVERED only when we performed discovery ourselves.
    # Batch callers pre-discover and emit the
    # event once for the entire batch, so we skip it here to avoid
    # sending duplicate events.
    if plugins_discovered_here:
        event_queue.put_nowait(_plugins_discovered_event(plugins))

    # Seed runtime_context from pre-resolved plugins (e.g. from
    # API.discover_plugins(resolve_runtime=True)).  A defensive copy
    # prevents in-place mutations from aliasing the shared plugins
    # object when propagate_runtime() writes into the dict later.
    seeded_context = (
        RuntimeContext(executables=dict(plugins.runtime_context.executables))
        if plugins.runtime_context is not None
        else RuntimeContext()
    )

    state = ExecutionState(
        actions=actions,
        # .copy() builds fresh plugin instances (when factory metadata
        # is present) so that accidental state on a plugin object
        # cannot leak between runs or back to the caller.
        plugins=plugins.copy(),
        parameters=parameters,
        event_queue=event_queue,
        manifest_directory=root_directory,
        preview=preview,
        manifest_index=manifest_index,
        runtime_context=seeded_context,
    )

    # Emit MANIFEST_LOADED — the fully-resolved preview.
    state.emit(ManifestLoadedEvent(manifest=preview, manifest_index=manifest_index))

    # Run all phases via the generalized phase loop.
    await run_phases(state)

    # Populate CLI commands on results for display.
    for result in state.results:
        result.cli_command = get_cli_command(result.action, state.plugins, state.strategy)

    return SetupResults(
        actions=actions,
        action_indices=preview.action_indices,
        results=state.results,
        manifest_index=manifest_index,
        manifest_path=state.manifest_path,
        root_directory=state.manifest_directory,
        metadata=state.metadata,
        preferences=state.preview.preferences,
    )


# ---------------------------------------------------------------------------
# Phase grouping and helpers
# ---------------------------------------------------------------------------


def group_actions_by_phase(
    actions: list[SetupAction],
) -> dict[PluginKind, list[SetupAction]]:
    """Group actions into phase buckets keyed by `PluginKind`.

    Returns:
        Dict mapping each phase to its action list.
    """
    phases: dict[PluginKind, list[SetupAction]] = {k: [] for k in PHASE_ORDER}
    for action in actions:
        if action.kind is not None:
            phases[action.kind].append(action)
    return phases


async def _propagate_runtime(
    runtime_actions: list[SetupAction],
    plugins: DiscoveredPlugins,
) -> tuple[str, Path] | None:
    """Resolve the interpreter path and propagate to downstream consumers.

    After runtime-provider actions complete, finds the first
    `RuntimeProvider` that can resolve an executable.  The result
    is returned so the caller can store it in its
    :class:`RuntimeContext`.

    Plugin instances are **not** mutated — runtime state is
    threaded explicitly through execution parameters.

    Returns:
        A ``(kind, executable)`` tuple on success, or ``None`` if no
        runtime could be resolved.
    """
    environments = plugins.environments

    # Find a RuntimeProvider among the runtime action installers
    for action in runtime_actions:
        if action.installer is None or action.package is None:
            continue
        env = environments.get(action.installer)
        if env is None or not isinstance(env, RuntimeProvider):
            continue

        kind = cast(type[RuntimeProvider], type(env)).provided_runtime_kind()
        tag = action.package.name
        executable = await env.resolve_executable(tag)
        if executable is None:
            logger.debug(
                'RuntimeProvider %s could not resolve executable for tag %s',
                action.installer,
                tag,
            )
            continue

        logger.info('Runtime resolved: %s -> %s', tag, executable)

        # Make the runtime's directory (and its Scripts/bin sibling)
        # visible on PATH so that downstream tools like pip and python
        # are discoverable via shutil.which() during plugin
        # re-discovery at the next phase transition.
        inject_runtime_path(executable)

        # Use only the first successfully resolved runtime
        return (kind, executable)

    return None


def resolve_deferred_actions(
    actions: list[SetupAction],
    plugins: DiscoveredPlugins,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
    preferences: dict[Ecosystem, str] | None = None,
    runtime_context: RuntimeContext | None = None,
) -> None:
    """Resolve deferred actions whose `installer` is `None`.

    After a preceding phase installs new tools (e.g. pip installs pipx),
    plugins are re-discovered and a fresh `BackendResolver` determines
    the correct backend for each deferred action.  Actions that still
    cannot be resolved are left with `installer = None` so that the
    normal execution path reports them as unavailable.

    When a *runtime_context* is provided (i.e. the RUNTIME phase has
    already resolved an interpreter), the resolver uses
    ``RuntimeConsumer.is_available_for(runtime_context)`` to probe
    plugin availability against the target interpreter rather than
    only checking the host process's PATH.

    Frozen ``SetupAction`` objects are replaced via
    ``dataclasses.replace()``; the list is mutated in-place (element
    swap) so that all collections sharing the same list see the update.

    Args:
        actions: List of actions — elements are swapped in-place.
        plugins: Freshly-discovered plugin container.
        strategy: Sync strategy (for description verb).
        preferences: Optional ecosystem → plugin-name preferences from the manifest.
        runtime_context: Resolved runtime executables, if any.
    """
    deferred_indices = [i for i, a in enumerate(actions) if a.installer is None and a.ecosystem is not None]
    if not deferred_indices:
        return

    deferred = [actions[i] for i in deferred_indices]

    # Pass runtime_context so that RuntimeConsumer plugins can be
    # probed against the target interpreter, not just the host PATH.
    needed_pairs = {(a.kind, a.ecosystem) for a in deferred if a.kind is not None and a.ecosystem is not None}
    resolver = BackendResolver(
        plugins.all_plugins, preferences, runtime_context=runtime_context, needed_pairs=needed_pairs
    )
    verb = STRATEGY_VERB[strategy]

    for idx in deferred_indices:
        action = actions[idx]
        assert action.kind is not None
        assert action.ecosystem is not None
        installer = resolver.resolve(action.kind, action.ecosystem)
        if installer is not None:
            new_description = action_description(
                action.kind,
                verb,
                installer,
                package=action.package,
            )
            actions[idx] = replace(action, installer=installer, description=new_description)
            logger.info('Deferred action resolved: %s -> %s', new_description, installer)
        else:
            registered = resolver.registered_names(action.kind, action.ecosystem)
            if registered:
                logger.error(
                    'Deferred action permanently unresolved: %s (registered plugins %s for %s/%s are all unavailable)',
                    action.description,
                    registered,
                    action.kind.value,
                    action.ecosystem,
                )
            else:
                logger.error(
                    'Deferred action permanently unresolved: %s (no plugin found for %s/%s)',
                    action.description,
                    action.kind.value,
                    action.ecosystem,
                )


def skip_actions(
    actions: list[SetupAction],
    skip_reason: SkipReason,
    message: str,
    event_queue: asyncio.Queue[ProgressEvent | None],
    *,
    action_ref_map: dict[int, ActionRef] | None = None,
) -> list[SetupActionResult]:
    """Skip a list of actions, emitting progress events and a warning for each.

    Args:
        actions: The actions to skip.
        skip_reason: Machine-readable skip code.
        message: Human-readable skip detail.
        event_queue: Queue for progress events.
        action_ref_map: Optional id(action) → stable action ref map.

    Returns:
        List of skipped action results.
    """
    results: list[SetupActionResult] = []
    for action in actions:
        logger.warning("Skipping '%s': %s", action.description, message)
        result = SetupActionResult(
            action=action,
            success=True,
            skipped=True,
            skip_reason=skip_reason,
            message=message,
        )
        results.append(result)
        ref = _action_ref(action, action_ref_map)
        started_at = _emit_started(action, ref, event_queue)
        _emit_completed(action, result, ref, event_queue, started_at)
    return results


# ---------------------------------------------------------------------------
# Project install execution
# ---------------------------------------------------------------------------


async def _execute_project_install_actions(
    project_install_actions: list[SetupAction],
    project_environments: dict[str, ProjectInstaller] | None,
    manifest_directory: Path,
    parameters: SetupParameters,
    event_queue: asyncio.Queue[ProgressEvent | None],
    *,
    runtime_context: RuntimeContext | None = None,
    action_ref_map: dict[int, ActionRef] | None = None,
) -> list[SetupActionResult]:
    """Execute project-install actions sequentially.

    Each action builds the resolved project plugin's
    `ProjectInstaller.command_plan()` and runs its steps. When
    `parameters.project_directory` is an explicit `Path` it is
    used as the working directory for every plugin.  Otherwise each
    plugin auto-discovers its project root by walking ancestor
    directories of *manifest_directory* looking for its ecosystem's
    marker file (e.g. `package.json`, `pyproject.toml`).

    Args:
        project_install_actions: The project-install actions.
        project_environments: Dict of project plugins.
        manifest_directory: Directory containing the manifest file.
        parameters: Setup parameters.
        event_queue: Queue for progress events.
        runtime_context: Resolved runtime paths for this execution run.
        action_ref_map: Optional id(action) → stable action ref map.

    Returns:
        List of action results.
    """
    results: list[SetupActionResult] = []

    for action in project_install_actions:
        ref = _action_ref(action, action_ref_map)
        started_at = _emit_started(action, ref, event_queue)

        with use_trace_context(_action_trace_context('execute', action, ref, operation='project_install')):
            result = await _execute_project_install(
                action,
                project_environments,
                manifest_directory,
                parameters,
                event_queue=event_queue,
                runtime_context=runtime_context,
            )

        results.append(result)
        _emit_completed(action, result, ref, event_queue, started_at)
        if not result.success and parameters.fail_fast:
            logger.error(f'Project install failed: {action.description} - {result.message}')
            break

    return results


async def _execute_project_install(
    action: SetupAction,
    project_environments: dict[str, ProjectInstaller] | None,
    manifest_directory: Path,
    parameters: SetupParameters,
    *,
    event_queue: asyncio.Queue[ProgressEvent | None],
    runtime_context: RuntimeContext | None = None,
) -> SetupActionResult:
    """Execute a single project-install action.

    The install command is always run via ``run_command`` with progress so that
    stdout/stderr lines are emitted as action progress
    events in real time.

    When `parameters.project_directory` is an explicit `Path`
    it is used unconditionally.  Otherwise the plugin's
    `resolve_project_root()` is called to auto-discover the
    project root from *manifest_directory*.  If discovery fails
    (no marker found), *manifest_directory* is used as fallback
    and a warning is logged.

    Args:
        action: The project-install action.
        project_environments: Dict of project plugins.
        manifest_directory: Directory containing the manifest file.
        parameters: Setup parameters.
        event_queue: Queue for progress events.
        runtime_context: Resolved runtime paths for this execution run.

    Returns:
        The result of the project-install operation.
    """
    proj_envs = project_environments or {}

    if action.installer is None or action.installer not in proj_envs:
        return SetupActionResult(
            action=action,
            success=False,
            message=f"Project environment '{action.installer}' is not available",
        )

    proj_env = proj_envs[action.installer]

    # Determine the effective directory for this plugin
    effective_dir: Path
    if isinstance(parameters.project_directory, Path):
        # Explicit override — use as-is
        effective_dir = parameters.project_directory
    else:
        # Auto-discover per-plugin project root
        discovered = type(proj_env).resolve_project_root(manifest_directory)
        if discovered is not None:
            effective_dir = discovered
            if discovered != manifest_directory:
                logger.info(
                    "Auto-discovered %s project root for '%s': %s",
                    proj_env.ecosystem(),
                    action.installer,
                    discovered,
                )
        else:
            effective_dir = manifest_directory
            marker = type(proj_env).project_marker()
            if marker is not None:
                logger.warning(
                    "No '%s' found in ancestors of %s; falling back to manifest directory for %s project install",
                    marker,
                    manifest_directory,
                    proj_env.ecosystem(),
                )

    try:
        return await _run_project_install_steps(
            action,
            proj_env,
            effective_dir,
            event_queue,
            runtime_context=runtime_context,
        )
    except Exception as e:
        return SetupActionResult(action=action, success=False, message=str(e))


async def _run_project_install_steps(
    action: SetupAction,
    proj_env: ProjectInstaller,
    effective_dir: Path,
    event_queue: asyncio.Queue[ProgressEvent | None],
    *,
    runtime_context: RuntimeContext | None = None,
) -> SetupActionResult:
    """Run the resolved project-install steps and return their outcome."""
    # Always observe output: build the CLI steps from the plugin and
    # run them via run_command for line-by-line progress.
    plan = type(proj_env).command_plan(effective_dir, runtime_context=runtime_context)
    effective_dir = plan.directory
    steps = plan.steps or ([plan.argv] if plan.argv else [])

    success = True
    returncode: int | None = None
    step_total = len(steps)
    failed_step_index: int | None = None

    def make_step_callback(
        step_number: int,
        args: list[str],
    ) -> Callable[[ActionProgress], None]:
        base_callback = _make_progress_callback(action, event_queue)

        def step_callback(update: ActionProgress) -> None:
            base_callback(
                replace(
                    update,
                    phase='project',
                    message=update.message or f'Step {step_number}/{step_total}',
                    step_index=step_number,
                    step_total=step_total,
                    command=tuple(args),
                )
            )

        return step_callback

    for step_number, args in enumerate(steps, start=1):
        step_callback = make_step_callback(step_number, args)
        step_callback(
            ActionProgress(
                action=action,
                phase='project',
                message=f'Step {step_number}/{step_total}',
                step_index=step_number,
                step_total=step_total,
                command=tuple(args),
            )
        )
        progress = CommandProgress(action=action, callback=step_callback, phase='project')
        cmd_result = await run_command(args, progress=progress, cwd=effective_dir, timeout=300.0)
        success = cmd_result.returncode == 0
        returncode = cmd_result.returncode
        if not success:
            failed_step_index = step_number
            break

    if success:
        return SetupActionResult(
            action=action,
            success=True,
            message=f'Installed project via {action.installer}',
            cli_command=tuple(plan.argv),
            cli_steps=tuple(tuple(step) for step in steps),
        )
    # Detailed subprocess output (stdout/stderr) is streamed live as
    # ActionProgress events during the run — the CLI surfaces a tail of
    # it under the failed action.  The result message stays a short,
    # semantic summary so it remains clean for JSONL/GUI consumers.
    message = f'Project install failed via {action.installer}'
    if returncode is not None:
        message = f'{message} (exit {returncode})'
    return SetupActionResult(
        action=action,
        success=False,
        message=message,
        cli_command=tuple(plan.argv),
        cli_steps=tuple(tuple(step) for step in steps),
        failed_step_index=failed_step_index,
    )


# ---------------------------------------------------------------------------
# SCM execution
# ---------------------------------------------------------------------------


async def _execute_scm_actions(
    scm_actions: list[SetupAction],
    scm_environments: dict[str, ScmEnvironment] | None,
    working_dir: Path,
    parameters: SetupParameters,
    event_queue: asyncio.Queue[ProgressEvent | None],
    *,
    action_ref_map: dict[int, ActionRef] | None = None,
) -> list[SetupActionResult]:
    """Execute SCM_CLONE actions sequentially.

    Each action invokes the resolved SCM-environment plugin's
    `ScmEnvironment.clone()` method.

    Args:
        scm_actions: The SCM clone actions.
        scm_environments: Dict of SCM-environment plugins.
        working_dir: Working directory (manifest location).
        parameters: Setup parameters.
        event_queue: Queue for progress events.
        action_ref_map: Optional id(action) → stable action ref map.

    Returns:
        List of action results.
    """
    results: list[SetupActionResult] = []

    for action in scm_actions:
        ref = _action_ref(action, action_ref_map)
        started_at = _emit_started(action, ref, event_queue)

        with use_trace_context(_action_trace_context('execute', action, ref, operation='scm')):
            result = await _execute_scm_clone(
                action,
                scm_environments,
                working_dir,
                parameters,
                event_queue=event_queue,
            )

        results.append(result)
        _emit_completed(action, result, ref, event_queue, started_at)
        if not result.success and not result.skipped and parameters.fail_fast:
            logger.error(f'SCM clone failed: {action.description} - {result.message}')
            break

    return results


async def _execute_scm_clone(
    action: SetupAction,
    scm_environments: dict[str, ScmEnvironment] | None,
    working_dir: Path,
    parameters: SetupParameters,
    *,
    event_queue: asyncio.Queue[ProgressEvent | None],
) -> SetupActionResult:
    """Execute a single SCM_CLONE action.

    Delegates to the resolved plugin's ``ScmEnvironment.clone()``,
    threading a progress callback through ``CloneParameters`` so that
    subprocess output streams as ``ActionProgress`` events in real
    time (the CLI surfaces the tail under the action on failure) —
    consistent with how package and project-install actions report
    progress.

    Args:
        action: The SCM clone action.
        scm_environments: Dict of SCM-environment plugins.
        working_dir: Working directory (manifest location).
        parameters: Setup parameters.
        event_queue: Queue for progress events.

    Returns:
        The result of the clone operation.
    """
    scm_envs = scm_environments or {}

    if action.installer is None or action.installer not in scm_envs:
        message = (
            f"No SCM plugin found for installer '{action.installer}'. "
            f'Available SCM plugins: {sorted(scm_envs) or "(none)"}. '
            f'The plugin may not be installed or registered.'
        )
        logger.warning(message)
        return SetupActionResult(
            action=action,
            success=False,
            message=message,
        )

    if action.package is None:
        return SetupActionResult(action=action, success=False, message='No repository URL specified')

    scm_env = scm_envs[action.installer]
    url = action.package.name

    # Clone directly into the working directory, not into a derived subdirectory.
    destination = working_dir

    # Skip if already cloned (checks all remotes and walks up to repo root)
    clone_status = await scm_env.is_cloned(url, destination)

    skip_result = clone_status_to_result(action, clone_status, url, destination)
    if skip_result is not None:
        return skip_result

    # Only MISSING reaches here — the repository needs cloning.
    logger.info("SCM clone needed: repository not found at '%s'", destination)

    try:
        success = await scm_env.clone(
            CloneParameters(
                url=url,
                destination=destination,
                dry=False,
                progress_callback=_make_progress_callback(action, event_queue),
            )
        )
    except Exception as e:
        return SetupActionResult(action=action, success=False, message=str(e))

    message = (
        f"Cloned '{url}' via {action.installer}" if success else f"Clone failed for '{url}' via {action.installer}"
    )
    return SetupActionResult(action=action, success=success, message=message)
