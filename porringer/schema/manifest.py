"""Data models and schemas for manifest.

Manifest schemas.
"""

import hashlib
import os
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Annotated, Any

from packaging.utils import canonicalize_name
from pydantic import Field, HttpUrl, StringConstraints, model_validator

from porringer.core.schema import (
    Ecosystem,
    PackageRef,
    PlatformScoped,
    PluginKind,
    PorringerModel,
)
lazy from porringer.utility.exception import ManifestValidationCode

MANIFEST_SCHEMA_URL = 'https://synodic.github.io/porringer/schema.json'
"""Canonical published URL (``$id``) for the porringer manifest JSON Schema."""

MANIFEST_SCHEMA_DIALECT = 'https://json-schema.org/draft/2020-12/schema'
"""JSON Schema dialect (``$schema``) the generated manifest schema conforms to."""

GroupName = Annotated[str, StringConstraints(pattern=r'^[a-z0-9][a-z0-9-]*$')]
"""Name of an optional entry group: lowercase letters, digits and hyphens."""


class ManifestDiagnosticSeverity(Enum):
    """Severity level for a manifest validation diagnostic."""

    ERROR = auto()
    WARNING = auto()


@dataclass(slots=True)
class ManifestDiagnostic:
    """A single diagnostic produced by manifest validation.

    Args:
        field: Dot-path to the relevant field (e.g. `"packages.python"`, `"preferences.python"`).
        message: Human-readable description of the problem or concern.
        code: Machine-readable diagnostic code.
        severity: Whether this diagnostic is an error or a warning.
    """

    field: str
    message: str
    code: ManifestValidationCode
    severity: ManifestDiagnosticSeverity


@dataclass(slots=True)
class ManifestValidationResult:
    """Structured result of manifest validation.

    Args:
        diagnostics: All validation diagnostics (errors and warnings).
    """

    diagnostics: list[ManifestDiagnostic] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """A manifest is valid when it has no error-level diagnostics."""
        return not any(d.severity == ManifestDiagnosticSeverity.ERROR for d in self.diagnostics)

    @property
    def errors(self) -> list[ManifestDiagnostic]:
        """All error-level diagnostics."""
        return [d for d in self.diagnostics if d.severity == ManifestDiagnosticSeverity.ERROR]

    @property
    def warnings(self) -> list[ManifestDiagnostic]:
        """All warning-level diagnostics."""
        return [d for d in self.diagnostics if d.severity == ManifestDiagnosticSeverity.WARNING]


class PackageSpec(PlatformScoped):
    """A package entry with optional display metadata.

    Supports both string shorthand (just a package specifier) and object form
    with additional metadata for GUI consumers.
    """

    name: PackageRef = Field(description='The package reference (name with optional version constraint)')
    description: str | None = Field(default=None, description='Human-readable description of this package')
    include_prereleases: bool = Field(
        default=False,
        description='Include pre-release versions when checking this package for updates',
    )
    installer: str | None = Field(
        default=None,
        description='Installer plugin to use for this entry (e.g. "pipx"); must belong to the entry\'s ecosystem. '
        'Overrides the ecosystem preference and is never substituted.',
    )
    runtime: str | None = Field(
        default=None,
        description='Runtime tag the installer runs under (e.g. "3.14"); packages and tools only',
    )
    group: GroupName | None = Field(
        default=None,
        description='Optional group this entry belongs to; it is installed only when that group is selected. '
        'The group must be declared under "groups".',
    )

    @model_validator(mode='before')
    @classmethod
    def _coerce_string(cls, data: Any) -> Any:
        """Allow plain strings as shorthand for `{"name": "..."}`."""
        if isinstance(data, str):
            return {'name': data}
        return data


class ManifestGroup(PorringerModel):
    """An optional set of manifest entries that callers opt into by name."""

    description: str | None = Field(default=None, description='Human-readable purpose of the group')


