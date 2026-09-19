"""Plugin integration for plugin.

Plugin implementation for APT (Advanced Package Tool) package manager.
"""

import sys
from pathlib import Path
from typing import override

from porringer.core.plugin_schema.environment import (
    CheckUpdatesParameters,
    Environment,
    PackageCommand,
    PackageParameters,
    PackageVerb,
)
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Ecosystem, Package, PackageRef


class APTEnvironment(Environment):
    """Represents a Debian/Ubuntu Linux environment managed by APT.

    Provides methods to install, search, uninstall, upgrade, and list packages using
    APT as the backend package manager.

    This plugin is available on Debian-based Linux distributions (Debian, Ubuntu, etc.).

    This plugin provides the "python-runtime" capability, which pip and pipx can use
    to manage Python versions via packages like python3.12.

    Note:
        APT operations (install, remove, upgrade) require root privileges.
        Run with sudo: `sudo porringer ...`

    CLI Reference:
        - dpkg -l  - List installed packages
        - apt install <package>  - Install a package
        - apt remove <package>  - Remove a package
        - apt upgrade <package>  - Upgrade a package
        - apt-cache show <package>  - Get package info
        - apt-cache search <text>  - Search for packages
        - apt-cache policy <package>  - Get version/installation status
    """

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        """APT belongs to the `system` ecosystem."""
        return Ecosystem('system')

    @staticmethod
    @override
    def is_supported() -> bool:
        """Supported on Linux only."""
        return sys.platform not in {'win32', 'darwin'}

    @classmethod
    @override
    def tool_name(cls) -> str:
        """APT wraps the `apt` CLI."""
        return 'apt'

    @override
    def install_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        """Returns the CLI command to install a package via apt."""
        return ['apt', 'install', '-y', package.specifier_for('equals')]

    @override
    def upgrade_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        """Returns the CLI command to upgrade a package via apt."""
        return ['apt', 'install', '-y', '--only-upgrade', package.specifier_for('equals')]

    @override
    def package_command(self, verb: PackageVerb, params: PackageParameters) -> PackageCommand:
        """Keep APT dry runs log-only while retaining the complete argv."""
        command = super().package_command(verb, params)
        if params.dry and verb in {'install', 'upgrade'}:
            args = list(command.argv)
            args.insert(2, '--simulate')
            return PackageCommand(argv=tuple(args), execute=False)
        return command

    @override
    def uninstall_command(self, package: PackageRef, *, runtime_context: RuntimeContext | None = None) -> list[str]:
        """Returns the CLI command to uninstall a package via apt."""
        return ['apt', 'remove', '-y', package.name]

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        """Checks for available updates via ``apt-cache policy``.

        For each requested package, parses the ``Candidate:`` line
        from ``apt-cache policy`` to determine the latest available
        version in the configured repositories.

        Args:
            params: The check parameters.

        Returns:
            A list of packages with their latest available version.
        """
        results: list[Package] = []
        for pkg_ref in params.packages:
            output = await self._run_text_command(['apt-cache', 'policy', pkg_ref.name])
            if output is None:
                continue
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith('Candidate:'):
                    version = stripped.split(':', 1)[1].strip()
                    if version and version != '(none)':
                        results.append(Package(name=pkg_ref.name, version=version))
                    break
        return results

    @override
    async def install(self, params: PackageParameters) -> Package | None:
        """Asynchronously installs a package using APT.

        Resolves the installed version after the shared command runs.
        """
        result = await super().install(params)
        if result is not None and not params.dry:
            version = await self._get_package_version(params.package.name)
            return Package(name=result.name, version=version)
        return result

    @override
    async def upgrade(self, params: PackageParameters) -> Package | None:
        """Asynchronously upgrades a package using APT.

        Resolves the installed version after the shared command runs.
        """
        result = await super().upgrade(params)
        if result is not None and not params.dry:
            version = await self._get_package_version(params.package.name)
            return Package(name=result.name, version=version)
        return result

    @override
    async def packages(
        self, *, project_path: Path | None = None, runtime_context: RuntimeContext | None = None
    ) -> list[Package]:
        """Lists all installed packages via dpkg.

        apt manages system packages globally; *project_path* is accepted
        for interface compatibility but has no effect.

        Args:
            project_path: Unused.  apt is inherently global.
            runtime_context: Unused.  apt is not Python-scoped.

        Returns:
            A list of installed packages
        """
        output = await self._run_text_command(['dpkg-query', '-W', '-f', '${Package}\t${Version}\n'])
        if output is None:
            return []

        packages: list[Package] = []
        for line in output.strip().split('\n'):
            if '\t' in line:
                parts = line.split('\t', 1)
                name = parts[0].strip()
                version = parts[1].strip() if len(parts) > 1 else 'unknown'
                if name:
                    packages.append(Package(name=name, version=version))
        return packages

    async def _get_package_version(self, package: str) -> str | None:
        """Gets the installed version for a package.

        Args:
            package: The package name

        Returns:
            The version string, or None if not found
        """
        output = await self._run_text_command(['dpkg-query', '-W', '-f', '${Version}', package])
        if output and output.strip():
            return output.strip()
        return None
