"""CLI command implementation for resolution.

Unified operation resolution for inspection and real execution.

Determines the correct operation (install, upgrade, or skip) for a
given action based on the sync strategy and current system state.
Both the inspection path and the real execution path delegate to
:func:`resolve_operation` so that strategy logic lives in one place.

Also hosts :func:`is_package_installed`, the shared presence-detection
helper used by resolution, inspection, and execution paths.
"""

import asyncio
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import aiohttp
from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from porringer.core.plugin_schema.environment import CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.project_environment import ProjectInstaller
from porringer.core.plugin_schema.python_environment import PythonEnvironment
from porringer.core.plugin_schema.runtime import (
    RuntimeContext,
    RuntimeProvider,
    runtime_tag_matches,
)
from porringer.core.schema import Package, PackageRef, PluginKind
from porringer.schema import (
    Install,
    InstallReason,
    Operation,
    SetupAction,
    SetupActionResult,
    Skip,
    SkipReason,
    SyncStrategy,
    Uninstall,
    Upgrade,
)
from porringer.utility.exception import PluginError
from porringer.utility.trace import CommandTrace

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedOperation:
    """Result of resolving what operation an action requires.

    Produced by :func:`resolve_operation` and consumed by both the
    inspection reporter and the real execution engine.
    """

    action: SetupAction
    operation: Operation
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PackageCacheStats:
    """Counters for package presence cache behavior."""

    package_hits: int = 0
    package_misses: int = 0
    package_invalidations: int = 0


def resolved_to_result(resolved: ResolvedOperation) -> SetupActionResult:
    """Map a :class:`ResolvedOperation` to a :class:`SetupActionResult`.

    This is the single mapping used by both the inspection and real
    execution paths so that the translation lives in one place.

    For :class:`Skip` operations the result carries version metadata
    and the skip reason.  For :class:`Upgrade` and :class:`Install`
    operations the result carries version metadata when available.
    """
    match resolved.operation:
        case Skip(reason=reason, installed_version=iv, available_version=av):
            logger.debug('resolved to skip: reason=%s message=%s', reason, resolved.message)
            return SetupActionResult(
                action=resolved.action,
                success=True,
                skipped=True,
                skip_reason=reason,
                message=resolved.message,
                installed_version=iv,
                available_version=av,
            )
        case Install(installed_version=iv, available_version=av) | Upgrade(installed_version=iv, available_version=av):
            return SetupActionResult(
                action=resolved.action,
                success=True,
                message=resolved.message,
                installed_version=iv,
                available_version=av,
            )
        case Uninstall(installed_version=iv):
            return SetupActionResult(
                action=resolved.action,
                success=True,
                message=resolved.message,
                installed_version=iv,
            )

    # Operation is a closed union — this is unreachable.
    raise AssertionError(f'unhandled operation type: {type(resolved.operation)}')


