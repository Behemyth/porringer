"""Themed, semantic console output for the Porringer CLI.

This module centralizes all human-facing terminal styling so individual
commands no longer embed ad-hoc colour markup such as ``[red]Error:[/red]``.

* :data:`PORRINGER_THEME` defines named semantic styles. Tuning the look of
  the CLI is a single-file change.
* :func:`build_console` constructs a Rich :class:`~rich.console.Console`
  pre-loaded with the theme and honouring ``NO_COLOR``.
* :class:`Output` wraps a stdout and a stderr console and exposes semantic
  helpers (:meth:`Output.success`, :meth:`Output.error`, etc.) plus a
  ``quiet`` mode. Errors and warnings are written to stderr and are always
  shown; informational output goes to stdout and is suppressed when quiet.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any
lazy from collections.abc import Generator

from rich.console import Console
from rich.theme import Theme
lazy from rich.status import Status

# Named semantic styles shared by every CLI command. Keeping the colour
# choices here means the whole CLI can be restyled in one place.
PORRINGER_THEME = Theme({
    'error': 'red',
    'success': 'green',
    'warning': 'yellow',
    'info': 'cyan',
    'muted': 'dim',
    'detail': 'dim italic',
    'heading': 'bold',
    # Markdown-style inline code, e.g. for shell commands in the
    # preview list. Underline + dim keeps it quiet relative to the
    # bold action description, while still reading as visually
    # distinct text (not just "normal" prose) at a glance.
    'code': 'underline dim',
    # Per-InspectionStatus styles for the preview/install action list.
    # Keyed by InspectionStatus.value so renderers can look them up directly.
    'status.satisfied': 'green',
    'status.needed': 'yellow',
    'status.update_available': 'cyan',
    'status.unavailable': 'red',
    'status.failed': 'bold red',
    'status.skipped': 'dim',
    'status.unknown': 'dim',
})


def no_color_requested(explicit: bool = False) -> bool:
    """Return whether colour output should be disabled.

    Args:
        explicit: ``True`` when the user passed ``--no-color``.

    Returns:
        ``True`` when colour should be disabled, honouring the
        ``NO_COLOR`` convention (https://no-color.org/).
    """
    return explicit or 'NO_COLOR' in os.environ


def build_console(*, no_color: bool = False, stderr: bool = False) -> Console:
    """Build a themed Rich console.

    Args:
        no_color: Disable ANSI colour output.
        stderr: Write to ``sys.stderr`` instead of ``sys.stdout``.

    Returns:
        A :class:`~rich.console.Console` loaded with
        :data:`PORRINGER_THEME`.
    """
    return Console(theme=PORRINGER_THEME, no_color=no_color, stderr=stderr)


class Output:
    """Semantic output facade over a pair of Rich consoles.

    Informational helpers (:meth:`success`, :meth:`info`, :meth:`detail`,
    :meth:`print`, :meth:`blank`) write to the stdout console and are
    suppressed in ``quiet`` mode. Problem helpers (:meth:`error`,
    :meth:`warning`) write to the stderr console and are always shown so
    failures remain visible even when output is quietened.
    """

    def __init__(self, console: Console, error_console: Console | None = None, *, quiet: bool = False) -> None:
        """Initialize the facade.

        Args:
            console: Console for informational stdout output.
            error_console: Console for warnings/errors. Defaults to *console*.
            quiet: When ``True``, suppress informational output.
        """
        self.console = console
        self.error_console = error_console if error_console is not None else console
        self.quiet = quiet

    def print(self, renderable: Any = '', **kwargs: Any) -> None:
        """Print a renderable or markup string to stdout (suppressed when quiet)."""
        if not self.quiet:
            self.console.print(renderable, **kwargs)

    def blank(self) -> None:
        """Print a blank line to stdout (suppressed when quiet)."""
        if not self.quiet:
            self.console.print()

    @contextlib.contextmanager
    def status(self, message: str) -> Generator[Status | None]:
        """Show an indeterminate spinner with *message* while a block runs.

        Use this for a single opaque async call with no incremental
        progress to report (e.g. manifest inspection), as opposed to
        :class:`rich.progress.Progress`, which needs per-item events to
        drive a percentage. Suppressed when quiet, matching the other
        informational helpers.
        """
        if self.quiet:
            yield None
            return
        with self.console.status(message) as status:
            yield status

    def success(self, message: str, *, prefix: str | None = None) -> None:
        """Report a successful outcome to stdout (suppressed when quiet)."""
        if self.quiet:
            return
        body = f'[success]{prefix}:[/success] {message}' if prefix else f'[success]{message}[/success]'
        self.console.print(body)

    def warning(self, message: str, *, prefix: str | None = None) -> None:
        """Report a warning to stderr (always shown, even when quiet)."""
        body = f'[warning]{prefix}:[/warning] {message}' if prefix else f'[warning]{message}[/warning]'
        self.error_console.print(body)

    def error(self, message: str, *, prefix: str | None = 'Error') -> None:
        """Report an error to stderr (always shown, even when quiet)."""
        body = f'[error]{prefix}:[/error] {message}' if prefix else f'[error]{message}[/error]'
        self.error_console.print(body)
