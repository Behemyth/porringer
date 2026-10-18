"""Read-only inventory of runtimes and the packages installed in each location.

Runtime-bound plugins (pip, uv) are listed once per interpreter; plugins
with one store for the machine or user (pipx, winget, brew) are listed
once. A location that fails reports its error instead of looking empty.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from packaging.version import InvalidVersion, Version

from porringer.core.plugin_schema.environment import (
    CheckUpdatesParameters,
    Environment,
    PackageScope,
)
from porringer.core.plugin_schema.runtime import (
    DefaultRuntimeExecutableProvider,
    RuntimeConsumer,
    RuntimeContext,
    RuntimeProvider,
    runtime_tag_matches,
)
from porringer.core.schema import PackageRef, PluginKind
from porringer.schema import (
    InventoryLocation,
    InventoryPackage,
    InventoryReport,
    InventoryStatus,
    ProviderFailure,
    RuntimeRecord,
)
from porringer.utility.concurrency import gather_bounded

from .core.discovery import DiscoveredPlugins

logger = logging.getLogger(__name__)

_PACKAGE_KINDS = frozenset({PluginKind.PACKAGE, PluginKind.TOOL})


def _is_newer(latest: str, current: str | None) -> bool:
    if current is None:
        return True
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return latest != current


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


async def list_runtimes(
    environments: dict[str, Environment],
) -> tuple[list[RuntimeRecord], list[ProviderFailure]]:
    """List every runtime each available provider can resolve, with default and managed flags."""
    records: list[RuntimeRecord] = []
    failures: list[ProviderFailure] = []
    for name, env in sorted(environments.items()):
        if not type(env).is_supported() or not isinstance(env, RuntimeProvider) or not env.query_availability(None):
            continue
        provider = env
        try:
            tags = provider.sort_tags(await provider.available_tags())
            default_tag = await provider.default_tag()
            default_exe = (
                await provider.default_executable() if isinstance(provider, DefaultRuntimeExecutableProvider) else None
            )
            managed = {package.name for package in await env.packages()}
            executables = await asyncio.gather(*(provider.resolve_executable(tag) for tag in tags))
        except Exception as exc:
            logger.warning('Runtime provider %s failed to list runtimes: %s', name, exc)
            failures.append(ProviderFailure(provider=name, error=str(exc)))
            continue
        kind = provider.provided_runtime_kind()
        # The default executable can be a virtual environment (py honours VIRTUAL_ENV); then fall back to the tag.
        by_executable = default_exe is not None and any(
            exe is not None and _same_path(exe, default_exe) for exe in executables
        )
        for tag, exe in zip(tags, executables, strict=True):
            if exe is None:
                continue
            if by_executable:
                default = default_exe is not None and _same_path(exe, default_exe)
            else:
                default = default_tag is not None and runtime_tag_matches(tag, default_tag)
            records.append(
                RuntimeRecord(
                    provider=name,
                    kind=kind,
                    tag=tag,
                    executable=exe,
                    default=default,
                    managed=tag in managed,
                )
            )
    return records, failures


async def _query_location(
    name: str,
    env: Environment,
    runtime: RuntimeRecord | None,
    context: RuntimeContext | None,
    *,
    include_updates: bool,
    include_prereleases: bool = False,
) -> InventoryLocation:
    """List one location's packages, then optionally attach newer versions."""
    try:
        installed = await env.packages(runtime_context=context)
    except Exception as exc:
        logger.warning('Listing packages via %s failed: %s', name, exc)
        return InventoryLocation(plugin=name, runtime=runtime, status=InventoryStatus.ERROR, error=str(exc))

    packages = [InventoryPackage(name=package.name, version=package.version) for package in installed]
    update_error: str | None = None
    if include_updates and packages:
        try:
            params = CheckUpdatesParameters(
                packages=[PackageRef(name=package.name) for package in packages],
                include_prereleases=include_prereleases,
                runtime_context=context,
            )
            latest = {str(package.name): package.version for package in await env.check_updates(params)}
            packages = [
                package.model_copy(update={'latest_version': version})
                if (version := latest.get(package.name)) and _is_newer(version, package.version)
                else package
                for package in packages
            ]
        except Exception as exc:
            logger.warning('Checking updates via %s failed: %s', name, exc)
            update_error = str(exc)

    return InventoryLocation(
        plugin=name,
        runtime=runtime,
        packages=tuple(packages),
        update_error=update_error,
    )


async def build_inventory(
    plugins: DiscoveredPlugins,
    *,
    runtimes: set[str] | None = None,
    plugin_names: set[str] | None = None,
    include_global: bool = True,
    include_updates: bool = False,
    include_prereleases: bool = False,
    max_concurrency: int = 8,
) -> InventoryReport:
    """Build the inventory.

    Args:
        plugins: Discovered plugins.
        runtimes: Runtime tags to list runtime-bound packages for; ``None`` means all.
        plugin_names: Package plugins to list; ``None`` means all.
        include_global: Whether to list plugins with one global store.
        include_updates: Whether to look up newer versions (slower, uses the network).
        include_prereleases: Whether newer versions may be pre-releases.
        max_concurrency: Maximum locations queried at once.
    """
    environments = dict(plugins.environments or {})
    records, failures = await list_runtimes(environments)
    selected = [
        record
        for record in records
        if runtimes is None or any(runtime_tag_matches(record.tag, wanted) for wanted in runtimes)
    ]

    query = partial(_query_location, include_updates=include_updates, include_prereleases=include_prereleases)
    jobs: list[Callable[[], Awaitable[InventoryLocation]]] = []
    seen: set[tuple[str, str]] = set()
    for name, env in sorted(environments.items()):
        plugin_type = type(env)
        if plugin_type.plugin_kind() not in _PACKAGE_KINDS or not plugin_type.is_supported():
            continue
        if plugin_names is not None and name not in plugin_names:
            continue

        if plugin_type.package_scope() is PackageScope.GLOBAL:
            if include_global and env.query_availability(plugins.runtime_context):
                jobs.append(partial(query, name, env, None, None))
            continue

        consumed = env.consumed_runtime_kind() if isinstance(env, RuntimeConsumer) else None
        for record in selected:
            # Two providers can report the same interpreter; list it once.
            key = (name, os.path.normcase(str(record.executable)))
            if record.kind != consumed or key in seen:
                continue
            context = RuntimeContext(executables={record.kind: record.executable})
            if not env.query_availability(context):
                continue
            seen.add(key)
            jobs.append(partial(query, name, env, record, context))

    locations = await gather_bounded(jobs, limit=max_concurrency)
    return InventoryReport(
        runtimes=tuple(records),
        locations=tuple(locations),
        provider_failures=tuple(failures),
    )
