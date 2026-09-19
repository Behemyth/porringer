"""Core helpers and types for environment.

Plugin utilities for package environments.
"""

import contextlib
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal

import aiohttp
from pydantic import Field

from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.plugin_schema.tool_based import ToolBasedPlugin
from porringer.core.schema import (
    Package,
    PackageRef,
    PluginKind,
    PorringerModel,
)
from porringer.schema import ActionProgress, SetupAction
from porringer.utility import HTTP_TIMEOUT
from porringer.utility.concurrency import gather_bounded
from porringer.utility.utility import CommandProgress, run_command

PackageVerb = Literal['install', 'upgrade', 'uninstall']


@dataclass(frozen=True, slots=True)
class PackageCommand:
    """Concrete command and dry-run behavior for one package operation."""

    argv: tuple[str, ...]
    execute: bool = True


@dataclass(frozen=True, slots=True)
class BootstrapRequirement:
    """Describes a prerequisite package needed before a tool can be used.

    For example, ``pipx`` must itself be installed via ``pip`` before it
    can be used to install other CLI tools.  The sync engine uses this
    to synthesize the prerequisite install automatically.

    Args:
        installer: Name of the environment plugin that installs the
            prerequisite (e.g. ``'pip'``).
        package: The prerequisite package to install (e.g. ``'pipx'``).
        kind: The plugin-kind section the synthesized action belongs
            to. Defaults to ``PluginKind.PACKAGE``.
    """

    installer: str
    package: PackageRef
    kind: PluginKind = dataclass_field(default=PluginKind.PACKAGE)


class PackageParameters(PorringerModel):
    """Parameters for a package install or upgrade operation."""

    package: PackageRef = Field(description='The target package')
    dry: bool = Field(
        default=False, description='If True, rehearses the operation without modifying what is actually installed'
    )
    include_prereleases: bool = Field(
        default=False,
        description='When True, allow pre-release versions during install/upgrade',
    )
    progress_callback: Callable[[ActionProgress], None] | None = Field(
        default=None,
        exclude=True,
        description='Optional callback for reporting action progress (download %, install phase, etc.)',
    )
    runtime_context: RuntimeContext | None = Field(
        default=None,
        exclude=True,
        description=(
            'Runtime context carrying resolved interpreter paths. '
            'Passed to command-generation methods so that the correct '
            'runtime is targeted without storing state on the plugin.'
        ),
    )


class CheckUpdatesParameters(PorringerModel):
    """Parameters for checking updates via a plugin."""

    packages: list[PackageRef] = Field(
        default_factory=list, description='Packages to check for updates. Empty means check all installed packages.'
    )
    include_prereleases: bool = Field(default=False, description='Include pre-release versions')
    max_concurrency: int = Field(
        default=8,
        description=(
            'Maximum number of packages queried concurrently against a '
            'remote registry. Set to 0 for unlimited concurrency. Applied '
            'via an ``asyncio.Semaphore`` around each registry request.'
        ),
    )
    http_client: aiohttp.ClientSession | None = Field(
        default=None,
        exclude=True,
        description=(
            'Shared ``aiohttp.ClientSession`` for connection pooling. '
            'When ``None`` (default), each check creates its own '
            'short-lived session.'
        ),
    )
    runtime_context: RuntimeContext | None = Field(
        default=None,
        exclude=True,
        description=(
            'Runtime context carrying resolved interpreter paths. '
            'Forwarded to ``python_command()`` so that update checks '
            'target the same interpreter as package queries.'
        ),
    )


