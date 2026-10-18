"""CLI command implementation for sync.

Shared manifest execution helpers for Porringer CLI commands.

The user-facing entry point is ``porringer install``; this module keeps the
progress tracking, observable execution, and result rendering helpers it
reuses.
"""

import asyncio
import contextlib
import json
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn

from porringer.api import API
from porringer.console.schema import ConsoleConfiguration
from porringer.schema import (
    ActionCompletedEvent,
    ActionProgress,
    ActionProgressEvent,
    ActionRef,
    ActionStartedEvent,
    BatchSetupResults,
    ManifestFailedEvent,
    ManifestLoadedEvent,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SyncPlan,
    SyncStrategy,
    progress_event_snapshot,
)
from porringer.utility.observability import batch_envelope, replay_record

DEFAULT_TIMEOUT = 300

# Arrow prefix for command display
ARROW = '→'

# Trailing subprocess output lines retained per in-flight action, surfaced
# under the action's line when it fails.
_OUTPUT_TAIL_LINES = 15


@dataclass(slots=True)
class _ProgressState:
    """Execution progress state for event updates."""

    completed: int = 0
    total_actions: int = 0
    active_tasks: dict[str, TaskID] = field(default_factory=dict)
    overall_task: TaskID | None = None
    output_tails: dict[str, deque[str]] = field(default_factory=dict)
    """Bounded tail of streamed subprocess output lines per action key,
    printed under the action when it fails so the CLI shows *why* without
    requiring ``PORRINGER_TRACE_DIR``."""
    latest_output: dict[str, str] = field(default_factory=dict)
    started_at: dict[str, float] = field(default_factory=dict)
    verbose_output: bool = False


def _progress_label(strategy: SyncStrategy) -> str:
    """Get the progress label based on sync strategy."""
    if strategy == SyncStrategy.LATEST:
        return 'Upgrading packages...'
    return 'Syncing packages...'


def _action_description(action: SetupAction) -> str:
    """Build a short description for progress output."""
    return str(action.package) if action.package else action.description[:30]


def _action_event_key(action: SetupAction, action_ref: ActionRef | None) -> str:
    """Return the stable progress-task key for an action event."""
    if action_ref is not None:
        return action_ref.action_id
    return f'legacy:{id(action)}'


@dataclass(slots=True)
class _ProgressTracker:
    """Tracks progress during evented execution, reducing parameter passing."""

    progress: Progress
    setup_params: SetupParameters
    state: _ProgressState

    def handle_action_started(self, action_key: str, action_desc: str) -> None:
        """Record that an action has started."""
        if self.state.total_actions > 0:
            task_id = self.progress.add_task(f'  {action_desc}', total=1)
            self.state.active_tasks[action_key] = task_id
            self.state.started_at[action_key] = time.monotonic()

    def handle_action_completed(
        self,
        action_key: str,
        action_desc: str,
        action_ref: ActionRef | None,
        result: SetupActionResult | None,
    ) -> None:
        """Record that an action has completed and update the progress bar."""
        if action_key in self.state.active_tasks:
            task_id = self.state.active_tasks.pop(action_key)
            if result and result.success:
                if result.skipped:
                    self.progress.update(
                        task_id,
                        description=f'  [dim]{action_desc} (skipped)[/dim]',
                        completed=1,
                    )
                else:
                    self.progress.update(
                        task_id,
                        description=f'  [green]{action_desc}[/green]',
                        completed=1,
                    )
            else:
                self.progress.update(task_id, description=f'  [red]{action_desc}[/red]', completed=1)

        tail = self.state.output_tails.pop(action_key, None)
        self.state.latest_output.pop(action_key, None)
        self.state.started_at.pop(action_key, None)
        if result is not None and not result.success and not result.skipped and tail:
            self.progress.console.print(f'  [error]{ARROW}[/error] {action_desc} output:')
            for line in tail:
                self.progress.console.print(f'    [muted]{escape(line)}[/muted]')

        self.state.completed += 1
        overall_task = self.state.overall_task
        if self.state.total_actions > 0 and overall_task is not None:
            self.progress.update(overall_task, completed=self.state.completed)

    def handle_action_progress(self, action_key: str, action_desc: str, progress_update: ActionProgress | None) -> None:
        """Update the progress bar with action progress detail, and buffer raw output."""
        if progress_update is None or action_key not in self.state.active_tasks:
            return

        if progress_update.output is not None:
            tail = self.state.output_tails.setdefault(action_key, deque(maxlen=_OUTPUT_TAIL_LINES))
            tail.append(progress_update.output)
            compact_output = ' '.join(progress_update.output.split())
            if compact_output:
                self.state.latest_output[action_key] = compact_output
            if self.state.verbose_output:
                channel = progress_update.channel or 'output'
                self.progress.console.print(
                    f'  [muted]{escape(action_desc)} [{channel}][/muted] {escape(progress_update.output)}'
                )

        task_id = self.state.active_tasks[action_key]
        phase = progress_update.phase
        started_at = self.state.started_at.get(action_key)
        elapsed = time.monotonic() - (started_at if started_at is not None else time.monotonic())
        elapsed_text = f'{elapsed:.0f}s'
        step = ''
        if progress_update.step_index is not None and progress_update.step_total is not None:
            step = f' step {progress_update.step_index}/{progress_update.step_total}'
        latest = self.state.latest_output.get(action_key)
        detail = progress_update.message or latest

        desc = (
            f'  {action_desc} [{phase}{step} · {elapsed_text}] {detail}'
            if detail
            else f'  {action_desc} [{phase}{step} · {elapsed_text}]'
        )

        console_width = getattr(self.progress.console, 'width', 104)
        max_desc_len = max(40, console_width - 24)
        if len(desc) > max_desc_len:
            desc = desc[: max_desc_len - 3] + '...'

        if progress_update.progress is not None:
            self.progress.update(task_id, description=desc, completed=progress_update.progress, total=1.0)
        else:
            self.progress.update(task_id, description=desc)

    def handle_progress_event(self, event: ProgressEvent) -> None:
        """Dispatch a progress event to the appropriate handler."""
        if isinstance(event, ManifestLoadedEvent):
            self.state.total_actions += len(event.manifest.actions)
            if self.state.overall_task is not None:
                self.progress.update(self.state.overall_task, total=self.state.total_actions)
            elif self.state.total_actions > 0:
                self.state.overall_task = self.progress.add_task(
                    _progress_label(self.setup_params.strategy),
                    total=self.state.total_actions,
                )
            return
        if isinstance(event, ManifestFailedEvent):
            return
        if isinstance(event, ActionStartedEvent):
            action_key = _action_event_key(event.action, event.action_ref)
            action_desc = _action_description(event.action)
            self.handle_action_started(action_key, action_desc)
            return
        if isinstance(event, ActionCompletedEvent):
            action_key = _action_event_key(event.action, event.action_ref)
            action_desc = _action_description(event.action)
            self.handle_action_completed(action_key, action_desc, event.action_ref, event.result)
            return
        if isinstance(event, ActionProgressEvent):
            action_key = _action_event_key(event.action, event.action_ref)
            action_desc = _action_description(event.action)
            self.handle_action_progress(action_key, action_desc, event.progress)


