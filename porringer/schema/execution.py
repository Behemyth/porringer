"""Data models and schemas for execution.

Execution schemas.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from pathlib import Path
from typing import Literal

from pydantic import Field

from porringer.core.schema import Ecosystem, PackageRef, PluginKind, PorringerModel
from porringer.schema.manifest import ManifestMetadata
from porringer.schema.observability import (
    SCHEMA_VERSION,
    ActionRef,
    Diagnostic,
    FollowUpAction,
    ResultStatus,
)

# ---------------------------------------------------------------------------
# Operation union — discriminated by type
# ---------------------------------------------------------------------------


class InstallReason(Enum):
    """Why an install operation was resolved."""

    NOT_INSTALLED = auto()
    """Package is not present on the system."""

    ENSURE_EXTRAS = auto()
    """Package is already installed but extras/features need to be ensured."""


@dataclass(frozen=True, slots=True)
class Install:
    """Resolved operation: install a package.

    ``reason`` distinguishes a fresh install from an
    extras-ensuring re-run of the same install command.

    ``installed_version`` is populated when ``reason`` is
    ``ENSURE_EXTRAS`` — the package is already present.

    ``available_version`` carries the manifest constraint string
    (e.g. ``'>=1.0'``) so downstream clients can display the
    version requirement even before installation.
    """

    reason: InstallReason = InstallReason.NOT_INSTALLED
    installed_version: str | None = None
    available_version: str | None = None


@dataclass(frozen=True, slots=True)
class Upgrade:
    """Resolved operation: upgrade a package to a newer version.

    Version metadata is carried so that both the inspection reporter
    and the real execution path can surface it on the result.
    """

    installed_version: str | None = None
    available_version: str | None = None


@dataclass(frozen=True, slots=True)
class Uninstall:
    """Resolved operation: remove a package."""

    installed_version: str | None = None


@dataclass(frozen=True, slots=True)
class Skip:
    """Resolved operation: no action required.

    ``reason`` and version metadata are propagated to the
    ``SetupActionResult`` for display purposes.
    """

    reason: SkipReason | None = None
    installed_version: str | None = None
    available_version: str | None = None


type Operation = Install | Upgrade | Uninstall | Skip
"""Union of all resolved operation variants.

Use ``match`` / ``isinstance`` to dispatch; each variant
carries its own typed payload.
"""


# ---------------------------------------------------------------------------
# Clone status
# ---------------------------------------------------------------------------


class CloneStatusKind(Enum):
    """Discriminator for `CloneStatus`.

    Indicates whether a repository is already cloned, missing, or present
    but pointing at a different remote URL.
    """

    CLONED = auto()
    """Repository is present and the remote URL matches."""

    MISSING = auto()
    """No repository exists at the destination."""

    URL_MISMATCH = auto()
    """A repository exists but its remote URL does not match."""


@dataclass(frozen=True)
class CloneStatus:
    """Result of an SCM clone-presence check.

    Returned by `ScmEnvironment.is_cloned()`.  Carries the
    discriminator `kind` together with the actual remote URL
    (when available) so callers can produce diagnostic messages
    without re-querying the SCM tool.
    """

    kind: CloneStatusKind
    """The high-level result of the check."""

    remote_url: str | None = None
    """The remote URL found at the destination, if any."""

    matched_remote: str | None = None
    """Name of the remote whose URL matched (e.g. ``"origin"``, ``"upstream"``)."""

    repo_root: Path | None = None
    """The SCM repository root directory, when it differs from the
    originally requested destination (e.g. when the manifest lives
    inside a subdirectory of the repo)."""


class SkipReason(Enum):
    """Machine-readable reason an action was skipped.

    Use `SetupActionResult.message` for the human-readable detail.
    """

    ALREADY_INSTALLED = auto()
    NOT_INSTALLED = auto()
    NO_PROJECT_DIRECTORY = auto()
    UPDATE_AVAILABLE = auto()
    ALREADY_LATEST = auto()
    GUARD_SATISFIED = auto()
    NOT_MANAGED = auto()
    """Present, but installed by something else, so this installer can't remove it."""


