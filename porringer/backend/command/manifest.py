"""CLI command implementation for manifest.

Manifest loading, parsing, and validation.

Handles finding, loading, and validating porringer manifests.
Supports three modes:

1. **Native** — a standalone ``porringer.json`` file.
2. **Inline embed** — a ``[tool.porringer]`` (or equivalent) section
   inside a host config file (``pyproject.toml``, ``package.json``),
   contributed by environment plugins via the
   ``ManifestContributor`` protocol.
3. **Reference** — a host config section containing only a
   ``manifest = "relative/path.json"`` key that redirects to an
   external manifest file.

Extracted from the monolithic ``sync`` module for clarity.
"""

import json
import logging
import tomllib
from collections.abc import Callable
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import ValidationError

from porringer.backend.backend import BackendResolver
from porringer.core.plugin_schema.manifest import ManifestContributor
from porringer.core.schema import ManifestContribution, Plugin, PluginKind
from porringer.schema import (
    ManifestDiagnostic,
    ManifestDiagnosticSeverity,
    ManifestValidationResult,
    SetupManifest,
)
from porringer.schema.manifest import (
    MANIFEST_SCHEMA_DIALECT,
    MANIFEST_SCHEMA_URL,
    ManifestResult,
)
from porringer.utility.exception import ManifestError, ManifestValidationCode

from .core.discovery import (
    discover_all_plugins,
    register_invalidation_hook,
    scan_environment_group,
)

logger = logging.getLogger(__name__)

# The native manifest filename — always probed first before any
# plugin-contributed files.
NATIVE_MANIFEST = 'porringer.json'


# ---------------------------------------------------------------------------
# Plugin-contributed manifest sources
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Manifest contribution cache — invalidated alongside the plugin scan cache
# ---------------------------------------------------------------------------

_manifest_contributions: dict[str, tuple[ManifestContribution, ...]] = {}


def invalidate_manifest_cache() -> None:
    """Clear the cached manifest contributions.

    Called automatically when :func:`invalidate_plugin_cache` fires
    (via the registered hook).  Can also be called directly.
    """
    _manifest_contributions.pop('value', None)


# Wire into the plugin scan invalidation so that newly installed
# environment plugins (and their manifest contributions)
# are visible after a cache clear.
register_invalidation_hook(invalidate_manifest_cache)


def collect_manifest_contributions() -> tuple[ManifestContribution, ...]:
    """Discover manifest contributions from all installed environment plugins.

    Scans the single ``porringer.environment`` entry-point group, calls
    ``manifest_contribution()`` on each class that implements
    ``ManifestContributor``, and returns a deduplicated tuple of
    contributions ordered by first occurrence.

    The result is cached until :func:`invalidate_plugin_cache` (or
    :func:`invalidate_manifest_cache`) is called.

    Returns:
        Unique ``ManifestContribution`` instances contributed by plugins.
    """
    if 'value' in _manifest_contributions:
        return _manifest_contributions['value']

    seen_filenames: set[str] = set()
    contributions: list[ManifestContribution] = []

    env_infos, proj_infos, _ = scan_environment_group()
    for info in [*env_infos, *proj_infos]:
        cls = info.type
        if isinstance(cls, type) and issubclass(cls, ManifestContributor):
            contrib = cls.manifest_contribution()
            if contrib is not None and contrib.filename not in seen_filenames:
                seen_filenames.add(contrib.filename)
                contributions.append(contrib)

    result = tuple(contributions)
    _manifest_contributions['value'] = result
    return result


def manifest_filenames() -> tuple[str, ...]:
    """Return all recognised manifest filenames, native first.

    The first element is always ``'porringer.json'``.  Subsequent
    entries are contributed by installed project plugins.

    Returns:
        Ordered tuple of filenames the discovery engine will probe.
    """
    return (NATIVE_MANIFEST,) + tuple(c.filename for c in collect_manifest_contributions())


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------


def _check_removed_plugins_field(data: dict, source_path: Path) -> None:
    """Reject manifests that still use the removed ``plugins`` field.

    Porringer no longer supports native plugin-management entries
    (e.g. ``{"name": "pdm", "plugins": ["cppython"]}``).  Raising a
    targeted error here — before Pydantic's generic ``extra='forbid'``
    rejection — gives users clear migration guidance instead of an
    opaque schema error.

    Args:
        data: The raw manifest dict (already extracted from any host
            config section), prior to ``SetupManifest`` validation.
        source_path: Original file path, for the error message.

    Raises:
        ManifestError: When any package/tool/runtime entry declares a
            ``plugins`` key.
    """
    for section_name in ('packages', 'tools', 'runtimes'):
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for entries in section.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and 'plugins' in entry:
                    raise ManifestError(
                        f"'{section_name}' entry '{entry.get('name', '?')}' in {source_path} uses the removed "
                        "'plugins' field. Native plugin management was removed — declare the sub-package "
                        "directly in the target project's own configuration instead "
                        '(e.g. cppython via [build-system].requires in pyproject.toml).',
                        code=ManifestValidationCode.REMOVED_FIELD,
                    )


