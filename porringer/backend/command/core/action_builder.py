"""CLI command implementation for action builder.

Action plan construction.

Builds the list of `SetupAction` objects from a parsed manifest and
resolved plugins.  Also contains the preview/parse entry point that
loads a manifest and returns a `SetupResults` without executing.
"""

import logging
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from porringer.backend.backend import BackendResolver
from porringer.core.plugin_schema.environment import Environment, PackageParameters
from porringer.core.plugin_schema.project_environment import ProjectInstaller
from porringer.core.schema import Ecosystem, PackageRef, PluginKind
from porringer.schema import (
    ManifestMetadata,
    SetupAction,
    SetupManifest,
    SetupResults,
    SyncStrategy,
)

from ..manifest import find_manifest
from .discovery import DiscoveredPlugins, discover_all_plugins

logger = logging.getLogger(__name__)

# Known forges whose URLs are recognised for default-scm-from-url inference.
_CLONABLE_FORGES = {'github.com', 'gitlab.com', 'bitbucket.org', 'codeberg.org', 'sr.ht'}

# A clonable repo URL path has exactly `owner/repo` segments.
_OWNER_REPO_SEGMENTS = 2


def _clonable_repo_url(url: str) -> str | None:
    """Return a normalized clone URL when *url* looks like a repo page on a known forge.

    Conservative by design: requires a recognised forge host *and*
    exactly two non-empty path segments (``owner/repo``), so a project
    homepage, docs site (e.g. GitHub Pages), or org profile page is
    never mistaken for a clonable repository.

    Args:
        url: The manifest's ``url`` field, as a string.

    Returns:
        A normalized ``scheme://host/owner/repo`` clone URL, or
        ``None`` when *url* doesn't look like a repo page.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None
    if parsed.netloc.lower() not in _CLONABLE_FORGES:
        return None
    segments = [s for s in parsed.path.split('/') if s]
    if len(segments) != _OWNER_REPO_SEGMENTS:
        return None
    owner, repo = segments
    repo = repo.removesuffix('.git')
    if not owner or not repo:
        return None
    return f'{parsed.scheme}://{parsed.netloc}/{owner}/{repo}'


# Execution order for phased setup.
PHASE_ORDER: list[PluginKind] = [
    PluginKind.RUNTIME,
    PluginKind.PACKAGE,
    PluginKind.TOOL,
    PluginKind.SCM,
    PluginKind.PROJECT,
]

# O(1) lookup for the stable sort in `build_actions()`.
_PHASE_ORDER_INDEX: dict[PluginKind, int] = {kind: i for i, kind in enumerate(PHASE_ORDER)}


# Maps SyncStrategy to the human-readable verb used in action descriptions.
STRATEGY_VERB: dict[SyncStrategy, str] = {
    SyncStrategy.MINIMAL: 'Install',
    SyncStrategy.LATEST: 'Upgrade',
}


def action_description(
    kind: PluginKind,
    verb: str,
    installer: str | None,
    package: PackageRef | None = None,
    *,
    registered: bool = True,
) -> str:
    """Build a human-readable action description.

    Centralises the ``via <installer>`` / ``(deferred)`` / ``(no plugin)``
    pattern used in both `build_actions` (preview time) and
    `resolve_deferred_actions` (execution time).

    Args:
        kind: The plugin kind.
        verb: Action verb (e.g. ``"Install"``, ``"Upgrade"``).
        installer: Resolved installer name, or ``None`` for deferred.
        package: The target package (may be ``None`` for PROJECT).
        registered: When *installer* is ``None``, distinguishes
            ``(deferred)`` (registered but unavailable) from
            ``(no plugin)`` (not registered at all).

    Returns:
        Formatted description string.
    """
    if installer:
        suffix = f'via {installer}'
    elif registered:
        suffix = '(deferred)'
    else:
        suffix = '(no plugin)'

    if kind == PluginKind.PROJECT:
        return f'Sync project {suffix}'

    if kind == PluginKind.SCM:
        return f"Clone '{package}' {suffix}" if package else f'Clone {suffix}'

    if package is not None:
        return f"{verb} '{package}' {suffix}"

    return f'{verb} {suffix}'


def get_cli_steps(
    action: SetupAction,
    plugins: DiscoveredPlugins,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
) -> tuple[tuple[str, ...], ...]:
    """Gets the ordered CLI command steps for an action.

    Most actions run a single command, so this returns a one-element
    tuple. PROJECT actions can run multiple steps (e.g. Poetry's
    `poetry env use` before `poetry install`); those plugins return
    every step so a preview shows exactly what execution will run,
    never just the final one.

    Args:
        action: The action to get steps for.
        plugins: Discovered plugin container.
        strategy: The sync strategy (determines install vs upgrade command).

    Returns:
        Ordered command steps, or an empty tuple if not applicable.
    """
    environments = plugins.environments
    project_environments = plugins.project_environments
    scm_environments = plugins.scm_environments
    # Preview must show the command execution will actually run, so every
    # branch forwards the same resolved runtime context execution uses.
    runtime_context = plugins.runtime_context

    match action.kind:
        case PluginKind.PACKAGE | PluginKind.TOOL | PluginKind.RUNTIME:
            if action.installer and action.package and action.installer in environments:
                env = environments[action.installer]
                verb = 'upgrade' if strategy == SyncStrategy.LATEST else 'install'
                params = PackageParameters(
                    package=action.package,
                    include_prereleases=action.include_prereleases,
                    runtime_context=runtime_context,
                )
                command = env.package_command(verb, params)
                return (command.argv,) if command.argv else ()
        case PluginKind.PROJECT:
            proj_envs = project_environments or {}
            if action.installer and action.installer in proj_envs:
                # search_from only affects ProjectCommandPlan.directory (project
                # root auto-discovery), never the steps we read here, so a
                # placeholder path is fine for command display purposes.
                plan = proj_envs[action.installer].command_plan(Path('.'), runtime_context=runtime_context)
                return tuple(tuple(step) for step in plan.steps)
        case PluginKind.SCM:
            scm_envs = scm_environments or {}
            if action.installer and action.package and action.installer in scm_envs:
                scm_env = scm_envs[action.installer]
                cmd = scm_env.clone_command(action.package.name, Path('.'))
                return (tuple(cmd),) if cmd else ()
        case None:
            pass
    return ()


def get_cli_command(
    action: SetupAction,
    plugins: DiscoveredPlugins,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
) -> tuple[str, ...]:
    """Gets the primary CLI command for an action.

    For multi-step PROJECT actions this is the final step (matching
    `ProjectCommandPlan.argv`'s convention); use `get_cli_steps()` when
    every step matters, such as rendering a full preview.

    Args:
        action: The action to get the command for.
        plugins: Discovered plugin container.
        strategy: The sync strategy (determines install vs upgrade command).

    Returns:
        The CLI command as a tuple of strings, or empty tuple if not applicable.
    """
    steps = get_cli_steps(action, plugins, strategy)
    return steps[-1] if steps else ()


def _log_unresolved(resolver: BackendResolver, kind: PluginKind, ecosystem: Ecosystem) -> None:
    """Log an appropriate message when no installer could be resolved.

    Distinguishes between a completely unregistered ``(kind, ecosystem)``
    pair (ERROR — will never self-resolve) and a registered-but-unavailable
    pair (INFO — may become available after a preceding phase).
    """
    if not resolver.is_registered(kind, ecosystem):
        logger.error(
            "No plugin registered for (%s, '%s'). "
            'Ensure all required plugin packages are installed '
            'with their entry points available to porringer.',
            kind.value,
            ecosystem,
        )
    else:
        logger.info(
            "Plugin(s) %s registered for (%s, '%s') but unavailable; deferring",
            resolver.registered_names(kind, ecosystem),
            kind.value,
            ecosystem,
        )


def _emit_section_actions(
    actions: list[SetupAction],
    kind: PluginKind,
    ecosystem: Ecosystem,
    packages: list,
    installer: str | None,
    verb: str,
    is_registered: bool,
) -> None:
    """Append actions for a single manifest section to *actions*.

    Handles project, SCM and package/tool/runtime kinds.
    """
    if kind == PluginKind.PROJECT:
        actions.append(
            SetupAction(
                description=action_description(kind, verb, installer, registered=is_registered),
                kind=kind,
                ecosystem=ecosystem,
                installer=installer,
            )
        )
        return

    if kind == PluginKind.SCM:
        for package in packages:
            if not package.is_applicable():
                continue
            scm_description = package.description or str(package.name)
            desc = action_description(kind, verb, installer, package=package.name, registered=is_registered)
            actions.append(
                SetupAction(
                    description=desc,
                    kind=kind,
                    ecosystem=ecosystem,
                    installer=installer,
                    package=package.name,
                    package_description=scm_description,
                )
            )
        return

    for package in packages:
        if not package.is_applicable():
            continue
        desc = action_description(kind, verb, installer, package=package.name, registered=is_registered)
        actions.append(
            SetupAction(
                description=desc,
                kind=kind,
                ecosystem=ecosystem,
                installer=installer,
                package=package.name,
                package_description=package.description,
                include_prereleases=package.include_prereleases,
            )
        )


def _select_project_candidate(
    ecosystem: Ecosystem,
    candidates: list[str],
    *,
    preferences: dict[Ecosystem, str],
    evidence_by_ecosystem: dict[Ecosystem, list[str]],
    project_environments: dict[str, ProjectInstaller],
    resolver: BackendResolver,
) -> tuple[str | None, str | None]:
    """Pick which of *candidates* should own project sync for *ecosystem*.

    Returns:
        A ``(selected, intended)`` pair. ``selected`` is the resolved
        installer name to run now, or ``None`` when the intended plugin
        isn't available yet. ``intended`` is the plugin that should
        eventually own this ecosystem regardless of current
        availability, used to synthesize a bootstrap TOOL action; it is
        ``None`` only when no single candidate could be determined at
        all (ambiguous, no preference or evidence).
    """
    preferred = preferences.get(ecosystem)
    if preferred in candidates:
        return resolver.resolve(PluginKind.PROJECT, ecosystem), preferred

    evidence_candidates = evidence_by_ecosystem.get(ecosystem, [])
    if evidence_candidates:
        suitable_evidence = sorted(
            name for name in evidence_candidates if project_environments[name].query_availability()
        )
        selected = suitable_evidence[0] if suitable_evidence else None
        return selected, evidence_candidates[0]

    if len(candidates) == 1:
        return resolver.resolve(PluginKind.PROJECT, ecosystem), candidates[0]

    logger.warning(
        "Multiple project plugins are relevant for ecosystem '%s' but none has project-specific "
        'evidence: %s. Set a preference to choose explicitly.',
        ecosystem,
        ', '.join(candidates),
    )
    return None, None


def _synthesize_project_tool_bootstrap(
    ecosystem: Ecosystem,
    intended: str,
    *,
    project_environments: dict[str, ProjectInstaller],
    existing_tool_packages: set[tuple[Ecosystem, str]],
    resolver: BackendResolver,
    verb: str,
) -> SetupAction | None:
    """Synthesize a TOOL install action for *intended*'s own package, if needed.

    The project sync itself can't run yet, so an earlier phase must
    install the intended plugin's CLI first (e.g. ``pdm`` via
    ``pipx``). Returns ``None`` when there's nothing to add: no
    package name, an explicit manifest ``tools`` entry already covers
    it, or no TOOL plugin is registered for this ecosystem at all.
    """
    tool_package_name = project_environments[intended].tool_name()
    if tool_package_name is None or (ecosystem, tool_package_name) in existing_tool_packages:
        return None

    tool_installer = resolver.resolve(PluginKind.TOOL, ecosystem)
    tool_registered = tool_installer is not None or resolver.is_registered(PluginKind.TOOL, ecosystem)
    if not tool_registered:
        return None

    package = PackageRef.model_validate(tool_package_name)
    existing_tool_packages.add((ecosystem, tool_package_name))
    return SetupAction(
        description=action_description(PluginKind.TOOL, verb, tool_installer, package=package, registered=True),
        kind=PluginKind.TOOL,
        ecosystem=ecosystem,
        installer=tool_installer,
        package=package,
    )


def _build_implicit_project_actions(
    plugins: DiscoveredPlugins,
    resolver: BackendResolver,
    preferences: dict[Ecosystem, str],
    *,
    search_from: Path,
    existing_actions: list[SetupAction],
    verb: str,
) -> list[SetupAction]:
    """Add one implicit project-sync action for each relevant ecosystem.

    When the intended project plugin's own CLI is unavailable (e.g. the
    ``pdm`` binary is not on PATH), also synthesize a TOOL install action
    for it (e.g. ``pdm`` via ``pipx``) so an earlier phase installs the
    missing tool automatically — chaining into the pipx bootstrap when
    needed.  An explicit manifest ``tools`` entry for the same package
    always wins (no duplicate synthesized).

    Args:
        plugins: Discovered plugin container.
        resolver: The backend resolver used for PROJECT/TOOL resolution.
        preferences: Ecosystem → preferred plugin-name mapping.
        search_from: The directory from which project-relevance discovery
            should start.
        existing_actions: Actions already built for this manifest
            (read-only; used to detect explicit ``tools`` overrides).
        verb: Action verb (e.g. ``"Install"``) for synthesized TOOL
            action description text.
    """
    actions: list[SetupAction] = []
    project_environments = plugins.project_environments or {}
    by_ecosystem: dict[Ecosystem, list[str]] = {}
    evidence_by_ecosystem: dict[Ecosystem, list[str]] = {}

    for installer, plugin in sorted(project_environments.items()):
        if not plugin.project_relevance(search_from):
            continue
        ecosystem = plugin.ecosystem()
        by_ecosystem.setdefault(ecosystem, []).append(installer)
        if plugin.project_evidence(search_from):
            evidence_by_ecosystem.setdefault(ecosystem, []).append(installer)

    existing_tool_packages = {
        (a.ecosystem, a.package.name)
        for a in existing_actions
        if a.kind == PluginKind.TOOL and a.ecosystem is not None and a.package is not None
    }

    for ecosystem, candidates in sorted(by_ecosystem.items(), key=lambda item: str(item[0])):
        selected, intended = _select_project_candidate(
            ecosystem,
            candidates,
            preferences=preferences,
            evidence_by_ecosystem=evidence_by_ecosystem,
            project_environments=project_environments,
            resolver=resolver,
        )
        if intended is None:
            continue

        if selected is None:
            if resolver.is_registered(PluginKind.PROJECT, ecosystem):
                actions.append(
                    SetupAction(
                        description=action_description(PluginKind.PROJECT, 'Sync', None, registered=True),
                        kind=PluginKind.PROJECT,
                        ecosystem=ecosystem,
                        installer=None,
                    )
                )
            else:
                _log_unresolved(resolver, PluginKind.PROJECT, ecosystem)

            bootstrap_action = _synthesize_project_tool_bootstrap(
                ecosystem,
                intended,
                project_environments=project_environments,
                existing_tool_packages=existing_tool_packages,
                resolver=resolver,
                verb=verb,
            )
            if bootstrap_action is not None:
                actions.append(bootstrap_action)
            continue

        actions.append(
            SetupAction(
                description=action_description(PluginKind.PROJECT, 'Sync', selected),
                kind=PluginKind.PROJECT,
                ecosystem=ecosystem,
                installer=selected,
            )
        )
    return actions


def _build_bootstrap_actions(
    actions: list[SetupAction],
    plugins: DiscoveredPlugins,
    resolver: BackendResolver,
    preferences: dict[Ecosystem, str],
    verb: str,
) -> list[SetupAction]:
    """Synthesize prerequisite package installs for deferred tool/runtime actions.

    A TOOL or RUNTIME action is *deferred* (``installer=None``) when its
    registered candidate(s) are not currently available.  When one of
    those candidates declares a :meth:`Environment.bootstrap_requirement`
    (e.g. ``pipx`` needs ``pip``), synthesize a PACKAGE-phase action that
    installs the prerequisite.  The existing phase ordering
    (Package → refresh → Tool) and deferred-resolution mechanism then
    pick up the now-available candidate automatically.

    An explicit preference for a *different* registered candidate
    suppresses the bootstrap (e.g. ``preferences: {"python": "uv"}``
    must not trigger a ``pipx`` bootstrap for a pdm/poetry tool entry).

    Args:
        actions: Actions already built for this manifest (read-only;
            used to detect deferred actions and existing explicit
            entries for dedupe).
        plugins: Discovered plugin container.
        resolver: The backend resolver used to find registered candidates.
        preferences: Ecosystem → preferred plugin-name mapping.
        verb: Action verb (e.g. ``"Install"``) for description text.

    Returns:
        Newly synthesized bootstrap actions (empty when none are needed).
    """
    environments = plugins.environments or {}
    synthesized: list[SetupAction] = []
    seen_keys = {
        (a.kind, a.ecosystem, a.package.name)
        for a in actions
        if a.kind is not None and a.ecosystem is not None and a.package is not None
    }

    for action in actions:
        if (
            action.installer is not None
            or action.kind not in {PluginKind.TOOL, PluginKind.RUNTIME}
            or action.ecosystem is None
        ):
            continue

        ecosystem = action.ecosystem
        candidates = resolver.registered_names(action.kind, ecosystem)
        preferred = preferences.get(ecosystem)

        for name in candidates:
            plugin = environments.get(name)
            if plugin is None:
                continue
            requirement = type(plugin).bootstrap_requirement()
            if requirement is None:
                continue

            # GATE: an explicit preference for a *different* registered
            # candidate means the user chose another tool — don't
            # bootstrap this one on their behalf.
            if preferred is not None and preferred != name and preferred in candidates:
                continue

            key = (requirement.kind, ecosystem, requirement.package.name)
            if key in seen_keys:
                continue  # explicit manifest entry (or prior bootstrap) already covers this

            # Resolve the actual installer through the same resolver used
            # everywhere else, so an unavailable prerequisite (e.g. pip
            # not yet on PATH) defers exactly like any other action
            # instead of being assumed present.
            boot_installer = resolver.resolve(requirement.kind, ecosystem)
            is_registered = boot_installer is not None or resolver.is_registered(requirement.kind, ecosystem)
            if not is_registered:
                logger.debug(
                    "Cannot bootstrap '%s': no installer plugin registered for (%s, '%s')",
                    requirement.package,
                    requirement.kind.value,
                    ecosystem,
                )
                continue

            desc = action_description(
                requirement.kind, verb, boot_installer, package=requirement.package, registered=is_registered
            )
            synthesized.append(
                SetupAction(
                    description=desc,
                    kind=requirement.kind,
                    ecosystem=ecosystem,
                    installer=boot_installer,
                    package=requirement.package,
                )
            )
            seen_keys.add(key)

    return synthesized


def _build_scm_from_url_action(
    manifest: SetupManifest,
    resolver: BackendResolver,
    verb: str,
) -> SetupAction | None:
    """Synthesize a git-clone action from ``manifest.url`` when ``scm`` is empty.

    Only triggers when the manifest declares no ``scm`` section at all —
    an explicit ``scm`` entry (for git or any other ecosystem) always
    wins and no duplicate is produced.  ``url`` remains display metadata
    even when it isn't clonable (e.g. a homepage or docs site).

    Args:
        manifest: The parsed setup manifest.
        resolver: The backend resolver used for SCM resolution.
        verb: Action verb (e.g. ``"Install"``) for description text.

    Returns:
        The synthesized SCM action, or ``None`` when not applicable.
    """
    if manifest.scm or manifest.url is None:
        return None

    clonable = _clonable_repo_url(str(manifest.url))
    if clonable is None:
        return None

    git_ecosystem = Ecosystem('git')
    installer = resolver.resolve(PluginKind.SCM, git_ecosystem)
    is_registered = installer is not None or resolver.is_registered(PluginKind.SCM, git_ecosystem)
    if not is_registered:
        return None

    package = PackageRef.model_validate(clonable)
    desc = action_description(PluginKind.SCM, verb, installer, package=package, registered=is_registered)
    return SetupAction(
        description=desc,
        kind=PluginKind.SCM,
        ecosystem=git_ecosystem,
        installer=installer,
        package=package,
        package_description=clonable,
    )


# ---------------------------------------------------------------------------
# Runtime inference from requires-python
# ---------------------------------------------------------------------------

_PYTHON_ECOSYSTEM = Ecosystem('python')

# Specifier operators that establish a *lower* bound on the version.
_LOWER_BOUND_OPERATORS = {'>=', '>', '==', '~='}


def _find_ancestor_pyproject(search_from: Path) -> Path | None:
    """Walk ancestor directories from *search_from* looking for ``pyproject.toml``.

    Mirrors the discovery strategy already used for project-root
    resolution (:meth:`ProjectEnvironment.resolve_project_root`) so that
    a manifest living in a subdirectory of the actual project still
    finds the project's ``pyproject.toml``.

    Returns:
        The discovered ``pyproject.toml`` path, or ``None`` when no
        ancestor directory contains one.
    """
    current = search_from.resolve()
    while True:
        candidate = current / 'pyproject.toml'
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _infer_runtime_version(requires_python: str) -> str | None:
    """Derive an install-target version from a ``requires-python`` specifier set.

    Uses the lowest declared lower-bound version's ``major.minor`` as
    the version to install (e.g. ``'>=3.11'`` → ``'3.11'``).  Per audit
    D8, returns ``None`` when no lower bound exists (e.g. a bare
    ``'<4'``) — there's nothing safe to infer in that case.

    Args:
        requires_python: The raw ``requires-python`` specifier string.

    Returns:
        A ``'major.minor'`` version string, or ``None`` when it cannot
        be determined.
    """
    try:
        spec_set = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return None

    lower_bounds: list[Version] = []
    for spec in spec_set:
        if spec.operator in _LOWER_BOUND_OPERATORS:
            try:
                lower_bounds.append(Version(spec.version))
            except InvalidVersion:
                continue

    if not lower_bounds:
        return None

    lowest = min(lower_bounds)
    return f'{lowest.major}.{lowest.minor}'


def _read_requires_python(pyproject: Path) -> str | None:
    """Read the ``[project].requires-python`` value from *pyproject*, if present."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    except OSError, tomllib.TOMLDecodeError:
        return None
    project_table = data.get('project')
    if not isinstance(project_table, dict):
        return None
    requires_python = project_table.get('requires-python')
    return requires_python if isinstance(requires_python, str) else None


