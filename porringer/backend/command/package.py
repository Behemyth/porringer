"""CLI command implementation for package.

The package command module.

Update-checking for packages managed by environment plugins.

This module complements :mod:`.plugin` (which manages *porringer*
extension packages) and :mod:`.sync` (which handles manifest-driven
execution).
"""

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from packaging.version import Version

from porringer.backend.builder import Builder
from porringer.backend.command.core.discovery import (
    DiscoveredPlugins,
    discover_all_plugins,
)
from porringer.backend.command.inventory import build_inventory
from porringer.core.plugin_schema.environment import CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.schema import (
    CheckParameters,
    CheckResult,
    InventoryReport,
    PackageUpdateInfo,
)
from porringer.utility.concurrency import gather_bounded
from porringer.utility.exception import PluginError, UpdateError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _build_update_infos(
    env: Any,
    check_params: CheckUpdatesParameters,
    runtime_context: RuntimeContext,
) -> builtins.list[PackageUpdateInfo]:
    """Query an environment for package updates and return structured info."""
    installed = await env.packages(runtime_context=runtime_context)
    installed_map = {str(p.name): p for p in installed}

    updates = await env.check_updates(check_params)

    infos: builtins.list[PackageUpdateInfo] = []
    for update_pkg in updates:
        current = installed_map.get(str(update_pkg.name))
        current_version = Version(current.version) if current and current.version else None
        latest_version = Version(update_pkg.version) if update_pkg.version else None
        infos.append(
            PackageUpdateInfo(
                name=str(update_pkg.name),
                current_version=current_version,
                latest_version=latest_version,
                update_available=True,
            )
        )
    return infos


async def _check_env_updates(
    env: Environment,
    name: str,
    *,
    include_prereleases: bool,
    runtime_context: RuntimeContext,
    log_context: str = '',
) -> CheckResult:
    """Run an update check for a single environment, returning a ``CheckResult``.

    Builds :class:`CheckUpdatesParameters` for *env*, delegates to
    :func:`_build_update_infos`, and normalises any failure into a
    ``CheckResult`` carrying the error string.  Shared by
    ``check_updates``.

    Args:
        env: The environment plugin to query.
        name: The plugin name (used in the result and log messages).
        include_prereleases: Whether to include pre-release versions.
        runtime_context: Resolved runtime paths for this check.
        log_context: Optional suffix appended to log messages (e.g.
            ``' on runtime 3.12'``) for extra diagnostic context.

    Returns:
        A ``CheckResult`` with either the discovered packages or an error.
    """
    try:
        check_params = CheckUpdatesParameters(
            packages=[],
            include_prereleases=include_prereleases,
            runtime_context=runtime_context,
        )
        package_infos = await _build_update_infos(env, check_params, runtime_context)
        return CheckResult(plugin=name, packages=package_infos)
    except (PluginError, UpdateError) as e:
        logger.error('Plugin error checking updates for %s%s: %s', name, log_context, e)
        return CheckResult(plugin=name, error=str(e))
    except Exception as e:
        logger.warning('Failed to check updates for %s%s: %s', name, log_context, e)
        return CheckResult(plugin=name, error=str(e))


# ---------------------------------------------------------------------------
# Public namespace
# ---------------------------------------------------------------------------


class PackageCommands:
    """Update-checking for packages managed by environment plugins.

    All methods are static — the class acts as a namespace and does
    not require instantiation.

    This namespace covers *managed* packages (e.g. ``requests`` via pip,
    ``typescript`` via npm).  For managing *porringer extension* packages
    (e.g. ``porringer-plugin-apt``), see :class:`PluginCommands`.
    """

    # --- Update checking ---

    @staticmethod
    async def check_updates(
        params: CheckParameters | None = None,
        *,
        plugins: DiscoveredPlugins | None = None,
    ) -> builtins.list[CheckResult]:
        """Check for package updates across all (or selected) plugins.

        Args:
            params: Optional check parameters (plugin filter,
                pre-release flag).
            plugins: Pre-discovered plugins.

        Returns:
            One :class:`CheckResult` per queried plugin.
        """
        if params is None:
            params = CheckParameters()

        if plugins is None:
            plugins = await asyncio.to_thread(discover_all_plugins, use_cache=True)

        runtime_context = plugins.resolved_runtime()
        if runtime_context is None:
            runtime_context = await Builder.resolve_runtime_context(plugins.environments)

        environments: dict[str, Environment] = dict(plugins.environments)

        eligible: builtins.list[tuple[str, Environment]] = []
        for name, env in environments.items():
            if params.plugin_names is not None and name not in params.plugin_names:
                continue

            plugin_type = type(env)
            if not plugin_type.is_supported() or not env.query_availability(runtime_context):
                logger.debug('Skipping unavailable plugin %s for update check', name)
                continue

            eligible.append((name, env))

        # ``_check_env_updates`` isolates per-plugin failures into ``CheckResult``,
        # so these independent checks are safe to run concurrently. Bounded fan-out
        # keeps wall-clock near the slowest single check instead of their sum.
        resolved_context = runtime_context

        def _make_check(name: str, target_env: Environment) -> Callable[[], Awaitable[CheckResult]]:
            return lambda: _check_env_updates(
                target_env,
                name,
                include_prereleases=params.include_prereleases,
                runtime_context=resolved_context,
            )

        results = await gather_bounded(
            (_make_check(name, target_env) for name, target_env in eligible),
            limit=params.max_concurrency,
        )

        return results

    # --- Inventory ---

    @staticmethod
    async def inventory(
        *,
        runtimes: set[str] | None = None,
        plugin_names: set[str] | None = None,
        include_global: bool = True,
        include_updates: bool = False,
        include_prereleases: bool = False,
        plugins: DiscoveredPlugins | None = None,
        max_concurrency: int = 8,
    ) -> InventoryReport:
        """List runtimes and the packages installed in each location, read-only.

        Packages of runtime-bound plugins (pip, uv) are listed once per
        interpreter; plugins with one global store (pipx, winget) once.
        Failures are reported per provider and location, never as empty lists.

        Args:
            runtimes: Runtime tags to list interpreter packages for; ``None`` means all.
            plugin_names: Package plugins to list; ``None`` means all.
            include_global: Whether to include plugins with one global store.
            include_updates: Whether to look up newer versions (slower, uses the network).
            include_prereleases: Whether newer versions may be pre-releases.
            plugins: Pre-discovered plugins.
            max_concurrency: Maximum locations queried at once.
        """
        if plugins is None:
            plugins = await asyncio.to_thread(discover_all_plugins, use_cache=True)
        return await build_inventory(
            plugins,
            runtimes=runtimes,
            plugin_names=plugin_names,
            include_global=include_global,
            include_updates=include_updates,
            include_prereleases=include_prereleases,
            max_concurrency=max_concurrency,
        )