def _load_native_manifest(path: Path) -> SetupManifest:
    """Load a native ``porringer.json`` file.

    Args:
        path: Path to the JSON manifest file.

    Returns:
        The parsed manifest.

    Raises:
        ManifestError: On JSON syntax errors or schema violations.
    """
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        _check_removed_plugins_field(data, path)
        return SetupManifest.model_validate(data)
    except json.JSONDecodeError as e:
        raise ManifestError(
            f'Invalid JSON in manifest {path}: {e}',
            code=ManifestValidationCode.SYNTAX_ERROR,
        ) from e
    except ValidationError as e:
        raise ManifestError(
            f'Schema validation failed for manifest {path}: {e}',
            code=ManifestValidationCode.SCHEMA_INVALID,
        ) from e
    except ManifestError:
        raise
    except Exception as e:
        raise ManifestError(
            f'Failed to load manifest {path}: {e}',
            code=ManifestValidationCode.LOAD_FAILED,
        ) from e


def _read_file(path: Path, file_format: str) -> dict:
    """Read and parse a file as TOML or JSON.

    Args:
        path: Path to the file.
        file_format: ``'toml'`` or ``'json'``.

    Returns:
        Parsed data as a dict.

    Raises:
        ManifestError: On parse errors.
    """
    try:
        if file_format == 'toml':
            with open(path, 'rb') as f:
                return tomllib.load(f)
        else:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(
            f'Invalid {file_format.upper()} in {path}: {e}',
            code=ManifestValidationCode.SYNTAX_ERROR,
        ) from e
    except Exception as e:
        raise ManifestError(f'Failed to read {path}: {e}', code=ManifestValidationCode.LOAD_FAILED) from e


def _extract_section(data: dict, config_path: tuple[str, ...], source_path: Path) -> dict:
    """Navigate a nested dict by key path to extract the porringer section.

    Args:
        data: Parsed file data.
        config_path: Key path (e.g. ``('tool', 'porringer')``).
        source_path: Original file path (for error messages).

    Returns:
        The extracted section dict.

    Raises:
        ManifestError: If the key path does not exist.
    """
    current = data
    for key in config_path:
        if not isinstance(current, dict) or key not in current:
            dotted = '.'.join(config_path)
            raise ManifestError(
                f'No [{dotted}] section found in {source_path}',
                code=ManifestValidationCode.NO_MANIFEST,
            )
        current = current[key]

    if not isinstance(current, dict):
        dotted = '.'.join(config_path)
        raise ManifestError(
            f'[{dotted}] in {source_path} is not a table/object',
            code=ManifestValidationCode.SCHEMA_INVALID,
        )

    return current


