"""Helpers for api.

Public API surface for Porringer.
"""

import asyncio
import logging

from porringer.backend.builder import Builder
from porringer.backend.command.core.discovery import (
    DiscoveredPlugins,
    discover_all_plugins,
)
from porringer.backend.command.package import PackageCommands
from porringer.backend.command.plugin import PluginCommands
from porringer.backend.command.profile import ProfileCommands
from porringer.backend.command.self import check_self_updates
from porringer.backend.command.sync import SyncCommands
from porringer.backend.command.tool import ToolCommands
from porringer.backend.resolver import resolve_configuration
from porringer.backend.schema import GlobalConfiguration
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.schema import (
    DownloadParameters,
    DownloadResult,
    LocalConfiguration,
    PackageUpdateInfo,
    ProgressCallback,
)
from porringer.utility.download import download_file

logger = logging.getLogger(__name__)


class API:
    """Programmatic interface for Porringer's core operations.

    The class exposes stable sub-APIs for manifest inspection and execution,
    package management, cached tool operations, and
    profile handling.

    Cross-cutting helpers live directly on the ``API`` class:
    :meth:`discover_plugins`, :meth:`check_self_updates`, and
    :meth:`download`.
    """

    def __init__(
        self,
        local_configuration: LocalConfiguration | None = None,
        global_configuration: GlobalConfiguration | None = None,
    ) -> None:
        """Initializes the API.

        Args:
            local_configuration: Optional local configuration (uses defaults if not provided).
            global_configuration: Optional global configuration (uses defaults if not provided).
        """
        if local_configuration is None:
            local_configuration = LocalConfiguration()
        if global_configuration is None:
            global_configuration = GlobalConfiguration()

        directories = resolve_configuration(local_configuration, global_configuration)

        self.global_manifest_path = directories.config_directory / 'porringer.json'
        """The user's global manifest. It may not exist yet."""

        self.extension = PluginCommands()
        self.package = PackageCommands()
        self.sync = SyncCommands(self.global_manifest_path)
        self.tool = ToolCommands(self.sync)
        self.profile = ProfileCommands(self.sync)

    # Discover plugins and resolve runtime information.

    @staticmethod
    async def discover_plugins(
        *,
        use_cache: bool = True,
        resolve_runtime: bool = True,
    ) -> DiscoveredPlugins:
        """Discover all plugins and optionally resolve runtime context.

        This is the recommended entry-point for GUI callers.  It
        returns a :class:`DiscoveredPlugins` object that can be
        forwarded to every subsequent operation
        (``api.sync.inspect``, ``api.sync.run``, ``api.package.check_updates``,
        ``api.tool.upgrade_project``, etc.) so that plugin discovery and
        runtime resolution happen exactly once.

        Args:
            use_cache: Reuse cached entry-point scan metadata when
                ``True`` (the default).  Pass ``False`` to force a
                fresh scan after installing or uninstalling extension packages.
            resolve_runtime: When ``True`` (the default), resolve a
                :class:`RuntimeContext` from available
                ``RuntimeProvider`` plugins and attach it to the
                returned object as ``plugins.runtime_context``.

        Returns:
            A :class:`DiscoveredPlugins` carrying environment,
            project-environment, and SCM plugins, plus an optional
            :class:`RuntimeContext`.
        """
        plugins = await asyncio.to_thread(discover_all_plugins, use_cache=use_cache)
        if resolve_runtime:
            await API.resolve_runtime_context(plugins)
        return plugins

    @staticmethod
    async def resolve_runtime_context(plugins: DiscoveredPlugins) -> RuntimeContext:
        """Resolve and attach runtime context for previously discovered plugins.

        Args:
            plugins: A :class:`DiscoveredPlugins` object, usually from
                :meth:`discover_plugins` with ``resolve_runtime=False``.

        Returns:
            The resolved :class:`RuntimeContext` attached to ``plugins``.
        """
        plugins.runtime_context = await Builder.resolve_runtime_context(plugins.environments)
        logger.debug(
            'resolve_runtime_context: runtime_context=%s',
            {k: str(v) for k, v in plugins.runtime_context.executables.items()}
            if plugins.runtime_context.executables
            else '<empty>',
        )
        return plugins.runtime_context

    @staticmethod
    async def check_self_updates() -> PackageUpdateInfo:
        """Check for updates to the Porringer package by querying PyPI.

        Returns:
            PackageUpdateInfo with current version, latest version, and update status.
        """
        return await check_self_updates()

    @staticmethod
    async def download(
        parameters: DownloadParameters,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        """Download a file with optional hash verification.

        Uses aiohttp for non-blocking HTTP requests.  Suitable for GUI
        applications that need to keep their event loop responsive
        during downloads.

        Args:
            parameters: Download parameters including URL and destination.
            progress_callback: Optional callback for progress updates.

        Returns:
            DownloadResult with success status and details.
        """
        logger.info(f'Downloading: {parameters.url}')
        return await download_file(parameters, progress_callback)