def _format_cli_command(result: SetupActionResult) -> str:
    """Format an action result as a CLI command string.

    Args:
        result: The action result.

    Returns:
        Formatted command string with arrow prefix.
    """
    if result.cli_command:
        return ' '.join(result.cli_command)
    # Fallback to description if no CLI command
    return result.action.description


def _duration_text(result: SetupActionResult) -> str:
    """Format an optional action duration for the final result line."""
    if result.duration_seconds is None:
        return ''
    return f' ({result.duration_seconds:.1f}s)'


def _step_summary(result: SetupActionResult) -> str:
    """Return compact multi-step context for a completed action."""
    step_count = len(result.cli_steps or ())
    if step_count <= 1:
        return ''
    return f' [{step_count} steps]'


def _failed_step_text(result: SetupActionResult) -> str | None:
    """Return the failed project command step when result metadata identifies it."""
    if result.failed_step_index is None or not result.cli_steps:
        return None
    step_position = result.failed_step_index - 1
    if step_position < 0 or step_position >= len(result.cli_steps):
        return None
    return ' '.join(result.cli_steps[step_position])


def _display_summary(
    configuration: ConsoleConfiguration,
    results: BatchSetupResults,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
) -> None:
    """Display summary panel.

    Args:
        configuration: CLI configuration with console.
        results: Batch execution results.
        strategy: The sync strategy.
    """
    configuration.output.blank()

    # Count results by category
    package_results = [r for mr in results.manifest_results for r in mr.results]

    succeeded = sum(1 for r in package_results if r.success and not r.skipped)
    skipped = sum(1 for r in package_results if r.success and r.skipped)
    failed = sum(1 for r in package_results if not r.success)

    if results.success:
        skip_msg = f', {skipped} skipped' if skipped else ''
        # Use strategy to determine the verb
        detail = f'{succeeded} upgraded' if strategy == SyncStrategy.LATEST else f'{succeeded} synced'
        configuration.output.print(
            Panel(
                f'[success]Complete![/success] {detail}{skip_msg}.',
                border_style='green',
            )
        )
    else:
        skip_msg = f', {skipped} skipped' if skipped else ''
        configuration.output.print(
            Panel(
                f'[error]Failed![/error] {succeeded} succeeded{skip_msg}, {failed} failed.',
                border_style='red',
            )
        )


