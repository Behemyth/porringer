"""Utility helpers for utility.

Utility definitions.
"""

import asyncio
import contextlib
import os
from collections import deque
from collections.abc import Callable, Sequence
from typing import Literal, NamedTuple, cast

from porringer.schema import ActionProgress, SetupAction
from porringer.utility.trace import CommandTrace

__all__ = [
    'DEFAULT_COMMAND_OUTPUT_TAIL_LINES',
    'CommandProgress',
    'CommandResult',
    'run_command',
]

DEFAULT_COMMAND_OUTPUT_TAIL_LINES = 200
"""Default number of observed subprocess output lines retained per channel."""


class CommandResult(NamedTuple):
    """Result of a subprocess run (sync or async)."""

    returncode: int
    stdout: str
    stderr: str


class CommandProgress(NamedTuple):
    """Configuration for command progress reporting.

    Groups the progress-related parameters used by `run_command` when
    subprocess output should be reported line by line.
    """

    action: SetupAction
    callback: Callable[[ActionProgress], None]
    phase: str = 'running'


class _OutputTail:
    """Bounded line accumulator for observed subprocess result output."""

    def __init__(self, max_lines: int | None) -> None:
        """Create an accumulator retaining at most *max_lines* lines."""
        if max_lines is not None and max_lines < 0:
            raise ValueError('output_tail_lines must be non-negative or None')
        self._max_lines = max_lines
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._seen = 0

    def append(self, line: str) -> None:
        """Record one output line."""
        self._seen += 1
        self._lines.append(line)

    def text(self, channel: Literal['stdout', 'stderr']) -> str:
        """Return retained output text, with an omission marker when truncated."""
        retained = len(self._lines)
        omitted = 0 if self._max_lines is None else max(0, self._seen - retained)
        if omitted == 0:
            return '\n'.join(self._lines)

        marker = f'[porringer omitted {omitted} earlier {channel} line(s); retained last {retained}]'
        if retained == 0:
            return marker
        return '\n'.join((marker, *self._lines))


async def run_command(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout_seconds: float | None = None,
    cancellation_check: asyncio.Event | None = None,
    progress: CommandProgress | None = None,
    output_tail_lines: int | None = DEFAULT_COMMAND_OUTPUT_TAIL_LINES,
    **kwargs: object,
) -> CommandResult:
    """Run a command asynchronously using asyncio subprocess.

    Uses modern asyncio.timeout() context manager (Python 3.11+) for
    structured timeout handling.  When ``progress`` is supplied,
    stdout/stderr are observed line by line and reported through the
    callback while the returned result retains a bounded output tail.

    Args:
        args: Command and arguments to run.
        cwd: Optional working directory for the subprocess.
        timeout_seconds: Optional timeout in seconds.
        cancellation_check: Optional Event that, when set, triggers cancellation.
        progress: Optional progress sink for line-by-line subprocess output.
        output_tail_lines: Number of trailing lines retained in the returned
            ``CommandResult`` per channel when ``progress`` is provided.
            ``None`` preserves full output.
        **kwargs: Backward-compatible support for legacy ``timeout=`` calls.

    Returns:
        CommandResult with returncode, stdout, and stderr.

    Raises:
        asyncio.TimeoutError: If the command times out.
        asyncio.CancelledError: If cancelled via cancellation_check.
        FileNotFoundError: If the command is not found.
    """
    timeout = kwargs.pop('timeout', None)
    if timeout_seconds is None and timeout is not None:
        timeout_seconds = float(cast(float | int | str, timeout))
    if kwargs:
        unexpected = ', '.join(sorted(kwargs))
        raise TypeError(f'Unexpected keyword arguments: {unexpected}')

    trace = CommandTrace.start(args, cwd=cwd)
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            # Output is captured, so a prompt would be invisible; reading input must fail instead of hanging.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except Exception as exc:
        trace.finish(returncode=None, error=f'{type(exc).__name__}: {exc}')
        raise

    if progress is None:
        return await _run_buffered_command(
            process,
            trace,
            timeout_seconds=timeout_seconds,
            cancellation_check=cancellation_check,
        )

    return await _run_observed_command(
        process,
        trace,
        timeout_seconds=timeout_seconds,
        cancellation_check=cancellation_check,
        progress=progress,
        output_tail_lines=output_tail_lines,
    )