def infer_runtime_from_pyproject(search_from: Path) -> str | None:
    """Infer a Python runtime version from an already-local ``pyproject.toml``.

    Walks ancestors of *search_from* looking for ``pyproject.toml`` and,
    when found, derives an install version from its ``requires-python``
    field (see :func:`_infer_runtime_version`).

    This only covers the case where the project is **already on disk**
    (e.g. the manifest lives inside the project it configures).  A
    manifest that clones the project via ``scm`` before it exists
    locally cannot be inferred this way — see the runtime-inference
    limitation note in ``_build_implicit_runtime_actions``.

    Returns:
        A ``'major.minor'`` version string, or ``None`` when no
        ``pyproject.toml``/``requires-python`` is found or no lower
        bound could be derived.
    """
    pyproject = _find_ancestor_pyproject(search_from)
    if pyproject is None:
        return None
    requires_python = _read_requires_python(pyproject)
    if requires_python is None:
        return None
    return _infer_runtime_version(requires_python)


def _build_implicit_runtime_action(
    manifest: SetupManifest,
    resolver: BackendResolver,
    verb: str,
    *,
    search_from: Path,
) -> SetupAction | None:
    """Synthesize a Python RUNTIME action from ``requires-python`` when none is declared.

    An explicit ``runtimes.python`` manifest entry always wins (e.g. to
    pin a patch version) — inference only fills the gap when the
    section is absent entirely.

    Known limitation (audit D7/D9): when the project is not yet cloned
    (a standalone manifest whose only reference to the project is an
    ``scm`` entry), ``pyproject.toml`` doesn't exist locally yet and no
    inference is possible at preview time.  In that case this logs an
    informational message and the manifest should declare `runtimes`
    explicitly if the target runtime must be pinned.  (A synchronous
    "acquire-then-infer" two-pass flow was scoped for this case but is
    deferred as future work — see plan D7's fallback.)

    Args:
        manifest: The parsed setup manifest.
        resolver: The backend resolver used for RUNTIME resolution.
        verb: Action verb (e.g. ``"Install"``) for description text.
        search_from: The directory to search for ``pyproject.toml``.

    Returns:
        The synthesized RUNTIME action, or ``None`` when not applicable.
    """
    if _PYTHON_ECOSYSTEM in manifest.runtimes:
        return None  # explicit entry always wins

    pyproject = _find_ancestor_pyproject(search_from)
    if pyproject is None:
        if manifest.scm:
            logger.info(
                'No local pyproject.toml found to infer a Python runtime from. '
                'If this manifest clones the project via scm, declare `runtimes.python` '
                'explicitly until the project exists locally.'
            )
        return None

    requires_python = _read_requires_python(pyproject)
    if requires_python is None:
        return None

    version = _infer_runtime_version(requires_python)
    if version is None:
        logger.info(
            'Could not infer a runtime version from requires-python=%r (no lower bound); '
            'declare `runtimes.python` explicitly to pin a version.',
            requires_python,
        )
        return None

    installer = resolver.resolve(PluginKind.RUNTIME, _PYTHON_ECOSYSTEM)
    is_registered = installer is not None or resolver.is_registered(PluginKind.RUNTIME, _PYTHON_ECOSYSTEM)
    if not is_registered:
        return None

    package = PackageRef.model_validate(version)
    desc = action_description(PluginKind.RUNTIME, verb, installer, package=package, registered=is_registered)
    return SetupAction(
        description=desc,
        kind=PluginKind.RUNTIME,
        ecosystem=_PYTHON_ECOSYSTEM,
        installer=installer,
        package=package,
    )


