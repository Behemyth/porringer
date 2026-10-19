"""Core helpers and types for python environment.

Intermediate base for Python-ecosystem environment plugins.

Plugins that install Python packages (pip, uv, pipx) share several
boilerplate declarations:

* `ecosystem() -> Ecosystem('python')`
* `package_name_validator() -> 'pep440'`
* `consumed_runtime_kind() -> 'python'`
* `_discover_venv_python()` — locating the interpreter in a project's
  `.venv` directory.
* `_check_pypi_updates()` — querying PyPI for newer package versions.

`PythonEnvironment` bundles these once so that concrete plugins can
focus on their tool-specific behaviour.
"""

import contextlib
import logging
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
from packaging.version import InvalidVersion, Version

from porringer.core.plugin_schema.environment import (
    CheckUpdatesParameters,
    Environment,
    PackageScope,
)
from porringer.core.plugin_schema.runtime import RuntimeConsumer, RuntimeContext
from porringer.core.schema import Ecosystem, Package, PackageRef
from porringer.utility import HTTP_TIMEOUT
from porringer.utility.concurrency import gather_bounded
from porringer.utility.trace import CommandTrace


def _pick_highest_version(
    releases: dict[str, object], *, stable_only: bool
) -> Version | None:
    """Return the highest ``Version`` from *releases* keys, or ``None``.

    Args:
        releases: The ``releases`` dict from PyPI's JSON API.
        stable_only: When ``True``, skip pre-release and dev versions.
    """
    best: Version | None = None
    for ver_str in releases:
        try:
            ver = Version(ver_str)
        except InvalidVersion:
            continue
        if stable_only and (ver.is_prerelease or ver.is_devrelease):
            continue
        if best is None or ver > best:
            best = ver
    return best


def _venv_base_python() -> str | None:
    """Return the interpreter the running virtual environment was created from, or ``None`` outside one."""
    if sys.prefix == sys.base_prefix:
        return None
    base = getattr(sys, "_base_executable", "")
    return base if base and Path(base).is_file() else None


def default_python() -> str:
    """Return the interpreter to target when no runtime override applies.

    Packages and tools belong to the user's interpreter, never to the
    environment Porringer itself runs from. Inside a virtual environment
    (a project ``.venv`` or Porringer's own pipx venv) that is the base
    interpreter the venv was created from; in a frozen binary it is the
    ``python`` on ``PATH``. Otherwise it is the running interpreter.
    """
    if getattr(sys, "frozen", False):
        for candidate in ("python", "python3"):
            found = shutil.which(candidate)
            if found is not None:
                return found
        return sys.executable
    return _venv_base_python() or sys.executable


