"""CLI command implementation for preview.

Inspects a manifest or setup profile and reports the plan without executing anything.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.panel import Panel

from porringer.console.common import (
    EXIT_FAILURE,
    GlobalOption,
    OnlyActionOption,
    PluginOption,
    ProjectDirOption,
    StrategyOption,
    TargetArgument,
    build_setup_parameters,
    classify_target,
    create_api,
    global_manifest_or_exit,
    parse_shared_options,
    resolve_target_or_exit,
)
from porringer.console.schema import ConsoleConfiguration
from porringer.schema import (
    InspectionMode,
    InspectionStatus,
    InspectionSummary,
    SyncInspectionReport,
    SyncStrategy,
)
from porringer.utility.observability import (
    explain_inspection_report,
    inspection_envelope,
)


@dataclass(slots=True)
class _PreviewOptions:
    """Bundled options for manifest preview."""

    strategy: SyncStrategy = SyncStrategy.MINIMAL
    inspection_mode: InspectionMode = InspectionMode.COMPLETE
    project_directory: Path | None = None
    plugin_names: set[str] | None = None
    action_ids: set[str] | None = None
    as_json: bool = False
    as_envelope: bool = False
    explain: bool = False


def _parse_inspection_mode(configuration: ConsoleConfiguration, mode: str) -> InspectionMode:
    """Parse a CLI inspection mode string."""
    try:
        return InspectionMode(mode.lower())
    except ValueError as exc:
        configuration.output.error(f"Invalid mode '{mode}'. Use: complete or fast")
        raise typer.Exit(EXIT_FAILURE) from exc


def _command_text(command: tuple[str, ...]) -> str:
    r"""Return display text for one command step.

    Shortens the leading token to its executable basename, so a full
    interpreter path like ``C:\...\Scripts\python.exe`` becomes
    ``python.exe``. This keeps long interpreter paths from crowding out
    the rest of the command. Plain command names (``git``, ``pdm``) pass
    through unchanged.
    """
    head, *rest = command
    return ' '.join((Path(head).name, *rest))


# Glyph shown before each status label in the preview list. Colour comes
# from the matching ``status.<value>`` style in PORRINGER_THEME, so both
# live in one place: output.py owns colour, this dict owns the glyph.
_STATUS_GLYPHS: dict[InspectionStatus, str] = {
    InspectionStatus.SATISFIED: '✓',
    InspectionStatus.NEEDED: '●',
    InspectionStatus.UPDATE_AVAILABLE: '↑',
    InspectionStatus.UNAVAILABLE: '✗',
    InspectionStatus.FAILED: '✗',
    InspectionStatus.SKIPPED: '⊝',
    InspectionStatus.UNKNOWN: '?',
}

# Labels for non-zero summary counts, in display order. Zero counts are
# omitted so the summary line stays short on the common "mostly satisfied"
# case instead of always spelling out every status at 0.
_SUMMARY_LABELS: tuple[tuple[str, str], ...] = (
    ('needed', 'needed'),
    ('update_available', 'update available'),
    ('unavailable', 'unavailable'),
    ('failed', 'failed'),
    ('skipped', 'skipped'),
    ('unknown', 'unknown'),
    ('satisfied', 'satisfied'),
)


def _status_text(status: InspectionStatus) -> str:
    """Return a themed, glyph-prefixed status label for the preview list."""
    style = f'status.{status.value}'
    glyph = _STATUS_GLYPHS.get(status, '')
    return f'[{style}]{glyph} {status.value}[/{style}]'


def _summary_text(summary: InspectionSummary) -> str:
    """Render non-zero summary counts, coloured to match the status glyphs."""
    parts = [
        f'[status.{field}]{count} {label}[/status.{field}]'
        for field, label in _SUMMARY_LABELS
        if (count := getattr(summary, field))
    ]
    body = ', '.join(parts) if parts else '[muted]nothing to do[/muted]'
    return f'{summary.actions} action(s): {body}'


def _compact_summary_text(summary: InspectionSummary) -> str:
    """Render the install plan count without treating advisories as actions."""
    executable = summary.actions - summary.satisfied - summary.update_available
    parts = [f'{executable} to execute']
    if summary.satisfied:
        parts.append(f'{summary.satisfied} already satisfied')
    if summary.update_available:
        parts.append(f'{summary.update_available} update available')
    return ' · '.join(parts)


def _display_action(output: Any, action: Any, *, display_index: int | None = None) -> None:
    """Render one action and its command steps."""
    index = action.index + 1 if display_index is None else display_index
    output.print(f'\n  {index}. {_status_text(action.status)}  [bold]{action.action.description}[/bold]')
    for step in action.cli_steps:
        output.print(f'     [muted]❯[/muted] [code]{escape(_command_text(step))}[/code]')
    if action.message:
        output.print(f'     [detail]{action.message}[/detail]')


def _display_compact_actions(output: Any, actions: tuple[Any, ...]) -> None:
    """Render executable actions and compact advisory sections for install."""
    plan_actions = tuple(
        action
        for action in actions
        if action.status not in {InspectionStatus.SATISFIED, InspectionStatus.UPDATE_AVAILABLE}
    )
    if plan_actions:
        output.print('  [heading]Plan:[/heading]')
        for display_index, action in enumerate(plan_actions, start=1):
            _display_action(output, action, display_index=display_index)

    satisfied_count = sum(action.status == InspectionStatus.SATISFIED for action in actions)
    if satisfied_count:
        output.print(f'  [muted]{satisfied_count} action(s) already satisfied[/muted]')

    updates = tuple(action for action in actions if action.status == InspectionStatus.UPDATE_AVAILABLE)
    if updates:
        output.print('\n  [heading]Available updates:[/heading]')
        for action in updates:
            message = action.message or action.action.description
            output.print(f'  [status.update_available]↑[/status.update_available] {escape(message)}')


def _display_report(
    configuration: ConsoleConfiguration,
    report: SyncInspectionReport,
    *,
    compact: bool = False,
) -> None:
    """Render an inspection report as a scannable action list.

    Each action gets its own header line (index, status, description)
    plus indented sub-lines for its command and message, if present.
    Unlike a fixed-width table, every line wraps at the console's full
    width, so long paths and messages stay readable instead of being
    squeezed into a narrow column.
    """
    output = configuration.output

    if report.inspection_mode == InspectionMode.FAST:
        output.warning("Fast inspection: statuses were not probed. 'unknown' means not checked, not confirmed needed.")

    for manifest in report.manifests:
        title = str(manifest.manifest_path or '(unknown manifest)')
        output.print(f'\n[heading]Manifest:[/heading] {title}')

        for diagnostic in manifest.diagnostics:
            style = 'error' if diagnostic.severity == 'error' else 'warning'
            field = f'{diagnostic.field}: ' if diagnostic.field else ''
            output.print(f'  [{style}]{diagnostic.severity}[/{style}] {field}{diagnostic.message}')

        if not manifest.actions:
            output.print('  [muted]No actions[/muted]')
            continue

        if compact:
            _display_compact_actions(output, manifest.actions)
        else:
            for action in manifest.actions:
                _display_action(output, action)

    for failed in report.failed_paths:
        output.print(f'\n[error]Failed:[/error] {failed.path}')
        output.print(f'  [muted]{failed.error}[/muted]')

    output.blank()
    summary_text = _compact_summary_text(report.summary) if compact else _summary_text(report.summary)
    output.print(
        Panel(
            summary_text,
            border_style='success' if report.success else 'error',
        )
    )


def _emit_report(
    configuration: ConsoleConfiguration,
    report: SyncInspectionReport,
    options: _PreviewOptions,
) -> None:
    """Emit an inspection report in the requested format."""
    if options.as_envelope:
        envelope = inspection_envelope(report, operation='sync.inspect')
        typer.echo(json.dumps(envelope.model_dump(mode='json'), indent=2))
        return

    if options.as_json:
        typer.echo(json.dumps(report.model_dump(mode='json'), indent=2))
        return

    if options.explain:
        for line in explain_inspection_report(report):
            configuration.output.print(line)
        if not report.success:
            raise typer.Exit(EXIT_FAILURE)
        return

    _display_report(configuration, report)

    if not report.success:
        raise typer.Exit(EXIT_FAILURE)


def preview_profile(
    configuration: ConsoleConfiguration,
    profile_url: str,
    options: _PreviewOptions,
    *,
    expected_hash: str | None = None,
) -> None:
    """Preview a remote setup profile without applying it."""
    api = create_api(configuration)
    try:
        inspection = asyncio.run(
            api.profile.inspect(
                profile_url,
                inspection_mode=options.inspection_mode,
                expected_hash=expected_hash,
            )
        )
    except ValueError as exc:
        configuration.output.error(str(exc))
        raise typer.Exit(EXIT_FAILURE) from exc

    configuration.output.print(f'[heading]Profile:[/heading] {inspection.profile.name}')
    configuration.output.print(f'[heading]Origin:[/heading] {profile_url}')
    pinned = 'pinned' if expected_hash else 'not pinned'
    configuration.output.print(f'[heading]Integrity:[/heading] HTTPS, sha256 {pinned}')

    _emit_report(configuration, inspection.inspection, options)


def preview_default(  # noqa: PLR0913
    context: typer.Context,
    target: TargetArgument = None,
    *,
    project_dir: ProjectDirOption = None,
    strategy: StrategyOption = 'minimal',
    mode: Annotated[
        str,
        typer.Option('--mode', help='Inspection mode: complete (default) or fast'),
    ] = InspectionMode.COMPLETE.value,
    plugin: PluginOption = None,
    as_json: Annotated[
        bool,
        typer.Option('--json', help='Emit machine-readable JSON'),
    ] = False,
    as_envelope: Annotated[
        bool,
        typer.Option('--envelope', help='Emit the common result envelope JSON'),
    ] = False,
    explain: Annotated[
        bool,
        typer.Option('--explain', help='Explain diagnostics and available follow-up actions'),
    ] = False,
    only_action: OnlyActionOption = None,
    global_: GlobalOption = False,
) -> None:
    """Preview what Porringer would do, without executing any actions."""
    configuration = context.ensure_object(ConsoleConfiguration)
    shared = parse_shared_options(
        configuration,
        strategy=strategy,
        project_dir=project_dir,
        plugin=plugin,
        only_action=only_action,
    )
    options = _PreviewOptions(
        strategy=shared.strategy,
        inspection_mode=_parse_inspection_mode(configuration, mode),
        project_directory=shared.project_directory,
        plugin_names=shared.plugin_names,
        action_ids=shared.action_ids,
        as_json=as_json,
        as_envelope=as_envelope,
        explain=explain,
    )

    if global_:
        api = create_api(configuration)
        manifest_paths: Path | list[str] | None = global_manifest_or_exit(
            configuration, api, target=target, project_dir=project_dir
        )
    else:
        resolved_target = resolve_target_or_exit(configuration, target)
        api = create_api(configuration)
        plan = classify_target(configuration, api, resolved_target)

        if plan.is_profile and plan.profile_url is not None:
            preview_profile(
                configuration,
                plan.profile_url,
                options,
                expected_hash=plan.expected_hash,
            )
            return
        manifest_paths = plan.manifest_paths

    params = build_setup_parameters(
        manifest_paths,
        project_directory=options.project_directory,
        strategy=options.strategy,
        plugin_names=options.plugin_names,
        action_ids=options.action_ids,
        inspection_mode=options.inspection_mode,
        fail_fast=False,
    )

    try:
        report = asyncio.run(api.sync.inspect(params))
    except ValueError as exc:
        configuration.output.error(str(exc))
        raise typer.Exit(EXIT_FAILURE) from exc

    _emit_report(configuration, report, options)
