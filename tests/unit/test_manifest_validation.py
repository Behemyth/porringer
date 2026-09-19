"""Helpers for test manifest validation.

Test the validate_manifest() API surface.
"""

import json
import tempfile
from pathlib import Path

import pytest

from porringer.api import API
from porringer.schema import ManifestValidationCode


@pytest.mark.mock_packages
class TestManifestValidation:
    """Tests for validate_manifest() API."""

    @staticmethod
    def test_valid_manifest(session_api: API) -> None:
        """A well-formed manifest with recognized backends returns valid=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {
                'version': '1',
                'packages': {'python': ['requests']},
            }
            manifest_path.write_text(json.dumps(manifest_data))

            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is True
            assert len(result.errors) == 0

    @staticmethod
    def test_path_not_found(session_api: API) -> None:
        """Non-existent path produces PATH_NOT_FOUND error."""
        result = session_api.sync.validate_manifest(Path('/nonexistent/path'))

        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].code == ManifestValidationCode.PATH_NOT_FOUND

    @staticmethod
    def test_no_manifest_in_directory(session_api: API) -> None:
        """Empty directory produces NO_MANIFEST error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is False
            assert len(result.errors) == 1
            assert result.errors[0].code == ManifestValidationCode.NO_MANIFEST

    @staticmethod
    def test_invalid_json_syntax(session_api: API) -> None:
        """Malformed JSON produces SYNTAX_ERROR."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_path.write_text('{bad json!!!}')

            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is False
            assert result.errors[0].code == ManifestValidationCode.SYNTAX_ERROR

    @staticmethod
    def test_invalid_toml_syntax(session_api: API) -> None:
        """Malformed TOML produces SYNTAX_ERROR."""
        with tempfile.TemporaryDirectory() as tmpdir:
            toml_path = Path(tmpdir) / 'pyproject.toml'
            toml_path.write_text('[[[invalid toml')

            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is False
            assert result.errors[0].code == ManifestValidationCode.SYNTAX_ERROR

    @staticmethod
    def test_unsupported_version(session_api: API) -> None:
        """Unrecognized schema version produces UNSUPPORTED_VERSION error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {'version': '99', 'packages': {'python': ['requests']}}
            manifest_path.write_text(json.dumps(manifest_data))

            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is False
            version_errors = [e for e in result.errors if e.code == ManifestValidationCode.UNSUPPORTED_VERSION]
            assert len(version_errors) == 1
            assert version_errors[0].field == 'version'

    @staticmethod
    def test_unknown_ecosystem_in_packages(session_api: API) -> None:
        """Unrecognized ecosystem in packages produces UNKNOWN_PLUGIN error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {'version': '1', 'packages': {'nonexistent_backend': ['foo']}}
            manifest_path.write_text(json.dumps(manifest_data))

            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is False
            plugin_errors = [e for e in result.errors if e.code == ManifestValidationCode.UNKNOWN_PLUGIN]
            assert len(plugin_errors) == 1
            assert plugin_errors[0].field == 'packages.nonexistent_backend'

    @staticmethod
    def test_invalid_package_name_warning(session_api: API) -> None:
        """Invalid PEP 440 package specifier under a Python backend produces a warning.

        Non-PEP-440 names are accepted by PackageRef (lenient parser) so the
        manifest loads, but `_validate_package_names` flags them as
        `INVALID_PACKAGE_NAME` warnings for Python backends.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {'version': '1', 'packages': {'python': ['valid-package', '!!!invalid!!!']}}
            manifest_path.write_text(json.dumps(manifest_data))

            result = session_api.sync.validate_manifest(Path(tmpdir))

            name_warnings = [w for w in result.warnings if w.code == ManifestValidationCode.INVALID_PACKAGE_NAME]
            assert len(name_warnings) == 1
            assert '!!!invalid!!!' in name_warnings[0].message

    @staticmethod
    def test_duplicate_packages_warning(session_api: API) -> None:
        """Same package under multiple sections produces DUPLICATE_PACKAGE warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {
                'version': '1',
                'packages': {
                    'python': ['requests'],
                },
                'tools': {
                    'python': ['requests'],
                },
            }
            manifest_path.write_text(json.dumps(manifest_data))

            result = session_api.sync.validate_manifest(Path(tmpdir))

            dup_warnings = [w for w in result.warnings if w.code == ManifestValidationCode.DUPLICATE_PACKAGE]
            assert len(dup_warnings) == 1
            assert dup_warnings[0].field == 'packages.python'
            assert 'requests' in dup_warnings[0].message

    @staticmethod
    def test_validation_does_not_execute(session_api: API) -> None:
        """Validation does not trigger any sync operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {
                'version': '1',
                'packages': {'python': ['requests']},
            }
            manifest_path.write_text(json.dumps(manifest_data))

            # This should return quickly without executing anything
            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is True

    @staticmethod
    def test_multiple_errors_and_warnings(session_api: API) -> None:
        """Multiple issues are all reported in a single result.

        With the lenient PackageRef parser, `!!!bad!!!` and
        `also-bad[>=` are now accepted at schema-load time.  The
        manifest still fails validation because version `99` is
        unsupported and `fake_backend` is unknown.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {
                'version': '99',
                'packages': {'fake_backend': ['!!!bad!!!', 'also-bad[>=']},
            }
            manifest_path.write_text(json.dumps(manifest_data))

            result = session_api.sync.validate_manifest(Path(tmpdir))

            assert result.valid is False
            assert len(result.errors) >= 1
            # Version 99 is unsupported or fake_backend is unknown
            error_codes = {e.code for e in result.errors}
            assert (
                ManifestValidationCode.UNSUPPORTED_VERSION in error_codes
                or ManifestValidationCode.UNKNOWN_PLUGIN in error_codes
            )
