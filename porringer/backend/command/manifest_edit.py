"""Edit native ``porringer.json`` manifests in place.

Edits keep the file's key order, ``$schema`` and indentation, are checked
against the manifest schema before writing, and replace the file atomically.
Only ``porringer.json`` files are edited; manifests embedded in other files
(such as ``[tool.porringer]`` in ``pyproject.toml``) are left to the user.
"""

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import ValidationError

from porringer.core.schema import Ecosystem, PluginKind
from porringer.schema import ManifestEntry, PackageSpec, SetupManifest, entry_id
from porringer.utility.exception import ManifestError, ManifestValidationCode

from .manifest import find_manifest

NATIVE_MANIFEST = 'porringer.json'

_EDITABLE_KINDS = (
    PluginKind.PACKAGE,
    PluginKind.TOOL,
    PluginKind.RUNTIME,
    PluginKind.SCM,
)


def editable_manifest_path(target: Path) -> Path:
    """Resolve *target* to the ``porringer.json`` an edit should change.

    A directory without any manifest resolves to a new ``porringer.json`` in it.

    Raises:
        ManifestError: If the manifest in use is embedded in another file.
    """
    if target.exists():
        try:
            located = find_manifest(target).manifest_path
        except ManifestError:
            if target.is_dir():
                return target / NATIVE_MANIFEST
            raise
        if located.suffix.lower() != '.json':
            raise ManifestError(
                f'{located} is not a porringer.json manifest; edit it by hand',
                code=ManifestValidationCode.LOAD_FAILED,
            )
        return located
    if target.suffix.lower() != '.json':
        raise ManifestError(f'Path does not exist: {target}', code=ManifestValidationCode.PATH_NOT_FOUND)
    return target


def _ref_text(spec: PackageSpec) -> str:
    """Render the entry's name and constraint the way it is written in a manifest."""
    ref = spec.name
    name = f'{ref.name}[{",".join(ref.extras)}]' if ref.extras else ref.name
    if not ref.constraint:
        return name
    try:
        SpecifierSet(ref.constraint)
    except InvalidSpecifier:
        # Non-PEP 440 constraints (npm and similar) are written as name@constraint.
        return f'{name}@{ref.constraint}'
    return f'{name}{ref.constraint}'


def serialize_spec(spec: PackageSpec) -> str | dict[str, Any]:
    """Return the shortest manifest form of *spec*: a string unless it has more than a name."""
    data: dict[str, Any] = {'name': _ref_text(spec)}
    if spec.description is not None:
        data['description'] = spec.description
    if spec.platforms:
        data['platforms'] = list(spec.platforms)
    if spec.include_prereleases:
        data['include_prereleases'] = True
    if spec.installer is not None:
        data['installer'] = spec.installer
    if spec.runtime is not None:
        data['runtime'] = spec.runtime
    return data['name'] if len(data) == 1 else data


def bumped_constraint(constraint: str | None, version: str) -> str | None:
    """Move *constraint* up to *version*, keeping a single ``==``, ``~=`` or ``>=`` operator."""
    if not constraint:
        return None
    try:
        specifiers = list(SpecifierSet(constraint))
    except InvalidSpecifier:
        return f'>={version}'
    if len(specifiers) == 1 and specifiers[0].operator in {'==', '~=', '>='}:
        return f'{specifiers[0].operator}{version}'
    return f'>={version}'


def _detect_indent(text: str) -> int:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(' ')
        if stripped and len(stripped) != len(line):
            return len(line) - len(stripped)
    return 4


class ManifestDocument:
    """A ``porringer.json`` loaded for editing."""

    def __init__(self, path: Path) -> None:
        """Load *path*, or start an empty manifest when it doesn't exist yet."""
        self.path = path
        if path.exists():
            text = path.read_text(encoding='utf-8')
            self.data: dict[str, Any] = json.loads(text)
            self._indent = _detect_indent(text)
        else:
            self.data = {'version': '1'}
            self._indent = 4

    def entry_id(self, kind: PluginKind, ecosystem: Ecosystem, spec: PackageSpec) -> str:
        """Return the identity *spec* has, or would have, in this manifest."""
        return entry_id(self.path, kind, ecosystem, spec)

    def entries(self) -> Iterator[ManifestEntry]:
        """Yield each entry as written, with its stable identity."""
        for kind in _EDITABLE_KINDS:
            for ecosystem, raw_entries in self.data.get(kind.value, {}).items():
                for raw in raw_entries:
                    spec = PackageSpec.model_validate(raw)
                    yield ManifestEntry(
                        entry_id=self.entry_id(kind, Ecosystem(ecosystem), spec),
                        manifest_path=self.path,
                        kind=kind,
                        ecosystem=ecosystem,
                        spec=spec,
                    )

    def find(self, ids: set[str]) -> dict[str, ManifestEntry]:
        """Return the entries whose ids are in *ids*, keyed by id."""
        return {entry.entry_id: entry for entry in self.entries() if entry.entry_id in ids}

    def upsert(self, kind: PluginKind, ecosystem: Ecosystem, spec: PackageSpec) -> str:
        """Add *spec*, or update the entry with the same identity in place. Returns its entry id."""
        target = self.entry_id(kind, ecosystem, spec)
        section = self.data.setdefault(kind.value, {}).setdefault(ecosystem, [])
        new = serialize_spec(spec)
        for index, raw in enumerate(section):
            if self.entry_id(kind, ecosystem, PackageSpec.model_validate(raw)) != target:
                continue
            if isinstance(raw, dict):
                merged = {**raw, **new} if isinstance(new, dict) else {**raw, 'name': new}
                section[index] = merged['name'] if set(merged) == {'name'} else merged
            else:
                section[index] = new
            return target
        section.append(new)
        return target

    def remove(self, ids: set[str]) -> None:
        """Drop the entries whose ids are in *ids*, and any section left empty."""
        for kind in _EDITABLE_KINDS:
            sections: dict[str, list[Any]] = self.data.get(kind.value, {})
            for ecosystem in list(sections):
                sections[ecosystem] = [
                    raw
                    for raw in sections[ecosystem]
                    if self.entry_id(kind, Ecosystem(ecosystem), PackageSpec.model_validate(raw)) not in ids
                ]
                if not sections[ecosystem]:
                    del sections[ecosystem]
            if kind.value in self.data and not sections:
                del self.data[kind.value]

    def save(self) -> None:
        """Check the edited manifest against the schema, then replace the file atomically.

        Raises:
            ManifestError: If the edit would make the manifest invalid.
        """
        try:
            SetupManifest.model_validate(self.data)
        except ValidationError as exc:
            raise ManifestError(
                f'Edit would make {self.path} invalid: {exc}',
                code=ManifestValidationCode.SCHEMA_INVALID,
            ) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, indent=self._indent, ensure_ascii=False) + '\n'
        fd, temp = tempfile.mkstemp(prefix='.porringer-', suffix='.json', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(text)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
