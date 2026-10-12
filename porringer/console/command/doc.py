"""CLI command implementation for doc.

Porringer CLI doc command — resolve the documentation URL and optionally open it.
"""

import json
from typing import Annotated
lazy import webbrowser

import typer

from porringer.console.common import EXIT_FAILURE
from porringer.console.schema import ConsoleConfiguration

# Canonical documentation site root (see ``site_url`` in ``zensical.toml``).
DOCS_URL = 'https://synodic.github.io/porringer/'


def doc_default(
    context: typer.Context,
    *,
    open_browser: Annotated[
        bool,
        typer.Option('--open', help='Open the documentation in the default web browser'),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option('--json', help='Emit the documentation URL as JSON'),
    ] = False,
) -> None:
    """Resolve the documentation URL, printing it by default.

    Prints the canonical URL to stdout so it stays scriptable and CI-safe.
    Pass --open to launch the documentation in the default web browser.

    Examples:
        porringer doc            # Print the documentation URL
        porringer doc --open     # Open the documentation in a browser
        porringer doc --json     # Emit the documentation URL as JSON
    """
    configuration = context.ensure_object(ConsoleConfiguration)

    if open_browser:
        opened = webbrowser.open(DOCS_URL)
        if not opened:
            configuration.output.error(f'Unable to open a web browser. Visit {DOCS_URL} directly.')
            raise typer.Exit(EXIT_FAILURE)
        configuration.output.print(f'[muted]Opening {DOCS_URL}[/muted]')
        return

    if as_json:
        typer.echo(json.dumps({'url': DOCS_URL}))
    else:
        typer.echo(DOCS_URL)
