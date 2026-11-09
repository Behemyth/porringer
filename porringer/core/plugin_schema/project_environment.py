"""Core helpers and types for project environment.

Plugin utilities for project-scoped environments.

A `ProjectInstaller` plugin wraps a project dependency manager
(PDM, Poetry, uv) and delegates venv creation, dependency resolution,
and lock-file installation entirely to the underlying tool.

The engine invokes `ProjectInstaller.command_plan()` to build the
sync command, then runs its steps directly, after all per-package
actions have completed so that the tool itself is already installed
(e.g. via pipx).  `command_plan()` is each plugin's single source of
truth: the same steps are executed and shown in preview, so a
previewed command can never diverge from what actually runs.  When
the manifest file lives in a subdirectory of the project root, each
plugin auto-discovers the correct project root by walking ancestor
directories looking for its ecosystem's marker file (e.g.
`package.json` for Node, `pyproject.toml` for Python).
"""

import json
import tomllib
from abc import abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, override

from pydantic import Field

from porringer.core.plugin_schema.manifest import ManifestContributor
from porringer.core.plugin_schema.runtime import RuntimeConsumer, RuntimeContext
from porringer.core.plugin_schema.tool_based import ToolBasedPlugin
from porringer.core.schema import Ecosystem, ManifestContribution, PluginKind, PorringerModel

# Default mapping from ecosystem name to the file that marks a project root.
ECOSYSTEM_MARKERS: dict[Ecosystem, str] = {
    Ecosystem('python'): 'pyproject.toml',
    Ecosystem('node'): 'package.json',
}

# Default mapping from ecosystem name to the manifest contribution.
ECOSYSTEM_CONTRIBUTIONS: dict[Ecosystem, ManifestContribution] = {
    Ecosystem('python'): ManifestContribution(
        filename='pyproject.toml', config_path=('tool', 'porringer'), file_format='toml'
    ),
    Ecosystem('node'): ManifestContribution(filename='package.json', config_path=('porringer',), file_format='json'),
}


class ProjectCommandPlan(PorringerModel):
    """A project-sync command plan produced by a project plugin."""

    directory: Path = Field(description='Working directory for the sync command')
    argv: list[str] = Field(default_factory=list, description='Primary command and arguments to execute')
    steps: list[list[str]] = Field(default_factory=list, description='Ordered command steps to execute')


