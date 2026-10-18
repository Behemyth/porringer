"""Protocols for runtime provider and consumer plugins.

Plugins that manage interpreter installations (e.g. pim, pyenv) implement
`RuntimeProvider` so the sync engine can resolve the filesystem path
to a managed interpreter.  Plugins that need a runtime (e.g. pip, uv, pipx)
implement `RuntimeConsumer` so the engine knows where to propagate
the resolved path.

The two-part protocol system keeps phasing logic entirely protocol-driven —
the sync engine never needs to match on backend-name strings to decide
which plugins provide or consume runtimes.

Runtime state is held externally in a :class:`RuntimeContext`, not on
plugin instances.  This keeps cached plugin objects stateless and
prevents mutation from leaking across execution boundaries.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


def runtime_tag_matches(tag: str, wanted: str) -> bool:
    """Whether runtime *tag* is selected by *wanted*; ``3.14`` also selects ``3.14-64``."""
    return tag == wanted or tag.partition('-')[0] == wanted


# ---------------------------------------------------------------------------
# Runtime context — externalised runtime state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Resolved runtime executables for a single execution run.

    Frozen dataclass — instances are immutable.  Use
    :meth:`with_executable` to produce a new context with an
    additional or overridden runtime entry.

    Owned by ``ExecutionState``; passed explicitly to every plugin
    method that needs to know which interpreter to target.  Plugin
    instances themselves never store runtime state.

    ``executables`` maps a *kind* string (e.g. ``"python"``) to the
    resolved filesystem path of the managed interpreter.
    """

    executables: dict[str, Path] = field(default_factory=dict)

    def get(self, kind: str) -> Path | None:
        """Return the resolved executable for *kind*, or ``None``."""
        return self.executables.get(kind)

    def with_executable(self, kind: str, executable: Path) -> RuntimeContext:
        """Return a new context with *kind* mapped to *executable*.

        Existing entries are preserved; *kind* is added or
        overwritten.
        """
        return RuntimeContext(executables={**self.executables, kind: executable})


@dataclass(slots=True)
class ResolvedRuntime:
    """A single resolved runtime executable.

    Produced by :meth:`Builder.resolve_all_runtime_executables
    <porringer.backend.builder.Builder.resolve_all_runtime_executables>`
    for every successfully resolved tag across all runtime providers.

    Attributes:
        provider: Canonical name of the runtime-provider plugin
            (e.g. ``"pim"``, ``"pyenv"``).
        tag: The version tag that was resolved (e.g. ``"3.14"``,
            ``"3.12-64"``).
        kind: The runtime kind string (e.g. ``"python"``).
        executable: Absolute path to the resolved interpreter.
    """

    provider: str
    tag: str
    kind: str
    executable: Path


# ---------------------------------------------------------------------------
# Provider / consumer protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class DefaultRuntimeExecutableProvider(Protocol):
    """Optional runtime-provider hook for cheap default executable resolution.

    Runtime providers can implement this when the provider's default
    runtime executable can be resolved directly, without first resolving a
    tag and then resolving that tag to an executable. Providers that do not
    implement it continue to use ``default_tag`` and ``available_tags``.
    """

    @abstractmethod
    async def default_executable(self) -> Path | None:
        """Return the provider's default runtime executable, if available."""
        ...