def _load_embedded_manifest(
    path: Path,
    contribution: ManifestContribution,
) -> ManifestResult:
    """Load a porringer manifest from a host config file.

    Supports two modes:

    * **Inline** — the extracted section is a full manifest and is
      validated directly as ``SetupManifest``.
    * **Reference** — the section contains a ``manifest`` key whose
      string value is a relative path to an external manifest file.
      The referenced file is loaded as native JSON.

    In both cases the ``root_directory`` is the host file's parent.

    Args:
        path: Path to the host config file (e.g. ``pyproject.toml``).
        contribution: The ``ManifestContribution`` describing where
            to find the porringer section.

    Returns:
        A ``ManifestResult`` with the parsed manifest and root.

    Raises:
        ManifestError: On any loading or validation error.
    """
    data = _read_file(path, contribution.file_format)
    section = _extract_section(data, contribution.config_path, path)
    root_directory = path.parent

    # Reference mode: section has a sole "manifest" key pointing elsewhere
    if 'manifest' in section and isinstance(section['manifest'], str):
        ref_path = root_directory / section['manifest']
        if not ref_path.exists():
            raise ManifestError(
                f'Referenced manifest does not exist: {ref_path} (from {path})',
                code=ManifestValidationCode.NO_MANIFEST,
            )
        manifest = _load_native_manifest(ref_path)
        return ManifestResult(
            manifest_path=ref_path.resolve(),
            root_directory=root_directory.resolve(),
            manifest=manifest,
        )

    # Inline mode: the section *is* the manifest
    try:
        _check_removed_plugins_field(section, path)
        manifest = SetupManifest.model_validate(section)
    except ValidationError as e:
        raise ManifestError(
            f'Schema validation failed for manifest in {path}: {e}',
            code=ManifestValidationCode.SCHEMA_INVALID,
        ) from e
    except ManifestError:
        raise
    except Exception as e:
        raise ManifestError(
            f'Invalid manifest in {path}: {e}',
            code=ManifestValidationCode.LOAD_FAILED,
        ) from e

    return ManifestResult(
        manifest_path=path.resolve(),
        root_directory=root_directory.resolve(),
        manifest=manifest,
    )


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def find_manifest(path: Path) -> ManifestResult:
    """Find and load the porringer manifest from the given path.

    Discovery order:

    1. If *path* is a **file**:

       * If the filename is ``porringer.json`` → load as native JSON.
       * Otherwise, match against plugin-contributed filenames and use
         the corresponding ``ManifestContribution`` to extract the section.
       * Fall back to native JSON loading for unrecognised filenames.

    2. If *path* is a **directory**:

       * Try ``porringer.json`` first (native format, always).
       * Then try each plugin-contributed filename in order.
       * Raise ``ManifestError`` if nothing is found.

    Args:
        path: Path to a manifest file or directory containing one.

    Returns:
        A ``ManifestResult`` carrying the parsed manifest, the actual
        manifest file path, and the logical project root directory.

    Raises:
        ManifestError: If no valid manifest can be found.
    """
    if path.is_file():
        return _find_manifest_from_file(path)

    if path.is_dir():
        return _find_manifest_from_directory(path)

    raise ManifestError(f'Path does not exist: {path}', code=ManifestValidationCode.NO_MANIFEST)


def _find_manifest_from_file(path: Path) -> ManifestResult:
    """Load a manifest from an explicit file path."""
    # Native porringer.json
    if path.name == NATIVE_MANIFEST:
        manifest = _load_native_manifest(path)
        resolved = path.resolve()
        return ManifestResult(
            manifest_path=resolved,
            root_directory=resolved.parent,
            manifest=manifest,
        )

    # Check plugin-contributed filenames
    for contrib in collect_manifest_contributions():
        if path.name == contrib.filename:
            return _load_embedded_manifest(path, contrib)

    # Unknown file — attempt native JSON as a fallback
    manifest = _load_native_manifest(path)
    resolved = path.resolve()
    return ManifestResult(
        manifest_path=resolved,
        root_directory=resolved.parent,
        manifest=manifest,
    )


def _find_manifest_from_directory(path: Path) -> ManifestResult:
    """Probe a directory for a manifest, trying native first then contributed."""
    # 1. Native porringer.json
    native = path / NATIVE_MANIFEST
    if native.exists():
        manifest = _load_native_manifest(native)
        resolved = native.resolve()
        return ManifestResult(
            manifest_path=resolved,
            root_directory=resolved.parent,
            manifest=manifest,
        )

    # 2. Plugin-contributed files
    contributions = collect_manifest_contributions()
    for contrib in contributions:
        candidate = path / contrib.filename
        if candidate.exists():
            try:
                return _load_embedded_manifest(candidate, contrib)
            except ManifestError as exc:
                # File exists but has no porringer section — skip to next.
                # Re-raise syntax / load errors so the caller sees them.
                if exc.code != ManifestValidationCode.NO_MANIFEST:
                    raise
                continue

    # Nothing found
    tried = ', '.join(f"'{f}'" for f in manifest_filenames())
    raise ManifestError(
        f'No manifest found in directory: {path}. Tried: {tried}',
        code=ManifestValidationCode.NO_MANIFEST,
    )


def has_manifest(path: Path) -> bool:
    """Check whether a path resolves to a valid porringer manifest.

    A lightweight existence + parsability check without deep validation
    (no plugin resolution, no PEP 440 checks).

    Args:
        path: Path to a manifest file or directory containing one.

    Returns:
        ``True`` if a manifest can be found and loaded, ``False`` otherwise.
    """
    try:
        find_manifest(path)
    except ManifestError:
        return False
    return True


