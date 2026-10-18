"""Plugin integration for plugin.

Plugin implementation for Python Install Manager (pymanager).
"""

import asyncio
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, override

from packaging.version import InvalidVersion, Version

from porringer.core.plugin_schema.environment import CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.runtime import RuntimeContext, RuntimeProvider
from porringer.core.schema import (
    Ecosystem,
    Package,
    PackageRef,
    PluginDependency,
    PluginKind,
)

# Architecture suffix pattern: a trailing dash followed by digits (e.g. "-64", "-32").
_ARCH_SUFFIX = re.compile(r'-\d+$')
_DEFAULT_EXECUTABLE_PROBE_LINE_COUNT = 2

logger = logging.getLogger(__name__)


async def _run_py(args: Sequence[str], *, timeout_seconds: float) -> tuple[int, str, str]:
    """Run the ``py`` launcher with ``args`` and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        'py',
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    stdout = stdout_bytes.decode('utf-8', errors='replace') if stdout_bytes else ''
    stderr = stderr_bytes.decode('utf-8', errors='replace') if stderr_bytes else ''
    return proc.returncode or 0, stdout, stderr


async def _configure_pim_shortcuts() -> None:
    r"""Register PIM's per-user shortcuts by running ``py install --configure --yes`` once.

    The Python Install Manager creates shims such as ``python.exe``, ``pip.exe``,
    ``pipx.exe``, and ``python3.14.exe`` in ``%LOCALAPPDATA%\Python\bin`` and adds that
    directory to the user PATH. Without ``py install --configure``, a freshly installed
    runtime is reachable only through ``py -<tag>``. That breaks downstream tools such as
    pip and pipx, which expect the canonical names on PATH.

    A fast existence check gates the call, so the subprocess runs only the first time a
    runtime is installed on a machine. ``--yes`` accepts the long-path-support and
    PATH-registration prompts without interaction.

    Kept separate from :meth:`PIMEnvironment.setup`: inlining it under that method's
    ``sys.platform`` guard makes the body unreachable off Windows, where type checkers
    then stop narrowing types and report false positives.
    """
    local_app_data = os.environ.get('LOCALAPPDATA')
    if not local_app_data:
        logger.debug('LOCALAPPDATA not set; skipping PIM --configure')
        return

    bin_dir = Path(local_app_data) / 'Python' / 'bin'
    if (bin_dir / 'python.exe').exists():
        logger.debug('PIM bin directory already populated at %s; skipping --configure', bin_dir)
        return

    configure_logger = logging.getLogger('porringer.pim.configure')
    configure_logger.info('Running py install --configure --yes to register PIM shortcuts')
    try:
        returncode, stdout, stderr = await _run_py(
            ['install', '--configure', '--yes'],
            timeout_seconds=120,
        )
    except FileNotFoundError:
        configure_logger.debug('py launcher not found; cannot run --configure')
        return
    except (TimeoutError, OSError) as exc:
        configure_logger.warning('py install --configure --yes failed: %s', exc)
        return

    if returncode != 0:
        configure_logger.warning(
            'py install --configure --yes failed (rc=%s): %s',
            returncode,
            stderr.strip(),
        )
        return
    stdout = stdout.strip()
    if stdout:
        configure_logger.debug('py install --configure --yes output: %s', stdout)


class PIMEnvironment(Environment, RuntimeProvider):
    """Represents a Python runtime environment managed by Python Install Manager (pymanager).

    Provides methods to install, search, uninstall, upgrade, and list Python runtimes using
    the official Python Install Manager (py/pymanager) as the backend.

    This plugin is Windows-only and requires winget to be available for installing
    the Python Install Manager itself.

    This plugin provides the "python-runtime" capability, which pip and pipx can use
    to manage Python versions.

    CLI Reference:
        - py list [-f=<FMT>] [--online] [<TAG>...]  - List installed/available runtimes
        - py install [-f|--force] [-u|--update] [--dry-run] [<TAG>...]  - Install runtimes
        - py uninstall [-y|--yes] <TAG>...  - Uninstall runtimes
        - pymanager install 9NQ7512CXL7T  - Install via winget (Store app ID)
    """

    _default_executable_cache: ClassVar[Path | None] = None

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        """PIM belongs to the `python` ecosystem."""
        return Ecosystem('python')

    @staticmethod
    @override
    def plugin_kind() -> PluginKind:
        """PIM manages language runtimes."""
        return PluginKind.RUNTIME

    @staticmethod
    @override
    def is_supported() -> bool:
        """Supported on Windows only."""
        return sys.platform == 'win32'

    @staticmethod
    @override
    def package_name_validator() -> str:
        """Python runtimes use PEP 440 validation."""
        return 'pep440'

    @classmethod
    @override
    def provided_runtime_kind(cls) -> str:
        """PIM provides Python runtimes."""
        return 'python'

    @classmethod
    @override
    def tool_name(cls) -> str:
        """PIM wraps the `py` CLI."""
        return 'py'

    @classmethod
    def invalidate_runtime_cache(cls) -> None:
        """Clear process-local runtime resolution caches."""
        cls._default_executable_cache = None

    @staticmethod
    @override
    def dependencies() -> list[PluginDependency]:
        """Declares plugin dependencies.

        This plugin requires Windows and depends on winget when on Windows
        for installing the Python Install Manager.

        Returns:
            A list of plugin dependencies
        """
        return [
            PluginDependency(
                plugin='winget',
                required=True,
                platforms=['win32'],
            ),
        ]

    async def default_executable(self) -> Path | None:
        """Return the launcher's default Python executable in one subprocess.

        This is the fast path used by ``Builder.resolve_runtime_context``.
        It avoids running ``py`` once to discover the default tag and then
        again to resolve that tag to ``sys.executable``.
        """
        cached = type(self)._default_executable_cache
        if cached is not None:
            if cached.exists():
                return cached
            type(self)._default_executable_cache = None

        logger = logging.getLogger('porringer.pim.default_executable')
        try:
            returncode, stdout, stderr = await _run_py(
                [
                    '-c',
                    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}"); print(sys.executable)',
                ],
                timeout_seconds=30,
            )
        except Exception as e:
            message = 'py launcher not found' if isinstance(e, FileNotFoundError) else f'default_executable failed: {e}'
            logger.debug(message)
            return None

        if returncode != 0:
            logger.debug('py (default executable) failed: %s', stderr.strip())
            return None

        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) < _DEFAULT_EXECUTABLE_PROBE_LINE_COUNT:
            logger.debug('py default executable probe returned unexpected output: %r', stdout)
            return None

        tag, executable = lines[0], Path(lines[-1])
        if not await asyncio.to_thread(executable.exists):
            logger.warning('py default resolved to %s but it does not exist', executable)
            return None
        type(self)._default_executable_cache = executable
        logger.debug('Resolved default Python %s to %s', tag, executable)
        return executable

    @override
    async def default_tag(self) -> str | None:
        """Return the tag of the launcher's default Python runtime.

        Runs ``py -c "..."`` **without** a version flag so the
        launcher dispatches to its configured default interpreter.
        The major.minor version of that interpreter is returned as
        the tag (e.g. ``"3.14"``).

        Returns:
            A major.minor version tag, or ``None`` if the default
            cannot be determined.
        """
        logger = logging.getLogger('porringer.pim.default_tag')
        try:
            returncode, stdout, stderr = await _run_py(
                [
                    '-c',
                    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
                ],
                timeout_seconds=30,
            )
        except FileNotFoundError:
            logger.debug('py launcher not found')
            return None
        except Exception as e:
            logger.debug('default_tag failed: %s', e)
            return None

        if returncode != 0:
            logger.debug('py (default) failed: %s', stderr.strip())
            return None
        stdout = stdout.strip()
        if stdout:
            return stdout
        return None

    @override
    async def resolve_executable(self, tag: str) -> Path | None:
        """Return the path to the Python interpreter for a managed runtime.

        Uses `py -<tag> -c "import sys; print(sys.executable)"` to ask
        the Python Install Manager where the given runtime lives.

        Args:
            tag: The Python version tag (e.g. `"3.14"`, `"3.12"`).

        Returns:
            Absolute path to the interpreter, or `None` if not installed.
        """
        logger = logging.getLogger('porringer.pim.resolve_executable')
        try:
            returncode, stdout, stderr = await _run_py(
                [f'-{tag}', '-c', 'import sys; print(sys.executable)'],
                timeout_seconds=30,
            )
        except FileNotFoundError:
            logger.debug('py launcher not found')
            return None
        except Exception as e:
            logger.debug('resolve_executable failed for tag %s: %s', tag, e)
            return None

        if returncode != 0:
            logger.debug('py -%s failed: %s', tag, stderr.strip())
            return None
        path = Path(stdout.strip())
        if await asyncio.to_thread(path.exists):
            return path
        logger.warning('py -%s resolved to %s but it does not exist', tag, path)
        return None

    @override
    async def setup(self) -> None:
        """Register PIM's shortcuts on Windows, if not already configured.

        Do nothing on non-Windows platforms. PIM supports Windows only.
        :func:`_configure_pim_shortcuts` holds the configuration logic.
        """
        if sys.platform != 'win32':
            return
        await _configure_pim_shortcuts()

    @override
    def install_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command to install a Python runtime via pymanager."""
        return ['py', 'install', package.name]

    @override
    def upgrade_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command to upgrade a Python runtime via pymanager."""
        return ['py', 'install', '--update', package.name]

    @override
    def uninstall_command(self, package: PackageRef, *, runtime_context: RuntimeContext | None = None) -> list[str]:
        """Returns the CLI command to uninstall one Python runtime via pymanager.

        ``-y`` skips confirmation. Never ``--purge``: pymanager rejects it
        with a tag, and without one it removes every runtime.
        """
        return ['py', 'uninstall', '-y', package.name]

    @override
    def sort_tags(self, tags: list[str]) -> list[str]:
        """Filter and sort PIM tags, handling architecture suffixes.

        The ``py`` launcher emits tags like ``"3.14-64"`` or
        ``"3.12-32"`` where the trailing ``-<arch>`` component is an
        architecture discriminator, not part of the :pep:`440` version.
        This override strips that suffix for version parsing while
        preserving the **full** tag in the returned list so that
        :meth:`resolve_executable` receives the architecture-qualified
        string (e.g. ``py -3.14-64``).

        Tags that remain unparseable after suffix stripping (e.g.
        ``"(venv)"``) are silently dropped.
        """
        parsed: list[tuple[Version, str]] = []
        for tag in tags:
            version_part = _ARCH_SUFFIX.sub('', tag)
            try:
                parsed.append((Version(version_part), tag))
            except InvalidVersion:
                logger.debug('Dropping unparseable tag %r from %s', tag, type(self).__name__)
        parsed.sort(key=lambda pair: pair[0], reverse=True)
        return [tag for _, tag in parsed]

    @override
    async def available_tags(self) -> list[str]:
        """Return all Python tags the ``py`` launcher can resolve.

        Runs ``py list -f json`` **without** ``--only-managed`` so that
        runtimes installed via the official python.org installer,
        Microsoft Store, or any other source are included — not just
        those managed by pymanager.

        The returned list may contain non-version strings (e.g.
        ``"(venv)"``) emitted by the ``py`` launcher.
        :meth:`~RuntimeProvider.sort_tags` is responsible for filtering
        and ordering before the builder attempts resolution.

        Returns:
            A list of raw tag strings as reported by the launcher.
        """
        data = await self._run_json_command(['py', 'list', '-f', 'json'])
        if not isinstance(data, dict):
            return []

        return [runtime.get('tag', '') for runtime in data.get('versions', []) if runtime.get('tag')]

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        """Checks for newer Python runtimes via ``py list --online``.

        Queries the Python Install Manager's online listing and finds
        the highest available version for each requested tag.

        Args:
            params: The check parameters.

        Returns:
            A list of packages with their latest available version.
        """
        data = await self._run_json_command(['py', 'list', '--online', '-f', 'json'])
        if not isinstance(data, dict):
            return []

        results: list[Package] = []
        for pkg_ref in params.packages:
            best_version: str | None = None
            for runtime in data.get('versions', []):
                tag = runtime.get('tag', '')
                version = runtime.get('sort-version') or tag
                if (tag.startswith(pkg_ref.name) or pkg_ref.name == tag) and (
                    best_version is None or version > best_version
                ):
                    best_version = version
            if best_version is not None:
                results.append(Package(name=pkg_ref.name, version=best_version))
        return results

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        """Lists all installed Python runtimes.

        pim manages Python runtimes globally; *project_path* is
        accepted for interface compatibility but has no effect.

        Args:
            project_path: Unused.  pim is inherently global.
            runtime_context: Unused.  pim manages runtimes, not
                per-interpreter packages.

        Returns:
            A list of installed Python runtime packages
        """
        data = await self._run_json_command(['py', 'list', '--only-managed', '-f', 'json'])
        if not isinstance(data, dict):
            return []

        packages: list[Package] = []
        for runtime in data.get('versions', []):
            tag = runtime.get('tag', 'unknown')
            version = runtime.get('sort-version') or tag
            packages.append(Package(name=tag, version=version))
        return packages
