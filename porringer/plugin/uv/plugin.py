"""Plugin integration for plugin.

Plugin implementation.
"""

from pathlib import Path
from typing import override

from porringer.core.plugin_schema.project_environment import ProjectInstaller
from porringer.core.plugin_schema.python_environment import PythonEnvironment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Package, PackageRef


class UvEnvironment(PythonEnvironment, ProjectInstaller):
    """Represents a Python environment managed by uv.

    Installs individual packages via ``uv pip install`` and also installs
    whole projects via ``uv sync`` (the project-install capability).
    """

    _install_verb = 'sync'
    _project_evidence_files = ('uv.lock',)
    _pyproject_tool_tables = (('tool', 'uv'),)

    @classmethod
    @override
    def runtime_selection_args(cls, runtime_context: RuntimeContext | None) -> list[str]:
        """Return `['--python', '<path>']` when an override is active.

        uv accepts an inline interpreter flag on every relevant
        sub-command (`uv sync`, `uv pip install`), so this single hook
        serves both the project-sync and package command builders.

        Args:
            runtime_context: Resolved runtime paths for this execution run.
        """
        if runtime_context is not None:
            exe = runtime_context.get(cls.consumed_runtime_kind())
            if exe is not None:
                return ['--python', str(exe)]
        return []

    def _python_args(self, runtime_context: RuntimeContext | None = None) -> list[str]:
        """Return `['--python', '<path>']` when an override is active.

        Args:
            runtime_context: Resolved runtime paths for this execution run.
        """
        return self.runtime_selection_args(runtime_context)

    @classmethod
    @override
    def tool_name(cls) -> str:
        """UV wraps the `uv` CLI."""
        return 'uv'

    @classmethod
    @override
    def run_prefix(cls) -> list[str]:
        """`uv run --no-sync` runs commands in the environment the sync step just prepared."""
        return ['uv', 'run', '--no-sync']

    @classmethod
    @override
    def standalone_binary(cls) -> bool:
        """UV is a standalone Rust binary, not a Python module."""
        return True

    @override
    def install_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command to install a package via uv."""
        cmd = ['uv', 'pip', 'install', *self._python_args(runtime_context), package.specifier]
        if include_prereleases:
            cmd.append('--prerelease=allow')
        return cmd

    @override
    def upgrade_command(
        self,
        package: PackageRef,
        *,
        include_prereleases: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command to upgrade a package via uv."""
        cmd = [
            'uv',
            'pip',
            'install',
            '--upgrade',
            *self._python_args(runtime_context),
            package.specifier,
        ]
        if include_prereleases:
            cmd.append('--prerelease=allow')
        return cmd

    @override
    def uninstall_command(
        self,
        package: PackageRef,
        *,
        runtime_context: RuntimeContext | None = None,
    ) -> list[str]:
        """Returns the CLI command to uninstall a package via uv."""
        return ['uv', 'pip', 'uninstall', *self._python_args(runtime_context), package.name]

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        """Gathers installed packages using `uv pip list --format=json`.

        When *project_path* is provided, the method discovers the
        project's virtual environment (`<project_path>/.venv`) and
        lists packages from that interpreter.  Otherwise it falls back
        to the runtime context or the default Python.

        Args:
            project_path: Optional project directory.  When set, the
                listing is scoped to the project's `.venv`.
            runtime_context: Resolved runtime paths for this execution
                run.  ``None`` means use defaults.

        Returns:
            A list of installed packages.
        """
        # Determine the effective Python target
        effective_args = self._python_args(runtime_context)
        if project_path is not None:
            venv_python = self._discover_venv_python(project_path)
            if venv_python is not None:
                effective_args = ['--python', str(venv_python)]

        entries = await self._run_json_command(['uv', 'pip', 'list', '--format=json', *effective_args])
        if isinstance(entries, list):
            return [Package(name=e['name'], version=e.get('version')) for e in entries]

        return []