class ProjectInstaller(ToolBasedPlugin, RuntimeConsumer, ManifestContributor):
    """Capability base for installing a project's declared dependencies.

    Unlike `Environment`,
    which installs individual packages, a `ProjectInstaller` runs the
    tool's native install command (e.g. ``pdm install``, ``uv sync``)
    inside the project directory.
    Venv creation, lock-file handling, and dependency resolution are left
    entirely to the wrapped tool.

    Subclasses **must** override `tool_name()`.  Everything else has
    sensible defaults that can be overridden when the tool's CLI differs
    from the common pattern (e.g. Poetry's `poetry env use` step).

    Plugin instances are **stateless** with respect to runtime
    configuration.  A :class:`RuntimeContext` is passed explicitly to
    methods that need to know which interpreter to target.
    """

    _install_verb: str = 'install'
    """The sub-command the tool uses for project synchronisation.

    Defaults to `"install"` (used by PDM and Poetry).
    Override to `"sync"` for tools like uv.
    """

    _project_evidence_files: ClassVar[tuple[str, ...]] = ()
    """Files that indicate this specific project manager owns the project."""

    _pyproject_tool_tables: ClassVar[tuple[tuple[str, ...], ...]] = ()
    """TOML table paths in pyproject.toml that indicate project ownership."""

    _package_manager_names: ClassVar[tuple[str, ...]] = ()
    """package.json packageManager prefixes that indicate project ownership."""

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @classmethod
    @abstractmethod
    def tool_name(cls) -> str:
        """Return the CLI executable name this plugin wraps.

        Used by `is_available()` to verify the tool is on PATH and
        by `sync_command()` to build the default command.
        """
        ...

    # ------------------------------------------------------------------
    # Defaults (override only when the tool deviates from the pattern)
    # ------------------------------------------------------------------

    @staticmethod
    @abstractmethod
    def ecosystem() -> Ecosystem:
        """Return the ecosystem this project environment belongs to.

        Examples: `"python"`, `"node"`.
        """
        ...

    @classmethod
    @abstractmethod
    def consumed_runtime_kind(cls) -> str:
        """Return the kind of runtime this project environment consumes.

        Examples: `"python"`, `"node"`.
        """
        ...

    @classmethod
    def manifest_contribution(cls) -> ManifestContribution | None:
        """Return the manifest contribution for this ecosystem.

        The default implementation looks up ``ecosystem()`` in
        ``ECOSYSTEM_CONTRIBUTIONS``.  Override this method for
        plugins that use a non-standard hosted config layout.

        Returns:
            A ``ManifestContribution`` describing the hosted config, or
            ``None`` if this ecosystem does not embed porringer config.
        """
        return ECOSYSTEM_CONTRIBUTIONS.get(cls.ecosystem())

    @classmethod
    def run_prefix(cls) -> list[str] | None:
        """Return the argv prefix that runs a command inside the synced project environment.

        Used for the native handoff, which runs the project's own locked
        CPPython after sync. ``None`` (the default) means the tool can't.
        """
        return None

    @classmethod
    def prepare_command(cls) -> list[str] | None:
        """Return the project-owned preparation entry point, or None if unsupported."""
        prefix = cls.run_prefix()
        return [*prefix, 'prepare'] if prefix else None

    @classmethod
    def project_marker(cls) -> str | None:
        """Return the filename that marks this ecosystem's project root.

        The sync engine uses this marker to auto-discover the project
        root when the manifest file lives in a subdirectory.  It walks
        ancestor directories starting from the manifest's location
        and returns the first directory containing this file.

        The default implementation looks up `ecosystem()` in a
        built-in mapping:

        ==========  ================
        Ecosystem   Marker
        ==========  ================
        `python`  `pyproject.toml`
        `node`    `package.json`
        ==========  ================

        Override this method when a plugin uses a non-standard marker
        or when multiple markers should be checked.

        Returns:
            Filename to search for, or `None` to disable
            auto-discovery (always use the manifest directory).
        """
        return ECOSYSTEM_MARKERS.get(cls.ecosystem())

    @classmethod
    def resolve_project_root(
        cls,
        search_from: Path,
        *,
        boundary: Path | None = None,
    ) -> Path | None:
        """Walk ancestor directories looking for `project_marker()`.

        Starting from *search_from* (inclusive) and moving towards the
        filesystem root, return the first directory that contains the
        marker file returned by `project_marker()`.

        Args:
            search_from: Directory to start the search from
                (typically the manifest's parent directory).
            boundary: Optional upper-bound directory.  The search
                will not ascend above this path.  When `None`, the
                search continues to the filesystem root.

        Returns:
            The discovered project root, or `None` if the marker
            was not found (or `project_marker()` returns `None`).
        """
        marker = cls.project_marker()
        if marker is None:
            return None

        current = search_from.resolve()
        boundary_resolved = boundary.resolve() if boundary is not None else None

        while True:
            if (current / marker).exists():
                return current

            # Stop if we've reached the boundary
            if boundary_resolved is not None and current == boundary_resolved:
                break

            parent = current.parent
            # Stop at the filesystem root
            if parent == current:
                break
            current = parent

        return None

    @classmethod
    def runtime_selection_args(cls, runtime_context: RuntimeContext | None) -> list[str]:
        """Return CLI args that select the resolved runtime interpreter.

        The default is no args, because most project tools do not accept
        an inline interpreter flag on their install verb (``pdm install``
        rejects ``--python``; Poetry selects interpreters with a separate
        ``poetry env use`` step). Tools that do support inline selection
        must opt in explicitly (uv overrides this to return
        ``['--python', <path>]`` for ``uv sync``).

        Never assume one tool's flag syntax generalises: an unrecognised
        flag makes the whole sync action fail at execution time.

        Args:
            runtime_context: Resolved runtime paths for this execution
                run. ``None`` means no runtime was resolved.
        """
        return []

    @classmethod
    def project_relevance(cls, search_from: Path) -> bool:
        """Return whether this plugin should run for the given directory.

        The default implementation looks for the plugin's marker file in
        the current directory or an ancestor, mirroring the existing
        project-root discovery behavior.
        """
        return cls.resolve_project_root(search_from) is not None

    @classmethod
    def project_evidence(cls, search_from: Path) -> bool:
        """Return whether project files specifically identify this plugin.

        Marker files such as ``pyproject.toml`` and ``package.json`` establish
        project relevance for an ecosystem. Evidence is narrower: lock files,
        tool-specific config tables, or package-manager declarations identify
        which project manager should own sync for that project.
        """
        directory = cls.resolve_project_root(search_from)
        if directory is None:
            return False

        if any((directory / filename).exists() for filename in cls._project_evidence_files):
            return True

        pyproject = directory / 'pyproject.toml'
        if cls._pyproject_tool_tables and pyproject.exists():
            try:
                data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
            except OSError, tomllib.TOMLDecodeError:
                data = {}
            if any(_has_nested_key(data, table_path) for table_path in cls._pyproject_tool_tables):
                return True

        package_json = directory / 'package.json'
        if cls._package_manager_names and package_json.exists():
            try:
                data = json.loads(package_json.read_text(encoding='utf-8'))
            except OSError, json.JSONDecodeError:
                data = {}
            package_manager = data.get('packageManager') if isinstance(data, dict) else None
            if isinstance(package_manager, str) and package_manager.startswith(cls._package_manager_names):
                return True

        return False

    @classmethod
    def command_plan(cls, search_from: Path, *, runtime_context: RuntimeContext | None = None) -> ProjectCommandPlan:
        """Build a sync command plan for the provided directory.

        This is each plugin's single command-building entry point: the
        engine executes ``plan.steps`` directly and reuses the same
        plan for preview display, so what a user confirms is exactly
        what runs. The default builds one step from `tool_name()`,
        `_install_verb`, and `runtime_selection_args()`. Override this
        method when the tool needs a different shape, such as multiple
        steps (Poetry's separate `poetry env use` step).
        """
        directory = cls.resolve_project_root(search_from) or search_from
        cmd = [cls.tool_name(), cls._install_verb, *cls.runtime_selection_args(runtime_context)]
        return ProjectCommandPlan(directory=directory, argv=cmd, steps=[cmd])


