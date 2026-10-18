"""Data models for the read-only inventory of runtimes and installed packages."""

from enum import StrEnum
from pathlib import Path

from pydantic import Field

from porringer.core.schema import PorringerModel
from porringer.schema.observability import SCHEMA_VERSION


class InventoryStatus(StrEnum):
    """Outcome of one inventory query."""

    OK = 'ok'
    ERROR = 'error'


class RuntimeRecord(PorringerModel):
    """An interpreter a runtime provider can resolve on this machine."""

    provider: str = Field(description='Runtime provider plugin, such as "pim" or "pyenv"')
    kind: str = Field(description='Runtime kind, such as "python"')
    tag: str = Field(description='Version tag, such as "3.14"')
    executable: Path
    default: bool = Field(default=False, description="Whether this is the provider's default runtime")
    managed: bool = Field(
        default=False,
        description='Whether the provider installed it (and can remove it)',
    )


class InventoryPackage(PorringerModel):
    """A package installed in one location."""

    name: str
    version: str | None = None
    latest_version: str | None = Field(default=None, description='Newer version available, when updates were requested')


class InventoryLocation(PorringerModel):
    """Packages one plugin has installed in one place: an interpreter or a global store."""

    plugin: str
    runtime: RuntimeRecord | None = Field(default=None, description='The interpreter, for runtime-bound plugins')
    status: InventoryStatus = InventoryStatus.OK
    error: str | None = None
    packages: tuple[InventoryPackage, ...] = Field(default_factory=tuple)
    update_error: str | None = Field(default=None, description='Why update information is missing, if it is')


class ProviderFailure(PorringerModel):
    """A runtime provider that could not list its runtimes."""

    provider: str
    error: str


class InventoryReport(PorringerModel):
    """Runtimes and installed packages on this machine."""

    schema_version: str = SCHEMA_VERSION
    runtimes: tuple[RuntimeRecord, ...] = Field(default_factory=tuple)
    locations: tuple[InventoryLocation, ...] = Field(default_factory=tuple)
    provider_failures: tuple[ProviderFailure, ...] = Field(default_factory=tuple)

    @property
    def success(self) -> bool:
        """Whether every provider and location was queried without error."""
        return not self.provider_failures and all(
            location.status is InventoryStatus.OK and location.update_error is None for location in self.locations
        )
