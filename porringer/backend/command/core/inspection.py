"""CLI command implementation for inspection.

Build structured inspection reports for manifest previews.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp

from porringer.backend.command.core.action_builder import get_cli_steps
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.execution import (
    determine_fallback_dir,
    discovered_plugin_entries,
)
from porringer.backend.command.core.presence import inspect_action
from porringer.backend.command.core.resolution import PackageCache, ResolutionContext
from porringer.core.schema import PluginKind
from porringer.schema import (
    ActionInspectedEvent,
    ActionInspection,
    ActionRef,
    ActionSnapshot,
    DiscoveredPluginSnapshot,
    FailedPathInspection,
    InspectionMode,
    InspectionStatus,
    InspectionSummary,
    ManifestDiagnostic,
    ManifestDiagnosticSnapshot,
    ManifestInspection,
    ManifestMetadata,
    ManifestMetadataSnapshot,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SkipReason,
    SyncInspectionReport,
)
from porringer.utility import HTTP_TIMEOUT
from porringer.utility.observability import (
    inspection_diagnostics,
    inspection_follow_up_actions,
    result_status,
)
from porringer.utility.trace import TraceContext, use_trace_context

type InspectedCallback = Callable[[ActionInspectedEvent], Awaitable[None]]
"""Awaited with each action as soon as its inspection finishes."""


def action_snapshot(action: SetupAction, index: int, *, ref: ActionRef | None = None) -> ActionSnapshot:
    """Project a setup action into a JSON-stable snapshot."""
    package = str(action.package) if action.package is not None else None
    return ActionSnapshot(
        index=index,
        ref=ref,
        action_id=ref.action_id if ref is not None else None,
        manifest_index=ref.manifest_index if ref is not None else None,
        action_index=ref.action_index if ref is not None else index,
        description=action.description,
        kind=action.kind.value if action.kind is not None else None,
        ecosystem=str(action.ecosystem) if action.ecosystem is not None else None,
        installer=action.installer,
        package=package,
        package_name=action.package.name if action.package is not None else None,
        package_constraint=action.package.constraint if action.package is not None else None,
        package_description=action.package_description,
        include_prereleases=action.include_prereleases,
        runtime_tag=action.runtime_tag,
        entry_id=action.entry_id,
    )


def action_source_indices(preview: SetupResults) -> list[int]:
    """Return stable source action indexes for a preview."""
    if len(preview.action_indices) == len(preview.actions):
        return preview.action_indices
    return list(range(len(preview.actions)))


def action_trace_context(mode: str, action: SetupAction, ref: ActionRef) -> TraceContext:
    """Build compact trace metadata for an action."""
    return TraceContext(
        mode=mode,
        action_ref=ref,
        action_description=action.description,
        action_kind=action.kind.value if action.kind is not None else None,
        installer=action.installer,
        package_name=action.package.name if action.package is not None else None,
    )


def metadata_snapshot(
    metadata: ManifestMetadata | None,
) -> ManifestMetadataSnapshot | None:
    """Project manifest metadata into a JSON-stable snapshot."""
    if metadata is None:
        return None
    return ManifestMetadataSnapshot(
        name=metadata.name,
        description=metadata.description,
        author=metadata.author,
        url=metadata.url,
    )


def diagnostic_snapshot(diagnostic: ManifestDiagnostic) -> ManifestDiagnosticSnapshot:
    """Project a manifest diagnostic into a JSON-stable snapshot."""
    return ManifestDiagnosticSnapshot(
        field=diagnostic.field,
        message=diagnostic.message,
        code=diagnostic.code.name,
        severity=diagnostic.severity.name.lower(),
    )


def plugin_snapshots(
    plugins: DiscoveredPlugins,
) -> tuple[DiscoveredPluginSnapshot, ...]:
    """Return JSON-stable plugin availability entries."""
    runtime_context = plugins.runtime_context
    return tuple(
        DiscoveredPluginSnapshot(
            name=entry.name,
            available=entry.available,
            capabilities=tuple(sorted(cap.name.lower() for cap in entry.capabilities)),
            kind=entry.kind.value,
        )
        for entry in discovered_plugin_entries(plugins, runtime_context)
    )


def _installer_available(action: SetupAction, plugins: DiscoveredPlugins) -> bool:
    """Return whether the action's current installer is available."""
    if action.installer is None:
        return False
    match action.kind:
        case PluginKind.PACKAGE | PluginKind.TOOL | PluginKind.RUNTIME:
            return action.installer in plugins.environments
        case PluginKind.PROJECT:
            return action.installer in plugins.project_environments
        case PluginKind.SCM:
            return action.installer in plugins.scm_environments
        case None:
            return False
    return False


