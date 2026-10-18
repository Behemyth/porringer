"""CLI command implementation for tool.

Managed tool/package operations over manifests, including the combined
verbs that change what is installed and edit the manifest in one step.
"""

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path

from porringer.backend.command.core.discovery import (
    DiscoveredPlugins,
    discover_all_plugins,
    invalidate_plugin_cache,
)
from porringer.backend.command.core.execution import (
    execute_manifest,
    uninstall_manifest,
)
from porringer.backend.command.inventory import build_inventory, list_runtimes
from porringer.backend.command.manifest_edit import (
    ManifestDocument,
    bumped_constraint,
    editable_manifest_path,
)
from porringer.backend.command.sync import SyncCommands
from porringer.core.plugin_schema.environment import Environment, PackageParameters
from porringer.core.plugin_schema.runtime import runtime_tag_matches
from porringer.core.schema import Ecosystem, PackageRef, PluginKind
from porringer.schema import (
    BatchSetupResults,
    FailedPathInspection,
    InspectionMode,
    InspectionStatus,
    ManagedPackageResult,
    ManagedToolReport,
    ManifestEntry,
    PackageSpec,
    ProgressEvent,
    RangeStrategy,
    RuntimeDependent,
    RuntimeRecord,
    RuntimeRemovalCheck,
    SetupActionResult,
    SetupParameters,
    SyncStrategy,
)
from porringer.schema.inspection import ActionInspection, SyncInspectionReport
from porringer.utility.exception import RuntimeInUseError
from porringer.utility.observability import (
    batch_diagnostics,
    batch_follow_up_actions,
    result_status,
)

logger = logging.getLogger(__name__)

# Tool operations manage installed packages only; project syncs and
# repository clones are left to ``sync``.
_MANAGED_KINDS = frozenset({PluginKind.PACKAGE, PluginKind.TOOL, PluginKind.RUNTIME})

type _Entry = tuple[PluginKind, Ecosystem, PackageSpec]