@dataclass(frozen=True, slots=True)
class SetupAction:
    """A single action to perform during setup.

    The `kind` field is the primary discriminator:

    * `PluginKind.PACKAGE`, `PluginKind.TOOL`,
      `PluginKind.RUNTIME` — install/upgrade a single package.
    * `PluginKind.PROJECT` — sync a project lock-file / venv.
    * `PluginKind.SCM` — clone a repository.

    Frozen and hashable — can be used as dict keys and in sets.

    Args:
        description: Human-readable description of the action.
        kind: The plugin kind.
        ecosystem: The ecosystem identifier (e.g. `"python"`, `"node"`).
        installer: The plugin name (for PACKAGE/TOOL/RUNTIME/PROJECT/SCM).
        package: The package reference (for PACKAGE/TOOL/RUNTIME/SCM).
        package_description: Optional per-package description from the manifest.
        include_prereleases: Per-package opt-in for pre-release update detection.
        runtime_tag: Runtime the installer runs under, from the entry's ``runtime``.
        entry_id: Stable identity of the manifest entry this action came from;
            ``None`` for actions Porringer synthesized.
    """

    description: str
    kind: PluginKind | None = None
    ecosystem: Ecosystem | None = None
    installer: str | None = None
    package: PackageRef | None = None
    package_description: str | None = None
    include_prereleases: bool = False
    runtime_tag: str | None = None
    entry_id: str | None = None


@dataclass(slots=True)
class SetupActionResult:
    """Result of executing a single setup action.

    Args:
        action: The action that was executed.
        success: Whether the action succeeded.
        message: Optional human-readable detail (error on failure, description on skip).
        skipped: Whether the action was skipped.
        skip_reason: Machine-readable skip code (see `SkipReason`).
        installed_version: The currently installed version string, populated
            when the action is skipped due to presence detection.
        available_version: The latest upstream version string, populated
            when ``skip_reason`` is ``UPDATE_AVAILABLE``.
        duration_seconds: Monotonic elapsed execution time, when the action
            was executed through the evented runner.
        cli_steps: Ordered command steps planned for the action, when it has
            more than one command.
        failed_step_index: One-based command step that failed, when known.
        ref: Stable action reference, attached by ``sync.run``.
    """

    action: SetupAction
    success: bool
    message: str | None = None
    skipped: bool = False
    skip_reason: SkipReason | None = None
    installed_version: str | None = None
    available_version: str | None = None
    cli_command: tuple[str, ...] | None = None
    duration_seconds: float | None = None
    cli_steps: tuple[tuple[str, ...], ...] | None = None
    failed_step_index: int | None = None
    ref: ActionRef | None = None


class SyncStrategy(Enum):
    """Strategy controlling how the sync engine reconciles manifest state.

    MINIMAL: Default.  Install packages that aren't already present.
             Already-installed packages are left untouched.
    LATEST:  Upgrade every package to its latest allowed version.
             Falls back to install if a package isn't installed.
    """

    MINIMAL = auto()
    LATEST = auto()


class InspectionMode(StrEnum):
    """Controls how much system probing an inspect request performs."""

    COMPLETE = 'complete'
    """Perform package presence, update, extras, and SCM presence checks."""

    FAST = 'fast'
    """Report manifest/plugin shape without expensive presence/update probing."""


class SetupParameters(PorringerModel):
    """Parameters for the setup command."""

    paths: Path | Sequence[str | Path] | None = Field(
        default=None,
        description=(
            'Path(s) to manifest file(s) or directories, or URL strings '
            '(``http://`` / ``https://``) pointing to remote manifests. '
            'None uses the nearest manifest in the current directory.'
        ),
    )
    project_directory: Path | Literal[False] | None = Field(
        default=None,
        description=(
            'Controls where project-sync actions run. '
            'None (default) lets each project-environment plugin auto-discover '
            'its project root by walking ancestor directories from the manifest '
            'location looking for an ecosystem-specific marker file '
            '(e.g. pyproject.toml for Python, package.json for Node). '
            'A Path overrides the working directory for all plugins, '
            'disabling per-ecosystem auto-discovery. '
            'False skips project-sync actions entirely.'
        ),
    )
    fail_fast: bool = Field(default=True, description='Stop on first error when processing multiple paths')
    strategy: SyncStrategy = Field(default=SyncStrategy.MINIMAL, description='Sync strategy: minimal or latest')
    inspection_mode: InspectionMode = Field(
        default=InspectionMode.COMPLETE,
        description=(
            'Inspection depth. ``complete`` performs presence/update checks; '
            '``fast`` reports manifest/plugin shape without expensive probing.'
        ),
    )
    prerelease_packages: set[str] | None = Field(
        default=None,
        description=(
            'Set of package names whose ``include_prereleases`` flag '
            'should be forced to ``True``, overriding the manifest '
            'default.  Names are compared case-insensitively against '
            '``action.package.name``.  ``None`` means no overrides.'
        ),
    )
    include_packages: set[str] | None = Field(
        default=None,
        description=(
            'Set of package names to include.  When set, only actions '
            'whose ``action.package.name`` appears in this set '
            '(case-insensitive) are executed.  ``None`` means all '
            'packages; an empty set matches no package actions.  Non-package actions '
            'are always included regardless of this filter.'
        ),
    )
    action_ids: set[str] | None = Field(
        default=None,
        description=(
            'Stable action ids to include (for example ``0:2``). '
            '``None`` means all actions; an empty set matches none. '
            'Ids are matched against the resolved input path index and original '
            'manifest action order, after path resolution but before other '
            'filters can renumber actions. Failed paths keep their resolved '
            'input slot, so later manifest ids stay stable when '
            '``fail_fast=False``.'
        ),
    )
    entry_ids: set[str] | None = Field(
        default=None,
        description=(
            'Manifest entry ids to include (``entry_id`` on actions and results). '
            'Unlike action ids they survive edits and reordering of the manifest. '
            '``None`` means all actions; an empty set matches none.'
        ),
    )
    max_concurrency: int = Field(
        default=8,
        description=(
            'Maximum number of concurrent tasks for parallel package '
            'operations.  Set to 0 for unlimited concurrency.  '
            'Applied via an ``asyncio.Semaphore`` around each '
            'TaskGroup-dispatched coroutine.'
        ),
    )
    plugin_names: set[str] | None = Field(
        default=None,
        description=(
            'Plugin names to include.  ``None`` means all plugins; an empty '
            'set matches no plugin-handled actions.  Actions without an '
            'installer are always included.'
        ),
    )
    action_kinds: set[PluginKind] | None = Field(
        default=None,
        description=(
            'Action kinds to include.  ``None`` means all kinds; an empty set '
            'matches no actions.  Actions without a kind are excluded when set.'
        ),
    )