def _status_from_result(
    action: SetupAction,
    result: SetupActionResult,
    plugins: DiscoveredPlugins,
    *,
    checked: bool = True,
) -> InspectionStatus:
    """Map an inspected action result to a frontend-oriented status.

    Args:
        action: The action being classified.
        result: The inspected (or fast-inspected) result for *action*.
        plugins: Discovered plugins, used to check installer availability.
        checked: Whether presence/update probing actually ran. ``False``
            (FAST inspection) downgrades what would otherwise be
            :attr:`InspectionStatus.NEEDED` to :attr:`InspectionStatus.UNKNOWN`.
            FAST never confirms an action is genuinely needed. It only
            confirms that nothing disqualified it up front (unavailable
            installer, skip).
    """
    if not _installer_available(action, plugins):
        status = InspectionStatus.UNAVAILABLE
    elif result.skip_reason == SkipReason.NO_PROJECT_DIRECTORY:
        status = InspectionStatus.SKIPPED
    elif not result.success:
        status = InspectionStatus.FAILED
    elif result.skipped:
        if result.skip_reason == SkipReason.UPDATE_AVAILABLE:
            status = InspectionStatus.UPDATE_AVAILABLE
        else:
            status = InspectionStatus.SATISFIED
    elif not checked:
        status = InspectionStatus.UNKNOWN
    else:
        status = InspectionStatus.NEEDED
    return status


def _fast_inspect_result(
    action: SetupAction,
    plugins: DiscoveredPlugins,
    parameters: SetupParameters,
) -> SetupActionResult:
    """Inspect one action without running package/update/SCM probes."""
    if action.kind == PluginKind.PROJECT and parameters.project_directory is False:
        return SetupActionResult(
            action=action,
            success=True,
            skipped=True,
            skip_reason=SkipReason.NO_PROJECT_DIRECTORY,
            message='No project directory provided',
        )

    if not _installer_available(action, plugins):
        installer = action.installer or '(unresolved)'
        return SetupActionResult(
            action=action,
            success=False,
            message=f"Installer '{installer}' is not available",
        )

    return SetupActionResult(action=action, success=True)


async def _inspect_result(
    action: SetupAction,
    plugins: DiscoveredPlugins,
    parameters: SetupParameters,
    context: ResolutionContext,
    working_dir: Path,
) -> SetupActionResult:
    """Inspect one action and return a SetupActionResult-shaped decision."""
    if action.kind == PluginKind.PROJECT and parameters.project_directory is False:
        return SetupActionResult(
            action=action,
            success=True,
            skipped=True,
            skip_reason=SkipReason.NO_PROJECT_DIRECTORY,
            message='No project directory provided',
        )

    if not _installer_available(action, plugins):
        installer = action.installer or '(unresolved)'
        return SetupActionResult(
            action=action,
            success=False,
            message=f"Installer '{installer}' is not available",
        )

    return await inspect_action(
        action,
        plugins.environments,
        context=context,
        scm_environments=plugins.scm_environments,
        working_dir=working_dir,
        parameters=parameters,
    )


def _inspection_from_result(
    *,
    index: int,
    ref: ActionRef,
    action: SetupAction,
    result: SetupActionResult,
    plugins: DiscoveredPlugins,
    parameters: SetupParameters,
) -> ActionInspection:
    """Create an action inspection record from an inspected result."""
    cli_steps = get_cli_steps(action, plugins, parameters.strategy)
    cli_command = cli_steps[-1] if cli_steps else ()
    checked = parameters.inspection_mode != InspectionMode.FAST
    return ActionInspection(
        index=index,
        ref=ref,
        action_id=ref.action_id,
        manifest_index=ref.manifest_index,
        action_index=ref.action_index,
        action=action_snapshot(action, index, ref=ref),
        status=_status_from_result(action, result, plugins, checked=checked),
        cli_command=cli_command,
        cli_steps=cli_steps,
        success=result.success,
        skipped=result.skipped,
        skip_reason=result.skip_reason.name if result.skip_reason is not None else None,
        message=result.message,
        installed_version=result.installed_version,
        available_version=result.available_version,
    )


def _inspected_event(
    action: SetupAction, result: SetupActionResult, inspection: ActionInspection
) -> ActionInspectedEvent:
    """Build the event for one finished check, with the result carrying the inspection's ref and commands."""
    result.ref = inspection.ref
    if result.cli_command is None:
        result.cli_command = inspection.cli_command or None
    if result.cli_steps is None:
        result.cli_steps = inspection.cli_steps or None
    return ActionInspectedEvent(action=action, result=result, inspection=inspection)


