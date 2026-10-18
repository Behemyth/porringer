"""Backend helpers for backend.

Backend resolution for mapping `(kind, ecosystem)` pairs to installer plugins.

The `BackendResolver` chooses the plugin that should handle each
`(PluginKind, ecosystem)` pair declared in a manifest. For example,
`(PACKAGE, "python")` might resolve to `uv`, `pip`, or another matching
plugin depending on availability and user preferences.
"""

import logging
from collections import defaultdict
from collections.abc import Mapping

from porringer.core.plugin_schema.project_environment import ProjectInstaller
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.plugin_schema.tool_based import ToolBasedPlugin
from porringer.core.schema import Ecosystem, Plugin, PluginKind

logger = logging.getLogger(__name__)

# A plugin that participates in backend resolution.
BackendPlugin = Plugin


class BackendResolver:
    """Maps `(PluginKind, ecosystem)` pairs to the best available plugin.

    Construction requires a single *plugins* mapping containing **all**
    instantiated plugins (environments, project environments, SCM
    environments) keyed by their canonical name, plus an optional
    *preferences* dict from the manifest.

    Resolution algorithm per `(kind, ecosystem)` pair:

    1. If the user gave an explicit **preference** for the ecosystem and
       the named plugin is available, use it.
    2. Otherwise filter to supported & available candidates, sort
       alphabetically by name, and pick the first one.
    """

    def __init__(
        self,
        plugins: Mapping[str, BackendPlugin],
        preferences: Mapping[Ecosystem, str] | None = None,
        runtime_context: RuntimeContext | None = None,
        needed_pairs: set[tuple[PluginKind, Ecosystem]] | None = None,
    ) -> None:
        """Initialize the backend resolver with available plugins and preferences.

        Args:
            plugins: All instantiated plugins keyed by canonical name.
            preferences: Optional ecosystem → plugin-name preference mapping.
            runtime_context: Resolved runtime executables.  When provided,
                ``RuntimeConsumer`` plugins are probed via
                ``is_available_for(runtime_context)`` instead of the
                class-level ``is_available()``.
            needed_pairs: When provided, only these ``(kind, ecosystem)``
                pairs are eagerly resolved.  Pairs not in the set are
                still indexed (available to ``is_registered`` /
                ``registered_names``) but not resolved, suppressing
                log messages for irrelevant ecosystems.
        """
        self._all_plugins: dict[str, BackendPlugin] = dict(plugins)
        self._preferences = preferences or {}
        self._runtime_context = runtime_context

        # Index registered plugin names by `(kind, ecosystem)` pair.
        #
        # A plugin's declared `plugin_kind()` reflects its primary manifest
        # section (package/tool/runtime/scm). `PROJECT` is different: it is
        # a derived *capability* rather than a declared identity, so a
        # plugin that also implements `ProjectInstaller` (e.g. `uv`, which
        # both installs packages and can `uv sync` a project) must be
        # additionally indexed under `(PROJECT, ecosystem)` even though its
        # declared kind is `PACKAGE`. Without this, `resolve(PROJECT, ...)`
        # silently fails to find such plugins whenever selection isn't
        # evidence-based (see `_build_implicit_project_actions`).
        self._backend_plugins: dict[tuple[PluginKind, Ecosystem], list[str]] = defaultdict(list)
        for name, plugin in self._all_plugins.items():
            ecosystem = type(plugin).ecosystem()
            if ecosystem is None:
                continue
            declared_kind = type(plugin).plugin_kind()
            self._backend_plugins[(declared_kind, ecosystem)].append(name)
            if declared_kind != PluginKind.PROJECT and isinstance(plugin, ProjectInstaller):
                self._backend_plugins[(PluginKind.PROJECT, ecosystem)].append(name)

        # Resolve only the pairs the caller needs right away. When
        # *needed_pairs* is ``None``, every registered pair is resolved to
        # preserve the existing behavior. Passing an explicit set avoids
        # noisy "No available plugin" log messages for ecosystems that are
        # registered via entry points but irrelevant to the current manifest.
        resolve_keys = needed_pairs if needed_pairs is not None else set(self._backend_plugins)

        # Resolve once and cache
        self._resolved: dict[tuple[PluginKind, Ecosystem], str | None] = {}
        for key in resolve_keys:
            if key in self._backend_plugins:
                self._resolved[key] = self._resolve(key)

        logger.debug(
            'Backend resolution map: %s',
            {f'({k.value}, {e})': v for (k, e), v in self._resolved.items()},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, kind: PluginKind, ecosystem: Ecosystem) -> str | None:
        """Return the chosen plugin name for *(kind, ecosystem)*, or `None`."""
        key = (kind, ecosystem)
        if key not in self._resolved:
            available_for_kind = [eco for (k, eco) in self._resolved if k == kind]
            logger.debug(
                "No plugins registered for (%s, '%s'). Available %s ecosystems: %s.",
                kind.value,
                ecosystem,
                kind.value,
                available_for_kind or '(none)',
            )
            return None
        return self._resolved[key]

    def is_registered(self, kind: PluginKind, ecosystem: Ecosystem) -> bool:
        """Return ``True`` if at least one plugin is registered for *(kind, ecosystem)*.

        A registered plugin may still be unavailable (e.g. the underlying
        tool is not installed yet).  Use :meth:`resolve` to determine
        whether a *suitable* plugin exists.
        """
        return (kind, ecosystem) in self._backend_plugins

    def registered_names(self, kind: PluginKind, ecosystem: Ecosystem) -> list[str]:
        """Return the names of all plugins registered for *(kind, ecosystem)*.

        Returns an empty list when no plugin is registered for the pair.
        """
        return list(self._backend_plugins.get((kind, ecosystem), []))

    def is_available(self, plugin_name: str) -> bool:
        """Return ``True`` if *plugin_name* is supported here and its tool is available now."""
        return self._is_suitable(plugin_name)

    def validator_for(self, kind: PluginKind, ecosystem: Ecosystem) -> str | None:
        """Return the `package_name_validator()` tag for the resolved plugin.

        Returns `None` when no plugin is resolved or the plugin
        declares no validator.
        """
        name = self.resolve(kind, ecosystem)
        if name is None:
            return None
        plugin = self._all_plugins.get(name)
        if plugin is None:
            return None
        return type(plugin).package_name_validator()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve(self, key: tuple[PluginKind, Ecosystem]) -> str | None:
        """Pick the best plugin for *(kind, ecosystem)*.

        1. Explicit preference (if supported & available).
        2. Sort supported & available candidates alphabetically, pick first.
        """
        kind, ecosystem = key
        candidates = self._backend_plugins.get(key, [])
        if not candidates:
            return None

        # 1. Explicit preference
        if ecosystem in self._preferences:
            preferred = self._preferences[ecosystem]
            if preferred in candidates and self._is_suitable(preferred):
                return preferred
            logger.warning(
                "Preferred plugin '%s' for (%s, '%s') is not available; falling back",
                preferred,
                kind.value,
                ecosystem,
            )

        # 2. Alphabetical among supported & available candidates
        suitable = sorted(name for name in candidates if self._is_suitable(name))
        if not suitable:
            logger.debug("No available plugin for (%s, '%s')", kind.value, ecosystem)
            return None

        if len(suitable) > 1:
            logger.info(
                "Multiple plugins available for (%s, '%s'): %s — selecting '%s'. "
                'Set a preference to choose explicitly.',
                kind.value,
                ecosystem,
                ', '.join(suitable),
                suitable[0],
            )
        return suitable[0]

    def _is_suitable(self, plugin_name: str) -> bool:
        """Check if *plugin_name* is both supported and available.

        Delegates to :meth:`ToolBasedPlugin.query_availability` which
        handles the ``is_supported`` / ``RuntimeConsumer`` /
        ``is_available_for`` decision tree.  Falls back to
        ``is_available()`` for bare ``Plugin`` instances that are not
        ``ToolBasedPlugin`` subclasses.
        """
        plugin = self._all_plugins.get(plugin_name)
        if plugin is None:
            return False
        if isinstance(plugin, ToolBasedPlugin):
            return plugin.query_availability(self._runtime_context)
        try:
            return type(plugin).is_supported() and plugin.is_available()
        except Exception:
            logger.warning("Suitability check failed for plugin '%s'", plugin_name, exc_info=True)
            return False