@runtime_checkable
class RuntimeProvider(Protocol):
    """A plugin that can resolve a managed interpreter path.

    Only plugins that manage language runtimes need to implement this.
    The sync engine checks `isinstance(env, RuntimeProvider)` after
    executing runtime actions, then calls `resolve_executable()` to
    obtain the interpreter path for downstream `RuntimeConsumer`
    plugins.
    """

    @classmethod
    @abstractmethod
    def provided_runtime_kind(cls) -> str:
        """Return the kind of runtime this provider supplies.

        Used to scope propagation — e.g. a Python runtime provider
        returns `"python"` and only consumers that declare
        `consumed_runtime_kind() == "python"` receive the resolved
        executable.

        Returns:
            A runtime kind identifier (e.g. `"python"`).
        """
        ...

    @abstractmethod
    async def resolve_executable(self, tag: str) -> Path | None:
        """Return the filesystem path to the interpreter for *tag*.

        Args:
            tag: The runtime version tag as declared in the manifest
                 (e.g. `"3.14"`, `"3.12"`).

        Returns:
            Absolute path to the interpreter executable, or `None` if
            the runtime is not installed or the path cannot be determined.
        """
        ...

    @abstractmethod
    async def available_tags(self) -> list[str]:
        """Return version tags this provider can resolve.

        Unlike the ``packages()`` query on
        :class:`~porringer.core.plugin_schema.environment.Environment`
        (which returns only actively managed installs), this method
        probes the system for **all** tags the underlying tool can
        dispatch to — including runtimes installed by other tools.

        :meth:`Builder.resolve_runtime_context
        <porringer.backend.builder.Builder.resolve_runtime_context>`
        calls this instead of ``packages()`` so that interpreters
        installed via the official installer, Microsoft Store, or
        other sources are still discoverable for downstream
        ``RuntimeConsumer`` plugins.

        Returns:
            A list of version tag strings (e.g.
            ``["3.14", "3.12", "3.11"]``).  May be empty when no
            runtime is available.
        """
        ...

    async def default_tag(self) -> str | None:
        """Return the tag of the tool's currently configured default runtime.

        When the underlying tool has a notion of "the active" or
        "the default" runtime (e.g. ``pyenv global``, the ``py``
        launcher's default lookup), this method should return the
        corresponding version tag so that
        :meth:`Builder.resolve_runtime_context
        <porringer.backend.builder.Builder.resolve_runtime_context>`
        can prefer it over the highest-version heuristic.

        The default implementation returns ``None``, which causes the
        builder to fall back to :meth:`available_tags` →
        :meth:`sort_tags` → first-resolvable iteration.

        Providers should override this when the tool exposes a
        deterministic default (most runtime managers do).

        Returns:
            A version tag string (e.g. ``"3.14"``, ``"3.12-64"``),
            or ``None`` if the tool has no default concept or the
            default cannot be determined.
        """
        _ = self  # Protocol default; overriders use self
        return None

    def sort_tags(self, tags: list[str]) -> list[str]:
        """Filter and sort *tags* in descending order for this provider's ecosystem.

        The default implementation parses each tag as a :pep:`440`
        version.  Tags that cannot be parsed are silently dropped;
        the remainder is returned sorted highest-first.

        Providers whose ecosystem uses a different versioning scheme
        (e.g. semver for Node.js) should override this method with
        the appropriate parsing and ordering logic.

        :meth:`Builder.resolve_runtime_context
        <porringer.backend.builder.Builder.resolve_runtime_context>`
        calls this after :meth:`available_tags` as a fallback when
        :meth:`default_tag` returns ``None`` or its tag cannot be
        resolved.

        Args:
            tags: Raw version tag strings from :meth:`available_tags`.

        Returns:
            A filtered, descending-sorted list of valid tag strings.
        """
        parsed: list[tuple[Version, str]] = []
        for tag in tags:
            try:
                parsed.append((Version(tag), tag))
            except InvalidVersion:
                logger.debug('Dropping unparseable tag %r from %s', tag, type(self).__name__)
        parsed.sort(key=lambda pair: pair[0], reverse=True)
        return [tag for _, tag in parsed]


@runtime_checkable
class RuntimeConsumer(Protocol):
    """A plugin that consumes a resolved runtime executable.

    Plugins that operate against a specific language runtime (pip, uv,
    pipx, PDM, Poetry, etc.) implement this protocol so the sync
    engine knows which runtime *kind* to supply via
    :class:`RuntimeContext`.

    Runtime state is **not** stored on the plugin instance.  Instead,
    a ``RuntimeContext`` is passed to every method that needs it.
    """

    @classmethod
    @abstractmethod
    def consumed_runtime_kind(cls) -> str:
        """Return the kind of runtime this consumer requires.

        Must match a `RuntimeProvider.provided_runtime_kind()` value
        for propagation to occur.

        Returns:
            A runtime kind identifier (e.g. `"python"`).
        """
        ...

    @classmethod
    def is_available_for(cls, runtime_context: RuntimeContext) -> bool:
        """Check whether this plugin can operate with *runtime_context*.

        Called during **deferred resolution** — after a preceding phase
        has resolved a runtime and injected it onto ``PATH``.  This
        gives plugins an opportunity to probe the *target* interpreter
        rather than only checking the host process.

        The default implementation delegates to the class-level
        ``is_available()`` (if present) so that plugins which do not
        need runtime-aware probing continue to work unchanged.

        Subclasses should override when their availability depends on
        the resolved runtime (e.g. ``pip`` checking whether
        ``python -m pip`` works with the target interpreter).

        Args:
            runtime_context: The resolved runtime executables for the
                current execution run.

        Returns:
            ``True`` if the plugin can handle operations with the
            given runtime, ``False`` otherwise.
        """
        # Default: fall back to the class-level is_available() when
        # the concrete type defines one (all ToolBasedPlugin subclasses do).
        if hasattr(cls, 'is_available'):
            return cls.is_available()
        return True
