"""Helpers for test run command progress.

Tests for subprocess command progress output retention.
"""

import asyncio
import sys
from typing import Any

import pytest

from porringer.schema import ActionProgress, SetupAction
from porringer.utility.utility import CommandProgress, run_command
from tests.fixtures.command_process import CommandProcess

_OUTPUT_LINES = 5
_TAIL_LINES = 2


def _action() -> SetupAction:
    """Create an action descriptor for progress tests."""
    return SetupAction(description='Run chatty command')


async def test_run_command_retains_tail_and_reports_all_events(
    command_process: CommandProcess,
) -> None:
    """Returned output is bounded while progress still receives every line."""
    command_process.script(
        ['chatty'],
        stdout=[f'out-{index}' for index in range(_OUTPUT_LINES)],
        stderr=[f'err-{index}' for index in range(_OUTPUT_LINES)],
    )
    events: list[ActionProgress] = []

    result = await run_command(
        ['chatty'],
        progress=CommandProgress(action=_action(), callback=events.append),
        output_tail_lines=_TAIL_LINES,
    )

    assert [event.output for event in events if event.channel == 'stdout'] == [
        f'out-{index}' for index in range(_OUTPUT_LINES)
    ]
    assert [event.output for event in events if event.channel == 'stderr'] == [
        f'err-{index}' for index in range(_OUTPUT_LINES)
    ]
    assert result.stdout.splitlines() == [
        '[porringer omitted 3 earlier stdout line(s); retained last 2]',
        'out-3',
        'out-4',
    ]
    assert result.stderr.splitlines() == [
        '[porringer omitted 3 earlier stderr line(s); retained last 2]',
        'err-3',
        'err-4',
    ]


async def test_run_command_can_retain_full_output(
    command_process: CommandProcess,
) -> None:
    """Passing output_tail_lines=None preserves the previous full-output behavior."""
    command_process.script(['quiet'], stdout=['one', 'two', 'three'])

    result = await run_command(
        ['quiet'],
        progress=CommandProgress(action=_action(), callback=lambda _: None),
        output_tail_lines=None,
    )

    assert result.stdout == 'one\ntwo\nthree'
    assert not result.stderr


async def test_run_command_cancellation_stops_observed_process(
    command_process: CommandProcess,
) -> None:
    """Cancellation of an observed command raises and cleans up the child."""
    command_process.script(['cancellable'], stdout=['started'])
    cancellation = asyncio.Event()
    cancellation.set()

    with pytest.raises(asyncio.CancelledError, match='Operation cancelled'):
        await run_command(
            ['cancellable'],
            progress=CommandProgress(action=_action(), callback=lambda _: None),
            cancellation_check=cancellation,
        )


@pytest.mark.parametrize('observed', [False, True])
async def test_run_command_closes_child_stdin(observed: bool) -> None:
    """A child that reads input gets end-of-file instead of waiting on the console."""
    progress = CommandProgress(action=_action(), callback=lambda _: None) if observed else None

    result = await run_command(
        [sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'],
        progress=progress,
        timeout_seconds=30,
    )

    assert result.stdout.strip() == "''"


@pytest.mark.parametrize('observed', [False, True])
async def test_task_cancellation_kills_real_subprocess(observed: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling the awaiting task kills the child instead of orphaning it."""
    processes: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def _capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', _capture)
    progress = CommandProgress(action=_action(), callback=lambda _: None) if observed else None

    task = asyncio.create_task(run_command([sys.executable, '-c', 'import time; time.sleep(60)'], progress=progress))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert processes[0].returncode is not None