class PackageCache:
    """Per-phase cache for ``packages()`` results.

    Ensures each environment plugin's ``packages()`` method is called
    at most once per unique key, eliminating redundant subprocess or
    filesystem queries when many actions share the same installer.

    Thread-safe via per-key `asyncio.Lock` instances so concurrent
    ``TaskGroup`` tasks that hit the same installer serialise on
    the first query and share its result.
    """

    def __init__(self) -> None:
        """Initialise empty caches and lock registry."""
        self._packages: dict[str, list[Package]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._package_hits = 0
        self._package_misses = 0
        self._package_invalidations = 0

    def stats(self) -> PackageCacheStats:
        """Return a snapshot of package cache counters."""
        return PackageCacheStats(
            package_hits=self._package_hits,
            package_misses=self._package_misses,
            package_invalidations=self._package_invalidations,
        )

    def log_debug_stats(self, label: str) -> None:
        """Emit cache counters to the debug log when enabled."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        stats = self.stats()
        logger.debug(
            '%s PackageCache stats: package_hits=%d package_misses=%d package_invalidations=%d',
            label,
            stats.package_hits,
            stats.package_misses,
            stats.package_invalidations,
        )

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Return (creating if needed) the lock for *key*."""
        return self._locks.setdefault(key, asyncio.Lock())

    async def get_packages(
        self,
        installer: str,
        environment: Environment,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        """Return cached ``packages()`` result, querying on first access.

        The cache key is ``(installer, project_path, runtime executables)``
        so project-scoped and runtime-tagged queries are cached separately.

        Args:
            installer: The installer/plugin name.
            environment: The environment plugin instance.
            project_path: Optional project directory scope.
            runtime_context: Resolved runtime paths for this execution run.

        Returns:
            The list of installed packages.
        """
        runtimes = sorted(runtime_context.executables.items()) if runtime_context else []
        key = f'pkg:{installer}:{project_path}:{runtimes}'
        async with self._lock_for(key):
            if key in self._packages:
                self._package_hits += 1
                return self._packages[key]

            self._package_misses += 1
            self._packages[key] = await environment.packages(project_path=project_path, runtime_context=runtime_context)
            return self._packages[key]

    def invalidate_packages(self, installer: str, project_path: Path | None = None) -> None:
        """Remove cached packages for an installer so next access re-queries.

        Call this after a successful install/upgrade so subsequent
        presence checks see fresh state.  Entries for every runtime are
        dropped.

        Args:
            installer: The installer/plugin name.
            project_path: Optional project directory scope.
        """
        prefix = f'pkg:{installer}:{project_path}:'
        for key in [key for key in self._packages if key.startswith(prefix)]:
            del self._packages[key]
            self._package_invalidations += 1

    def invalidate_all(self) -> None:
        """Clear all cached data.

        Locks are intentionally retained — clearing them while a
        concurrent coroutine holds one would be unsafe.
        """
        self._package_invalidations += len(self._packages)
        self._packages.clear()


@dataclass(slots=True)
class ResolutionContext:
    """Optional context for :func:`resolve_operation`.

    Groups the keyword-only parameters so the public API stays concise.
    """

    project_path: Path | None = None
    """Project directory for scoped package queries."""
    project_environments: dict[str, ProjectInstaller] | None = None
    """Dict of project-environment plugins, used for project-install
    resolution."""
    http_client: aiohttp.ClientSession | None = None
    """Shared ``aiohttp.ClientSession`` for connection pooling across
    concurrent update checks.  ``None`` means each check creates
    its own short-lived session."""
    package_cache: PackageCache | None = None
    """Optional shared cache for ``packages()`` results.  When set,
    multiple actions using the same installer share a single
    ``packages()`` call instead of querying independently."""
    runtime_context: RuntimeContext | None = None
    """Resolved runtime paths.  Threaded through to ``packages()``
    so Python-ecosystem plugins can query packages from the correct
    interpreter."""


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


async def apply_runtime_tag(
    action: SetupAction,
    environments: dict[str, Environment],
    ctx: ResolutionContext,
) -> ResolutionContext | SetupActionResult:
    """Apply *action*'s runtime-tag override, or return a failure result."""
    if action.runtime_tag is None:
        return ctx
    override_ctx = await resolve_runtime_tag_override(action.runtime_tag, action.ecosystem, environments)
    if override_ctx is None:
        return SetupActionResult(
            action=action,
            success=False,
            message=f"Could not resolve runtime tag '{action.runtime_tag}' for ecosystem '{action.ecosystem}'",
        )
    return replace(ctx, runtime_context=override_ctx)


async def _query_installed_packages(installer: str, environment: Environment, ctx: ResolutionContext) -> list[Package]:
    """Return installed packages for *installer*, via cache when available."""
    if ctx.package_cache is not None:
        return await ctx.package_cache.get_packages(installer, environment, ctx.project_path, ctx.runtime_context)
    return await environment.packages(project_path=ctx.project_path, runtime_context=ctx.runtime_context)


async def resolve_operation(
    action: SetupAction,
    environments: dict[str, Environment],
    strategy: SyncStrategy,
    context: ResolutionContext | None = None,
) -> ResolvedOperation:
    """Determine the correct operation for an action.

    Queries the system for installed packages/plugins, applies the
    strategy, and optionally checks for upstream updates.  The
    returned :class:`ResolvedOperation` tells the caller whether to
    install, upgrade, or skip — and carries version metadata for
    display purposes.

    This function is intentionally **side-effect-free**: it reads
    system state but does not modify it.

    Args:
        action: The action to resolve.
        environments: Dict of instantiated environment plugins.
        strategy: The sync strategy.
        context: Optional resolution context with project path,
            project environments, and update-detection flag.

    Returns:
        A fully resolved operation descriptor.
    """
    ctx = context or ResolutionContext()

    if action.installer is None or action.package is None:
        return ResolvedOperation(
            action=action,
            operation=Skip(),
            message='Installer or package not specified',
        )

    return await _resolve_package_operation(action, environments, strategy, ctx)


async def resolve_uninstall_operation(
    action: SetupAction,
    environment: Environment,
    context: ResolutionContext | None = None,
) -> ResolvedOperation:
    """Resolve whether *action*'s package is installed and can be removed.

    Presence is matched by name only; the manifest's version constraint
    does not decide whether a package gets removed.

    Returns:
        :class:`Uninstall` when present, otherwise :class:`Skip` with
        ``NOT_INSTALLED``.
    """
    assert action.installer is not None
    assert action.package is not None
    ctx = context or ResolutionContext()

    installed_packages = await _query_installed_packages(action.installer, environment, ctx)
    present, _detail, matched = is_package_installed(
        PackageRef(name=action.package.name),
        installed_packages,
        type(environment).package_name_validator(),
        action.kind,
    )
    if not present:
        if action.kind is PluginKind.RUNTIME and isinstance(environment, RuntimeProvider):
            tags = await environment.available_tags()
            if any(runtime_tag_matches(tag, action.package.name) for tag in tags):
                return ResolvedOperation(
                    action=action,
                    operation=Skip(reason=SkipReason.NOT_MANAGED),
                    message=f"'{action.package.name}' was installed by something other than {action.installer}",
                )
        return ResolvedOperation(
            action=action,
            operation=Skip(reason=SkipReason.NOT_INSTALLED),
            message=f"'{action.package.name}' is not installed",
        )
    return ResolvedOperation(
        action=action,
        operation=Uninstall(installed_version=matched.version if matched is not None else None),
    )


async def _resolve_package_operation(
    action: SetupAction,
    environments: dict[str, Environment],
    strategy: SyncStrategy,
    ctx: ResolutionContext,
) -> ResolvedOperation:
    """Resolve the operation for a normal package action."""
    assert action.installer is not None
    assert action.package is not None

    if action.installer not in environments:
        return ResolvedOperation(
            action=action,
            operation=Install(),
            message=f"Installer '{action.installer}' is not available",
        )

    environment = environments[action.installer]
    validator = type(environment).package_name_validator()

    introspection_python: str | None = None
    if isinstance(environment, PythonEnvironment):
        # For environments that install each package into its own isolated
        # venv (e.g. pipx), prefer the package-specific interpreter so
        # that extras introspection queries the correct environment.
        # Falls back to the environment's shared python_command when the
        # package-specific lookup returns None.
        pkg_python = environment.package_python(action.package.name) if action.package else None
        introspection_python = pkg_python if pkg_python is not None else environment.python_command(ctx.runtime_context)

    presence = _PresenceResult(env_for_updates=environment, introspection_python=introspection_python)
    try:
        installed_packages = await _query_installed_packages(action.installer, environment, ctx)
        presence.is_installed, presence.detail, presence.matched = is_package_installed(
            action.package, installed_packages, validator, action.kind
        )
        presence.installed_names = frozenset(canonicalize_name(p.name) for p in installed_packages)
    except PluginError as e:
        logger.debug('Plugin error checking packages for %s: %s', action.installer, e)
    except Exception as e:
        logger.debug('Could not check installed packages for %s: %s', action.installer, e)

    # Secondary detection: if the package was not found via the
    # environment's package list and we are running in a frozen
    # (PyInstaller) context, check whether it exists as a CLI
    # executable on PATH.  This catches cross-environment installs
    # (e.g. pipx installed in a different Python's site-packages)
    # where pip list fails entirely because sys.executable is not
    # a Python interpreter.
    #
    # Only applied in frozen apps to avoid masking genuine "not
    # installed" results during normal development / CI.
    if (
        not presence.is_installed
        and getattr(sys, 'frozen', False)
        and action.kind in {PluginKind.PACKAGE, PluginKind.TOOL}
        and action.package is not None
        and shutil.which(action.package.name) is not None
    ):
        logger.debug(
            'Package %s not found via %s but available on PATH; treating as installed',
            action.package.name,
            action.installer,
        )
        presence.is_installed = True
        presence.detail = 'found on PATH'
        version = await probe_tool_version(action.package.name)
        presence.matched = Package(name=action.package.name, version=version)
        if version is None:
            # Cannot compare versions — suppress update check to
            # avoid false UPDATE_AVAILABLE results.
            presence.env_for_updates = None

    return await _apply_strategy(
        action=action,
        strategy=strategy,
        presence=presence,
        http_client=ctx.http_client,
        runtime_context=ctx.runtime_context,
    )


_VERSION_PATTERN = re.compile(r'v?(\d+\.\d+(?:\.\d+)*)')


async def _capture_subprocess(args: list[str], *, timeout_seconds: int) -> tuple[int | None, bytes, bytes] | None:
    """Run *args*, capturing stdout/stderr under a command trace.

    Returns ``(returncode, stdout, stderr)`` on completion, or ``None``
    when the subprocess cannot be started or times out.
    """
    trace = CommandTrace.start(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        trace.finish(returncode=proc.returncode, stdout=stdout_bytes, stderr=stderr_bytes)
        return proc.returncode, stdout_bytes or b'', stderr_bytes or b''
    except (FileNotFoundError, OSError, TimeoutError) as exc:
        trace.finish(returncode=None, error=f'{type(exc).__name__}: {exc}')
        return None


async def probe_tool_version(name: str) -> str | None:
    """Run ``<name> --version`` and extract a version string.

    Uses the same regex pattern as
    ``ToolBasedPlugin.tool_version()`` so that version output is
    parsed consistently.

    Returns the version string on success, or ``None`` when the
    subprocess fails, times out, or the output cannot be parsed.
    """
    result = await _capture_subprocess([name, '--version'], timeout_seconds=5)
    if result is None:
        return None
    _, stdout_bytes, stderr_bytes = result
    output = stdout_bytes.decode('utf-8', errors='replace') + stderr_bytes.decode('utf-8', errors='replace')
    match = _VERSION_PATTERN.search(output)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Extras introspection
# ---------------------------------------------------------------------------

_REQUIRES_SCRIPT = (
    'import importlib.metadata, json, sys; '
    'd = importlib.metadata.distribution(sys.argv[1]); '
    'json.dump(d.requires or [], sys.stdout)'
)
"""Subprocess one-liner (stdlib only) that emits a package's
``Requires-Dist`` entries as a JSON list of strings."""


async def _run_metadata_script(
    python: str,
    script: str,
    package_name: str,
    *,
    timeout_seconds: int = 10,
) -> bytes | None:
    """Run a metadata-introspection one-liner in *python* and return stdout.

    Returns raw stdout bytes on success, or ``None`` when the
    subprocess fails, times out, or cannot be started.
    """
    result = await _capture_subprocess([python, '-c', script, package_name], timeout_seconds=timeout_seconds)
    if result is None:
        return None
    returncode, stdout_bytes, _ = result
    if returncode != 0:
        return None
    return stdout_bytes


async def fetch_package_requires(
    python: str,
    package_name: str,
) -> list[str] | None:
    """Fetch the ``Requires-Dist`` metadata for *package_name*.

    Runs a subprocess in the target *python* interpreter using only
    the standard library.  Returns the raw requirement strings on
    success or ``None`` when introspection fails for any reason.
    """
    raw = await _run_metadata_script(python, _REQUIRES_SCRIPT, package_name)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def extras_satisfied(
    requires: list[str],
    requested_extras: tuple[str, ...],
    installed_names: frozenset[str],
) -> bool:
    """Check whether all dependencies for *requested_extras* are present.

    For each ``Requires-Dist`` entry whose environment marker matches
    one of the *requested_extras*, verify that the dependency's
    canonicalised name exists in *installed_names*.

    Returns ``True`` when every conditional dependency for the
    requested extras is already installed.
    """
    if not requested_extras:
        return True

    base_env = cast(dict[str, str], default_environment())

    for extra in requested_extras:
        eval_env = {**base_env, 'extra': extra}
        for raw in requires:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if req.marker is None:
                continue  # unconditional dep — always present
            if not req.marker.evaluate(eval_env):
                continue  # not relevant for this extra
            if canonicalize_name(req.name) not in installed_names:
                return False
    return True


async def check_extras_installed(
    python: str,
    package_name: str,
    extras: tuple[str, ...],
    installed_names: frozenset[str],
) -> bool | None:
    """Determine whether a package's extras dependencies are satisfied.

    Combines :func:`fetch_package_requires` (subprocess in target
    env) with :func:`extras_satisfied` (marker evaluation in host
    process).

    Returns ``True`` / ``False`` when introspection succeeds, or
    ``None`` when it cannot be determined (subprocess failure, etc.).
    """
    raw_requires = await fetch_package_requires(python, package_name)
    if raw_requires is None:
        return None
    return extras_satisfied(raw_requires, extras, installed_names)


async def _extras_need_install(
    action: SetupAction,
    presence: _PresenceResult,
) -> bool:
    """Return ``True`` when the action requests extras that are not satisfied.

    Extras are a PEP 508 concept — only Python environments can be
    introspected via ``importlib.metadata``.  When
    ``presence.introspection_python`` is ``None`` the installer is not
    Python-based, so extras don't apply — return ``False``.

    ``installed_names`` is already populated on ``presence`` so only
    the ``Requires-Dist`` entries need fetching.

    When introspection fails the result is conservatively ``True``
    (re-install to ensure the extras are present).
    """
    if not action.package or not action.package.extras:
        return False

    if presence.introspection_python is None:
        # Non-Python installer → not applicable
        return False

    result = await check_extras_installed(
        presence.introspection_python,
        action.package.name,
        action.package.extras,
        presence.installed_names,
    )
    return result is not True  # False or None (failure) → need install


@dataclass(slots=True)
class _PresenceResult:
    """Result of querying whether a package/plugin is installed."""

    is_installed: bool = False
    detail: str | None = None
    matched: Package | None = None
    env_for_updates: Environment | None = None
    """Environment plugin to use for upstream update checks."""
    installed_names: frozenset[str] = frozenset()
    """Canonicalised names of all installed packages (for extras checks)."""
    introspection_python: str | None = None
    """Python interpreter to use for ``importlib.metadata`` introspection.

    This is the installer's Python (from
    ``PythonEnvironment.python_command``).  ``None`` means extras
    introspection is not available."""


async def _resolve_latest_installed(
    *,
    action: SetupAction,
    presence: _PresenceResult,
    installed_ver: str | None,
    http_client: aiohttp.ClientSession | None,
    runtime_context: RuntimeContext | None,
) -> ResolvedOperation:
    """Resolve a LATEST operation for an already-installed package.

    Queries for a newer version when possible.  Falls back to an
    unconditional upgrade when no update-check environment is available.
    """
    if presence.env_for_updates is not None:
        try:
            newer = await check_for_newer_version(
                presence.env_for_updates,
                action.package,
                installed_ver,
                include_prereleases=action.include_prereleases,
                http_client=http_client,
                runtime_context=runtime_context,
            )
        except UpdateCheckError:
            pass  # Fall through to unconditional upgrade
        else:
            if newer is not None:
                pkg_name = action.package.name if action.package else ''
                return ResolvedOperation(
                    action=action,
                    operation=Upgrade(
                        installed_version=installed_ver,
                        available_version=newer,
                    ),
                    message=f'{pkg_name} {installed_ver} → {newer}',
                )
            # Version is latest — check whether extras still need ensuring.
            if await _extras_need_install(action, presence):
                return ResolvedOperation(
                    action=action,
                    operation=Install(
                        reason=InstallReason.ENSURE_EXTRAS,
                        installed_version=installed_ver,
                    ),
                    message='ensuring extras',
                )
            return ResolvedOperation(
                action=action,
                operation=Skip(
                    reason=SkipReason.ALREADY_LATEST,
                    installed_version=installed_ver,
                ),
                message=presence.detail,
            )

    # No environment for update checks, or check failed — upgrade
    # unconditionally.
    return ResolvedOperation(
        action=action,
        operation=Upgrade(),
        message=presence.detail,
    )


async def _apply_strategy(
    *,
    action: SetupAction,
    strategy: SyncStrategy,
    presence: _PresenceResult,
    http_client: aiohttp.ClientSession | None = None,
    runtime_context: RuntimeContext | None = None,
) -> ResolvedOperation:
    """Apply the sync strategy to determine the operation.

    This is the single source of truth for the install/upgrade/skip
    decision.

    When an already-installed package is skipped under MINIMAL
    strategy, an upstream version check is always performed so that
    callers receive populated version metadata.
    """
    installed_ver = presence.matched.version if presence.matched else None
    assert action.package is not None

    if strategy == SyncStrategy.MINIMAL:
        if presence.is_installed:
            # Extras cannot be introspected from the installed-packages
            # list alone.  Query the target environment's metadata to
            # determine whether the requested extras' conditional deps
            # are satisfied.  When they are not (or introspection
            # fails) re-run the install so the underlying tool ensures
            # them.
            if await _extras_need_install(action, presence):
                return ResolvedOperation(
                    action=action,
                    operation=Install(
                        reason=InstallReason.ENSURE_EXTRAS,
                        installed_version=installed_ver,
                    ),
                    message='ensuring extras',
                )

            # Always check for updates so version metadata is populated
            skip_reason = SkipReason.ALREADY_INSTALLED
            available_ver: str | None = None
            msg: str | None = presence.detail

            if presence.env_for_updates is not None:
                try:
                    newer = await check_for_newer_version(
                        presence.env_for_updates,
                        action.package,
                        installed_ver,
                        include_prereleases=action.include_prereleases,
                        http_client=http_client,
                        runtime_context=runtime_context,
                    )
                except UpdateCheckError:
                    newer = None

                if newer is not None:
                    skip_reason = SkipReason.UPDATE_AVAILABLE
                    available_ver = newer
                    pkg_name = action.package.name if action.package else ''
                    msg = f'{pkg_name} {installed_ver} → {available_ver}'

            return ResolvedOperation(
                action=action,
                operation=Skip(
                    reason=skip_reason,
                    installed_version=installed_ver,
                    available_version=available_ver,
                ),
                message=msg,
            )
        # Not installed → install
        return ResolvedOperation(
            action=action,
            operation=Install(
                available_version=action.package.constraint,
            ),
        )

    # LATEST strategy
    if presence.is_installed:
        return await _resolve_latest_installed(
            action=action,
            presence=presence,
            installed_ver=installed_ver,
            http_client=http_client,
            runtime_context=runtime_context,
        )

    # Not installed under LATEST → fall back to install
    return ResolvedOperation(
        action=action,
        operation=Install(
            available_version=action.package.constraint,
        ),
        message='not installed, will install instead',
    )


class UpdateCheckError(Exception):
    """The update check could not be performed.

    The caller should fall back to a conservative action
    (e.g. attempt the upgrade anyway).
    """


async def check_for_newer_version(
    env: Environment,
    package: PackageRef | None,
    installed_version: str | None,
    *,
    include_prereleases: bool = False,
    http_client: aiohttp.ClientSession | None = None,
    runtime_context: RuntimeContext | None = None,
) -> str | None:
    """Query the plugin for a newer upstream version.

    Uses the plugin's ``check_updates()`` coroutine.  When
    *http_client* is provided it is forwarded via
    ``CheckUpdatesParameters`` so that concurrent checks share a
    single connection pool.

    Returns the newer version string when one is available, or ``None``
    when the installed version is confirmed up-to-date.

    Raises:
        UpdateCheckError: When the check cannot complete (network
            error, missing package, etc.).
    """
    if package is None:
        raise UpdateCheckError('no package reference')

    try:
        params = CheckUpdatesParameters(
            packages=[package],
            include_prereleases=include_prereleases,
            http_client=http_client,
            runtime_context=runtime_context,
        )
        updates = await env.check_updates(params)
    except Exception as e:
        logger.debug('check_updates failed for %s via %s: %s', package, env.tool_name(), e)
        raise UpdateCheckError(str(e)) from e

    if not updates or updates[0].version is None:
        # Plugin responded but reported no updates — confirmed up-to-date.
        return None

    latest_ver = updates[0].version

    # Defense-in-depth: reject pre-release versions when the caller
    # did not opt in, even if the plugin failed to filter them.
    if not include_prereleases:
        try:
            if Version(latest_ver).is_prerelease:
                return None
        except InvalidVersion:
            pass  # Non-PEP-440 version string — let the comparison below decide

    # Compare versions when possible to avoid false positives
    if installed_version is not None:
        try:
            if Version(latest_ver) <= Version(installed_version):
                return None
        except InvalidVersion:
            if latest_ver == installed_version:
                return None

    return latest_ver


# ---------------------------------------------------------------------------
# Presence detection
# ---------------------------------------------------------------------------


def is_package_installed(
    package: PackageRef,
    installed_packages: list[Package],
    name_validator: str | None = None,
    kind: PluginKind | None = None,
) -> tuple[bool, str | None, Package | None]:
    """Checks if a package is already installed with a compatible version.

    When *name_validator* is ``'pep440'``, uses PEP 440 canonicalization
    and specifier matching.  Otherwise, uses case-insensitive name
    comparison and simple string version equality.

    For ``RUNTIME`` actions, name comparison uses prefix matching so
    that a request for ``3.14`` matches an installed ``3.14-64``
    (architecture-qualified tag).

    Args:
        package: The package reference
        installed_packages: List of installed packages from the environment
        name_validator: The validator tag declared by the resolved plugin
        kind: The plugin kind (enables prefix matching for RUNTIME)

    Returns:
        Tuple of (is_installed, detail_message, matched_package).
        ``matched_package`` is the ``Package`` object that matched,
        or ``None`` when the package is not installed.
    """
    is_pep440 = name_validator == 'pep440'
    is_runtime = kind == PluginKind.RUNTIME

    for installed in installed_packages:
        if is_runtime:
            # Runtime tags use prefix matching: "3.14" matches "3.14-64"
            if not installed.name.lower().startswith(package.name.lower()):
                continue
        elif is_pep440:
            if canonicalize_name(installed.name) != canonicalize_name(package.name):
                continue
        elif installed.name.lower() != package.name.lower():
            continue

        # Name matched
        if not package.constraint:
            return (
                True,
                f'{installed.name}=={installed.version} already installed',
                installed,
            )

        if installed.version is not None:
            if is_pep440:
                try:
                    req = Requirement(str(package))
                    if Version(installed.version) in req.specifier:
                        return (
                            True,
                            f'{installed.name}=={installed.version} satisfies {package}',
                            installed,
                        )
                except InvalidVersion, InvalidRequirement:
                    pass
            else:
                # Non-PEP-440: installed means installed; constraint
                # satisfaction is left to the underlying tool.
                return (
                    True,
                    f'{installed.name}=={installed.version} already installed',
                    installed,
                )

    return False, None, None
