"""Data models and schemas for tool.

Managed tool/package operation schemas.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import Field

from porringer.core.schema import PluginKind, PorringerModel
from porringer.schema.inspection import FailedPathInspection
from porringer.schema.manifest import PackageSpec
from porringer.schema.observability import (
    SCHEMA_VERSION,
    Diagnostic,
    FollowUpAction,
    ResultStatus,
)


class RangeStrategy(StrEnum):
    """How ``tool.upgrade`` treats an entry's version constraint."""

    IN_RANGE = 'in_range'
    """Upgrade to the newest version the constraint allows; the manifest is unchanged."""

    REPLACE = 'replace'
    """Upgrade to the newest version, then move the constraint up to it."""


class ManifestEntry(PorringerModel):
    """One entry of a manifest, as written, with its stable identity."""

    entry_id: str
    manifest_path: Path
    kind: PluginKind
    ecosystem: str
    spec: PackageSpec


class RuntimeDependent(PorringerModel):
    """Something that breaks if a runtime is removed."""

    description: str = Field(description='Human-readable description, such as "pdm (tools.python) in porringer.json"')
    manifest_path: Path | None = Field(default=None, description='Manifest of a dependent entry')
    entry_id: str | None = Field(default=None, description='Id of a dependent manifest entry')
    plugin: str | None = Field(default=None, description='Plugin that installed a dependent tool')
    package: str | None = Field(default=None, description='Name of a dependent tool')


class RuntimeRemovalCheck(PorringerModel):
    """What removing one runtime entry would affect."""

    entry_id: str
    runtime: str = Field(description='Runtime tag of the entry')
    executable: Path | None = Field(default=None, description='Interpreter that would be removed, when it resolves')
    default: bool = Field(default=False, description="Whether it is the provider's default interpreter")
    dependents: tuple[RuntimeDependent, ...] = Field(default_factory=tuple)
    installed_packages: tuple[str, ...] = Field(
        default_factory=tuple,
        description='Packages inside the interpreter that are removed along with it',
    )


class ManagedPackageResult(PorringerModel):
    """Stable result for a managed package check, upgrade, or uninstall."""

    plugin: str | None = None
    package: str | None = None
    kind: str | None = None
    action_id: str | None = None
    entry_id: str | None = None
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    message: str | None = None
    installed_version: str | None = None
    available_version: str | None = None


class ManagedToolReport(PorringerModel):
    """Stable report for managed tool/package operations."""

    schema_version: str = SCHEMA_VERSION
    operation: str
    status: ResultStatus = ResultStatus.SUCCESS
    manifests_processed: int = 0
    results: tuple[ManagedPackageResult, ...] = Field(default_factory=tuple)
    failed_paths: tuple[FailedPathInspection, ...] = Field(default_factory=tuple)
    diagnostics: tuple[Diagnostic, ...] = Field(default_factory=tuple)
    follow_up_actions: tuple[FollowUpAction, ...] = Field(default_factory=tuple)

    @property
    def success(self) -> bool:
        """Whether the operation completed without failed paths or failed package results."""
        return not self.failed_paths and all(result.success for result in self.results)

    @property
    def updated(self) -> int:
        """Number of non-skipped successful package operations."""
        return sum(1 for result in self.results if result.success and not result.skipped)

    @property
    def failed(self) -> int:
        """Number of failed package operations."""
        return sum(1 for result in self.results if not result.success)

    @property
    def skipped(self) -> int:
        """Number of skipped package operations."""
        return sum(1 for result in self.results if result.skipped)
