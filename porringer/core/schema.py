"""Core helpers and types for schema.

Schema for Porringer.
"""

import re
import sys
from enum import Enum
from typing import Any, Literal, NewType, Protocol

__all__ = [
    'Distribution',
    'Ecosystem',
    'ManifestContribution',
    'Package',
    'PackageRef',
    'PackageRelation',
    'PackageRelationKind',
    'PlatformScoped',
    'Plugin',
    'PluginDependency',
    'PluginKind',
    'PluginParameters',
    'PorringerModel',
]

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

Ecosystem = NewType('Ecosystem', str)
"""Semantic alias for ecosystem identifiers (e.g. `"python"`, `"node"`).

A thin wrapper around `str` that makes ecosystem values self-documenting
in type signatures without restricting the open set of valid values.
"""


class PluginKind(Enum):
    """Fixed set of generic plugin operation types.

    Plugins declare their kind so the manifest can group entries by
    operation type rather than by ecosystem.  New ecosystems require
    zero changes to this enum — only a new `ecosystem()` string
    from the plugin.
    """

    PACKAGE = 'packages'
    """Install individual packages into an environment."""

    TOOL = 'tools'
    """Install CLI tools in isolated environments."""

    PROJECT = 'projects'
    """Install a project's declared dependencies from its lock-file / manifest."""

    RUNTIME = 'runtimes'
    """Manage language runtime installations."""

    SCM = 'scm'
    """Clone or manage source-control repositories."""


# Pattern for PEP 440 constraint operators at the start of a substring.
_PEP440_CONSTRAINT_START = re.compile(r'[><=!~]')


class PorringerModel(BaseModel):
    """The base model to use for all Porringer models."""

    model_config = ConfigDict(
        validate_by_name=False,
        validate_by_alias=True,
        arbitrary_types_allowed=True,
        extra='forbid',
    )


class PlatformScoped(PorringerModel):
    """Mixin for models that can be scoped to specific platforms.

    When `platforms` is empty the entry applies everywhere.
    Otherwise, the entry is only applicable when `sys.platform`
    appears in the list.
    """

    platforms: list[str] = Field(
        default_factory=list,
        description='List of platforms where this entry applies (e.g., ["win32"]). Empty means all platforms.',
    )

    def is_applicable(self) -> bool:
        """Check if this entry applies to the current platform.

        Returns:
            True if the entry applies to the current platform
        """
        if not self.platforms:
            return True
        return sys.platform in self.platforms