def validate_manifest(path: Path) -> ManifestValidationResult:
    """Validate a manifest for errors without executing any operations.

    Checks syntax, schema version, required fields, plugin availability,
    package-name validity, and duplicate packages across plugins.

    Args:
        path: Path to a manifest file or directory containing one.

    Returns:
        Structured validation result with diagnostics.
    """
    diagnostics: list[ManifestDiagnostic] = []

    def _error(field: str, message: str, code: ManifestValidationCode) -> None:
        diagnostics.append(ManifestDiagnostic(field, message, code, ManifestDiagnosticSeverity.ERROR))

    def _warning(field: str, message: str, code: ManifestValidationCode) -> None:
        diagnostics.append(ManifestDiagnostic(field, message, code, ManifestDiagnosticSeverity.WARNING))

    manifest = _load_manifest_for_validation(path, _error)
    if manifest is None:
        return ManifestValidationResult(diagnostics=diagnostics)

    _validate_schema_version(manifest, _error)

    plugins = discover_all_plugins(use_cache=True)
    all_plugins: dict[str, Plugin] = {
        **plugins.environments,
        **plugins.project_environments,
        **plugins.scm_environments,
    }
    resolver = BackendResolver(all_plugins, manifest.preferences)

    _validate_backends(manifest, resolver, _error, _warning)
    _validate_package_names(manifest, resolver, _warning)
    _validate_duplicate_packages(manifest, _warning)

    return ManifestValidationResult(diagnostics=diagnostics)


def _load_manifest_for_validation(
    path: Path,
    error_callback: Callable[[str, str, ManifestValidationCode], None],
) -> SetupManifest | None:
    """Load a manifest for validation or record diagnostics."""
    if not path.exists():
        error_callback(
            'path',
            f'Path does not exist: {path}',
            ManifestValidationCode.PATH_NOT_FOUND,
        )
        return None

    try:
        result = find_manifest(path)
    except ManifestError as exc:
        code = _map_manifest_error_code(exc)
        error_callback('', str(exc), code)
        return None

    return result.manifest


def _map_manifest_error_code(error: ManifestError) -> ManifestValidationCode:
    """Map a manifest loading error to a validation code.

    Uses the structured `ManifestError.code` when available;
    falls back to substring matching for errors without one.
    """
    if error.code is not None:
        return error.code

    # Legacy fallback: substring matching for errors without a code.
    message = str(error)
    if 'No manifest found' in message or 'No [tool.porringer]' in message:
        return ManifestValidationCode.NO_MANIFEST
    if 'Invalid JSON' in message or 'Invalid TOML' in message:
        return ManifestValidationCode.SYNTAX_ERROR
    return ManifestValidationCode.SCHEMA_INVALID


def _validate_schema_version(
    manifest: SetupManifest,
    error_callback: Callable[[str, str, ManifestValidationCode], None],
) -> None:
    """Validate manifest schema version."""
    supported_versions = {'1'}
    if manifest.version not in supported_versions:
        error_callback(
            'version',
            f"Unsupported schema version '{manifest.version}'. Supported: {', '.join(sorted(supported_versions))}",
            ManifestValidationCode.UNSUPPORTED_VERSION,
        )


def _validate_backends(
    manifest: SetupManifest,
    resolver: BackendResolver,
    error_callback: Callable[[str, str, ManifestValidationCode], None],
    warning_callback: Callable[[str, str, ManifestValidationCode], None] | None = None,
) -> None:
    """Validate that each (kind, ecosystem) in the manifest resolves to a plugin.

    `TOOL` and `RUNTIME` kinds are allowed to have no resolver at
    validation time because the prerequisite tool may be installed by
    an earlier phase during execution.  For those kinds a *warning* is
    emitted (via *warning_callback*) instead of an error.
    """
    # Kinds whose backends may be deferred to a later phase.
    _DEFERRABLE_KINDS = {PluginKind.TOOL, PluginKind.RUNTIME}

    for kind, ecosystem, packages in manifest.iter_sections():
        registered = resolver.registered_names(kind, ecosystem)
        for index, spec in enumerate(packages):
            if spec.installer is not None and spec.installer not in registered:
                error_callback(
                    f'{kind.value}.{ecosystem}[{index}].installer',
                    f"Installer '{spec.installer}' does not install {kind.value} for '{ecosystem}' "
                    f'(choices: {", ".join(sorted(registered)) or "none registered"})',
                    ManifestValidationCode.UNKNOWN_PLUGIN,
                )
        installer = resolver.resolve(kind, ecosystem)
        if installer is None:
            if kind in _DEFERRABLE_KINDS and warning_callback is not None:
                warning_callback(
                    f'{kind.value}.{ecosystem}',
                    f"No installer currently available for ({kind.value}, '{ecosystem}'); "
                    'may be resolved after an earlier phase installs the prerequisite',
                    ManifestValidationCode.UNKNOWN_PLUGIN,
                )
            else:
                error_callback(
                    f'{kind.value}.{ecosystem}',
                    f"No available installer for ({kind.value}, '{ecosystem}')",
                    ManifestValidationCode.UNKNOWN_PLUGIN,
                )


