"""Public package exports.

Public package entry points for Porringer.

This package exposes the public API, backend helpers, CLI commands, and
supporting utilities that applications use when integrating with Porringer.
"""

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.schema.cache import LocalConfiguration
from porringer.schema.execution import SetupParameters
from porringer.schema.manifest import SetupManifest
from porringer.utility.exception import (
    CommandTimeoutError,
    ManifestError,
    ManifestValidationCode,
    NotSupportedError,
    PlanStaleError,
    PluginDependencyError,
    PluginError,
    PorringerBusyError,
    PorringerError,
    ProcessError,
    RuntimeInUseError,
    SetupError,
    UpdateError,
)

__all__ = [
    'API',
    'CommandTimeoutError',
    'DiscoveredPlugins',
    'LocalConfiguration',
    'ManifestError',
    'ManifestValidationCode',
    'NotSupportedError',
    'PlanStaleError',
    'PluginDependencyError',
    'PluginError',
    'PorringerBusyError',
    'PorringerError',
    'ProcessError',
    'RuntimeContext',
    'RuntimeInUseError',
    'SetupError',
    'SetupManifest',
    'SetupParameters',
    'UpdateError',
    '__version__',
]

try:
    __version__: str = _metadata_version('porringer')
except PackageNotFoundError:
    __version__ = '0.0.0'

# Register a NullHandler so library consumers can attach their own logging
# handlers without seeing warnings about an unconfigured logger.
# See https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library
logging.getLogger(__name__).addHandler(logging.NullHandler())