def _compute_needed_pairs(
    manifest: SetupManifest,
    plugins: DiscoveredPlugins,
    *,
    search_from: Path,
) -> set[tuple[PluginKind, Ecosystem]]:
    """Return the (kind, ecosystem) pairs the resolver needs to resolve.

    Only resolving pairs actually referenced by the manifest or relevant
    project plugins keeps the resolver from warning about unrelated
    plugins, and lets the synthesis helpers below (bootstrap, SCM-from-URL,
    implicit runtime) query real availability instead of assuming it.
    """
    needed_pairs: set[tuple[PluginKind, Ecosystem]] = set()
    for kind, ecosystem, _packages in manifest.iter_sections():
        needed_pairs.add((kind, ecosystem))
    for plugin in (plugins.project_environments or {}).values():
        if plugin.project_relevance(search_from):
            eco = plugin.ecosystem()
            needed_pairs.add((PluginKind.PROJECT, eco))
            needed_pairs.add((PluginKind.TOOL, eco))
    # Eagerly resolve (kind, ecosystem) pairs for any bootstrap prerequisite
    # (e.g. pipx -> pip) so `_build_bootstrap_actions` can query real
    # availability via the resolver instead of assuming it.
    for plugin in (plugins.environments or {}).values():
        requirement = type(plugin).bootstrap_requirement()
        if requirement is None:
            continue
        eco = type(plugin).ecosystem()
        if eco is not None:
            needed_pairs.add((requirement.kind, eco))
    # Eagerly resolve (SCM, git) when the manifest has no explicit scm
    # section but its url looks like a clonable repo, so
    # `_build_scm_from_url_action` can query real availability.
    if not manifest.scm and manifest.url is not None and _clonable_repo_url(str(manifest.url)) is not None:
        needed_pairs.add((PluginKind.SCM, Ecosystem('git')))
    # Eagerly resolve (RUNTIME, python) when requires-python can be read
    # from an already-local pyproject.toml, so `_build_implicit_runtime_action`
    # can query real availability via the resolver.
    if _PYTHON_ECOSYSTEM not in manifest.runtimes and infer_runtime_from_pyproject(search_from) is not None:
        needed_pairs.add((PluginKind.RUNTIME, _PYTHON_ECOSYSTEM))
    return needed_pairs


