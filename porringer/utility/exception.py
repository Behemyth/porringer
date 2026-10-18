"""Utility helpers for exception.

Exception definitions.
"""

from collections.abc import Sequence
from enum import Enum
from pathlib import Path

__all__ = [
    'CommandTimeoutError',
    'ManifestError',
    'ManifestValidationCode',
    'NotSupportedError',
    'PlanStaleError',
    'PluginDependencyError',
    'PluginError',
    'PorringerBusyError',
    'PorringerError',
    'ProcessError',
    'RuntimeInUseError',
    'SetupError',
    'UpdateError',
]


class PorringerError(Exception):
    """Base class for all Porringer exceptions.

    Provides a common error property for accessing the error message.
    """

    def __init__(self, error: str) -> None:
        """Initializes the error.

        Args:
            error: The error message
        """
        self._error = error
        super().__init__(error)

    @property
    def error(self) -> str:
        """The underlying error message.

        Returns:
            The underlying error message
        """
        return self._error


class ProcessError(PorringerError):
    """Raised when there is a configuration error."""


class PluginError(PorringerError):
    """Raised when there is a plugin error."""


class NotSupportedError(PorringerError):
    """Raised when something is not supported."""


class PorringerBusyError(PorringerError):
    """Raised when a sync run is started from inside a running sync on the same API."""


class PlanStaleError(PorringerError):
    """Raised when running a sync plan whose files changed after it was made.

    ``changed`` lists the files; make a new plan to preview the changes.
    """

    def __init__(self, changed: Sequence[Path]) -> None:
        """Initializes the error."""
        self.changed = tuple(changed)
        listed = ', '.join(str(path) for path in self.changed)
        super().__init__(f'The plan is out of date; these files changed since it was made: {listed}')


class RuntimeInUseError(PorringerError):
    """Raised when removing a runtime that entries or installed tools still depend on.

    ``dependents`` describes each one; remove them first or pass ``cascade=True``.
    """

    def __init__(self, runtime: str, dependents: Sequence[str]) -> None:
        """Initializes the error."""
        self.runtime = runtime
        self.dependents = tuple(dependents)
        super().__init__(f'Runtime {runtime} is still used by: ' + '; '.join(self.dependents))


class SetupError(PorringerError):
    """Base class for setup-related errors."""


class ManifestValidationCode(Enum):
    """Machine-readable codes for manifest validation and error diagnostics.

    Used both on `ManifestError` exceptions (for structured error
    classification) and on `ManifestDiagnostic` items produced by the
    validation engine.
    """

    SYNTAX_ERROR = 'syntax_error'
    SCHEMA_INVALID = 'schema_invalid'
    UNSUPPORTED_VERSION = 'unsupported_version'
    UNKNOWN_PLUGIN = 'unknown_plugin'
    INVALID_PACKAGE_NAME = 'invalid_package_name'
    DUPLICATE_PACKAGE = 'duplicate_package'
    UNKNOWN_FIELD = 'unknown_field'
    REMOVED_FIELD = 'removed_field'
    PATH_NOT_FOUND = 'path_not_found'
    NO_MANIFEST = 'no_manifest'
    LOAD_FAILED = 'load_failed'


class ManifestError(SetupError):
    """Raised when there is an error with the setup manifest.

    This includes errors such as:
    - Invalid JSON/TOML syntax
    - Missing required fields
    - Invalid package specifications

    Carries an optional `code` for structured error classification.
    """

    def __init__(self, error: str, *, code: ManifestValidationCode | None = None) -> None:
        """Initializes the error.

        Args:
            error: Human-readable error message.
            code: Optional machine-readable code for this error.
        """
        super().__init__(error)
        self._code = code

    @property
    def code(self) -> ManifestValidationCode | None:
        """The machine-readable error code, when present."""
        return self._code


class CommandTimeoutError(SetupError):
    """Raised when a post-install command exceeds the allowed timeout.

    This exception is raised when a command specified in the manifest
    runs longer than the configured timeout duration.
    """


class PluginDependencyError(PluginError):
    """Raised when a required plugin dependency is not available."""

    def __init__(self, plugin: str, dependency: str, error: str | None = None) -> None:
        """Initializes the error.

        Args:
            plugin: The plugin that has the unmet dependency
            dependency: The name of the missing dependency plugin
            error: Optional additional error message
        """
        self._plugin = plugin
        self._dependency = dependency

        message = f"Plugin '{plugin}' requires '{dependency}' but it is not available"
        if error:
            message = f'{message}: {error}'

        super().__init__(message)

    @property
    def plugin(self) -> str:
        """The plugin that has the unmet dependency.

        Returns:
            str -- The plugin name
        """
        return self._plugin

    @property
    def dependency(self) -> str:
        """The missing dependency plugin name.

        Returns:
            str -- The dependency plugin name
        """
        return self._dependency


class UpdateError(PorringerError):
    """Raised when there is an error checking for or downloading updates."""