async def _run_buffered_command(
    process: asyncio.subprocess.Process,
    trace: CommandTrace,
    *,
    timeout_seconds: float | None,
    cancellation_check: asyncio.Event | None,
) -> CommandResult:
    """Run a subprocess to completion and capture full output."""
    stdout_bytes = b''
    stderr_bytes = b''

    async def communicate_or_cancel() -> tuple[bytes, bytes]:
        """Communicate with process, checking for cancellation."""
        if cancellation_check is None:
            return await process.communicate()

        # Race between process completion and cancellation
        communicate_task = asyncio.create_task(process.communicate())
        cancel_task = asyncio.create_task(cancellation_check.wait())

        done, pending = await asyncio.wait(
            [communicate_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if cancel_task in done:
            # Cancellation requested - kill process
            process.kill()
            await process.wait()
            raise asyncio.CancelledError('Operation cancelled')

        return communicate_task.result()

    try:
        async with asyncio.timeout(timeout_seconds):
            stdout_bytes, stderr_bytes = await communicate_or_cancel()
    except (Exception, asyncio.CancelledError) as exc:
        if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
            process.kill()
            await process.wait()
        trace.finish(
            returncode=process.returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            error=f'{type(exc).__name__}: {exc}',
        )
        raise

    result = CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout_bytes.decode('utf-8', errors='replace'),
        stderr=stderr_bytes.decode('utf-8', errors='replace'),
    )
    trace.finish(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    return result


async def _run_observed_command(
    process: asyncio.subprocess.Process,
    trace: CommandTrace,
    *,
    timeout_seconds: float | None = None,
    cancellation_check: asyncio.Event | None = None,
    progress: CommandProgress,
    output_tail_lines: int | None = DEFAULT_COMMAND_OUTPUT_TAIL_LINES,
) -> CommandResult:
    """Observe stdout/stderr line by line and retain bounded output tails."""
    stdout_tail = _OutputTail(output_tail_lines)
    stderr_tail = _OutputTail(output_tail_lines)

    async def read_pipe(
        reader: asyncio.StreamReader,
        channel: Literal['stdout', 'stderr'],
        tail: _OutputTail,
    ) -> None:
        """Read a pipe line-by-line and emit progress events."""
        async for raw_line in reader:
            line = raw_line.decode('utf-8', errors='replace').rstrip()
            tail.append(line)
            progress.callback(
                ActionProgress(
                    action=progress.action,
                    phase=progress.phase,
                    output=line,
                    channel=channel,
                )
            )

    async def run_with_progress() -> None:
        """Observe both pipes and await process completion."""
        cancel_task: asyncio.Task[None] | None = None

        if cancellation_check is not None:
            check = cancellation_check

            async def _kill_on_cancel() -> None:
                await check.wait()
                process.kill()

            cancel_task = asyncio.create_task(_kill_on_cancel())

        try:
            async with asyncio.TaskGroup() as tg:
                if process.stdout:
                    tg.create_task(read_pipe(process.stdout, 'stdout', stdout_tail))
                if process.stderr:
                    tg.create_task(read_pipe(process.stderr, 'stderr', stderr_tail))
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

        await process.wait()

        if cancellation_check is not None and cancellation_check.is_set():
            raise asyncio.CancelledError('Operation cancelled')

    try:
        async with asyncio.timeout(timeout_seconds):
            await run_with_progress()
    except (Exception, asyncio.CancelledError) as exc:
        if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
            process.kill()
            await process.wait()
        trace.finish(
            returncode=process.returncode,
            stdout=stdout_tail.text('stdout'),
            stderr=stderr_tail.text('stderr'),
            error=f'{type(exc).__name__}: {exc}',
        )
        raise

    result = CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout_tail.text('stdout'),
        stderr=stderr_tail.text('stderr'),
    )
    trace.finish(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    return result
