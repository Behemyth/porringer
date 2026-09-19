"""Plugin integration for plugin.

Plugin implementation.
"""

import logging
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


class WingetEnvironment(Environment):
    """Represents a Windows environment managed by winget.

    Provides methods to install, search, uninstall, upgrade, and list packages using winget
    as the backend package manager.
    """

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        """Winget belongs to the `system` ecosystem."""
        return Ecosystem('system')

    @staticmethod
    @override
    def is_supported() -> bool:
        """Supported on Windows only."""
        return sys.platform == 'win32'

    @classmethod
    @override
    def tool_name(cls) -> str:
        """Winget wraps the `winget` CLI."""
        return 'winget'

    @override
    def install_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        """Returns the CLI command to install a package via winget."""
        cmd = ['winget', 'install', '--id', package.name]
        cmd.extend(['--accept-source-agreements', '--accept-package-agreements', '-e'])
        if package.constraint:
            cmd.extend(['--version', package.constraint])
        return cmd

    @override
    def upgrade_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        """Returns the CLI command to upgrade a package via winget."""
        cmd = ['winget', 'upgrade', '--id', package.name]
        cmd.extend(['--accept-source-agreements', '--accept-package-agreements', '-e'])
        if package.constraint:
            cmd.extend(['--version', package.constraint])
        return cmd

    @override
    def package_command(self, verb: PackageVerb, params: PackageParameters) -> PackageCommand:
        """Keep Winget dry runs log-only while sharing command construction."""
        command = super().package_command(verb, params)
        if params.dry and verb in {'install', 'upgrade'}:
            return PackageCommand(argv=command.argv, execute=False)
        return command

    @override
    def uninstall_command(self, package: PackageRef, *, runtime_context: RuntimeContext | None = None) -> list[str]:
        """Returns the CLI command to uninstall a package via winget."""
        return ['winget', 'uninstall', '--id', package.name, '--silent']

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        """Checks for available updates via ``winget upgrade``.

        Parses the tabular text output of ``winget upgrade`` to find
        packages with available updates.  The header's dashes separator
        line is used to determine column boundaries.  Results are
        filtered to the requested packages when specified.

        Args:
            params: The check parameters.

        Returns:
            A list of packages with their latest available version.
        """
        logger = logging.getLogger('porringer.winget.check_updates')
        output = await self._run_text_command(
            ['winget', 'upgrade', '--accept-source-agreements', '--disable-interactivity'],
        )
        if output is None:
            return []

        columns = self._parse_winget_columns(output)
        if columns is None:
            logger.debug('Could not parse winget table header')
            return []

        col_starts, col_names, data_lines = columns
        requested = {p.name.lower() for p in params.packages} if params.packages else None

        def _col(line: str, col_index: int) -> str:
            start = col_starts[col_index]
            end = col_starts[col_index + 1] if col_index + 1 < len(col_starts) else len(line)
            return line[start:end].strip()

        id_col = next((i for i, n in enumerate(col_names) if n.lower() == 'id'), None)
        avail_col = next((i for i, n in enumerate(col_names) if n.lower() == 'available'), None)
        if id_col is None or avail_col is None:
            logger.debug('Could not find Id/Available columns in winget header: %s', col_names)
            return []

        results: list[Package] = []
        for line in data_lines:
            if not line.strip() or len(line) < col_starts[-1]:
                continue
            pkg_id = _col(line, id_col)
            available = _col(line, avail_col)
            if not pkg_id or not available:
                continue
            if requested is not None and pkg_id.lower() not in requested:
                continue
            results.append(Package(name=pkg_id, version=available))

        if not results:
            logger.debug('No updates found via winget upgrade')
        return results

    @staticmethod
    def _parse_winget_columns(output: str) -> tuple[list[int], list[str], list[str]] | None:
        """Parse column layout from winget tabular output.

        Locates the dashes separator line, derives column start
        positions from the header, and returns the data lines.

        Returns:
            A tuple of ``(col_starts, col_names, data_lines)``, or
            ``None`` if the header cannot be parsed.
        """
        lines = output.splitlines()
        sep_idx: int | None = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and all(c == '-' for c in stripped):
                sep_idx = i
                break

        if sep_idx is None or sep_idx == 0:
            return None

        header = lines[sep_idx - 1]
        col_names = header.split()
        col_starts: list[int] = []
        pos = 0
        for col_name in col_names:
            idx = header.find(col_name, pos)
            col_starts.append(idx)
            pos = idx + len(col_name)

        return col_starts, col_names, lines[sep_idx + 1 :]

    @override
    async def packages(
        self, *, project_path: Path | None = None, runtime_context: RuntimeContext | None = None
    ) -> list[Package]:
        """Gathers installed packages in the given environment.

        winget manages system packages globally; *project_path* is
        accepted for interface compatibility but has no effect.

        Args:
            project_path: Unused.  winget is inherently global.
            runtime_context: Unused.  winget is not Python-scoped.

        Returns:
            A list of packages
        """
        return []