class PackageRef(PorringerModel):
    """A package reference with an optional version constraint.

    Represents a package identifier that may include a PEP 440 version specifier
    (e.g. `"ruff>=0.8.0"`, `"pydantic>=2,<3"`). A bare name such as `"pytest"`
    is also valid (constraint will be `None`).

    Can be constructed in several ways::

        PackageRef(name='ruff', constraint='>=0.8.0')
        PackageRef('ruff>=0.8.0')  # via model validator (string coercion)
    """

    model_config = {'frozen': True}

    name: str = Field(description='The bare, canonical package name')
    extras: tuple[str, ...] = Field(
        default=(), description='PEP 508 extras (e.g. ("cmake", "conan", "git") from "pkg[cmake,conan,git]")'
    )
    constraint: str | None = Field(
        default=None, description='Version constraint string (PEP 440 or raw, e.g. ">=0.8.0", "^4.0.0")'
    )

    @model_validator(mode='before')
    @classmethod
    def _coerce_string(cls, data: str | dict[str, Any]) -> dict[str, Any]:
        """Accept plain strings and auto-parse them into name + constraint."""
        if isinstance(data, str):
            return cls._split_spec(data)
        return data

    @staticmethod
    def _split_spec(spec: str) -> dict[str, Any]:
        """Decompose a specifier string into name, extras, and constraint components.

        Tries PEP 440 parsing first (via `packaging.requirements.Requirement`).
        On failure, falls back to a lenient splitter that handles npm-style
        specifiers such as `@scope/name@^4.0.0` or `lodash@~4.18`.

        The constraint is stored as a raw string — no semver interpretation.
        Plugins and underlying tools are responsible for passing it to their
        CLI in the correct format.
        """
        # 1. Try PEP 440 first
        try:
            req = Requirement(spec)
            # Reject PEP 508 URL requirements (e.g. "lodash@^4.0.0" parsed as
            # name=lodash url=^4.0.0) — fall through to the lenient parser
            # which will correctly split on the '@'.
            if req.url is not None:
                raise InvalidRequirement('URL requirement')
            extras = tuple(sorted(req.extras)) if req.extras else ()
            constraint = str(req.specifier) if req.specifier else None
            return {'name': req.name, 'extras': extras, 'constraint': constraint}
        except InvalidRequirement:
            pass

        # 2. Lenient fallback for non-PEP-440 specifiers (npm, etc.)
        return PackageRef._split_spec_lenient(spec)

    @staticmethod
    def _split_spec_lenient(spec: str) -> dict[str, Any]:
        """Lenient parser for non-PEP-440 package specifiers.

        Handles:
        - `@scope/name@constraint` → name=`@scope/name`, constraint
        - `@scope/name`            → name=`@scope/name`, no constraint
        - `name@constraint`        → name, constraint
        - `name`                   → name, no constraint
        """
        spec = spec.strip()
        if not spec:
            raise ValueError('Empty package specifier')

        # Scoped packages: @scope/name possibly followed by @constraint
        if spec.startswith('@'):
            # Find the slash that separates scope from name
            slash_idx = spec.find('/')
            if slash_idx == -1:
                raise ValueError(f'Invalid scoped package specifier: {spec!r}')
            # Look for a second @ after the slash (constraint separator)
            at_idx = spec.find('@', slash_idx + 1)
            if at_idx == -1:
                # Check for PEP-440-style constraint operators after the name
                match = _PEP440_CONSTRAINT_START.search(spec, slash_idx + 1)
                if match:
                    return {'name': spec[: match.start()], 'constraint': spec[match.start() :]}
                return {'name': spec, 'constraint': None}
            name = spec[:at_idx]
            constraint = spec[at_idx + 1 :] or None
            return {'name': name, 'constraint': constraint}

        # Unscoped: check for PEP-440-style operators before trying @
        match = _PEP440_CONSTRAINT_START.search(spec)
        if match and match.start() > 0:
            return {'name': spec[: match.start()], 'constraint': spec[match.start() :]}

        # Unscoped: name@constraint
        at_idx = spec.find('@')
        if at_idx != -1:
            name = spec[:at_idx]
            constraint = spec[at_idx + 1 :] or None
            return {'name': name, 'constraint': constraint}

        return {'name': spec, 'constraint': None}

    @property
    def specifier(self) -> str:
        """The full specifier string (name + extras + constraint) suitable for CLI commands.

        Examples:
            `"ruff>=0.8.0"`, `"pytest"`, `"cppython[cmake,conan,git]"`
        """
        extras_str = f'[{",".join(self.extras)}]' if self.extras else ''
        if self.constraint:
            return f'{self.name}{extras_str}{self.constraint}'
        return f'{self.name}{extras_str}'

    def specifier_for(self, style: Literal['pep440', 'at', 'equals']) -> str:
        """Render this package reference using a tool-specific separator.

        Plugins should prefer this helper over hand-rolling the ``name@version``
        / ``name==version`` / ``name=version`` join in their command-builders.

        Args:
            style: The separator style to use:

                * ``'pep440'`` — attach the constraint verbatim
                  (e.g. ``ruff>=0.8.0``).  This matches :attr:`specifier`.
                * ``'at'`` — npm-family separator (``name@constraint``).
                * ``'equals'`` — apt-family separator (``name=constraint``).

        Returns:
            A bare name when no constraint is present, otherwise the
            ``name<sep>constraint`` form.
        """
        if not self.constraint:
            return self.name
        if style == 'pep440':
            return f'{self.name}{self.constraint}'
        if style == 'at':
            return f'{self.name}@{self.constraint}'
        if style == 'equals':
            return f'{self.name}={self.constraint}'
        raise ValueError(f'Unknown specifier style: {style!r}')

    def __str__(self) -> str:
        """Return the full specifier string."""
        return self.specifier


class ManifestContribution(PorringerModel):
    """Declares that a plugin can host porringer configuration inside one of its own files.

    For example, a Python project plugin contributes
    ``ManifestContribution('pyproject.toml', ('tool', 'porringer'), 'toml')``
    to indicate that ``pyproject.toml`` may contain a ``[tool.porringer]``
    section with either inline manifest data or a ``manifest = "path"``
    reference to an external ``porringer.json`` file.

    Attributes:
        filename: The filename to look for (e.g. ``'pyproject.toml'``).
        config_path: Key path within the parsed file where porringer
            configuration lives (e.g. ``('tool', 'porringer')``).
        file_format: Parser to use — ``'toml'`` or ``'json'``.
    """

    model_config = {'frozen': True}

    filename: str = Field(description='Filename to probe (e.g. "pyproject.toml")')
    config_path: tuple[str, ...] = Field(description='Key path to the porringer section within the file')
    file_format: str = Field(description='File format: "toml" or "json"')


class PluginDependency(PlatformScoped):
    """Defines a dependency on another plugin."""

    plugin: str = Field(description='The name of the required plugin')
    required: bool = Field(default=True, description='Whether this dependency is required (True) or optional (False)')


class PackageRelationKind(Enum):
    """Kind of relationship a package has with a host tool.

    Used to distinguish how a sub-package was added to its host:
    via tool-level injection (e.g. ``pipx inject``).
    """

    INJECTED = 'injected'
    """Package was injected into a host tool's isolated environment."""


class PackageRelation(PorringerModel):
    """Describes a package's relationship to a host tool.

    When a package is a sub-package of another tool (injected into its
    venv, or added via native plugin management), this model captures
    the host tool name and the kind of relationship.

    A standalone top-level package has no relation (``None``).
    """

    host: str = Field(description='Name of the host tool this package belongs to')
    kind: PackageRelationKind = Field(description='How this package relates to its host')