def build_actions(
    manifest: SetupManifest,
    plugins: DiscoveredPlugins | dict[str, Environment],
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
    *,
    search_from: Path | None = None,
) -> list[SetupAction]:
    """Builds the list of actions from a manifest.

    Iterates each kind section in the manifest.  Package/tool/runtime
    entries produce one action per package.  Project entries produce a
    single action per ecosystem.  SCM entries produce one action per
    repository URL.  The `strategy` parameter controls only the
    human-readable description verb; the execution layer decides
    install-vs-upgrade behaviour at runtime.

    Args:
        manifest: The parsed setup manifest.
        plugins: Discovered plugin container, **or** a plain
            ``dict[str, Environment]`` for backward compatibility
            with existing callers / tests.
        strategy: The sync strategy (used for description text).
        search_from: The directory from which project-relevance discovery
            should start.

    Returns:
        List of actions to perform.
    """
    # Accept a plain environments dict for backward compat (tests, etc.)
    if isinstance(plugins, dict):
        plugins = DiscoveredPlugins(environments=plugins, project_environments={}, scm_environments={})

    actions: list[SetupAction] = []

    if search_from is None:
        search_from = Path('.')

    needed_pairs = _compute_needed_pairs(manifest, plugins, search_from=search_from)
    resolver = BackendResolver(plugins.all_plugins, manifest.preferences, needed_pairs=needed_pairs)

    verb = STRATEGY_VERB[strategy]

    # Iterate each non-project section from the manifest.
    for kind, ecosystem, packages in manifest.iter_sections():
        if kind == PluginKind.PROJECT:
            continue

        installer = resolver.resolve(kind, ecosystem)

        if installer is None:
            _log_unresolved(resolver, kind, ecosystem)

        is_registered = installer is not None or resolver.is_registered(kind, ecosystem)
        _emit_section_actions(actions, kind, ecosystem, packages, installer, verb, is_registered)

    implicit_project_actions = _build_implicit_project_actions(
        plugins,
        resolver,
        dict(manifest.preferences),
        search_from=search_from,
        existing_actions=actions,
        verb=verb,
    )
    if implicit_project_actions:
        actions.extend(implicit_project_actions)

    bootstrap_actions = _build_bootstrap_actions(actions, plugins, resolver, dict(manifest.preferences), verb)
    if bootstrap_actions:
        actions.extend(bootstrap_actions)

    scm_from_url_action = _build_scm_from_url_action(manifest, resolver, verb)
    if scm_from_url_action is not None:
        actions.append(scm_from_url_action)

    implicit_runtime_action = _build_implicit_runtime_action(manifest, resolver, verb, search_from=search_from)
    if implicit_runtime_action is not None:
        actions.append(implicit_runtime_action)

    # Stable-sort into canonical phase order (PHASE_ORDER) so the plan
    # displayed to the user always matches execution order, regardless
    # of the (now largely inference-driven) order actions were built in.
    # Stability preserves manifest-declared order within each kind, and
    # keeps explicit entries ahead of any synthesized action of the same
    # kind.
    actions.sort(key=lambda a: _PHASE_ORDER_INDEX.get(a.kind, len(PHASE_ORDER)))

    return actions