class ProjectEnvironment(ProjectInstaller):
    """Project-install plugin classified under :data:`PluginKind.PROJECT`.

    Thin base for project-only tools (PDM, Poetry, uv project install). Plugins
    that also install individual packages mix in :class:`ProjectInstaller`
    alongside an :class:`Environment` base instead of subclassing this.
    """

    @staticmethod
    @override
    def plugin_kind() -> PluginKind:
        """Project environments always have kind `PROJECT`."""
        return PluginKind.PROJECT


class NodeProjectInstaller(ProjectInstaller):
    """Node-ecosystem project-install capability (npm, pnpm).

    Centralises the `node` ecosystem and consumed-runtime kind so that
    each concrete plugin only declares its `tool_name()` and any CLI
    deviations (e.g. `_install_verb`).
    """

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        """Node project installers belong to the `node` ecosystem."""
        return Ecosystem('node')

    @classmethod
    @override
    def consumed_runtime_kind(cls) -> str:
        """Node project installers consume a Node runtime."""
        return 'node'


NATIVE_APPLY_COMMAND = ('cppython', 'local', 'apply')
"""The CPPython command that receives a manifest's ``native`` intent."""


class NativeHandoffUnsupportedError(ValueError):
    """The project installer has no way to run a command inside the project environment."""


def native_apply_step(installer: type[ProjectInstaller], native_intent: str) -> list[str]:
    """Return the step that hands a manifest's ``native`` intent to the project's own CPPython.

    The intent is passed inline as JSON, so preview and execution show the same command
    and nothing is written before the user confirms. ``--clone`` lets CPPython fetch
    missing checkouts of local packages from the URLs in the intent.

    Raises:
        NativeHandoffUnsupportedError: If *installer* has no run prefix.
    """
    prefix = installer.run_prefix()
    if not prefix or not prefix[0].strip() or any('\0' in arg for arg in prefix):
        raise NativeHandoffUnsupportedError(
            f"'{installer.tool_name()}' can't run commands in the project environment, "
            "so the manifest's 'native' section can't be applied"
        )
    return [*prefix, *NATIVE_APPLY_COMMAND, '--clone', '--inline', native_intent, '--json']


def project_steps(
    installer: type[ProjectInstaller],
    plan: ProjectCommandPlan,
    native_intent: str | None,
    prepare_command: tuple[str, ...] | None = None,
    *,
    native_command: tuple[str, ...] | None = None,
) -> list[list[str]]:
    """Return sync, native handoff, and project preparation in execution order."""
    steps = plan.steps or ([plan.argv] if plan.argv else [])
    if native_intent is not None:
        native_step = (
            list(native_command) if native_command is not None else native_apply_step(installer, native_intent)
        )
        steps = [*steps, native_step]
    if prepare_command is not None:
        steps = [*steps, list(prepare_command)]
    return steps


def _has_nested_key(data: object, path: Sequence[str]) -> bool:
    """Return whether *data* contains the nested mapping path."""
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True