class ToolCommands:
    """High-level managed package/tool operations for downstream clients."""

    def __init__(self, sync_commands: SyncCommands) -> None:
        """Initialize tool commands."""
        self._sync = sync_commands

    async def check_updates(
        self,
        *,
        paths: Path | Sequence[str | Path] | None = None,
        plugins: DiscoveredPlugins | None = None,
        plugin_names: set[str] | None = None,
        include_packages: set[str] | None = None,
        action_ids: set[str] | None = None,
        entry_ids: set[str] | None = None,
    ) -> ManagedToolReport:
        """Check manifests for managed packages with updates available.

        ``paths=None`` uses the current project's manifest.
        """
        params = SetupParameters(
            paths=paths,
            inspection_mode=InspectionMode.COMPLETE,
            plugin_names=plugin_names,
            action_kinds=set(_MANAGED_KINDS),
            include_packages=include_packages,
            action_ids=action_ids,
            entry_ids=entry_ids,
            fail_fast=False,
        )
        report = await self._sync.inspect(params, plugins=plugins)
        return _report_from_inspection('check_updates', report)

    async def upgrade_project(
        self,
        *,
        paths: Path | Sequence[str | Path] | None = None,
        plugins: DiscoveredPlugins | None = None,
        plugin_names: set[str] | None = None,
        include_packages: set[str] | None = None,
        action_ids: set[str] | None = None,
        entry_ids: set[str] | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> ManagedToolReport:
        """Upgrade managed packages declared by manifests.

        ``paths=None`` uses the current project's manifest.  Only package,
        tool, and runtime actions run; project syncs and clones are skipped.
        """
        params = SetupParameters(
            paths=paths,
            strategy=SyncStrategy.LATEST,
            plugin_names=plugin_names,
            action_kinds=set(_MANAGED_KINDS),
            include_packages=include_packages,
            action_ids=action_ids,
            entry_ids=entry_ids,
            fail_fast=False,
        )
        report = await self._sync.run(params, plugins=plugins, on_event=on_event)
        return _report_from_batch('upgrade_project', report.results)

    async def uninstall(
        self,
        *,
        paths: Path | Sequence[str | Path] | None = None,
        plugins: DiscoveredPlugins | None = None,
        plugin_names: set[str] | None = None,
        include_packages: set[str] | None = None,
        action_ids: set[str] | None = None,
        entry_ids: set[str] | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> ManagedToolReport:
        """Uninstall managed packages declared by manifests.

        ``paths=None`` uses the current project's manifest.  Select what
        to remove with ``include_packages``, ``action_ids`` and/or
        ``entry_ids``, and optionally narrow by ``plugin_names``.  Packages
        that are not installed are skipped with ``NOT_INSTALLED``.

        Raises:
            ValueError: If none of ``include_packages``, ``action_ids`` or
                ``entry_ids`` is given, so a manifest is never wiped by accident.
        """
        if include_packages is None and action_ids is None and entry_ids is None:
            raise ValueError('uninstall requires include_packages, action_ids or entry_ids')
        params = SetupParameters(
            paths=paths,
            plugin_names=plugin_names,
            action_kinds=set(_MANAGED_KINDS),
            include_packages=include_packages,
            action_ids=action_ids,
            entry_ids=entry_ids,
            fail_fast=False,
        )
        report = await self._sync._run_exclusive(
            params, plugins=plugins, on_event=on_event, executor=uninstall_manifest
        )
        return _report_from_batch('uninstall', report.results)

    # --- Combined verbs: change what is installed and edit the manifest together ---

    def entries(self, *, manifest: Path | None = None) -> tuple[ManifestEntry, ...]:
        """List a manifest's entries as written (the global manifest by default), read-only.

        Returns an empty tuple when the manifest doesn't exist yet.
        """
        path = self._manifest_target(manifest)
        if not path.is_file():
            return ()
        return tuple(ManifestDocument(path).entries())

    async def add(
        self,
        spec: PackageSpec | str,
        *,
        kind: PluginKind,
        ecosystem: str,
        manifest: Path | None = None,
        install: bool = True,
        plugins: DiscoveredPlugins | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> ManagedToolReport:
        """Install *spec*, then record it in the manifest (the global manifest by default).

        Adding an entry that already exists (same name, installer and runtime)
        updates it in place, for example to change its constraint or to skip a
        version with ``!=``. With ``install=False`` only the manifest is edited.
        Nothing is written when the install fails.

        Raises:
            ValueError: If *kind* is not a package, tool or runtime.
            ManifestError: If the manifest can't be edited or the edit is invalid.
        """
        if kind not in _MANAGED_KINDS:
            raise ValueError(f'add manages packages, tools and runtimes, not {kind.value}')
        entry = PackageSpec.model_validate(spec)
        target: _Entry = (kind, Ecosystem(ecosystem), entry)

        async with self._sync._exclusive():
            document = ManifestDocument(self._manifest_target(manifest))
            report = ManagedToolReport(operation='add')
            if install:
                async with self._staged(document, [target]) as (staged, ids):
                    report = await self._run_staged(staged, ids, 'add', SyncStrategy.MINIMAL, plugins, on_event)
                if not report.success:
                    return report
            document.upsert(*target)
            document.save()
            return report

    async def remove(
        self,
        entry_ids: set[str],
        *,
        manifest: Path | None = None,
        keep_installed: bool = False,
        cascade: bool = False,
        plugins: DiscoveredPlugins | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> ManagedToolReport:
        """Uninstall entries, then drop them from the manifest (the global manifest by default).

        With ``keep_installed=True`` only the manifest is edited. Entries stay
        in the manifest when their uninstall fails.

        Removing a runtime entry first checks what depends on it (see
        :meth:`runtime_dependents`). It is refused while anything does,
        unless ``cascade=True``, which removes those dependents first.

        Raises:
            ValueError: If *entry_ids* is empty or names entries the manifest doesn't have.
            ManifestError: If the manifest can't be edited.
            RuntimeInUseError: If a runtime still has dependents and *cascade* is false.
        """
        if not entry_ids:
            raise ValueError('remove requires entry_ids')

        async with self._sync._exclusive():
            document = self._document_with(entry_ids, manifest)
            report = ManagedToolReport(operation='remove')
            if not keep_installed:
                checks, plugins = await self._runtime_checks(document, entry_ids, plugins)
                blocked = [check for check in checks if check.dependents]
                if blocked and not cascade:
                    raise RuntimeInUseError(
                        ', '.join(check.runtime for check in blocked),
                        [dependent.description for check in blocked for dependent in check.dependents],
                    )
                if blocked:
                    report = await self._remove_dependents(blocked, plugins, on_event)
                    if not report.success:
                        return report

                params = SetupParameters(
                    paths=[document.path],
                    action_kinds=set(_MANAGED_KINDS),
                    entry_ids=entry_ids,
                    fail_fast=False,
                )
                run = await self._sync._run_unlocked(
                    params,
                    plugins=plugins,
                    on_event=on_event,
                    executor=uninstall_manifest,
                )
                report = _merge(report, _report_from_batch('remove', run.results))
                if not report.success:
                    return report
                _forget_runtimes(checks, plugins)
            # Cascading may have edited this manifest too.
            document = ManifestDocument(document.path)
            document.remove(entry_ids)
            document.save()
            return report

    async def runtime_dependents(
        self,
        entry_ids: set[str],
        *,
        manifest: Path | None = None,
        plugins: DiscoveredPlugins | None = None,
    ) -> tuple[RuntimeRemovalCheck, ...]:
        """Report what removing the runtime entries among *entry_ids* would affect, read-only.

        Dependents are entries in this manifest or the global one whose
        ``runtime`` is that runtime, and installed tools built on its
        interpreter (such as pipx venvs). Packages inside the interpreter
        are listed separately; they go with it.

        Raises:
            ValueError: If *entry_ids* names entries the manifest doesn't have.
        """
        document = self._document_with(entry_ids, manifest)
        checks, _plugins = await self._runtime_checks(document, entry_ids, plugins)
        return tuple(checks)

    async def _runtime_checks(
        self,
        document: ManifestDocument,
        entry_ids: set[str],
        plugins: DiscoveredPlugins | None,
    ) -> tuple[list[RuntimeRemovalCheck], DiscoveredPlugins | None]:
        """Build a removal check for each runtime entry in *entry_ids*."""
        runtimes = [entry for entry in document.find(entry_ids).values() if entry.kind is PluginKind.RUNTIME]
        if not runtimes:
            return [], plugins
        if plugins is None:
            plugins = await asyncio.to_thread(discover_all_plugins)
        environments = dict(plugins.environments or {})
        records, _failures = await list_runtimes(environments)

        manifests = [document.path]
        global_path = self._sync.global_manifest_path
        if global_path is not None and global_path.is_file() and global_path != document.path.resolve():
            manifests.append(global_path)

        entries = [entry for path in manifests for entry in ManifestDocument(path).entries()]
        checks: list[RuntimeRemovalCheck] = []
        for runtime in runtimes:
            tag = runtime.spec.name.name
            candidates = [r for r in records if r.kind == runtime.ecosystem and runtime_tag_matches(r.tag, tag)]
            record = next((r for r in candidates if r.provider == runtime.spec.installer), None) or next(
                iter(candidates), None
            )
            dependents = [
                RuntimeDependent(
                    description=(
                        f'{other.spec.name.name} ({other.kind.value}.{other.ecosystem}) in {other.manifest_path}'
                    ),
                    manifest_path=other.manifest_path,
                    entry_id=other.entry_id,
                )
                for other in entries
                if other.entry_id not in entry_ids
                and other.ecosystem == runtime.ecosystem
                and other.spec.runtime is not None
                and (runtime_tag_matches(other.spec.runtime, tag) or runtime_tag_matches(tag, other.spec.runtime))
            ]
            installed: list[str] = []
            if record is not None:
                dependents.extend(await _tool_dependents(environments, record))
                inventory = await build_inventory(plugins, runtimes={record.tag}, include_global=False)
                installed = sorted({
                    package.name
                    for location in inventory.locations
                    if location.runtime is not None and location.runtime.executable == record.executable
                    for package in location.packages
                })
            checks.append(
                RuntimeRemovalCheck(
                    entry_id=runtime.entry_id,
                    runtime=tag,
                    executable=record.executable if record else None,
                    default=record.default if record else False,
                    dependents=tuple(dependents),
                    installed_packages=tuple(installed),
                )
            )
        return checks, plugins

    async def _remove_dependents(
        self,
        checks: Sequence[RuntimeRemovalCheck],
        plugins: DiscoveredPlugins | None,
        on_event: Callable[[ProgressEvent], object] | None,
    ) -> ManagedToolReport:
        """Remove dependent entries (uninstall, then drop) and dependent tools, before their runtime."""
        report = ManagedToolReport(operation='remove')
        by_manifest: dict[Path, set[str]] = {}
        tools: list[RuntimeDependent] = []
        for check in checks:
            for dependent in check.dependents:
                if dependent.manifest_path is not None and dependent.entry_id is not None:
                    by_manifest.setdefault(dependent.manifest_path, set()).add(dependent.entry_id)
                elif dependent.plugin is not None and dependent.package is not None:
                    tools.append(dependent)

        for path, ids in by_manifest.items():
            params = SetupParameters(
                paths=[path],
                action_kinds=set(_MANAGED_KINDS),
                entry_ids=ids,
                fail_fast=False,
            )
            run = await self._sync._run_unlocked(
                params, plugins=plugins, on_event=on_event, executor=uninstall_manifest
            )
            report = _merge(report, _report_from_batch('remove', run.results))
            if not report.success:
                return report
            document = ManifestDocument(path)
            document.remove(ids)
            document.save()

        environments = dict(plugins.environments or {}) if plugins is not None else {}
        results = []
        for tool in tools:
            assert tool.plugin is not None
            assert tool.package is not None
            env = environments.get(tool.plugin)
            try:
                if env is None:
                    raise RuntimeError(f"Installer '{tool.plugin}' is not available")
                removed = await env.uninstall(PackageParameters(package=PackageRef(name=tool.package)))
                results.append(
                    ManagedPackageResult(
                        plugin=tool.plugin,
                        package=tool.package,
                        success=removed is not None,
                        message=None if removed is not None else 'Uninstall failed',
                    )
                )
            except Exception as exc:
                results.append(
                    ManagedPackageResult(
                        plugin=tool.plugin,
                        package=tool.package,
                        success=False,
                        message=str(exc),
                    )
                )
        return _merge(
            report,
            ManagedToolReport(
                operation='remove',
                status=result_status(all(result.success for result in results)),
                results=tuple(results),
            ),
        )

    async def upgrade(
        self,
        entry_ids: set[str],
        *,
        manifest: Path | None = None,
        range_strategy: RangeStrategy = RangeStrategy.IN_RANGE,
        plugins: DiscoveredPlugins | None = None,
        on_event: Callable[[ProgressEvent], object] | None = None,
    ) -> ManagedToolReport:
        """Upgrade entries of a manifest (the global manifest by default).

        ``IN_RANGE`` stays within each entry's constraint and leaves the
        manifest alone. ``REPLACE`` upgrades past the constraint, then moves
        the constraint of each entry that succeeded up to the installed version.

        Raises:
            ValueError: If *entry_ids* is empty or names entries the manifest doesn't have.
            ManifestError: If the manifest can't be edited.
        """
        if not entry_ids:
            raise ValueError('upgrade requires entry_ids')

        async with self._sync._exclusive():
            document = self._document_with(entry_ids, manifest)
            if range_strategy is RangeStrategy.IN_RANGE:
                params = SetupParameters(
                    paths=[document.path],
                    strategy=SyncStrategy.LATEST,
                    action_kinds=set(_MANAGED_KINDS),
                    entry_ids=entry_ids,
                    fail_fast=False,
                )
                run = await self._sync._run_unlocked(
                    params,
                    plugins=plugins,
                    on_event=on_event,
                    executor=execute_manifest,
                )
                return _report_from_batch('upgrade', run.results)

            found = document.find(entry_ids)
            unbounded = [
                (entry.kind, Ecosystem(entry.ecosystem), _with_constraint(entry.spec, None)) for entry in found.values()
            ]
            async with self._staged(document, unbounded) as (staged, ids):
                report = await self._run_staged(staged, ids, 'upgrade', SyncStrategy.LATEST, plugins, on_event)
                inspection = await self._sync.inspect(SetupParameters(paths=[staged], fail_fast=False), plugins=plugins)

            installed = {
                ids[action.action.entry_id]: action.installed_version
                for manifest_report in inspection.manifests
                for action in manifest_report.actions
                if action.action.entry_id in ids and action.installed_version
            }
            succeeded = {result.entry_id for result in report.results if result.success}
            changed = False
            for eid, entry in found.items():
                spec = entry.spec
                version = installed.get(eid)
                if eid not in succeeded or version is None:
                    continue
                constraint = bumped_constraint(spec.name.constraint, version)
                if constraint is not None and constraint != spec.name.constraint:
                    document.upsert(entry.kind, Ecosystem(entry.ecosystem), _with_constraint(spec, constraint))
                    changed = True
            if changed:
                document.save()
            return report

    def _manifest_target(self, manifest: Path | None) -> Path:
        """Resolve the manifest a combined verb edits; the global manifest when *manifest* is None."""
        if manifest is not None:
            return editable_manifest_path(manifest)
        if self._sync.global_manifest_path is None:
            raise ValueError('No manifest given and this API has no global manifest')
        return self._sync.global_manifest_path

    def _document_with(self, entry_ids: set[str], manifest: Path | None) -> ManifestDocument:
        """Open the target manifest, checking it has every entry in *entry_ids*."""
        document = ManifestDocument(self._manifest_target(manifest))
        if missing := entry_ids - document.find(entry_ids).keys():
            raise ValueError(f'{document.path} has no entries with ids: {", ".join(sorted(missing))}')
        return document

    @contextlib.asynccontextmanager
    async def _staged(
        self, document: ManifestDocument, entries: Sequence[_Entry]
    ) -> AsyncGenerator[tuple[Path, dict[str, str]]]:
        """Write *entries* to a throwaway manifest so they can run before the real one changes.

        Yields the staged path and a map from staged entry ids to the ids the
        entries have in *document*. Nothing is inferred from the staging folder.
        """
        with tempfile.TemporaryDirectory(prefix='porringer-stage-') as directory:
            staged = ManifestDocument((Path(directory) / 'porringer.json').resolve())
            if preferences := document.data.get('preferences'):
                staged.data['preferences'] = preferences
            ids = {staged.upsert(*entry): document.entry_id(*entry) for entry in entries}
            staged.save()

            with self._sync.standalone_manifest(staged.path):
                yield staged.path, ids

    async def _run_staged(
        self,
        staged: Path,
        ids: dict[str, str],
        operation: str,
        strategy: SyncStrategy,
        plugins: DiscoveredPlugins | None,
        on_event: Callable[[ProgressEvent], object] | None,
    ) -> ManagedToolReport:
        """Run a staged manifest and report results under the real manifest's entry ids."""
        params = SetupParameters(paths=[staged], strategy=strategy, fail_fast=False)
        run = await self._sync._run_unlocked(params, plugins=plugins, on_event=on_event, executor=execute_manifest)
        report = _report_from_batch(operation, run.results)
        results = tuple(
            result.model_copy(update={'entry_id': ids[result.entry_id]}) if result.entry_id in ids else result
            for result in report.results
        )
        return report.model_copy(update={'results': results})


def _with_constraint(spec: PackageSpec, constraint: str | None) -> PackageSpec:
    """Return *spec* with its version constraint replaced."""
    return spec.model_copy(update={'name': spec.name.model_copy(update={'constraint': constraint})})


def _merge(first: ManagedToolReport, second: ManagedToolReport) -> ManagedToolReport:
    """Combine two reports of the same operation."""
    return second.model_copy(
        update={
            'status': second.status if first.success else first.status,
            'manifests_processed': first.manifests_processed + second.manifests_processed,
            'results': (*first.results, *second.results),
            'failed_paths': (*first.failed_paths, *second.failed_paths),
            'diagnostics': (*first.diagnostics, *second.diagnostics),
            'follow_up_actions': (*first.follow_up_actions, *second.follow_up_actions),
        }
    )


async def _tool_dependents(environments: dict[str, Environment], record: RuntimeRecord) -> list[RuntimeDependent]:
    """Ask each plugin which of its installs are built on *record*'s interpreter."""
    dependents: list[RuntimeDependent] = []
    for name, env in sorted(environments.items()):
        try:
            packages = await env.runtime_dependents(record.executable)
        except Exception:
            logger.warning(
                '%s could not report what depends on %s',
                name,
                record.executable,
                exc_info=True,
            )
            continue
        dependents.extend(
            RuntimeDependent(description=f'{package} ({name})', plugin=name, package=package) for package in packages
        )
    return dependents


def _forget_runtimes(checks: Sequence[RuntimeRemovalCheck], plugins: DiscoveredPlugins | None) -> None:
    """Drop cached knowledge of removed interpreters so later calls don't use them."""
    removed = {os.path.normcase(str(check.executable)) for check in checks if check.executable is not None}
    if not removed:
        return
    invalidate_plugin_cache()
    context = plugins.runtime_context if plugins is not None else None
    if context is not None and any(os.path.normcase(str(exe)) in removed for exe in context.executables.values()):
        assert plugins is not None
        plugins.runtime_context = None


def _report_from_inspection(operation: str, report: SyncInspectionReport) -> ManagedToolReport:
    """Build a tool report from an inspection report."""
    results = [
        _inspection_snapshot(action)
        for manifest in report.manifests
        for action in manifest.actions
        if action.status == InspectionStatus.UPDATE_AVAILABLE
    ]
    return ManagedToolReport(
        operation=operation,
        status=result_status(report.success, report.diagnostics),
        manifests_processed=report.summary.manifests,
        results=tuple(results),
        failed_paths=report.failed_paths,
        diagnostics=report.diagnostics,
        follow_up_actions=tuple(
            action
            for action in report.follow_up_actions
            if action.target is not None and action.target.kind in {'action', 'package'}
        ),
    )


def _report_from_batch(operation: str, batch: BatchSetupResults) -> ManagedToolReport:
    """Build a tool report from collected execution results."""
    results = tuple(_result_snapshot(result) for manifest in batch.manifest_results for result in manifest.results)
    failed_paths = tuple(FailedPathInspection(path=path, error=error) for path, error in batch.failed_paths)
    diagnostics = batch_diagnostics(batch)
    return ManagedToolReport(
        operation=operation,
        status=result_status(batch.success, diagnostics),
        manifests_processed=len(batch.manifest_results),
        results=results,
        failed_paths=failed_paths,
        diagnostics=diagnostics,
        follow_up_actions=batch_follow_up_actions(batch),
    )


def _inspection_snapshot(action: ActionInspection) -> ManagedPackageResult:
    """Project an inspected package action into a tool result."""
    return ManagedPackageResult(
        plugin=action.action.installer,
        package=action.action.package_name,
        kind=action.action.kind,
        action_id=action.action_id,
        entry_id=action.action.entry_id,
        success=action.success,
        skipped=action.skipped,
        skip_reason=action.skip_reason,
        message=action.message,
        installed_version=action.installed_version,
        available_version=action.available_version,
    )


def _result_snapshot(result: SetupActionResult) -> ManagedPackageResult:
    """Project a setup action result into a tool result."""
    action = result.action
    return ManagedPackageResult(
        plugin=action.installer,
        package=action.package.name if action.package is not None else None,
        kind=action.kind.value if action.kind is not None else None,
        action_id=result.ref.action_id if result.ref is not None else None,
        entry_id=action.entry_id,
        success=result.success,
        skipped=result.skipped,
        skip_reason=result.skip_reason.name if result.skip_reason is not None else None,
        message=result.message,
        installed_version=result.installed_version,
        available_version=result.available_version,
    )
