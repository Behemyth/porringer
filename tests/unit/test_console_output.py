"""Helpers for test console output.

Tests for the themed console output facade.
"""

import inspect
import io
from collections.abc import Generator
from typing import get_type_hints

from rich.console import Console
from rich.status import Status

from porringer.console.output import PORRINGER_THEME, Output, build_console, no_color_requested


def _make_output(*, quiet: bool = False) -> tuple[Output, io.StringIO, io.StringIO]:
    """Build an Output backed by in-memory stdout/stderr consoles."""
    out_buffer = io.StringIO()
    err_buffer = io.StringIO()
    out_console = Console(file=out_buffer, force_terminal=False, no_color=True, theme=PORRINGER_THEME)
    err_console = Console(file=err_buffer, force_terminal=False, no_color=True, theme=PORRINGER_THEME)
    return Output(out_console, err_console, quiet=quiet), out_buffer, err_buffer


class TestOutputRouting:
    """Verify which stream each helper writes to."""

    @staticmethod
    def test_status_annotation_resolves() -> None:
        """Annotation-only imports remain available to runtime introspection."""
        hints = get_type_hints(inspect.unwrap(Output.status))

        assert hints['return'] == Generator[Status | None]

    @staticmethod
    def test_informational_helpers_write_to_stdout() -> None:
        """success/print render on stdout, not stderr."""
        output, out_buffer, err_buffer = _make_output()

        output.success('done')
        output.print('hello')

        stdout = out_buffer.getvalue()
        assert 'done' in stdout
        assert 'hello' in stdout
        assert not err_buffer.getvalue()

    @staticmethod
    def test_error_and_warning_write_to_stderr() -> None:
        """error/warning render on stderr, not stdout."""
        output, out_buffer, err_buffer = _make_output()

        output.error('boom')
        output.warning('careful')

        stderr = err_buffer.getvalue()
        assert 'Error: boom' in stderr
        assert 'careful' in stderr
        assert not out_buffer.getvalue()

    @staticmethod
    def test_prefix_arguments_are_rendered() -> None:
        """Semantic prefixes appear in the rendered output."""
        output, out_buffer, _ = _make_output()

        output.success('thing', prefix='Added')

        assert 'Added: thing' in out_buffer.getvalue()


class TestQuietMode:
    """Verify quiet mode suppresses chatter but never problems."""

    @staticmethod
    def test_quiet_suppresses_informational_output() -> None:
        """success/print/blank produce nothing when quiet."""
        output, out_buffer, _ = _make_output(quiet=True)

        output.success('done')
        output.print('renderable')
        output.blank()

        assert not out_buffer.getvalue()

    @staticmethod
    def test_quiet_still_shows_errors_and_warnings() -> None:
        """error/warning remain visible even when quiet."""
        output, _, err_buffer = _make_output(quiet=True)

        output.error('boom')
        output.warning('careful')

        stderr = err_buffer.getvalue()
        assert 'boom' in stderr
        assert 'careful' in stderr


class TestConsoleFactory:
    """Verify console construction helpers."""

    @staticmethod
    def test_build_console_applies_theme() -> None:
        """A built console resolves semantic theme styles without error."""
        buffer = io.StringIO()
        console = build_console(no_color=True)
        console.file = buffer
        console.print('[success]ok[/success]')

        assert 'ok' in buffer.getvalue()

    @staticmethod
    def test_no_color_requested_honours_env(monkeypatch) -> None:
        """NO_COLOR env var forces colourless output."""
        monkeypatch.delenv('NO_COLOR', raising=False)
        assert no_color_requested(explicit=False) is False
        assert no_color_requested(explicit=True) is True

        monkeypatch.setenv('NO_COLOR', '1')
        assert no_color_requested(explicit=False) is True
