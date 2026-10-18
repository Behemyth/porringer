"""Data models and schemas for check.

Check/update schemas.
"""

from dataclasses import dataclass, field

from packaging.version import Version
from pydantic import Field

from porringer.core.schema import PorringerModel


@dataclass(slots=True)
class PackageUpdateInfo:
    """Update information for a single package.

    Args:
        name: Package name.
        current_version: Currently installed version.
        latest_version: Latest available version.
        update_available: Whether an update is available.
    """

    name: str
    current_version: Version | None
    latest_version: Version | None
    update_available: bool


@dataclass(slots=True)
class CheckResult:
    """Result of checking updates for a plugin.

    Args:
        plugin: The plugin name.
        packages: List of package update info.
        error: Optional error message if check failed.
    """

    plugin: str
    packages: list[PackageUpdateInfo] = field(default_factory=list)
    error: str | None = None

    @property
    def success(self) -> bool:
        """True when the check completed without error."""
        return self.error is None

    @property
    def updates_available(self) -> int:
        """The count of packages with updates available."""
        return sum(1 for p in self.packages if p.update_available)


class CheckParameters(PorringerModel):
    """Parameters for checking updates via plugins."""

    plugin_names: set[str] | None = Field(
        default=None,
        description='Plugin names to check. None means all plugins; an empty set checks none.',
    )
    include_prereleases: bool = Field(default=False, description='Include pre-release versions')
    max_concurrency: int = Field(
        default=8,
        description=(
            'Maximum number of plugins checked concurrently. Set to 0 for '
            'unlimited concurrency. Applied via an ``asyncio.Semaphore`` around '
            'each dispatched check.'
        ),
    )
