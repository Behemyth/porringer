"""Console and CLI support for entry.

Typer CLI Application.
"""

import logging
from importlib.metadata import version
from typing import Annotated

import typer
from rich.console import Console

from porringer.console.command.check import app as check_app
from porringer.console.command.doc import doc_default
from porringer.console.command.env import app as env_app
from porringer.console.command.install import install_default
from porringer.console.command.manage import (
    add_default,
    remove_default,
    upgrade_default,
)
from porringer.console.command.open import open_default
from porringer.console.command.plugin import app as plugin_app
from porringer.console.command.preview import preview_default
from porringer.console.command.schema import app as schema_app
from porringer.console.command.self import app as self_app
from porringer.console.output import build_console, no_color_requested
from porringer.console.schema import (
    LOG_LEVELS,
    MAX_VERBOSITY_LEVEL,
    VERBOSITY_DEBUG_THRESHOLD,
    ConsoleConfiguration,
)

__version__ = version('porringer')

app = typer.Typer()
app.command(name='add')(add_default)
app.add_typer(check_app, name='check')
app.command(name='doc')(doc_default)
app.add_typer(env_app, name='env')
app.command(name='install')(install_default)
app.command(name='open')(open_default)
app.command(name='preview')(preview_default)
app.command(name='remove')(remove_default)
app.add_typer(schema_app, name='schema')
app.add_typer(plugin_app, name='plugin')
app.add_typer(self_app, name='self')
app.command(name='upgrade')(upgrade_default)


class TyperHandler(logging.Handler):
    """A logging handler that outputs to typer."""

    def __init__(self, console: Console) -> None:
        """Initializes the handler."""
        logging.Handler.__init__(self)

        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        """Emits the log record to typer."""
        level = next(level for level in LOG_LEVELS if level.name == record.levelname)
        message = self.format(record)
        self.console.print(message, style=level.colour)


def version_callback(value: bool) -> None:
    """Callback for the version option."""
    if value:
        typer.echo(f'Porringer {__version__}')
        raise typer.Exit()


@app.callback(invoke_without_command=True, no_args_is_help=True)
def application(
    context: typer.Context,
    verbose: Annotated[
        int,
        typer.Option('--verbose', '-v', count=True, help='', min=0, max=MAX_VERBOSITY_LEVEL),
    ] = 0,
    debug: Annotated[bool, typer.Option('--debug', help='')] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            '--quiet',
            '-q',
            help='Suppress informational output; only show warnings and errors',
        ),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option(
            '--no-color',
            help='Disable coloured output (also honoured via the NO_COLOR env var)',
        ),
    ] = False,
    version: Annotated[
        bool | None,
        typer.Option('--version', callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """A tool for automatic facilitation of program updates and package managers.

    Args:
        context: The click context object
        verbose: The input verbosity level
        debug: The debug flag
        quiet: Suppress informational output
        no_color: Disable coloured output
        version: The version request
    """
    configuration = context.ensure_object(ConsoleConfiguration)

    configuration.debug = debug
    configuration.verbosity = verbose
    configuration.quiet = quiet

    # Rebuild consoles without colour when requested or when NO_COLOR is set.
    if no_color_requested(no_color):
        configuration.console = build_console(no_color=True)
        configuration.error_console = build_console(no_color=True, stderr=True)

    logger = logging.getLogger('porringer')

    # Remove any previously-attached TyperHandlers so that repeated
    # CLI invocations (e.g. in tests) don't accumulate handlers
    # pointing to closed output streams.
    logger.handlers = [h for h in logger.handlers if not isinstance(h, TyperHandler)]

    handler = TyperHandler(configuration.console)
    logger.addHandler(handler)

    # The porringer logger has its own handler; disable propagation so
    # records are not also handled by the root logger.  This avoids
    # duplicate output and, under test runners that temporarily swap
    # sys.stdout / sys.stderr (e.g. pytest log_cli + CliRunner),
    # prevents stream wrappers from being garbage-collected during
    # capture suspension (Python 3.14 raises on BytesIO.getvalue()
    # after close).
    logger.propagate = False

    if debug or verbose >= VERBOSITY_DEBUG_THRESHOLD:
        logger.setLevel(logging.DEBUG)
    elif verbose >= 1:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)