async def inspect_actions(
    preview: SetupResults,
    plugins: DiscoveredPlugins,
    parameters: SetupParameters,
    *,
    manifest_index: int = 0,
    on_inspected: InspectedCallback | None = None,
) -> tuple[ActionInspection, ...]:
    """Inspect every action in a manifest preview.

    *on_inspected* is awaited with each action as soon as its checks
    finish: in source order for FAST inspection, in completion order
    otherwise. The returned tuple is always in source order.
    """
    assert preview.root_directory is not None

    if parameters.inspection_mode == InspectionMode.FAST:
        source_indices = action_source_indices(preview)
        inspections: list[ActionInspection] = []
        for display_index, (source_index, action) in enumerate(zip(source_indices, preview.actions, strict=True)):
            result = _fast_inspect_result(action, plugins, parameters)
            inspection = _inspection_from_result(
                index=display_index,
                ref=ActionRef.from_indices(manifest_index, source_index),
                action=action,
                result=result,
                plugins=plugins,
                parameters=parameters,
            )
            inspections.append(inspection)
            if on_inspected is not None:
                await on_inspected(_inspected_event(action, result, inspection))
        return tuple(inspections)

    working_dir = determine_fallback_dir(parameters, preview.root_directory)
    runtime_context = plugins.runtime_context
    max_concurrency = parameters.max_concurrency
    slots: list[ActionInspection | None] = [None] * len(preview.actions)
    cache = PackageCache()
    next_index = 0
    next_lock = asyncio.Lock()
    source_indices = action_source_indices(preview)

    async def _next_action() -> tuple[int, int, SetupAction] | None:
        nonlocal next_index
        async with next_lock:
            if next_index >= len(preview.actions):
                return None
            index = next_index
            next_index += 1
            return index, source_indices[index], preview.actions[index]

    async def _run(
        index: int,
        source_index: int,
        action: SetupAction,
        client: aiohttp.ClientSession,
    ) -> None:
        ref = ActionRef.from_indices(manifest_index, source_index)
        context = ResolutionContext(
            project_environments=plugins.project_environments,
            runtime_context=runtime_context,
            http_client=client,
            package_cache=cache,
        )
        try:
            with use_trace_context(action_trace_context('inspect', action, ref)):
                result = await _inspect_result(action, plugins, parameters, context, working_dir)
        except Exception as exc:
            result = SetupActionResult(action=action, success=False, message=str(exc))
        inspection = _inspection_from_result(
            index=index,
            ref=ref,
            action=action,
            result=result,
            plugins=plugins,
            parameters=parameters,
        )
        slots[index] = inspection
        if on_inspected is not None:
            await on_inspected(_inspected_event(action, result, inspection))

    async def _worker(client: aiohttp.ClientSession) -> None:
        while (item := await _next_action()) is not None:
            index, source_index, action = item
            await _run(index, source_index, action, client)

    async with (
        aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as shared_client,
        asyncio.TaskGroup() as task_group,
    ):
        worker_count = len(preview.actions) if max_concurrency <= 0 else min(max_concurrency, len(preview.actions))
        for _ in range(worker_count):
            task_group.create_task(_worker(shared_client))

    cache.log_debug_stats('inspect')

    return tuple(inspection for inspection in slots if inspection is not None)


def inspection_summary(
    manifests: tuple[ManifestInspection, ...],
    failed_paths: tuple[FailedPathInspection, ...],
) -> InspectionSummary:
    """Build aggregate counts for an inspection report."""
    actions = [action for manifest in manifests for action in manifest.actions]
    return InspectionSummary(
        manifests=len(manifests),
        failed_paths=len(failed_paths),
        actions=len(actions),
        needed=sum(1 for action in actions if action.status == InspectionStatus.NEEDED),
        satisfied=sum(1 for action in actions if action.status == InspectionStatus.SATISFIED),
        update_available=sum(1 for action in actions if action.status == InspectionStatus.UPDATE_AVAILABLE),
        unavailable=sum(1 for action in actions if action.status == InspectionStatus.UNAVAILABLE),
        failed=sum(1 for action in actions if action.status == InspectionStatus.FAILED),
        skipped=sum(1 for action in actions if action.status == InspectionStatus.SKIPPED),
        unknown=sum(1 for action in actions if action.status == InspectionStatus.UNKNOWN),
    )


async def build_manifest_inspection(
    *,
    index: int,
    preview: SetupResults,
    plugins: DiscoveredPlugins,
    parameters: SetupParameters,
    diagnostics: tuple[ManifestDiagnostic, ...],
    on_inspected: InspectedCallback | None = None,
) -> ManifestInspection:
    """Build an inspection report for a loaded manifest preview."""
    actions = await inspect_actions(preview, plugins, parameters, manifest_index=index, on_inspected=on_inspected)
    return ManifestInspection(
        index=index,
        manifest_path=preview.manifest_path,
        root_directory=preview.root_directory,
        metadata=metadata_snapshot(preview.metadata),
        preferences={str(key): value for key, value in preview.preferences.items()},
        diagnostics=tuple(diagnostic_snapshot(diagnostic) for diagnostic in diagnostics),
        actions=actions,
    )


def build_sync_inspection_report(
    *,
    manifests: tuple[ManifestInspection, ...],
    failed_paths: tuple[FailedPathInspection, ...],
    plugins: DiscoveredPlugins,
    inspection_mode: InspectionMode = InspectionMode.COMPLETE,
) -> SyncInspectionReport:
    """Build the top-level sync inspection report."""
    report = SyncInspectionReport(
        inspection_mode=inspection_mode,
        manifests=manifests,
        failed_paths=failed_paths,
        plugins=plugin_snapshots(plugins),
        summary=inspection_summary(manifests, failed_paths),
    )
    diagnostics = inspection_diagnostics(report)
    follow_up_actions = inspection_follow_up_actions(report)
    return report.model_copy(
        update={
            'status': result_status(report.success, diagnostics),
            'diagnostics': diagnostics,
            'follow_up_actions': follow_up_actions,
        }
    )