def _validate_package_names(
    manifest: SetupManifest,
    resolver: BackendResolver,
    warning_callback: Callable[[str, str, ManifestValidationCode], None],
) -> None:
    """Validate package specifiers in a manifest.

    PEP 440 validation is applied when the resolved plugin declares
    `package_name_validator() == 'pep440'`.  Other ecosystems
    accept any non-empty package name.
    """
    for kind, ecosystem, packages in manifest.iter_sections():
        validator = resolver.validator_for(kind, ecosystem)

        for j, spec in enumerate(packages):
            name_str = str(spec.name)
            if not name_str.strip():
                warning_callback(
                    f'{kind.value}.{ecosystem}[{j}].name',
                    'Empty package name',
                    ManifestValidationCode.INVALID_PACKAGE_NAME,
                )
                continue
            if validator == 'pep440':
                try:
                    Requirement(name_str)
                except InvalidRequirement as exc:
                    warning_callback(
                        f'{kind.value}.{ecosystem}[{j}].name',
                        f"Invalid package specifier '{spec.name}': {exc}",
                        ManifestValidationCode.INVALID_PACKAGE_NAME,
                    )


def _validate_duplicate_packages(
    manifest: SetupManifest,
    warning_callback: Callable[[str, str, ManifestValidationCode], None],
) -> None:
    """Warn when packages appear under multiple sections."""
    seen: dict[str, list[str]] = {}
    for kind, ecosystem, packages in manifest.iter_sections():
        label = f'{kind.value}.{ecosystem}'
        for spec in packages:
            canonical = str(canonicalize_name(spec.name.name))
            seen.setdefault(canonical, []).append(label)

    for pkg_name, locations in seen.items():
        if len(locations) > 1:
            warning_callback(
                locations[0],
                f"Package '{pkg_name}' is listed under multiple sections: {', '.join(locations)}",
                ManifestValidationCode.DUPLICATE_PACKAGE,
            )


def manifest_schema() -> dict:
    """Export a JSON Schema representation of the manifest format.

    Generates a standard JSON Schema from the ``SetupManifest`` Pydantic
    model, then post-processes it so that:

    * Root ``$schema`` and ``$id`` meta-fields are present.
    * A ``$schema`` property is exposed so editors can auto-detect the schema.
    * ``PackageRef`` and ``PackageSpec`` definitions accept
      both the **string shorthand** and the full **object form** via ``anyOf``.

    Returns:
        A dict containing the JSON Schema for ``SetupManifest``.
    """
    schema = SetupManifest.model_json_schema()

    # --- Root meta-fields ---
    schema['$schema'] = MANIFEST_SCHEMA_DIALECT
    schema['$id'] = MANIFEST_SCHEMA_URL

    # --- Expose $schema as an optional property ---
    if 'properties' in schema:
        schema['properties']['$schema'] = {
            'type': 'string',
            'format': 'uri',
            'description': 'URL pointing to the JSON Schema for this manifest',
        }

    # --- String-shorthand support for models with _coerce_string ---
    _patch_string_shorthand(schema)

    return schema


def _patch_string_shorthand(schema: dict) -> None:
    """Wrap ``$defs`` entries that accept string shorthand in ``anyOf``.

    Models like ``PackageRef`` and ``PackageSpec`` use a
    Pydantic ``model_validator(mode='before')`` to coerce plain strings
    into their object form at runtime.  JSON Schema has no way to express
    that automatically, so we patch the generated ``$defs`` to use
    ``anyOf: [{type: string}, {<original object schema>}]``.
    """
    defs = schema.get('$defs', {})

    _targets = {
        'PackageRef': 'Package specifier string (e.g. "requests", "ruff>=0.8.0", "@biomejs/biome@^1.0")',
        'PackageSpec': 'Package specifier string (e.g. "pytest", "ruff>=0.8.0")',
    }

    for name, string_description in _targets.items():
        if name not in defs:
            continue

        original = defs[name]

        # Already patched (idempotent guard)
        if 'anyOf' in original:
            continue

        # Pull out the object schema, preserving title/description at the top level
        title = original.get('title', name)
        description = original.get('description', '')

        defs[name] = {
            'title': title,
            'description': description,
            'anyOf': [
                {'type': 'string', 'description': string_description},
                {k: v for k, v in original.items() if k not in {'title', 'description'}},
            ],
        }
