"""Helpers for test manifest schema.

Test PackageSpec models, schema export, and strict field validation.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from porringer.backend.command.manifest import find_manifest
from porringer.backend.command.sync import SyncCommands
from porringer.core.schema import Ecosystem
from porringer.schema import ManifestValidationCode, PackageSpec, SetupManifest
from porringer.schema.manifest import MANIFEST_SCHEMA_DIALECT, MANIFEST_SCHEMA_URL
from porringer.utility.exception import ManifestError

_PY = Ecosystem('python')

TWO_PACKAGES = 2
THREE_PACKAGES = 3


class TestPackageSpec:
    """Tests for PackageSpec model and string/object coercion."""

    @staticmethod
    def test_manifest_coerces_string_packages() -> None:
        """String package entries are coerced to PackageSpec objects."""
        manifest = SetupManifest(packages={_PY: ['requests', 'flask']})
        assert len(manifest.packages[_PY]) == TWO_PACKAGES
        assert str(manifest.packages[_PY][0].name) == 'requests'
        assert manifest.packages[_PY][0].description is None

    @staticmethod
    def test_manifest_accepts_object_packages() -> None:
        """Object package entries are parsed as PackageSpec."""
        manifest = SetupManifest(packages={_PY: [{'name': 'ruff', 'description': 'Fast linter'}]})
        assert str(manifest.packages[_PY][0].name) == 'ruff'
        assert manifest.packages[_PY][0].description == 'Fast linter'

    @staticmethod
    def test_manifest_mixed_string_and_object_packages() -> None:
        """Manifest accepts a mix of string and object package entries."""
        manifest = SetupManifest(
            packages={
                _PY: [
                    'requests',
                    {'name': 'ruff', 'description': 'Fast linter'},
                    'pytest',
                ]
            }
        )
        pkgs = manifest.packages[_PY]
        assert len(pkgs) == THREE_PACKAGES
        assert str(pkgs[0].name) == 'requests'
        assert pkgs[0].description is None
        assert str(pkgs[1].name) == 'ruff'
        assert pkgs[1].description == 'Fast linter'
        assert str(pkgs[2].name) == 'pytest'
        assert pkgs[2].description is None

    @staticmethod
    def test_is_applicable_no_platforms() -> None:
        """PackageSpec with no platforms should apply to all platforms."""
        spec = PackageSpec(name='requests')
        assert spec.is_applicable() is True

    @staticmethod
    def test_is_applicable_with_empty_platforms() -> None:
        """PackageSpec with empty platforms list should apply to all platforms."""
        spec = PackageSpec(name='requests', platforms=[])
        assert spec.is_applicable() is True

    @staticmethod
    def test_is_applicable_matching_platform() -> None:
        """PackageSpec should apply when current platform is in the list."""
        spec = PackageSpec(name='pywin32', platforms=[sys.platform])
        assert spec.is_applicable() is True

    @staticmethod
    def test_is_applicable_non_matching_platform() -> None:
        """PackageSpec should not apply when current platform is not in the list."""
        spec = PackageSpec(name='pywin32', platforms=['nonexistent_platform'])
        assert spec.is_applicable() is False

    @staticmethod
    def test_is_applicable_multiple_platforms_matching() -> None:
        """PackageSpec should apply when current platform is one of multiple."""
        spec = PackageSpec(name='uvloop', platforms=['win32', 'darwin', 'linux', sys.platform])
        assert spec.is_applicable() is True

    @staticmethod
    def test_is_applicable_multiple_platforms_not_matching() -> None:
        """PackageSpec should not apply when current platform is not in multiple."""
        spec = PackageSpec(name='uvloop', platforms=['nonexistent1', 'nonexistent2'])
        assert spec.is_applicable() is False

    @staticmethod
    def test_string_coercion_has_empty_platforms() -> None:
        """String package entries should have empty platforms (all platforms)."""
        manifest = SetupManifest(packages={_PY: ['requests']})
        assert manifest.packages[_PY][0].platforms == []
        assert manifest.packages[_PY][0].is_applicable() is True

    @staticmethod
    def test_object_with_platforms_parsed() -> None:
        """Object package entries with platforms should be parsed correctly."""
        manifest = SetupManifest(packages={_PY: [{'name': 'pywin32', 'platforms': ['win32']}]})
        spec = manifest.packages[_PY][0]
        assert str(spec.name) == 'pywin32'
        assert spec.platforms == ['win32']


class TestManifestSchema:
    """Tests for manifest_schema() export."""

    @staticmethod
    def test_manifest_schema_returns_dict() -> None:
        """manifest_schema() returns a valid JSON Schema dict."""
        schema = SyncCommands.manifest_schema()

        assert isinstance(schema, dict)
        assert 'properties' in schema

    @staticmethod
    def test_manifest_schema_contains_expected_fields() -> None:
        """Exported schema contains the main manifest fields."""
        schema = SyncCommands.manifest_schema()
        props = schema['properties']

        assert 'version' in props
        assert 'packages' in props
        assert 'tools' in props
        assert 'runtimes' in props
        assert 'preferences' in props

    @staticmethod
    def test_manifest_schema_has_root_meta_fields() -> None:
        """Exported schema contains $schema and $id root meta-fields."""
        schema = SyncCommands.manifest_schema()

        assert schema['$schema'] == MANIFEST_SCHEMA_DIALECT
        assert schema['$id'] == MANIFEST_SCHEMA_URL

    @staticmethod
    def test_manifest_schema_has_dollar_schema_property() -> None:
        """Exported schema exposes $schema as an optional property for editor support."""
        schema = SyncCommands.manifest_schema()
        props = schema['properties']

        assert '$schema' in props
        assert props['$schema']['type'] == 'string'
        assert props['$schema']['format'] == 'uri'

    @staticmethod
    def test_manifest_schema_package_spec_anyof() -> None:
        """PackageSpec definition uses anyOf to allow string shorthand."""
        schema = SyncCommands.manifest_schema()
        defs = schema.get('$defs', {})

        assert 'PackageSpec' in defs
        package_spec = defs['PackageSpec']
        assert 'anyOf' in package_spec

        type_kinds = [alt.get('type') for alt in package_spec['anyOf']]
        assert 'string' in type_kinds

    @staticmethod
    def test_manifest_schema_package_ref_anyof() -> None:
        """PackageRef definition uses anyOf to allow string shorthand."""
        schema = SyncCommands.manifest_schema()
        defs = schema.get('$defs', {})

        assert 'PackageRef' in defs
        package_ref = defs['PackageRef']
        assert 'anyOf' in package_ref

        type_kinds = [alt.get('type') for alt in package_ref['anyOf']]
        assert 'string' in type_kinds

    @staticmethod
    def test_manifest_accepts_dollar_schema_field() -> None:
        """SetupManifest accepts $schema in input without raising ValidationError."""
        manifest = SetupManifest.model_validate({
            '$schema': MANIFEST_SCHEMA_URL,
            'version': '1',
            'packages': {'python': ['pytest']},
        })

        assert manifest.version == '1'
        assert len(manifest.packages) == 1

    @staticmethod
    def test_manifest_rejects_unknown_extra_fields() -> None:
        """SetupManifest still rejects arbitrary unknown fields (extra='forbid')."""
        with pytest.raises(ValidationError):
            SetupManifest.model_validate({
                'version': '1',
                'not_a_real_field': 'should fail',
            })


class TestPackageSpecExtras:
    """Tests for PEP 508 extras preservation on PackageSpec."""

    @staticmethod
    def test_package_spec_extras_preserved() -> None:
        """PackageSpec preserves PEP 508 extras on the package itself."""
        manifest = SetupManifest(packages={_PY: ['requests[security]']})
        pkg = manifest.packages[_PY][0]
        assert pkg.name.name == 'requests'
        assert pkg.name.extras == ('security',)
        assert pkg.name.specifier == 'requests[security]'


class TestStrictFieldValidation:
    """Tests that unknown/misspelled fields are rejected by manifest models.

    All manifest models use ``extra='forbid'`` so that typos in field
    names surface immediately rather than being silently ignored.
    """

    @staticmethod
    def test_setup_manifest_rejects_unknown_top_level_field() -> None:
        """SetupManifest raises ValidationError for unrecognised top-level keys."""
        with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
            SetupManifest.model_validate({'version': '1', 'packges': {'python': ['requests']}})

    @staticmethod
    def test_setup_manifest_rejects_multiple_unknown_fields() -> None:
        """SetupManifest reports all unknown fields, not just the first."""
        with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
            SetupManifest.model_validate({'version': '1', 'nme': 'test', 'descrption': 'oops'})

    @staticmethod
    def test_package_spec_rejects_unknown_field() -> None:
        """PackageSpec raises ValidationError for unknown keys."""
        with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
            PackageSpec.model_validate({'name': 'requests', 'vrsion': '1.0'})

    @staticmethod
    def test_nested_package_spec_unknown_field_in_manifest() -> None:
        """Unknown fields inside nested PackageSpec entries are rejected."""
        data = {
            'version': '1',
            'packages': {
                'python': [
                    {'name': 'requests', 'unknwon_key': 'value'},
                ]
            },
        }
        with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
            SetupManifest.model_validate(data)

    @staticmethod
    def test_valid_manifest_still_accepted() -> None:
        """A well-formed manifest with only known fields parses successfully."""
        data = {
            'version': '1',
            'name': 'Test',
            'description': 'A test manifest',
            'packages': {'python': ['requests']},
            'tools': {'python': [{'name': 'pdm'}]},
        }
        manifest = SetupManifest.model_validate(data)
        assert manifest.name == 'Test'
        assert len(manifest.packages[_PY]) == 1

    @staticmethod
    def test_loader_wraps_unknown_field_as_manifest_error() -> None:
        """_load_native_manifest wraps ValidationError into ManifestError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {'version': '1', 'packges': {'python': ['requests']}}
            manifest_path.write_text(json.dumps(manifest_data))

            with pytest.raises(ManifestError) as exc_info:
                find_manifest(manifest_path)

            assert isinstance(exc_info.value, ManifestError)
            assert exc_info.value.code == ManifestValidationCode.SCHEMA_INVALID

    @staticmethod
    def test_removed_plugins_field_gives_migration_guidance() -> None:
        """Manifests using the removed 'plugins' field get a targeted diagnostic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {
                'version': '1',
                'tools': {'python': [{'name': 'pdm', 'plugins': ['cppython']}]},
            }
            manifest_path.write_text(json.dumps(manifest_data))

            with pytest.raises(ManifestError) as exc_info:
                find_manifest(manifest_path)

            assert exc_info.value.code == ManifestValidationCode.REMOVED_FIELD
            assert 'plugins' in str(exc_info.value)
            assert 'build-system' in str(exc_info.value)


_NODE = Ecosystem('node')
_GIT = Ecosystem('git')

_REPRESENTATIVE_MANIFESTS: list[SetupManifest] = [
    SetupManifest(),
    SetupManifest(
        name='Demo',
        description='A representative manifest',
        author='Synodic',
        url='https://example.com/project',
        packages={
            _PY: ['flask', {'name': 'ruff>=0.8.0', 'description': 'Fast linter'}],
            _NODE: ['typescript@latest'],
        },
        preferences={_PY: 'uv'},
    ),
    SetupManifest(
        tools={
            _PY: [
                {
                    'name': 'pdm',
                }
            ]
        },
        scm={_GIT: ['https://github.com/synodic/porringer']},
    ),
    SetupManifest(
        packages={_PY: [{'name': 'requests[security]', 'platforms': ['win32', 'linux']}]},
    ),
]


class TestManifestRoundTrip:
    """Serialization round-trip invariants for ``SetupManifest``."""

    @staticmethod
    @pytest.mark.parametrize(
        'manifest',
        _REPRESENTATIVE_MANIFESTS,
        ids=['empty', 'metadata-packages', 'tools-scm', 'platform-scoped'],
    )
    def test_json_dump_validate_round_trip(manifest: SetupManifest) -> None:
        """``model_dump(mode='json')`` then ``model_validate`` restores an equal manifest.

        Serialization and validation are inverse operations, so a manifest
        survives a JSON round-trip unchanged.  Re-dumping the reloaded
        manifest yields an identical structure (a serialization fixed
        point), guarding against asymmetric dump/validate drift.
        """
        dumped = manifest.model_dump(mode='json')
        reloaded = SetupManifest.model_validate(dumped)
        assert reloaded == manifest
        assert reloaded.model_dump(mode='json') == dumped