def _build_preview(
    path: Path,
    strategy: SyncStrategy,
    *,
    use_cache: bool,
    log_label: str,
    plugins: DiscoveredPlugins | None = None,
) -> SetupResults:
    """Shared implementation for :func:`parse_manifest` and :func:`load_manifest`.

    Finds and parses the manifest, optionally uses cached plugin
    discovery to resolve installer names, builds the action list,
    and returns a :class:`SetupResults` preview.

    Args:
        path: Path to manifest file or directory containing one.
        strategy: The sync strategy.
        use_cache: Forwarded to :func:`discover_all_plugins` when
            *plugins* is ``None``.
        log_label: Human-readable label for the log message.
        plugins: Pre-discovered plugins.  When provided, plugin
            discovery is skipped entirely.

    Returns:
        SetupResults containing the action plan.

    Raises:
        ManifestError: If the manifest cannot be found or parsed.
    """
    logger.info(f'{log_label} from: {path}')

    result = find_manifest(path)
    resolved_plugins = plugins if plugins is not None else discover_all_plugins(use_cache=use_cache)
    actions = build_actions(
        result.manifest,
        resolved_plugins,
        strategy,
        search_from=result.root_directory,
    )
    metadata = ManifestMetadata(
        name=result.manifest.name,
        description=result.manifest.description,
        author=result.manifest.author,
        url=str(result.manifest.url) if result.manifest.url else None,
    )

    return SetupResults(
        actions=actions,
        manifest_path=result.manifest_path,
        root_directory=result.root_directory,
        metadata=metadata,
        preferences=dict(result.manifest.preferences),
    )


