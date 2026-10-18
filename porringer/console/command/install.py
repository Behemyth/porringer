"""CLI command implementation for install.

Porringer CLI install command.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from porringer.api import API
from porringer.console.command import sync as sync_command
from porringer.console.command.preview import _display_report
from porringer.console.common import (
    EXIT_FAILURE,
    GlobalOption,
    OnlyActionOption,
    PluginOption,
    ProjectDirOption,
    StrategyOption,
    TargetArgument,
    TargetPlan,
    build_setup_parameters,
    classify_target,
    confirm_or_abort,
    create_api,
    global_manifest_or_exit,
    parse_shared_options,
    resolve_target_or_exit,
)
from porringer.console.schema import ConsoleConfiguration
from porringer.schema import InspectionMode, SyncPlan, SyncStrategy
from porringer.utility.exception import PlanStaleError


@dataclass(slots=True)
class _InstallOptions:
    """Bundled options for install execution."""

    project_directory: Path | None = None
    fail_fast: bool = True
    strategy: SyncStrategy = SyncStrategy.MINIMAL
    plugin_names: set[str] | None = None
    action_ids: set[str] | None = None
    as_jsonl: bool = False
    record_path: Path | None = None
    yes: bool = False


def _confirm_or_abort(configuration: ConsoleConfiguration, yes: bool) -> None:
    """Require confirmation for the setup plan unless ``--yes`` was provided."""
    confirm_or_abort(configuration, yes=yes, prompt='Apply this setup plan?')


def _run_manifest_install(
    configuration: ConsoleConfiguration,
    api: API,
    paths: Path | list[str] | None,
    options: _InstallOptions,
) -> None:
    """Execute a manifest-oriented install target."""
    setup_params = build_setup_parameters(
        paths,
        project_directory=options.project_directory,
        strategy=options.strategy,
        plugin_names=options.plugin_names,
        action_ids=options.action_ids,
        fail_fast=options.fail_fast,
    )

    # JSONL output must stay machine-clean, so the human preview is skipped there.
    plan: SyncPlan | None = None
    if not options.as_jsonl:
        # Force COMPLETE inspection for the human preview regardless of what
        # setup_params carries. A confirmation prompt must show real
        # presence/update statuses, never FAST's unprobed placeholders.
        preview_params = setup_params.model_copy(update={'inspection_mode': InspectionMode.COMPLETE})
        with configuration.output.status('Inspecting...'):
            plan = asyncio.run(api.sync.plan(preview_params))
        _display_report(configuration, plan.report, compact=True)

    try:
        _confirm_or_abort(configuration, options.yes)
    except BaseException:
        if plan is not None:
            plan.discard()
        raise

    try:
        if options.as_jsonl or options.record_path is not None:
            execute_results = sync_command._execute_observable(
                setup_params,
                api=api,
                emit_jsonl=options.as_jsonl,
                record_path=options.record_path,
                plan=plan,
            )
        else:
            execute_results = sync_command._execute_with_progress(configuration, api, setup_params, plan=plan)
    except PlanStaleError as exc:
        configuration.output.error(f'{exc.error}. Run the install again to review the changes.')
        raise typer.Exit(EXIT_FAILURE) from None

    if execute_results.total_actions == 0:
        if not options.as_jsonl:
            configuration.output.warning('No actions to execute')
        return

    if not options.as_jsonl:
        sync_command._display_results(configuration, execute_results, options.strategy)

    if not execute_results.success:
        raise typer.Exit(EXIT_FAILURE)


def _run_profile_install(
    configuration: ConsoleConfiguration,
    api: API,
    plan: TargetPlan,
    options: _InstallOptions,
) -> None:
    """Execute a profile-oriented install target."""
    if plan.profile_url is None:
        configuration.output.error('Install link target is missing profile URL')
        raise typer.Exit(EXIT_FAILURE)

    with configuration.output.status('Inspecting...'):
        inspection = asyncio.run(
            api.profile.inspect(
                plan.profile_url,
                inspection_mode=InspectionMode.COMPLETE,
                expected_hash=plan.expected_hash,
            )
        )
    _display_report(configuration, inspection.inspection)

    _confirm_or_abort(configuration, options.yes)

    execution = asyncio.run(
        api.profile.run(
            plan.profile_url,
            parameters=build_setup_parameters(
                None,
                project_directory=options.project_directory,
                strategy=options.strategy,
                plugin_names=options.plugin_names,
                action_ids=options.action_ids,
                fail_fast=options.fail_fast,
            ),
            expected_hash=plan.expected_hash,
        )
    )

    sync_command._display_results(configuration, execution.results, options.strategy)
    if not execution.results.success:
        raise typer.Exit(EXIT_FAILURE)


def install_default(  # noqa: PLR0913
    context: typer.Context,
    target: TargetArgument = None,
    *,
    project_dir: ProjectDirOption = None,
    fail_fast: Annotated[
        bool,
        typer.Option('--fail-fast/--no-fail-fast', help='Stop on the first error'),
    ] = True,
    strategy: StrategyOption = 'minimal',
    plugin: PluginOption = None,
    only_action: OnlyActionOption = None,
    jsonl: Annotated[
        bool,
        typer.Option(
            '--jsonl',
            help='Stream progress and the final result as newline-delimited JSON',
        ),
    ] = False,
    record: Annotated[
        Path | None,
        typer.Option('--record', help='Write a replayable JSON run record to this path'),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            '--yes',
            '-y',
            envvar='PORRINGER_ASSUME_YES',
            help='Skip confirmation prompt (also honoured via the PORRINGER_ASSUME_YES env var)',
        ),
    ] = False,
    global_: GlobalOption = False,
) -> None:
    """Synchronize an environment from a manifest, profile, or install link.

    With no target, Porringer uses the nearest manifest in the current directory
    tree. You can also pass a local path, an https URL, or a porringer:// link,
    or --global for your global manifest.
    Porringer previews the plan and asks for confirmation before changing
    anything. Pass --yes (or set PORRINGER_ASSUME_YES=1) to run unattended.
    """
    configuration = context.ensure_object(ConsoleConfiguration)

    shared = parse_shared_options(
        configuration,
        strategy=strategy,
        project_dir=project_dir,
        plugin=plugin,
        only_action=only_action,
    )
    options = _InstallOptions(
        project_directory=shared.project_directory,
        fail_fast=fail_fast,
        strategy=shared.strategy,
        plugin_names=shared.plugin_names,
        action_ids=shared.action_ids,
        as_jsonl=jsonl,
        record_path=record,
        yes=yes,
    )

    if global_:
        api = create_api(configuration)
        path = global_manifest_or_exit(configuration, api, target=target, project_dir=project_dir)
        _run_manifest_install(configuration, api, path, options)
        return

    resolved_target = resolve_target_or_exit(configuration, target)
    api = create_api(configuration)
    plan = classify_target(configuration, api, resolved_target)

    if plan.is_profile:
        _run_profile_install(configuration, api, plan, options)
    else:
        _run_manifest_install(configuration, api, plan.manifest_paths, options)
