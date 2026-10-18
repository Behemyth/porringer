"""CLI command implementation for sync.

The sync command module.

Thin facade that wires together manifest loading, action building,
presence detection, and phased execution.  The heavy lifting lives
in the sibling modules:

* `.manifest`       — loading and validating manifests
* `.action_builder` — building the action plan from a manifest
* `.presence`       — inspection / presence detection
* `.execution`      — phased async execution engine
* `.discovery`      — plugin entry-point discovery
"""

import asyncio
import contextlib
import functools
import inspect as inspectlib
import logging
import shutil
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Sequence
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from porringer.backend.builder import Builder
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.core.plugin_schema.runtime import RuntimeConsumer
from porringer.core.schema import PluginKind
from porringer.schema import (
    ActionCompletedEvent,
    ActionRef,
    BatchSetupResults,
    DirectoryState,
    DirectoryStatus,
    DownloadParameters,
    DownloadResult,
    FailedPathInspection,
    InspectionMode,
    ManifestFailedEvent,
    ManifestLoadedEvent,
    ManifestValidationResult,
    ProgressEvent,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SyncInspectionReport,
    SyncPlan,
    SyncRunReport,
    action_id_for,
)
from porringer.utility import HTTP_TIMEOUT
from porringer.utility.concurrency import gather_bounded
from porringer.utility.download import download_file
from porringer.utility.exception import (
    ManifestError,
    PlanStaleError,
    PorringerBusyError,
)
from porringer.utility.observability import (
    batch_diagnostics,
    batch_follow_up_actions,
    result_status,
)

from .core.action_builder import load_manifest
from .core.discovery import discover_all_plugins, invalidate_plugin_cache
from .core.execution import _plugins_discovered_event, execute_manifest
from .core.fingerprint import changed_paths, plan_fingerprints
from .core.inspection import build_manifest_inspection, build_sync_inspection_report
from .manifest import has_manifest as _has_manifest
from .manifest import manifest_filenames as _manifest_filenames
from .manifest import manifest_schema, validate_manifest

logger = logging.getLogger(__name__)

# Ids of SyncCommands instances whose ``run`` is active in the current context.
# Callbacks run in the caller's context, so this detects reentrant runs.
_ACTIVE_RUNS: ContextVar[frozenset[int]] = ContextVar('porringer_active_runs', default=frozenset())

type ManifestExecutor = Callable[..., Awaitable[SetupResults]]

type _Prepared = tuple[list[SetupResults], list[tuple[int, Path, str]]]
"""Manifests already loaded by a plan, and the inputs that failed to load."""


async def _notify(on_event: Callable[[ProgressEvent], object] | None, event: ProgressEvent) -> None:
    """Pass *event* to *on_event*, awaiting it if it returns an awaitable."""
    if on_event is not None:
        maybe_awaitable = on_event(event)
        if inspectlib.isawaitable(maybe_awaitable):
            await maybe_awaitable


def _action_indices(preview: SetupResults) -> list[int]:
    """Return source action indexes for a preview, initializing when absent."""
    if len(preview.action_indices) != len(preview.actions):
        preview.action_indices = list(range(len(preview.actions)))
    return preview.action_indices


def _inspection_needs_runtime(previews: Sequence[SetupResults], plugins: DiscoveredPlugins) -> bool:
    """Return whether complete inspection needs a resolved runtime context."""
    for preview in previews:
        for action in preview.actions:
            if action.kind is None or action.installer is None:
                continue
            plugin = None
            match action.kind:
                case PluginKind.PACKAGE | PluginKind.TOOL | PluginKind.RUNTIME:
                    plugin = plugins.environments.get(action.installer)
                case PluginKind.PROJECT:
                    plugin = plugins.project_environments.get(action.installer)
                case PluginKind.SCM:
                    plugin = plugins.scm_environments.get(action.installer)
            if isinstance(plugin, RuntimeConsumer):
                return True
    return False