def parse_manifest(path: Path, strategy: SyncStrategy = SyncStrategy.MINIMAL) -> SetupResults:
    """Parse a manifest and build the action plan without executing.

    The returned `SetupResults.actions` list contains `SetupAction`
    objects with the following fields useful for introspection:

    * `installer` — canonical plugin name (e.g. `"uv"`, `"brew"`).
    * `kind` — `PluginKind` enum (`PACKAGE`, `TOOL`, `RUNTIME`,
            `PROJECT`, `SCM`).
    * `ecosystem` — ecosystem identifier (e.g. `"python"`, `"node"`).
    * `package` — `PackageRef` with name and optional version constraint.

    This is an internal helper for tests and implementation code. Public
    callers should use `api.sync.inspect(...)` for read-only manifest
    information.

    Args:
        path: Path to manifest file or directory containing one.
        strategy: The sync strategy.

    Returns:
        SetupResults containing the list of actions that would be performed.

    Raises:
        ManifestError: If the manifest cannot be found or parsed.
    """
    return _build_preview(path, strategy, use_cache=True, log_label='Parsing manifest')


def load_manifest(
    path: Path,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
    *,
    plugins: DiscoveredPlugins | None = None,
) -> SetupResults:
    """Load a manifest using cached plugin knowledge.

    This is the fast path for GUI clients: it reads JSON, builds
    ``SetupAction`` objects using cached plugin knowledge, and
    returns immediately.  On a warm cache the only I/O is the
    manifest file read.  Actions whose ``installer`` cannot be
    resolved from cached plugins will have ``installer=None``
    (deferred) — the execution engine resolves them at phase
    boundaries.

    Use :func:`parse_manifest` when you need a fully-resolved preview
    with populated CLI commands.

    Args:
        path: Path to manifest file or directory containing one.
        strategy: The sync strategy.
        plugins: Pre-discovered plugins. ``None`` uses cached
            discovery internally.

    Returns:
        SetupResults containing the action plan.  Actions with
        unresolvable installers have ``installer=None``.

    Raises:
        ManifestError: If the manifest cannot be found or parsed.
    """
    return _build_preview(path, strategy, use_cache=True, log_label='Loading manifest (fast)', plugins=plugins)