class PythonEnvironment(Environment, RuntimeConsumer):
    """Base for plugins that install packages into a Python environment.

    Provides default implementations for the common `ecosystem`,
    `package_name_validator`, `consumed_runtime_kind`, and
    `_discover_venv_python` methods shared by pip, uv, and pipx.

    Subclasses must still implement the `Environment` abstract methods
    (`install_command`, `upgrade_command`, `packages`), as well as
    `tool_name()`.
    """

    @staticmethod
    def ecosystem() -> Ecosystem:
        """Python-ecosystem plugins all share the `'python'` ecosystem."""
        return Ecosystem("python")

    @staticmethod
    def package_name_validator() -> str:
        """Python packages use PEP 440 validation."""
        return "pep440"

    @classmethod
    def consumed_runtime_kind(cls) -> str:
        """Python environment plugins consume a Python runtime."""
        return "python"

    @classmethod
    def package_scope(cls) -> PackageScope:
        """Packages live in the interpreter the plugin runs against."""
        return PackageScope.RUNTIME

    @classmethod
    def standalone_binary(cls) -> bool:
        """Whether this plugin wraps a standalone binary rather than a Python module.

        When ``True``, availability is determined solely by
        ``is_available()`` (PATH-based ``shutil.which``).  When
        ``False`` (the default), ``is_available_for()`` probes the
        resolved interpreter via ``python -c "import <tool>"``.

        Subclasses that wrap a non-Python binary (e.g. ``uv``) should
        override this to return ``True``.
        """
        return False

    @classmethod
    def is_available_for(cls, runtime_context: RuntimeContext) -> bool:
        """Check whether this plugin can operate with the given runtime.

        For standalone binaries (``standalone_binary() is True``),
        delegates directly to ``is_available()``.

        For Python modules (the default), determines the target Python
        interpreter from *runtime_context* (falling back to
        ``sys.executable``) and verifies that the plugin's underlying
        tool is importable in that interpreter.

        Args:
            runtime_context: Resolved runtime paths for this execution.

        Returns:
            ``True`` if the plugin can operate in the given context.
        """
        if cls.standalone_binary():
            return cls.is_available()
        tool = cls.tool_name()
        if tool is None:
            return True

        # Determine which interpreter to probe
        exe = runtime_context.get(cls.consumed_runtime_kind())
        python = str(exe) if exe is not None else default_python()

        return cls._probe_module(python, tool)

    @staticmethod
    def _probe_module(python: str, module: str) -> bool:
        """Check whether *module* is importable in the interpreter at *python*.

        Uses a lightweight subprocess invocation with ``-c "import <module>"``
        to avoid loading the module into the current process.

        This is called during deferred resolution (in a thread via
        ``asyncio.to_thread``), so blocking briefly on a subprocess
        is acceptable.

        Args:
            python: Path or command for the Python interpreter.
            module: The module name to check (e.g. ``'pip'``).

        Returns:
            ``True`` if the import succeeds, ``False`` otherwise.
        """
        args = [python, "-c", f"import {module}"]
        trace = CommandTrace.start(args)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                timeout=10,
                check=False,
            )
            trace.finish(
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            trace.finish(returncode=None, error=f"{type(exc).__name__}: {exc}")
            return False

    def python_command(self, runtime_context: RuntimeContext | None = None) -> str:
        """The Python interpreter command to target.

        Returns the runtime override path from *runtime_context* when
        a matching runtime kind is available, otherwise the user's
        default interpreter (see ``default_python``).

        Args:
            runtime_context: Resolved runtime paths for this execution
                run.  ``None`` means use the default interpreter.
        """
        _logger = logging.getLogger("porringer.python_environment")
        kind = self.consumed_runtime_kind()
        if runtime_context is not None:
            exe = runtime_context.get(kind)
            if exe is not None:
                _logger.debug(
                    "python_command: using runtime override %s for kind=%s", exe, kind
                )
                return str(exe)
            _logger.debug(
                "python_command: runtime_context present but no entry for kind=%s", kind
            )
        else:
            _logger.debug(
                "python_command: no runtime_context supplied, falling back to default interpreter"
            )

        resolved = default_python()
        _logger.debug("python_command: using default interpreter %s", resolved)
        return resolved

    @staticmethod
    def package_python(package_name: str) -> str | None:
        """Return the Python interpreter for a specific installed package.

        Environments that install each package into its own isolated
        venv (e.g. pipx) should override this to return the interpreter
        from within *that package's* venv.  The default returns
        ``None``, meaning the caller should fall back to
        :meth:`python_command`.

        Args:
            package_name: The name of the installed CLI tool / package.

        Returns:
            Path to the package-specific Python interpreter, or
            ``None`` when this environment does not use per-package
            venvs.
        """
        return None

    @staticmethod
    def _discover_venv_python(project_path: Path) -> Path | None:
        """Discover the Python interpreter inside a project's virtual environment.

        Looks for a `.venv` directory under *project_path* and returns
        the path to its Python executable if found.

        Args:
            project_path: Root directory of the project.

        Returns:
            Path to the venv Python, or `None` if no venv is found.
        """
        venv_dir = project_path / ".venv"
        if not venv_dir.is_dir():
            return None

        if sys.platform == "win32":
            python = venv_dir / "Scripts" / "python.exe"
        else:
            python = venv_dir / "bin" / "python"

        return python if python.is_file() else None

    async def _check_pypi_updates(
        self, params: CheckUpdatesParameters
    ) -> list[Package]:
        """Query the PyPI JSON API for newer versions of the requested packages.

        For each package in *params.packages*, fetches
        ``https://pypi.org/pypi/{name}/json`` and determines the latest
        available version.  When *params.include_prereleases* is
        ``False`` (default), only the stable ``info.version`` is
        returned.  When ``True``, the highest version across all
        ``releases`` keys is selected (including dev/alpha/beta/rc).

        Uses ``aiohttp.ClientSession`` so the event loop is never blocked
        by network I/O.

        This helper is shared by pip, uv, and pipx plugins.

        Args:
            params: The check parameters including which packages to
                check and whether to include pre-releases.

        Returns:
            A list of packages with their ``version`` set to the latest
            available on PyPI.
        """
        logger = logging.getLogger(f"porringer.{self.tool_name()}.check_pypi")

        async with (
            contextlib.nullcontext(params.http_client)
            if params.http_client is not None
            else aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
        ) as session:

            def _make_check(
                pkg_ref: PackageRef,
            ) -> Callable[[], Awaitable[Package | None]]:
                return lambda: self._check_single_pypi_package(
                    session, pkg_ref, params.include_prereleases, logger
                )

            gathered = await gather_bounded(
                (_make_check(pkg_ref) for pkg_ref in params.packages),
                limit=params.max_concurrency,
            )

        return [pkg for pkg in gathered if pkg is not None]

    @staticmethod
    async def _check_single_pypi_package(
        session: aiohttp.ClientSession,
        pkg_ref: PackageRef,
        include_prereleases: bool,
        logger: logging.Logger,
    ) -> Package | None:
        """Fetch one package from PyPI and return the latest version, or ``None``."""
        try:
            async with session.get(
                f"https://pypi.org/pypi/{pkg_ref.name}/json"
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except (aiohttp.ClientError, ValueError, KeyError) as exc:
            logger.debug("PyPI query failed for %s: %s", pkg_ref.name, exc)
            return None

        if include_prereleases:
            best = _pick_highest_version(data.get("releases", {}), stable_only=False)
            return (
                Package(name=pkg_ref.name, version=str(best))
                if best is not None
                else None
            )

        # Stable-only: prefer info.version when it is itself stable.
        version_str = data.get("info", {}).get("version")
        if version_str:
            try:
                if not Version(version_str).is_prerelease:
                    return Package(name=pkg_ref.name, version=version_str)
            except InvalidVersion:
                return Package(name=pkg_ref.name, version=version_str)

        # info.version was a pre-release or missing — scan releases.
        best_stable = _pick_highest_version(data.get("releases", {}), stable_only=True)
        if best_stable is not None:
            return Package(name=pkg_ref.name, version=str(best_stable))
        return None

    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        """Checks for available updates by querying PyPI.

        Default implementation for all Python-ecosystem plugins.
        Subclasses can override to use native tooling (e.g.
        ``pip list --outdated``) and fall back to this via ``super()``.

        Args:
            params: The check parameters including which packages to check.

        Returns:
            A list of packages that have updates available.
        """
        return await self._check_pypi_updates(params)