@dataclass(slots=True)
class SetupResults:
    """Results of a setup operation.

    Args:
        actions: The list of actions (for preview) or action results (for execute).
        manifest_path: The path to the manifest that was used.
        root_directory: The logical project root directory.
        metadata: Optional display metadata from the manifest.
        preferences: Ecosystem → plugin-name preferences from the manifest,
            forwarded to the execution engine for deferred resolution.
    """

    actions: list[SetupAction] = field(default_factory=list)
    action_indices: list[int] = field(default_factory=list)
    results: list[SetupActionResult] = field(default_factory=list)
    manifest_path: Path | None = None
    root_directory: Path | None = None
    metadata: ManifestMetadata | None = None
    preferences: dict[Ecosystem, str] = field(default_factory=dict)
    manifest_index: int | None = None


@dataclass(slots=True)
class BatchSetupResults:
    """Results of batch setup operations across multiple manifests.

    Args:
        manifest_results: Results for each manifest processed.
        failed_paths: Paths that failed to process (e.g., manifest not found).
    """

    manifest_results: list[SetupResults] = field(default_factory=list)
    failed_paths: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when all manifests were processed successfully."""
        if self.failed_paths:
            return False
        return all(all(r.success for r in m.results) for m in self.manifest_results)

    @property
    def total_actions(self) -> int:
        """Total number of actions across all manifests."""
        return sum(len(m.actions) for m in self.manifest_results)

    @property
    def total_succeeded(self) -> int:
        """Total number of successful action results (excludes skipped)."""
        return sum(1 for m in self.manifest_results for r in m.results if r.success and not r.skipped)

    @property
    def total_failed(self) -> int:
        """Total number of failed action results."""
        return sum(1 for m in self.manifest_results for r in m.results if not r.success)

    @property
    def total_skipped(self) -> int:
        """Total number of skipped action results."""
        return sum(1 for m in self.manifest_results for r in m.results if r.skipped)

    @property
    def skips(self) -> list[SetupActionResult]:
        """All skipped action results.

        Each result carries `skip_reason` (`SkipReason` enum),
        `message` (human-readable detail), and `action` (with
        `action_type`, `kind`, `ecosystem`, `installer`) for programmatic
        inspection.
        """
        return [r for m in self.manifest_results for r in m.results if r.skipped]


class SyncRunReport(PorringerModel):
    """Structured report for manifest execution."""

    schema_version: str = SCHEMA_VERSION
    operation: str = 'sync.run'
    status: ResultStatus = ResultStatus.SUCCESS
    results: BatchSetupResults
    diagnostics: tuple[Diagnostic, ...] = Field(default_factory=tuple)
    follow_up_actions: tuple[FollowUpAction, ...] = Field(default_factory=tuple)

    @property
    def success(self) -> bool:
        """Whether execution completed without failed paths or failed action results."""
        return self.results.success