def _display_results(
    configuration: ConsoleConfiguration,
    results: BatchSetupResults,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
) -> None:
    """Display execution results with arrow-prefixed commands.

    Args:
        configuration: CLI configuration with console.
        results: Batch execution results.
        strategy: The sync strategy.
    """
    for manifest_result in results.manifest_results:
        configuration.output.print(f'\n[heading]Manifest:[/heading] {manifest_result.manifest_path}')

        skipped_count = 0
        displayed_count = 0
        for result in manifest_result.results:
            if result.skipped and result.skip_reason:
                skipped_count += 1
                continue

            displayed_count += 1
            command_str = _format_cli_command(result)
            duration = _duration_text(result)
            if result.success:
                configuration.output.print(
                    f'  [success]{ARROW}[/success] {command_str}{_step_summary(result)}{duration}'
                )
            else:
                configuration.output.print(f'  [error]{ARROW}[/error] {command_str}{_step_summary(result)}{duration}')
                failed_step = _failed_step_text(result)
                if failed_step:
                    configuration.output.print(
                        f'    [error]Failed step {result.failed_step_index}:[/error] {failed_step}'
                    )
                if result.message:
                    configuration.output.print(f'    [muted]{result.message}[/muted]')

        if skipped_count:
            configuration.output.print(f'  [muted]{skipped_count} action(s) skipped[/muted]')

        if displayed_count == 0 and not skipped_count:
            configuration.output.print('  [muted]No actions to perform[/muted]')

    for path, error in results.failed_paths:
        configuration.output.print(f'\n[error]Failed:[/error] {path}')
        configuration.output.print(f'  [muted]{error}[/muted]')

    _display_summary(configuration, results, strategy)


def _dump_json_line(payload: dict) -> None:
    """Emit one compact JSON line for machine readers."""
    typer.echo(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))


def _write_replay_record(record_path: Path, payload: dict[str, Any]) -> None:
    """Write a replay record atomically within the destination directory."""
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            'w',
            encoding='utf-8',
            dir=record_path.parent,
            prefix=f'.{record_path.name}.',
            suffix='.tmp',
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, indent=2)
        temporary_path.replace(record_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()


def _execute_observable(
    setup_params: SetupParameters,
    *,
    api: API,
    emit_jsonl: bool,
    record_path: Path | None,
    plan: SyncPlan | None = None,
) -> BatchSetupResults:
    """Execute setup while optionally emitting JSONL and a replay record.

    Runs *plan* when given, otherwise loads *setup_params* afresh.
    """
    correlation_id = str(uuid4())
    started_at = datetime.now(UTC)
    event_payloads: list[dict] = []

    def _on_event(event: ProgressEvent) -> None:
        snapshot = progress_event_snapshot(event, correlation_id=correlation_id)
        payload = snapshot.model_dump(mode='json')
        event_payloads.append(payload)
        if emit_jsonl:
            _dump_json_line(payload)

    run = api.sync.run(plan=plan, on_event=_on_event) if plan else api.sync.run(setup_params, on_event=_on_event)
    report = asyncio.run(run)
    batch = report.results
    ended_at = datetime.now(UTC)
    envelope = batch_envelope(
        batch,
        operation='sync.run',
        correlation_id=correlation_id,
        started_at=started_at,
        ended_at=ended_at,
    )

    if emit_jsonl:
        _dump_json_line(envelope.model_dump(mode='json'))

    if record_path is not None:
        record = replay_record(
            operation='sync.run',
            correlation_id=correlation_id,
            parameters=setup_params,
            events=event_payloads,
            result=envelope,
            started_at=started_at,
            ended_at=ended_at,
        )
        _write_replay_record(record_path, record.model_dump(mode='json'))

    return batch


def _execute_with_progress(
    configuration: ConsoleConfiguration,
    api: API,
    setup_params: SetupParameters,
    plan: SyncPlan | None = None,
) -> BatchSetupResults:
    """Execute installation with progress display.

    Uses `api.sync.run(..., on_event=...)` to receive `ProgressEvent`
    items and update a Rich progress bar. Manifests are discovered via
    `MANIFEST_LOADED` events, so no separate preview step is required.

    Args:
        configuration: CLI configuration with console.
        api: The API instance.
        setup_params: Setup parameters.
        plan: A confirmed plan to run instead of loading *setup_params* afresh.

    Returns:
        BatchSetupResults from execution.
    """
    state = _ProgressState(verbose_output=configuration.verbosity >= 1)

    with Progress(
        SpinnerColumn(),
        TextColumn('[progress.description]{task.description}'),
        BarColumn(),
        TextColumn('[progress.percentage]{task.percentage:>3.0f}%'),
        TextColumn('({task.completed}/{task.total})'),
        console=configuration.console,
        transient=True,
    ) as progress:
        tracker = _ProgressTracker(progress=progress, setup_params=setup_params, state=state)
        handler = tracker.handle_progress_event
        run = api.sync.run(plan=plan, on_event=handler) if plan else api.sync.run(setup_params, on_event=handler)
        try:
            return asyncio.run(run).results
        except KeyboardInterrupt:
            configuration.output.warning('Cancelled. The active command was stopped.')
            raise typer.Exit(1) from None
