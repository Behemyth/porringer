"""Console and CLI support for common.

Shared helpers and constants for Porringer CLI commands.
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from porringer.api import API
from porringer.console.schema import ConsoleConfiguration
from porringer.core.target import TargetKind, TargetResolution, resolve_target
from porringer.schema import InspectionMode, SetupParameters, SetupProfile, SyncStrategy

# Process exit codes shared across CLI commands.
EXIT_SUCCESS = 0
EXIT_FAILURE = 1


def create_api(configuration: ConsoleConfiguration) -> API:
    """Create an :class:`API` instance from the CLI configuration."""
    return API(configuration.local_configuration, configuration.global_configuration)


def _stdin_is_interactive() -> bool:
    """Whether a person can type an answer on stdin."""
    return sys.stdin.isatty()


def confirm_or_abort(configuration: ConsoleConfiguration, *, yes: bool, prompt: str) -> None:
    """Require confirmation for *prompt* unless ``yes`` was provided.

    An explicit decline (answering "n") is a normal outcome, so it prints a
    plain "Aborted." and exits cleanly. Ctrl-C is likewise a normal manual
    cancellation and gets the same plain treatment. Only when there is no
    interactive terminal to prompt on at all (confirmation genuinely can't
    be obtained) does this exit with a failure and an actionable hint
    naming the non-interactive options.  The same applies when a person is
    at the keyboard but output is redirected, so the question would be
    invisible and the run would appear to hang.
    """
    if yes:
        return

    console = configuration.output.console
    if _stdin_is_interactive() and not console.is_terminal:
        configuration.output.warning(
            'Cannot ask for confirmation while output is redirected. '
            'Pass --yes or set PORRINGER_ASSUME_YES=1 to run non-interactively.'
        )
        raise typer.Exit(EXIT_FAILURE)

    while True:
        try:
            # markup=False: the "[y/N]" default hint would otherwise be
            # parsed as an invalid, unclosed Rich style tag.
            answer = console.input(f'{prompt} [y/N]: ', markup=False).strip().lower()
        except KeyboardInterrupt:
            console.print()
            configuration.output.print('[muted]Aborted.[/muted]')
            raise typer.Exit(EXIT_SUCCESS) from None
        except EOFError:
            console.print()
            configuration.output.warning('Aborted. Pass --yes or set PORRINGER_ASSUME_YES=1 to run non-interactively.')
            raise typer.Exit(EXIT_FAILURE) from None

        if answer in {'y', 'yes'}:
            return
        if answer in {'', 'n', 'no'}:
            configuration.output.print('[muted]Aborted.[/muted]')
            raise typer.Exit(EXIT_SUCCESS)

        configuration.output.warning('Please answer y or n.')


async def sniff_profile(api: API, url: str) -> SetupProfile | None:
    """Return the parsed setup profile when *url* points at one.

    Bare ``https`` targets are ambiguous between a setup profile and a
    manifest.  A strict profile parse decides: parse failures mean the URL
    is treated as a manifest, while download failures propagate.

    Returns:
        The parsed profile, or ``None`` when the URL is not a profile.
    """
    try:
        return await api.profile.resolve(url)
    except ValueError as exc:
        if 'Failed to parse profile' in str(exc):
            return None
        raise


def parse_strategy(configuration: ConsoleConfiguration, strategy: str) -> SyncStrategy:
    """Parse a CLI strategy string into a :class:`SyncStrategy`.

    Exits with :data:`EXIT_FAILURE` when *strategy* is not recognized.
    """
    strategy_map = {'minimal': SyncStrategy.MINIMAL, 'latest': SyncStrategy.LATEST}
    sync_strategy = strategy_map.get(strategy.lower())
    if sync_strategy is None:
        configuration.output.error(f"Invalid strategy '{strategy}'. Use: minimal or latest")
        raise typer.Exit(EXIT_FAILURE)
    return sync_strategy


@dataclass(slots=True)
class TargetPlan:
    """A resolved install/preview target: either a setup profile or manifest paths."""

    profile_url: str | None = None
    expected_hash: str | None = None
    manifest_paths: Path | list[str] | None = None

    @property
    def is_profile(self) -> bool:
        """Whether the target resolved to a setup profile / install link."""
        return self.profile_url is not None


def resolve_target_or_exit(configuration: ConsoleConfiguration, target: str | None) -> TargetResolution:
    """Resolve a CLI target, exiting with an error on an invalid target."""
    try:
        return resolve_target(target)
    except ValueError as exc:
        configuration.output.error(str(exc))
        raise typer.Exit(EXIT_FAILURE) from exc


def target_to_paths(configuration: ConsoleConfiguration, target: TargetResolution) -> Path | list[str] | None:
    """Resolve a non-profile target to manifest path(s), exiting on a bad path."""
    if target.kind == TargetKind.PATH:
        if target.path is None or not target.path.exists():
            configuration.output.error(f'Path does not exist: {target.path}')
            raise typer.Exit(EXIT_FAILURE)
        return target.path.resolve()
    if target.kind == TargetKind.URL and target.url is not None:
        return [target.url]
    configuration.output.error(f'Unsupported target: {target.kind}')
    raise typer.Exit(EXIT_FAILURE)


def classify_target(configuration: ConsoleConfiguration, api: API, target: TargetResolution) -> TargetPlan:
    """Classify a resolved target as a setup profile or manifest paths.

    A bare ``https`` URL is ambiguous, so a strict profile parse decides
    whether it is a setup profile or a remote manifest.
    """
    if target.kind == TargetKind.URL and target.url is not None:
        try:
            profile = asyncio.run(sniff_profile(api, target.url))
        except ValueError as exc:
            configuration.output.error(str(exc))
            raise typer.Exit(EXIT_FAILURE) from exc
        if profile is not None:
            return TargetPlan(profile_url=target.url)

    if target.kind == TargetKind.LINK:
        if target.profile_url is None:
            configuration.output.error('Install link target is missing profile URL')
            raise typer.Exit(EXIT_FAILURE)
        return TargetPlan(profile_url=target.profile_url, expected_hash=target.expected_hash)

    return TargetPlan(manifest_paths=target_to_paths(configuration, target))


def build_setup_parameters(
    paths: Path | list[str] | None,
    *,
    project_directory: Path | None = None,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
    plugin_names: set[str] | None = None,
    action_ids: set[str] | None = None,
    inspection_mode: InspectionMode = InspectionMode.COMPLETE,
    fail_fast: bool = True,
) -> SetupParameters:
    """Construct :class:`SetupParameters` shared by install and preview.

    Defaults to ``COMPLETE`` inspection, matching ``SetupParameters``'
    own default. Callers that genuinely want a cheap shape-only pass
    (e.g. untrusted profile links) should pass ``InspectionMode.FAST``
    explicitly rather than relying on this helper's default.
    """
    return SetupParameters(
        paths=paths,
        project_directory=project_directory,
        fail_fast=fail_fast,
        strategy=strategy,
        plugin_names=plugin_names,
        action_ids=action_ids,
        inspection_mode=inspection_mode,
    )


# Reusable CLI option declarations shared by install and preview so their flag
# names and help text stay identical. The default value is supplied at each
# command's parameter (e.g. ``target: TargetArgument = None``).
TargetArgument = Annotated[
    str | None,
    typer.Argument(help='Manifest path, https URL, or porringer:// install link. Defaults to the nearest manifest.'),
]
ProjectDirOption = Annotated[
    Path | None,
    typer.Option('--project-dir', '-d', help='Working directory for project-install actions'),
]
StrategyOption = Annotated[
    str,
    typer.Option('--strategy', '-s', help='Version strategy: minimal or latest'),
]
PluginOption = Annotated[
    list[str] | None,
    typer.Option('--plugin', help='Limit actions to these plugins (repeatable)'),
]
OnlyActionOption = Annotated[
    list[str] | None,
    typer.Option(
        '--only-action',
        help='Run only these stable action ids, such as 0:2 (repeatable)',
    ),
]
GlobalOption = Annotated[
    bool,
    typer.Option('--global', help="Use your global manifest instead of a project's"),
]


def global_manifest_or_exit(
    configuration: ConsoleConfiguration,
    api: API,
    *,
    target: str | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Return the global manifest path, exiting when it can't be used."""
    if target is not None or project_dir is not None:
        configuration.output.error('--global cannot be combined with a target or --project-dir')
        raise typer.Exit(EXIT_FAILURE)
    path = api.global_manifest_path
    if not path.is_file():
        configuration.output.print(f'[muted]No global manifest yet: {path}[/muted]')
        raise typer.Exit(EXIT_SUCCESS)
    return path


@dataclass(slots=True)
class SharedOptions:
    """Parsed values for the options install and preview have in common."""

    strategy: SyncStrategy
    project_directory: Path | None
    plugin_names: set[str] | None
    action_ids: set[str] | None


def parse_shared_options(
    configuration: ConsoleConfiguration,
    *,
    strategy: str,
    project_dir: Path | None,
    plugin: list[str] | None,
    only_action: list[str] | None,
) -> SharedOptions:
    """Parse the CLI options shared by install and preview into typed values."""
    return SharedOptions(
        strategy=parse_strategy(configuration, strategy),
        project_directory=project_dir.resolve() if project_dir else None,
        plugin_names=set(plugin) if plugin else None,
        action_ids=set(only_action) if only_action else None,
    )