class Environment(ToolBasedPlugin):
    """Plugin definition for package environments.

    Plugin instances are **stateless** with respect to runtime
    configuration.  A :class:`RuntimeContext` is passed explicitly to
    every method that needs to know which interpreter to target.
    """

    @abstractmethod
    def install_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command that would install a package.

        Override this method to provide the actual command line arguments
        that would be used to install a package.  This is used for
        displaying commands in dry-run / preview mode.

        Args:
            package: The package reference (may include a version constraint).
            include_prereleases: When ``True``, allow pre-release versions
                (e.g. append ``--pre`` for pip-based tools).
            runtime_context: Resolved runtime paths for this execution
                run.  ``None`` means use defaults (e.g. ``sys.executable``).

        Returns:
            A list of command arguments (e.g., ['pip', 'install', 'requests>=1.0']).
        """
        ...

    @abstractmethod
    def upgrade_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command that would upgrade a package.

        Override this method to provide the actual command line arguments
        that would be used to upgrade a package.  This is used for
        displaying commands in dry-run / preview mode.

        Args:
            package: The package reference (may include a version constraint).
            include_prereleases: When ``True``, allow pre-release versions
                (e.g. append ``--pre`` for pip-based tools).
            runtime_context: Resolved runtime paths for this execution
                run.  ``None`` means use defaults.

        Returns:
            A list of command arguments (e.g., ['pip', 'install', '--upgrade', 'requests']).
        """
        ...

    @abstractmethod
    def uninstall_command(
        self,
        package: PackageRef,
        *,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command that would uninstall a package.

        Override this method to provide the actual command line arguments
        that would be used to remove a package.  This is used for
        displaying commands in dry-run / preview mode.

        **Prefer reversal flags** when the package manager supports
        them.  For example, prefer ``apt purge`` over ``apt remove``,
        ``py uninstall --purge`` over plain ``py uninstall``, and
        ``brew uninstall --zap`` over plain ``brew uninstall``.
        Reversal flags keep per-package cleanup in the command layer
        (where it naturally belongs) and avoid the need for a
        ``teardown()`` override just to delete leftover files.

        Reserve ``Plugin.teardown()`` for global, plugin-level state
        that reversal flags cannot address (e.g. removing a PATH
        entry that ``setup()`` created).

        Unlike ``install_command`` and ``upgrade_command``, there is no
        ``include_prereleases`` parameter because pre-release handling
        is irrelevant when removing a package.

        Args:
            package: The package reference (only ``name`` is used).
            runtime_context: Resolved runtime paths for this execution
                run.  ``None`` means use defaults.

        Returns:
            A list of command arguments (e.g., ['pip', 'uninstall', '-y', 'requests']).
        """
        ...

    @staticmethod
    def supports_parallel() -> bool:
        """Returns whether this plugin supports parallel package installations.

        Some package managers (like pip without --no-deps) may have issues with
        concurrent installations. Override this method to return False if the
        plugin requires sequential installation.

        Returns:
            True if parallel installation is supported, False otherwise.
        """
        return True

    # --- Optional plugin hooks --------------------------------------------

    @classmethod
    def bootstrap_requirement(cls) -> BootstrapRequirement | None:
        """Declare a prerequisite package needed before this tool can run.

        Some tools (e.g. ``pipx``) are themselves installed as a package
        via another environment (e.g. ``pip``) before they can install
        anything else.  Overriding this hook lets the sync engine
        synthesize that bootstrap install automatically instead of
        requiring users to declare it explicitly in the manifest.

        Default is ``None`` — most plugins have no bootstrap
        prerequisite (they are expected to already be on PATH, or are
        installed via a runtime provider).

        Returns:
            A :class:`BootstrapRequirement` describing the prerequisite
            package and the installer that provides it, or ``None``.
        """
        return None

    def dry_run_flags(self, verb: PackageVerb) -> Sequence[str]:
        """Return extra CLI flags that turn *verb* into a no-op rehearsal.

        Override on plugins whose underlying tool supports a native
        dry-run mode (e.g. pip's ``--dry-run``, apt's ``--simulate``).
        When the returned sequence is empty, ``params.dry`` falls back
        to the plugin-specific behaviour in its overridden
        ``install`` / ``upgrade`` / ``uninstall`` methods (if any).

        Args:
            verb: Which package operation is being rehearsed.

        Returns:
            A sequence of extra arguments to append to the command,
            or an empty sequence to indicate this hook does not apply.
        """
        del self, verb
        return ()

    async def post_action(
        self,
        verb: PackageVerb,
        params: PackageParameters,
        success: bool,
    ) -> None:
        """Run plugin-specific bookkeeping after a package operation.

        Default is a no-op.  Plugins use this to refresh OS caches,
        regenerate aliases, or warm derived state after a successful
        operation.  Always called once when the subprocess exits, even
        on failure (``success`` reflects the exit status).
        """
        del self, verb, params, success

    def parse_progress_line(
        self,
        line: str,
        channel: Literal['stdout', 'stderr'],
        action: SetupAction,
    ) -> ActionProgress | None:
        """Translate a raw output line into a structured progress event.

        Default returns ``None`` (no structured event).  Plugins
        override this to parse their tool's output (download
        percentages, install phases, etc.) and produce richer
        ``ActionProgress`` events alongside the raw output channel.
        """
        del self, line, channel, action
        return None

    def package_command(self, verb: PackageVerb, params: PackageParameters) -> PackageCommand:
        """Build the command for a package operation.

        The default adapter preserves the existing command-builder methods
        while giving preview and execution one command-construction seam.
        """
        if verb == 'install':
            args = self.install_command(
                params.package,
                include_prereleases=params.include_prereleases,
                runtime_context=params.runtime_context,
            )
        elif verb == 'upgrade':
            args = self.upgrade_command(
                params.package,
                include_prereleases=params.include_prereleases,
                runtime_context=params.runtime_context,
            )
        else:
            args = self.uninstall_command(params.package, runtime_context=params.runtime_context)

        if params.dry:
            args = [*args, *self.dry_run_flags(verb)]
        return PackageCommand(argv=tuple(args))

    async def install(self, params: PackageParameters) -> Package | None:
        """Asynchronously installs the given package identified by its name.

        Uses a native async subprocess via `install_command()`.  Output
        is always observed line-by-line; when `params.progress_callback`
        is set, progress events are emitted to the caller.

        Subclasses only need to override this when the generated command
        differs from `install_command()` or when post-install logic
        (e.g. version retrieval) is required.

        Args:
            params: The package parameters

        Returns:
            The package, or None if installation failed
        """
        command = self.package_command('install', params)
        return await self._execute_package_command(command, params, phase='installing', verb='install')

    async def upgrade(self, params: PackageParameters) -> Package | None:
        """Asynchronously upgrades the given package.

        Uses a native async subprocess via `upgrade_command()`.  Output
        is always observed line-by-line; when `params.progress_callback`
        is set, progress events are emitted to the caller.

        Subclasses only need to override this when the generated command
        differs from `upgrade_command()` or when post-upgrade logic
        (e.g. version retrieval) is required.

        Args:
            params: The package parameters

        Returns:
            The package, or None if the upgrade failed.
        """
        command = self.package_command('upgrade', params)
        return await self._execute_package_command(command, params, phase='upgrading', verb='upgrade')

    async def uninstall(self, params: PackageParameters) -> Package | None:
        """Asynchronously uninstalls the given package.

        Uses a native async subprocess via `uninstall_command()`.  Output
        is always observed line-by-line; when `params.progress_callback`
        is set, progress events are emitted to the caller.

        Subclasses only need to override this when the generated command
        differs from `uninstall_command()` or when post-uninstall logic
        is required.

        Args:
            params: The package parameters

        Returns:
            The package, or None if the uninstall failed.
        """
        command = self.package_command('uninstall', params)
        return await self._execute_package_command(command, params, phase='uninstalling', verb='uninstall')

    # --- Helpers ----------------------------------------------------------

    def _build_action(self, description: str, package: PackageRef | None = None) -> SetupAction:
        """Build a `SetupAction` populated from this plugin's metadata.

        Uses `plugin_kind()`, `ecosystem()`, and `tool_name()`
        so that callers don't need to repeat these values.
        """
        return SetupAction(
            description=description,
            kind=self.plugin_kind(),
            ecosystem=self.ecosystem(),
            installer=self.tool_name(),
            package=package,
        )

    async def _execute_package_command(
        self,
        command: PackageCommand,
        params: PackageParameters,
        *,
        phase: str,
        verb: PackageVerb,
    ) -> Package | None:
        """Execute or report a command according to its dry-run policy."""
        if not command.execute:
            logger = logging.getLogger(f'porringer.{self.tool_name()}.{verb}')
            logger.info('[dry-run] Would run: %s', ' '.join(command.argv))
            return Package(name=params.package.name, version=None)
        return await self._execute_command(
            args=list(command.argv),
            params=params,
            phase=phase,
            verb=verb,
            apply_dry_run_flags=False,
        )

    async def _execute_command(
        self,
        *,
        args: list[str],
        params: PackageParameters,
        phase: str,
        verb: PackageVerb,
        apply_dry_run_flags: bool = True,
    ) -> Package | None:
        """Run *args* as an async subprocess with line-by-line progress.

        When ``params.progress_callback`` is set, progress events are
        emitted to the caller.  When it is ``None``, output is still
        collected through the same observed command path but no progress events are emitted.

        Args:
            args: Command and arguments to run.
            params: Package parameters.
            phase: Phase label for progress events (e.g. ``"installing"``).
            verb: Human-readable verb for log messages (e.g. ``"install"``).
            apply_dry_run_flags: Whether to append native dry-run flags before
                execution. ``PackageCommand`` callers provide their complete
                command and disable this to avoid adding flags twice.

        Returns:
            The installed/upgraded package, or ``None`` on failure.
        """
        logger = logging.getLogger(f'porringer.{self.tool_name()}.{verb}')
        action = self._build_action(
            description=f'{verb.capitalize()} {params.package.specifier}',
            package=params.package,
        )
        # Apply native dry-run flags when the plugin advertises them.
        if params.dry and apply_dry_run_flags:
            extra = list(self.dry_run_flags(verb))
            if extra:
                args = [*args, *extra]
        # Wrap the user callback so each output line can also be
        # parsed into a structured progress event by the plugin.
        user_callback = params.progress_callback if params.progress_callback is not None else lambda _: None

        def callback(progress: ActionProgress) -> None:
            user_callback(progress)
            if progress.output is not None and progress.channel is not None:
                parsed = self.parse_progress_line(progress.output, progress.channel, action)
                if parsed is not None:
                    user_callback(parsed)

        success = False
        try:
            result = await run_command(
                args,
                progress=CommandProgress(
                    action=action,
                    callback=callback,
                    phase=phase,
                ),
            )
            logger.info(result.stdout)
            success = result.returncode == 0
            if not success:
                logger.error(result.stderr)
        except FileNotFoundError:
            logger.error(f'{self.tool_name()} not found')
        except Exception as e:
            logger.error(f'Failed to {verb} {params.package.name}: {e}')
        finally:
            with contextlib.suppress(Exception):
                await self.post_action(verb, params, success)
        if not success:
            return None
        return Package(name=params.package.name, version=None)

    @abstractmethod
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        """Gathers installed packages in the given environment.

        When *project_path* is provided, plugins that manage
        project-scoped virtual environments (pip, uv) should discover
        the project's venv (e.g. ``<project_path>/.venv``) and list
        packages from that interpreter instead of the global/PATH one.
        Plugins that are inherently global (pipx, apt, brew, winget)
        may ignore this parameter.

        When *runtime_context* is provided, Python-ecosystem plugins
        should target the resolved interpreter instead of
        ``sys.executable``.

        Implementations should use the async helper methods
        (``_run_json_command``, ``_run_text_command``) instead of
        ``subprocess.run`` so the event loop is never blocked.

        Args:
            project_path: Optional path to a project directory.  When
                set, the listing is scoped to the project's virtual
                environment.
            runtime_context: Resolved runtime paths for this execution
                run.  ``None`` means use defaults.

        Returns:
            A list of packages
        """
        raise NotImplementedError

    @abstractmethod
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        """Check for available updates using the plugin's native tooling.

        Every ``Environment`` subclass **must** implement this method.
        Plugins that genuinely cannot check for updates should return
        an empty list.

        Use the shared helpers ``_check_npm_registry()`` or
        ``PythonEnvironment._check_pypi_updates()`` where applicable.

        Implementations should use async I/O (``_run_json_command``,
        ``_run_text_command``, ``aiohttp.ClientSession``) instead of
        blocking calls so the event loop is never blocked.

        Args:
            params: The check parameters including which packages to check.

        Returns:
            A list of packages that have updates available. Each Package
            should have its ``version`` field set to the latest
            available version.
        """
        ...

    @staticmethod
    async def _check_npm_registry(
        packages: list[PackageRef],
        *,
        include_prereleases: bool = False,
        max_concurrency: int = 8,
        logger: logging.Logger | None = None,
        http_client: aiohttp.ClientSession | None = None,
    ) -> list[Package]:
        """Query the npm registry for the latest versions of the given packages.

        Shared helper for plugins that install from the npm registry
        (npm and pnpm).

        Uses ``aiohttp.ClientSession`` so the event loop is never blocked
        by network I/O.  Per-package queries run with bounded concurrency.

        For each package, fetches
        ``https://registry.npmjs.org/{name}`` and extracts:

        * **stable** — ``dist-tags.latest``
        * **pre-release** — the last key in ``versions`` (highest semver)

        Args:
            packages: Package references to look up.
            include_prereleases: When ``True``, return pre-release versions.
            max_concurrency: Maximum number of concurrent registry requests.
                ``0`` means unlimited.
            logger: Optional logger for debug messages. Falls back to
                ``logging.getLogger('porringer.npm_registry')``.
            http_client: Shared ``aiohttp.ClientSession`` for connection pooling.
                When ``None``, a short-lived session is created per call.

        Returns:
            A list of packages with their latest available version.
        """
        if logger is None:
            logger = logging.getLogger('porringer.npm_registry')

        async with (
            contextlib.nullcontext(http_client)
            if http_client is not None
            else aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
        ) as session:

            def _make_check(pkg_ref: PackageRef) -> Callable[[], Awaitable[Package | None]]:
                return lambda: Environment._check_single_npm_package(session, pkg_ref, include_prereleases, logger)

            gathered = await gather_bounded(
                (_make_check(pkg_ref) for pkg_ref in packages),
                limit=max_concurrency,
            )

        return [pkg for pkg in gathered if pkg is not None]

    @staticmethod
    async def _check_single_npm_package(
        session: aiohttp.ClientSession,
        pkg_ref: PackageRef,
        include_prereleases: bool,
        logger: logging.Logger,
    ) -> Package | None:
        """Fetch one package from the npm registry and return the latest version, or ``None``."""
        try:
            async with session.get(f'https://registry.npmjs.org/{pkg_ref.name}') as response:
                response.raise_for_status()
                data = await response.json()
        except (aiohttp.ClientError, ValueError) as exc:
            logger.debug('npm registry query failed for %s: %s', pkg_ref.name, exc)
            return None

        if include_prereleases:
            versions = data.get('versions', {})
            if versions:
                latest = next(reversed(versions))
                return Package(name=pkg_ref.name, version=latest)
            return None

        dist_tags = data.get('dist-tags', {})
        latest = dist_tags.get('latest')
        if latest:
            return Package(name=pkg_ref.name, version=latest)
        return None