class SetupManifest(PorringerModel):
    """The setup manifest schema for .porringer files or pyproject.toml [tool.porringer].

    Manifest entries are grouped by **kind** (`packages`, `tools`,
    `runtimes`), each containing a dict keyed by **ecosystem**
    (e.g. `"python"`, `"node"`, `"system"`). Project install is now
    resolved implicitly from relevant project plugins based on the
    repository contents.

    Ecosystem names are free-form strings declared by plugins — the core
    schema does not enumerate them.  A third-party Cargo plugin declaring
    `ecosystem() = "rust"` "just works" with
    `"packages": {"rust": ["serde"]}` — zero core changes required.
    """

    version: str = Field(default='1', description='Manifest schema version')
    name: str | None = Field(default=None, description='Human-readable project/environment name')
    description: str | None = Field(
        default=None,
        description='Short description shown in the install preview header',
    )
    author: str | None = Field(default=None, description='Author or organization name')
    url: HttpUrl | None = Field(default=None, description='Project URL for reference')
    packages: dict[Ecosystem, list[PackageSpec]] = Field(
        default_factory=dict,
        description='Packages to install per ecosystem (e.g. {"python": ["requests"]})',
    )
    tools: dict[Ecosystem, list[PackageSpec]] = Field(
        default_factory=dict,
        description='CLI tools to install per ecosystem (e.g. {"python": ["pdm"]})',
    )
    runtimes: dict[Ecosystem, list[PackageSpec]] = Field(
        default_factory=dict,
        description='Language runtimes to install per ecosystem (e.g. {"python": ["3.12"]})',
    )
    scm: dict[Ecosystem, list[PackageSpec]] = Field(
        default_factory=dict,
        description='SCM repositories to clone per ecosystem (e.g. {"git": ["https://github.com/org/repo"]})',
    )
    preferences: dict[Ecosystem, str] = Field(
        default_factory=dict,
        description='Preferred installer per ecosystem (e.g. {"python": "uv"})',
    )
    groups: dict[GroupName, ManifestGroup] = Field(
        default_factory=dict,
        description='Optional entry groups. Entries that name a group are skipped unless the group is selected '
        '(e.g. {"maintainer": {"description": "Release tools"}})',
    )

    @model_validator(mode='before')
    @classmethod
    def _strip_schema_key(cls, data: Any) -> Any:
        """Strip ``$schema`` from input data so editors can include a schema URL.

        The ``$schema`` key is a JSON Schema convention for pointing editors
        at the schema that validates the file.  It is not part of the manifest
        domain model, so we silently remove it before Pydantic validation
        (which would otherwise reject it due to ``extra='forbid'``).
        """
        if isinstance(data, dict) and '$schema' in data:
            return {k: v for k, v in data.items() if k != '$schema'}
        return data

    @model_validator(mode='after')
    def _runtime_only_on_packages_and_tools(self) -> SetupManifest:
        """Reject ``runtime`` on runtime and SCM entries, where it has no meaning."""
        for kind, ecosystem, packages in self.iter_sections():
            if kind in {PluginKind.PACKAGE, PluginKind.TOOL}:
                continue
            for index, spec in enumerate(packages):
                if spec.runtime is not None:
                    raise ValueError(f"{kind.value}.{ecosystem}[{index}]: 'runtime' applies only to packages and tools")
        return self

    @model_validator(mode='after')
    def _groups_are_declared(self) -> SetupManifest:
        """Reject entries that name a group missing from ``groups``."""
        for kind, ecosystem, packages in self.iter_sections():
            for index, spec in enumerate(packages):
                if spec.group is not None and spec.group not in self.groups:
                    raise ValueError(
                        f"{kind.value}.{ecosystem}[{index}]: group '{spec.group}' is not declared in 'groups'"
                    )
        return self

    def select_groups(self, selected: Collection[str]) -> SetupManifest:
        """Return a copy without the entries of optional groups that are not *selected*.

        Entries without a group are always kept. Unknown names in *selected* are ignored.
        """
        update: dict[str, dict[Ecosystem, list[PackageSpec]]] = {}
        for kind in PluginKind:
            section: dict[Ecosystem, list[PackageSpec]] | None = getattr(self, kind.value, None)
            if not section:
                continue
            filtered = {
                ecosystem: kept
                for ecosystem, packages in section.items()
                if (kept := [spec for spec in packages if spec.group is None or spec.group in selected])
            }
            if filtered != section:
                update[kind.value] = filtered
        return self.model_copy(update=update) if update else self

    def group_entries(self, group: str) -> list[PackageSpec]:
        """Return the entries of *group* that apply to the current platform."""
        return [
            spec
            for _kind, _ecosystem, packages in self.iter_sections()
            for spec in packages
            if spec.group == group and spec.is_applicable()
        ]

    def iter_sections(
        self,
    ) -> Iterator[tuple[PluginKind, Ecosystem, list[PackageSpec]]]:
        """Yield `(kind, ecosystem, packages)` for every non-empty section.

        Replaces the repeated `for kind in PluginKind: getattr(…)`
        pattern used throughout the sync engine.
        """
        for kind in PluginKind:
            section: dict[Ecosystem, list[PackageSpec]] = getattr(self, kind.value, {})
            for ecosystem, packages in section.items():
                yield kind, ecosystem, packages


def entry_id(
    manifest_path: Path | None,
    kind: PluginKind,
    ecosystem: Ecosystem,
    spec: PackageSpec,
) -> str:
    """Stable identity of a manifest entry.

    Built from the manifest file, kind, ecosystem, package name, installer and
    runtime, so it survives constraint and description edits and reordering.
    """
    name = canonicalize_name(spec.name.name) if ecosystem == 'python' else spec.name.name.lower()
    manifest = os.path.normcase(str(manifest_path.resolve())) if manifest_path is not None else ''
    key = '\0'.join((
        manifest,
        kind.value,
        ecosystem,
        name,
        spec.installer or '',
        spec.runtime or '',
    ))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(slots=True)
class ManifestResult:
    """Result of locating and loading a manifest.

    Separates the physical manifest file from the logical project root.
    For a native ``porringer.json`` the root is the file's parent.
    For a reference from ``pyproject.toml`` (via ``manifest = "path"``) the
    root is the referencing file's parent while ``manifest_path`` points to
    the resolved target.

    Args:
        manifest_path: Absolute path to the file that was actually parsed
            as a ``SetupManifest``.
        root_directory: Logical project root — the directory that downstream
            phases use as the working directory.
        manifest: The parsed manifest data.
    """

    manifest_path: Path
    root_directory: Path
    manifest: SetupManifest


@dataclass(frozen=True, slots=True)
class ManifestGroupInfo:
    """An optional entry group declared by a manifest and whether it was selected.

    Args:
        name: Group name, as passed to ``--with``.
        description: Human-readable purpose of the group.
        selected: Whether the group's entries are part of the plan.
        entries: Number of the group's entries that apply to this platform.
    """

    name: str
    description: str | None = None
    selected: bool = False
    entries: int = 0


@dataclass(slots=True)
class ManifestMetadata:
    """Display metadata from a setup manifest.

    Carries optional human-readable information for GUI consumers
    (e.g. install preview screens).

    Args:
        name: Human-readable project/environment name.
        description: Short description shown in the install preview header.
        author: Author or organization name.
        url: Project URL for reference.
        groups: Optional entry groups the manifest declares.
    """

    name: str | None = None
    description: str | None = None
    author: str | None = None
    url: str | None = None
    groups: tuple[ManifestGroupInfo, ...] = ()