class SyncCommands:
    """Manifest-driven sync commands.

    Handles manifest loading, validation, evented execution, and
    manifest schema export.  Update checking for managed packages
    has moved to :class:`~porringer.backend.command.package.PackageCommands`.
    """

    def __init__(self, global_manifest_path: Path | None = None) -> None:
        """Initialize the SyncCommands class.

        Args:
            global_manifest_path: The user's global manifest. It declares
                entries only, so nothing is inferred from where it lives.
        """
        self._global_manifest_path = global_manifest_path.resolve() if global_manifest_path else None
        # Manifests that declare entries only; nothing is inferred from where they live.
        self._standalone_paths: set[Path] = set()
        if self._global_manifest_path is not None:
            self._standalone_paths |= {
                self._global_manifest_path,
                self._global_manifest_path.parent,
            }
        # Created per event loop so sequential ``asyncio.run`` calls can
        # share one API instance.
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _execution_lock(self) -> asyncio.Lock:
        """Return this instance's execution lock for the running loop."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    @property
    def global_manifest_path(self) -> Path | None:
        """The global manifest used by this instance, when configured."""
        return self._global_manifest_path

    @contextlib.contextmanager
    def standalone_manifest(self, path: Path) -> Generator[None]:
        """Disable project inference for *path* for the duration of the context."""
        resolved = path.resolve()
        registered = resolved in self._standalone_paths
        self._standalone_paths.add(resolved)
        try:
            yield
        finally:
            if not registered:
                self._standalone_paths.discard(resolved)

    # --- Static helpers delegated to sub-modules ---

    @staticmethod
    def validate_manifest(path: Path) -> ManifestValidationResult:
        """Validate a manifest for errors without executing any operations.

        Delegates to `manifest.validate_manifest`.
        """
        return validate_manifest(path)

    @staticmethod
    def manifest_schema() -> dict:
        """Export a JSON Schema representation of the manifest format.

        Delegates to `manifest.manifest_schema`.
        """
        return manifest_schema()

    @staticmethod
    def manifest_filenames() -> tuple[str, ...]:
        """Return all recognised manifest filenames, native first.

        The first element is always ``'porringer.json'``.  Subsequent
        entries are contributed dynamically by installed project plugins
        via the ``ManifestContributor`` protocol.

        GUI clients use this to build file-dialog filters without
        hardcoding filenames.

        Returns:
            Ordered tuple of filenames the discovery engine will probe.
        """
        return _manifest_filenames()

    @staticmethod
    def has_manifest(path: Path) -> bool:
        """Check whether a path resolves to a valid porringer manifest.

        A lightweight existence + parsability check without deep
        validation (no plugin resolution, no PEP 440 checks).

        Args:
            path: Path to a manifest file or directory containing one.

        Returns:
            ``True`` if a manifest can be found and loaded.
        """
        return _has_manifest(path)

    # --- Path resolution ---

    @staticmethod
    def _partition_paths(
        paths: Path | Sequence[str | Path],
    ) -> tuple[list[Path], list[str]]:
        """Split *paths* into local filesystem paths and remote URLs.

        Returns:
            ``(local, urls)`` — local paths and URL strings.
        """
        if isinstance(paths, Path):
            return [paths], []
        local: list[Path] = []
        urls: list[str] = []
        for p in paths:
            if isinstance(p, str):
                parsed = urlparse(p)
                if parsed.scheme in {'http', 'https'}:
                    urls.append(p)
                    continue
                local.append(Path(p))
            else:
                local.append(p)
        return local, urls

    def _resolve_paths(self, parameters: SetupParameters) -> tuple[list[Path], list[str]]:
        """Resolve paths from parameters.

        When ``parameters.paths`` is ``None`` the current working directory is
        used, so Porringer operates on the nearest manifest.

        Returns:
            ``(local_paths, urls)`` — local paths to process and URLs
            to download.
        """
        if parameters.paths is not None:
            return self._partition_paths(parameters.paths)
        return [Path('.')], []

    @staticmethod
    async def _download_urls(
        urls: list[str],
        tmp_dir: Path,
        *,
        max_concurrency: int = 8,
    ) -> list[Path]:
        """Download remote manifest URLs into *tmp_dir*.

        Downloads run with bounded concurrency over a single shared
        :class:`aiohttp.ClientSession` so connections are pooled and TLS
        setup is not repeated per URL.

        Returns a list of local paths to the downloaded files, ordered to
        match *urls*.
        """
        if not urls:
            return []

        dests = [tmp_dir / f'manifest_{i}.json' for i in range(len(urls))]

        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:

            def _make_download(url: str, dest: Path) -> Callable[[], Awaitable[tuple[str, DownloadResult]]]:
                async def _run() -> tuple[str, DownloadResult]:
                    result = await download_file(
                        DownloadParameters(url=url, destination=dest),
                        http_client=session,
                    )
                    return url, result

                return _run

            results = await gather_bounded(
                (_make_download(url, dest) for url, dest in zip(urls, dests, strict=True)),
                limit=max_concurrency,
            )

        for url, result in results:
            if not result.success:
                raise ValueError(f'Failed to download manifest from {url}: {result.message}')
        return dests

    # --- Manifest loading ---

    def _is_standalone(self, path: Path) -> bool:
        """Whether *path* names a standalone manifest such as the global one, or its directory."""
        return path.resolve() in self._standalone_paths

    def _load_manifests(
        self,
        parameters: SetupParameters,
        plugins: DiscoveredPlugins | None = None,
    ) -> tuple[list[SetupResults], list[tuple[int, Path, str]]]:
        """Load and filter manifests from the resolved paths.

        Shared by inspection and execution to avoid duplicating
        the parse → filter → error-handling loop.

        Args:
            parameters: The setup parameters.
            plugins: Optional pre-discovered plugin registry to reuse while
                building manifests.

        Returns:
            A tuple of (loaded previews, failed paths).
        """
        paths, _urls = self._resolve_paths(parameters)
        logger.info('Processing %d path(s)', len(paths))

        previews: list[SetupResults] = []
        failed_paths: list[tuple[int, Path, str]] = []

        for manifest_index, path in enumerate(paths):
            try:
                preview = load_manifest(
                    path,
                    strategy=parameters.strategy,
                    plugins=plugins,
                    infer=not self._is_standalone(path),
                )
            except ManifestError as e:
                logger.warning(f'Failed to load manifest at {path}: {e.error}')
                failed_paths.append((manifest_index, path, e.error))
                if parameters.fail_fast:
                    break
                continue

            preview.manifest_index = manifest_index
            self._apply_action_filters(preview, parameters, manifest_index=manifest_index)

            previews.append(preview)

        return previews, failed_paths

    @staticmethod
    def _apply_action_filters(preview: SetupResults, parameters: SetupParameters, *, manifest_index: int = 0) -> None:
        """Apply caller-level action filters to a loaded manifest preview."""
        indexed_actions = list(zip(_action_indices(preview), preview.actions, strict=True))

        # Filter actions to only included plugins.
        if parameters.plugin_names is not None:
            indexed_actions = [
                (index, action)
                for index, action in indexed_actions
                if action.installer is None or action.installer in parameters.plugin_names
            ]

        if parameters.action_kinds is not None:
            indexed_actions = [
                (index, action) for index, action in indexed_actions if action.kind in parameters.action_kinds
            ]

        # Filter actions to only included packages.
        if parameters.include_packages is not None:
            names = {name.lower() for name in parameters.include_packages}
            indexed_actions = [
                (index, action)
                for index, action in indexed_actions
                if action.package is None or action.package.name.lower() in names
            ]

        # Apply caller-level prerelease overrides.
        if parameters.prerelease_packages:
            overrides = {name.lower() for name in parameters.prerelease_packages}
            indexed_actions = [
                (index, replace(action, include_prereleases=True))
                if action.package is not None and action.package.name.lower() in overrides
                else (index, action)
                for index, action in indexed_actions
            ]

        if parameters.action_ids is not None:
            indexed_actions = [
                (index, action)
                for index, action in indexed_actions
                if action_id_for(manifest_index, index) in parameters.action_ids
            ]

        if parameters.entry_ids is not None:
            indexed_actions = [
                (index, action) for index, action in indexed_actions if action.entry_id in parameters.entry_ids
            ]

        preview.action_indices = [index for index, _action in indexed_actions]
        preview.actions = [action for _index, action in indexed_actions]

    async def inspect(
        self,
        parameters: SetupParameters,
        *,
        plugins: DiscoveredPlugins | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> SyncInspectionReport:
        """Inspect manifests without executing setup actions.

        This is the structured preview/diagnostics path for frontends
        and CLI JSON output.  It loads manifests, discovers plugins,
        validates each manifest, resolves native commands, performs
        presence/update checks, and returns a stable report.  To run
        exactly what was inspected, use :meth:`plan` instead.

        Args:
            parameters: Setup parameters controlling paths, strategy,
                filters, prerelease overrides, and project-directory
                behavior.
            plugins: Pre-discovered plugins.  When provided, plugin
                discovery is skipped and the supplied runtime context is
                reused.
            on_event: Optional callback for inspection events, as for :meth:`plan`.

        Returns:
            A structured inspection report.
        """
        plan = await self.plan(parameters, plugins=plugins, on_event=on_event)
        plan.discard()
        return plan.report

    async def plan(
        self,
        parameters: SetupParameters,
        *,
        plugins: DiscoveredPlugins | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> SyncPlan:
        """Inspect manifests and freeze the result into a plan for :meth:`run`.

        Events reach ``on_event`` in this order: one ``PluginsDiscoveredEvent``,
        a ``ManifestFailedEvent``
        for each input that could not be loaded, a ``ManifestLoadedEvent``
        for each manifest, then one ``ActionInspectedEvent`` per action,
        manifest by manifest.  Actions are reported as their checks
        finish, so with complete inspection they can arrive out of source
        order within a manifest; ``plan.report`` is always in source order.

        Args:
            parameters: Setup parameters, as for :meth:`inspect`.
            plugins: Pre-discovered plugins, as for :meth:`inspect`.
            on_event: Optional callback for inspection events. If it
                returns an awaitable, it is awaited.

        Returns:
            The plan. Run it with ``run(plan=...)`` or release it with ``plan.discard()``.
        """
        local_paths, urls = self._resolve_paths(parameters)
        tmp_dir = Path(tempfile.mkdtemp(prefix='porringer_')) if urls else None
        try:
            effective_params = parameters
            if tmp_dir is not None:
                url_paths = await self._download_urls(urls, tmp_dir, max_concurrency=parameters.max_concurrency)
                effective_params = parameters.model_copy(update={'paths': [*local_paths, *url_paths]})
            return await self._build_plan(
                effective_params,
                local_paths,
                tmp_dir,
                plugins=plugins,
                on_event=on_event,
            )
        except BaseException:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    async def _build_plan(
        self,
        parameters: SetupParameters,
        local_paths: Sequence[Path],
        download_directory: Path | None,
        *,
        plugins: DiscoveredPlugins | None,
        on_event: Callable[[ProgressEvent], object] | None,
    ) -> SyncPlan:
        """Load, fingerprint and inspect the manifests named by *parameters*."""
        shared_plugins = plugins
        if shared_plugins is None:
            shared_plugins = await asyncio.to_thread(discover_all_plugins, use_cache=True)

        previews, failed = await asyncio.to_thread(self._load_manifests, parameters, shared_plugins)
        indices = [
            preview.manifest_index if preview.manifest_index is not None else index
            for index, preview in enumerate(previews)
        ]

        # Announce everything that loaded before the slow part, so a client can lay out its rows.
        notify = functools.partial(_notify, on_event)
        if on_event is not None:
            await notify(await asyncio.to_thread(_plugins_discovered_event, shared_plugins))
        for manifest_index, path, error in failed:
            await notify(ManifestFailedEvent(failed_path=(path, error), manifest_index=manifest_index))
        for manifest_index, preview in zip(indices, previews, strict=True):
            await notify(ManifestLoadedEvent(manifest=preview, manifest_index=manifest_index))

        if (
            parameters.inspection_mode != InspectionMode.FAST
            and shared_plugins.runtime_context is None
            and _inspection_needs_runtime(previews, shared_plugins)
        ):
            shared_plugins.runtime_context = await Builder.resolve_runtime_context(shared_plugins.environments)
        fingerprints = await asyncio.to_thread(plan_fingerprints, local_paths, previews)

        manifests = []
        for manifest_index, preview in zip(indices, previews, strict=True):
            diagnostics = ()
            if preview.manifest_path is not None:
                validation = await asyncio.to_thread(validate_manifest, preview.manifest_path)
                diagnostics = tuple(validation.diagnostics)
            manifests.append(
                await build_manifest_inspection(
                    index=manifest_index,
                    preview=preview,
                    plugins=shared_plugins,
                    parameters=parameters,
                    diagnostics=diagnostics,
                    on_inspected=notify,
                )
            )

        report = build_sync_inspection_report(
            manifests=tuple(manifests),
            failed_paths=tuple(
                FailedPathInspection(path=path, error=error, manifest_index=manifest_index)
                for manifest_index, path, error in failed
            ),
            plugins=shared_plugins,
            inspection_mode=parameters.inspection_mode,
        )
        return SyncPlan(
            parameters=parameters,
            report=report,
            manifests=tuple(previews),
            failed_paths=tuple(failed),
            fingerprints=fingerprints,
            download_directory=download_directory,
        )

    async def inspect_paths(
        self,
        paths: Sequence[str | Path],
        *,
        inspection_mode: InspectionMode = InspectionMode.FAST,
        plugins: DiscoveredPlugins | None = None,
    ) -> tuple[DirectoryStatus, ...]:
        """Inspect a caller-supplied list of directories, one status per path.

        Stateless: the caller (e.g. a downstream GUI) owns the list of
        project directories. Each path is classified as missing, lacking a
        manifest, inspected, or failed, with an action summary when a
        manifest is present. Plugins are discovered once and shared across
        every path.

        Args:
            paths: Directories (or manifest files) to inspect.
            inspection_mode: Inspection depth for directories with a manifest.
            plugins: Pre-discovered plugins. Discovered once when omitted.

        Returns:
            One :class:`DirectoryStatus` per input path, in order.
        """
        shared_plugins = plugins
        if shared_plugins is None:
            shared_plugins = await asyncio.to_thread(discover_all_plugins, use_cache=True)

        statuses: list[DirectoryStatus] = []
        for raw in paths:
            path = await asyncio.to_thread(Path(raw).resolve)
            exists = await asyncio.to_thread(path.exists)
            if not exists:
                statuses.append(
                    DirectoryStatus(
                        path=path,
                        name=path.name,
                        exists=False,
                        state=DirectoryState.MISSING,
                    )
                )
                continue

            if not await asyncio.to_thread(self.has_manifest, path):
                statuses.append(
                    DirectoryStatus(
                        path=path,
                        name=path.name,
                        exists=True,
                        state=DirectoryState.NO_MANIFEST,
                    )
                )
                continue

            report = await self.inspect(
                SetupParameters(paths=path, inspection_mode=inspection_mode, fail_fast=False),
                plugins=shared_plugins,
            )
            error = None if report.success else next((failed.error for failed in report.failed_paths), None)
            statuses.append(
                DirectoryStatus(
                    path=path,
                    name=path.name,
                    exists=True,
                    has_manifest=True,
                    state=DirectoryState.INSPECTED if report.success else DirectoryState.FAILED,
                    summary=report.summary,
                    error=error,
                )
            )

        return tuple(statuses)

    # --- Execution API ---

    async def _execution_events(
        self,
        parameters: SetupParameters,
        *,
        plugins: DiscoveredPlugins | None = None,
        executor: ManifestExecutor = execute_manifest,
        prepared: _Prepared | None = None,
    ) -> AsyncIterator[ProgressEvent]:
        """Yield progress events while executing setup actions.

        Public callers should use :meth:`run` with ``on_event`` instead
        of depending on the generator implementation directly.  When
        *prepared* is given, its manifests run as-is instead of being
        downloaded and loaded from *parameters*.
        """
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        tmp_dir: Path | None = None

        async def _run() -> None:
            """Load manifests, emit events, and execute."""
            nonlocal tmp_dir
            try:
                # Download remote URLs to a temp directory, then merge
                # the downloaded paths into the parameters for manifest
                # loading.
                local_paths, urls = self._resolve_paths(parameters)
                effective_params = parameters
                if urls and prepared is None:
                    tmp = tempfile.mkdtemp(prefix='porringer_')
                    tmp_dir = Path(tmp)
                    url_paths = await self._download_urls(urls, tmp_dir, max_concurrency=parameters.max_concurrency)
                    all_paths = local_paths + url_paths
                    effective_params = parameters.model_copy(update={'paths': all_paths})

                shared_plugins = plugins
                if shared_plugins is None:
                    invalidate_plugin_cache()
                    shared_plugins = await asyncio.to_thread(discover_all_plugins)

                    # Emit PLUGINS_DISCOVERED once for the batch — before
                    # any per-manifest work so the GUI gets the availability
                    # map as early as possible.  Skipped when the caller
                    # pre-passed plugins (they already have the map).
                    queue.put_nowait(await asyncio.to_thread(_plugins_discovered_event, shared_plugins))

                previews, failed = (
                    prepared
                    if prepared is not None
                    else await asyncio.to_thread(self._load_manifests, effective_params, shared_plugins)
                )

                for manifest_index, path, error in failed:
                    queue.put_nowait(ManifestFailedEvent(failed_path=(path, error), manifest_index=manifest_index))

                for index, preview in enumerate(previews):
                    manifest_index = preview.manifest_index if preview.manifest_index is not None else index
                    # execute_manifest populates CLI commands, emits
                    # MANIFEST_LOADED, then reports ACTION_* events.
                    await executor(
                        preview,
                        parameters,
                        event_queue=queue,
                        plugins=shared_plugins,
                        manifest_index=manifest_index,
                    )
            finally:
                # Sentinel signals the generator to stop
                queue.put_nowait(None)

        task = asyncio.ensure_future(_run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        except GeneratorExit:
            # Consumer closed the async generator (e.g. ``break`` or
            # ``aclose()``).  Cancel synchronously only — ``await``
            # inside a ``GeneratorExit`` handler causes
            # ``RuntimeError: async generator ignored GeneratorExit``.
            if not task.done():
                task.cancel()
            return
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        # Propagate any exception from _run() so callers see the
        # real error instead of silently receiving no events.
        if task.done() and not task.cancelled():
            task.result()

    async def run(
        self,
        parameters: SetupParameters | None = None,
        *,
        plan: SyncPlan | None = None,
        plugins: DiscoveredPlugins | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> SyncRunReport:
        """Execute setup, optionally observe events, and return a report.

        This is the single public execution entry point.  All progress
        events flow through ``on_event`` when provided, then the same
        events are collected into ``BatchSetupResults``.

        Pass either *parameters*, to load and run the manifests now, or
        a *plan* from :meth:`plan`, to run exactly what was previewed.
        A plan is checked against its files first and is consumed by the
        run, whether it succeeds or not.

        .. warning::
            Execution mutates process-global ``PATH`` so that runtimes
            installed mid-sync become discoverable.  Runs on one ``API``
            instance are serialized; separate instances or processes are
            not coordinated.

        Args:
            parameters: The setup parameters (paths, strategy, filters, etc.).
            plan: A plan to run instead of *parameters*.
            plugins: Pre-discovered plugins from :meth:`API.discover_plugins`.
            on_event: Optional callback invoked for every progress event
                before collection. If it returns an awaitable, it is awaited.

        Returns:
            Structured sync run report with batch results, diagnostics, and follow-up actions.

        Raises:
            ValueError: If both or neither of *parameters* and *plan* are given,
                or the plan was already run or discarded.
            PlanStaleError: If a file the plan was built from changed since.
            PorringerBusyError: If called from inside a run on the same instance,
                for example from ``on_event``.
        """
        if plan is not None:
            if parameters is not None:
                raise ValueError('Pass either parameters or a plan, not both')
            return await self._run_plan(plan, plugins=plugins, on_event=on_event)
        if parameters is None:
            raise ValueError('Pass either parameters or a plan')
        return await self._run_exclusive(parameters, plugins=plugins, on_event=on_event, executor=execute_manifest)

    async def _run_plan(
        self,
        plan: SyncPlan,
        *,
        plugins: DiscoveredPlugins | None,
        on_event: Callable[[ProgressEvent], object] | None,
    ) -> SyncRunReport:
        """Run a frozen plan under the execution lock, refusing it if stale."""
        async with self._exclusive():
            if plan.consumed:
                raise ValueError('This plan was already run or discarded; make a new one')
            try:
                changed = await asyncio.to_thread(changed_paths, plan.fingerprints)
                if changed:
                    raise PlanStaleError(changed)
                # Fresh containers so execution never writes into the plan.
                previews = [
                    replace(
                        preview,
                        actions=list(preview.actions),
                        action_indices=list(preview.action_indices),
                        results=[],
                    )
                    for preview in plan.manifests
                ]
                return await self._run_unlocked(
                    plan.parameters,
                    plugins=plugins,
                    on_event=on_event,
                    executor=execute_manifest,
                    prepared=(previews, list(plan.failed_paths)),
                )
            finally:
                plan.discard()

    async def _run_exclusive(
        self,
        parameters: SetupParameters,
        *,
        plugins: DiscoveredPlugins | None,
        on_event: Callable[[ProgressEvent], object] | None,
        executor: ManifestExecutor,
    ) -> SyncRunReport:
        """Run *executor* over each manifest under this instance's execution lock."""
        async with self._exclusive():
            return await self._run_unlocked(parameters, plugins=plugins, on_event=on_event, executor=executor)

    @contextlib.asynccontextmanager
    async def _exclusive(self) -> AsyncGenerator[None]:
        """Hold this instance's execution lock, refusing re-entrant use.

        Raises:
            PorringerBusyError: If a run on this instance is already active in this task.
        """
        active = _ACTIVE_RUNS.get()
        if id(self) in active:
            raise PorringerBusyError('A sync run is already active on this API; reentrant runs are not supported')

        async with self._execution_lock():
            token = _ACTIVE_RUNS.set(active | {id(self)})
            try:
                yield
            finally:
                _ACTIVE_RUNS.reset(token)

    async def _run_unlocked(
        self,
        parameters: SetupParameters,
        *,
        plugins: DiscoveredPlugins | None,
        on_event: Callable[[ProgressEvent], object] | None,
        executor: ManifestExecutor,
        prepared: _Prepared | None = None,
    ) -> SyncRunReport:
        """Execute setup without taking the execution lock."""
        manifests: list[SetupResults] = []
        collected: list[tuple[ActionRef | None, SetupActionResult]] = []
        failed_paths: list[tuple[Path, str]] = []

        async for event in self._execution_events(parameters, plugins=plugins, executor=executor, prepared=prepared):
            await _notify(on_event, event)
            if isinstance(event, ManifestLoadedEvent):
                manifests.append(event.manifest)
            elif isinstance(event, ManifestFailedEvent):
                failed_paths.append(event.failed_path)
            elif isinstance(event, ActionCompletedEvent):
                result = event.result
                if result.ref is None and event.action_ref is not None:
                    result.ref = event.action_ref
                collected.append((event.action_ref, result))

        # Partition by stable action refs, keeping object-identity only as
        # a compatibility fallback for legacy events without refs. Results are
        # grouped in a single pass to avoid an O(manifests * results) scan.
        indexed_previews = [
            (
                preview,
                preview.manifest_index if preview.manifest_index is not None else index,
            )
            for index, preview in enumerate(manifests)
        ]

        # Map each action's identity to its owning manifest index, used only
        # for legacy ref-less completion events.
        action_to_manifest_index: dict[int, int] = {
            id(action): manifest_index for preview, manifest_index in indexed_previews for action in preview.actions
        }

        results_by_manifest_index: dict[int, list[SetupActionResult]] = {}
        for ref, result in collected:
            target_index = ref.manifest_index if ref is not None else action_to_manifest_index.get(id(result.action))
            if target_index is None:
                continue
            results_by_manifest_index.setdefault(target_index, []).append(result)

        manifest_results: list[SetupResults] = []

        for preview, manifest_index in indexed_previews:
            mr_results = results_by_manifest_index.get(manifest_index, [])
            sr = SetupResults(
                actions=preview.actions,
                action_indices=preview.action_indices,
                results=mr_results,
                manifest_index=manifest_index,
                manifest_path=preview.manifest_path,
                root_directory=preview.root_directory,
                metadata=preview.metadata,
                preferences=preview.preferences,
            )
            manifest_results.append(sr)

        results = BatchSetupResults(manifest_results=manifest_results, failed_paths=failed_paths)
        diagnostics = batch_diagnostics(results)
        return SyncRunReport(
            status=result_status(results.success, diagnostics),
            results=results,
            diagnostics=diagnostics,
            follow_up_actions=batch_follow_up_actions(results),
        )