class Package(PorringerModel):
    """Package definition — represents an installed package identity."""

    name: str = Field(description='The bare package name')
    version: str | None = None
    relation: PackageRelation | None = Field(
        default=None,
        description=(
            'Relationship to a host tool, or None for standalone packages. '
            'Set when the package is injected into another tool (e.g. pipx inject) '
            'or managed as a native plugin (e.g. pdm self add).'
        ),
    )


class Distribution(PorringerModel):
    """Data that describes the distribution of the plugin."""

    version: Version


class PluginParameters(PorringerModel):
    """Generic plugin parameters that will be used to construct a Plugin instance."""

    distribution: Distribution


class Plugin(Protocol):
    """Porringer plugin."""

    _distribution: Distribution

    def __init__(self, parameters: PluginParameters) -> None:
        """Initializes the plugin."""
        self._distribution = parameters.distribution

    @staticmethod
    def ecosystem() -> Ecosystem | None:
        """Return the ecosystem this plugin belongs to.

        A free-form identifier such as `"python"`, `"node"`,
        `"system"`, or `"deno"`.  New ecosystems (e.g. `"rust"`)
        can be introduced by third-party plugins without any changes to
        the core.

        Return `None` for plugins that don't participate in backend
        resolution (e.g. pure provider plugins).

        Returns:
            The ecosystem identifier string, or `None`.
        """
        raise NotImplementedError

    @staticmethod
    def plugin_kind() -> PluginKind:
        """Return the kind of operation this plugin performs.

        Defaults to `PluginKind.PACKAGE`.  Override in subclasses
        for tools, projects, or runtimes.

        Returns:
            The plugin kind.
        """
        return PluginKind.PACKAGE

    @staticmethod
    def is_supported() -> bool:
        """Return whether this plugin is supported on the current platform.

        Platform-specific plugins should override this to return `False`
        on operating systems they do not target (e.g. a Windows-only
        plugin returns `False` on Linux/macOS).

        This check is evaluated before `is_available()` during backend
        resolution so that unsupported plugins are excluded cheaply
        without probing the filesystem.

        Returns:
            `True` if the current platform is supported (the default).
        """
        return True

    @classmethod
    def is_available(cls) -> bool:
        """Check if this plugin is available on the current system.

        Plugins that depend on external tools can override this to check
        whether the required executables exist.  The default implementation
        always returns `True`.

        Returns:
            `True` if the plugin is available, `False` otherwise.
        """
        return True

    @staticmethod
    def package_name_validator() -> str | None:
        """Return the validation scheme for package names, or `None`.

        Well-known values:

        * `"pep440"` — validate via `packaging.requirements.Requirement`
        * `None` — accept any non-empty name (the default)

        Returns:
            A validation scheme identifier, or `None`.
        """
        return None

    @staticmethod
    def dependencies() -> list[PluginDependency]:
        """Declares plugin dependencies on other plugins.

        Dependencies can be platform-specific and either required or optional.
        Override this method to declare dependencies for your plugin.

        Returns:
            A list of plugin dependencies
        """
        return []

    async def setup(self) -> None:  # noqa: PLR6301
        """Plugin-level one-time native registration.

        Called by the framework **before** this plugin's first
        package-phase action in a sync.  Use this for operations that
        the framework cannot infer from the plugin's commands — for
        example, running ``py install --configure --yes`` to create
        PIM's global shortcuts directory and register it on the user
        PATH, or ``pipx ensurepath`` to add the pipx venvs bin
        directory.

        **Contract (enforced by the framework, not plugin code):**

        * **Idempotency** — called at most once per plugin per sync,
          but the implementation must also tolerate being called on a
          machine that was already set up by a previous sync.
        * **Soft-fail isolation** — a failure aborts this plugin's
          remaining actions in this sync but does not raise; the
          framework logs at WARNING.
                * **Inspection** — read-only inspection paths do not call
                    setup.

        The default implementation is a no-op.
        """
        return None

    async def teardown(self) -> None:  # noqa: PLR6301
        """Plugin-level cleanup that reverses what :meth:`setup` created.

        Called by the framework **after** this plugin's last
        ``uninstall()`` action when ``packages()`` returns empty.
        Use this for removing directories, PATH entries, shell-init
        lines, or other global state that :meth:`setup` created.

        **Contract (enforced by the framework, not plugin code):**

        * **Idempotency** — called at most once per plugin per sync,
          but the implementation must also tolerate being called on a
          machine where the plugin was never set up.
        * **Soft-fail isolation** — same as :meth:`setup`.
                * **Inspection** — read-only inspection paths do not call
                    teardown.

        The default implementation is a no-op.
        """
        return None

    @property
    def distribution(self) -> Distribution:
        """Additional plugin information that complements the packaged project metadata.

        Returns:
            The plugin's information
        """
        return self._distribution
