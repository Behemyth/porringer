"""CLI command implementation for check.

Porringer CLI check command module for checking package updates via plugins.
"""

import asyncio
from collections.abc import Callable
from typing import Annotated

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from porringer.backend.command.package import PackageCommands
from porringer.console.common import (
    EXIT_FAILURE,
    GlobalOption,
    JsonOption,
    create_api,
    emit_json,
    global_manifest_or_exit,
)
from porringer.console.schema import ConsoleConfiguration
from porringer.core.plugin_schema.runtime import runtime_tag_matches
from porringer.schema import (
    CheckParameters,
    CheckResult,
    InventoryLocation,
    InventoryReport,
    InventoryStatus,
    ManagedToolReport,
)

app = typer.Typer()


def _display_results(
    configuration: ConsoleConfiguration, results: list[CheckResult]
) -> None:
    """Display check results to the console.

    Args:
        configuration: CLI configuration.
        results: List of check results.
    """
    total_updates = sum(r.updates_available for r in results if r.success)

    if total_updates == 0:
        configuration.output.print(
            Panel("[muted]All packages are up to date.[/muted]", border_style="dim")
        )
        return

    configuration.output.print(
        f"\n[heading][success]{total_updates} update(s) available[/success][/heading]\n"
    )

    for result in results:
        if not result.success:
            configuration.output.print(
                f"[warning]{result.plugin}:[/warning] [error]Error: {result.error}[/error]"
            )
            continue

        if not result.packages:
            continue

        table = Table(title=f"[heading]{result.plugin}[/heading]", show_header=True)
        table.add_column("Package", style="cyan")
        table.add_column("Current", style="dim")
        table.add_column("Latest", style="green")

        for pkg in result.packages:
            table.add_row(
                pkg.name,
                str(pkg.current_version) if pkg.current_version else "N/A",
                str(pkg.latest_version) if pkg.latest_version else "N/A",
            )

        configuration.output.print(table)
        configuration.output.blank()


def _display_managed(
    configuration: ConsoleConfiguration, report: ManagedToolReport
) -> None:
    """Display update status for the entries of a manifest."""
    for failed in report.failed_paths:
        configuration.output.print(f"[error]{failed.path}: {failed.error}[/error]")

    outdated = [r for r in report.results if r.success and r.available_version]
    failures = [r for r in report.results if not r.success]
    if not outdated and not failures:
        configuration.output.print(
            Panel("[muted]All entries are up to date.[/muted]", border_style="dim")
        )
        return

    if outdated:
        table = Table(
            title=f"[heading]{len(outdated)} update(s) available[/heading]",
            show_header=True,
        )
        table.add_column("Package", style="cyan")
        table.add_column("Installer")
        table.add_column("Current", style="dim")
        table.add_column("Latest", style="green")
        for result in outdated:
            table.add_row(
                result.package or "",
                result.plugin or "",
                result.installed_version or "N/A",
                result.available_version,
            )
        configuration.output.print(table)

    for result in failures:
        configuration.output.print(
            f"[warning]{result.package}:[/warning] [error]{result.message}[/error]"
        )


def _location_label(location: InventoryLocation) -> str:
    runtime = location.runtime
    if runtime is None:
        return location.plugin
    flags = ", ".join(
        flag
        for flag, on in (("default", runtime.default), ("managed", runtime.managed))
        if on
    )
    detail = f"{runtime.provider}{', ' + flags if flags else ''}"
    return f"{location.plugin} on {runtime.kind} {runtime.tag} ({detail}) {runtime.executable}"


def _display_inventory(
    configuration: ConsoleConfiguration, report: InventoryReport
) -> None:
    """Display each location's installed packages that have updates, and any failures."""
    for failure in report.provider_failures:
        configuration.output.error(f"{failure.provider}: {failure.error}")
    if not report.locations:
        configuration.output.print(
            Panel(
                "[muted]No matching runtimes or package stores.[/muted]",
                border_style="dim",
            )
        )
        return

    for location in report.locations:
        label = escape(_location_label(location))
        if location.status is InventoryStatus.ERROR:
            configuration.output.error(f"{label}: {escape(location.error or 'failed')}")
            continue
        outdated = [package for package in location.packages if package.latest_version]
        configuration.output.print(
            f"[heading]{label}[/heading] [muted]{len(location.packages)} installed, {len(outdated)} update(s)[/muted]"
        )
        if location.update_error:
            configuration.output.warning(
                f"Updates unavailable: {location.update_error}"
            )
        if outdated:
            table = Table(show_header=True)
            table.add_column("Package", style="cyan")
            table.add_column("Current", style="dim")
            table.add_column("Latest", style="green")
            for package in outdated:
                table.add_row(
                    package.name, package.version or "N/A", package.latest_version
                )
            configuration.output.print(table)


def _show(as_json: bool, data: object, display: Callable[[], None]) -> None:
    """Emit *data* as JSON, or show it to a person with *display*."""
    if as_json:
        emit_json(data)
    else:
        display()


@app.callback(invoke_without_command=True)
def check_default(
    context: typer.Context,
    *,
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", "-p", help="Plugin(s) to check. Omit to check all."),
    ] = None,
    include_prereleases: Annotated[
        bool,
        typer.Option("--prereleases", help="Include pre-release versions"),
    ] = False,
    global_: GlobalOption = False,
    runtime: Annotated[
        list[str] | None,
        typer.Option(
            "--runtime",
            "-r",
            help="Check packages installed in this runtime, such as 3.14 (repeatable)",
        ),
    ] = None,
    all_runtimes: Annotated[
        bool,
        typer.Option(
            "--all-runtimes",
            help="Check packages in every runtime and every global store",
        ),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Check for available package updates via plugins.

    Each plugin uses its native tooling to check for updates. With --global,
    only the entries of your global manifest are checked. With --runtime or
    --all-runtimes, packages are checked per interpreter, and pipx-style
    global stores once.

    Examples:
        porringer check
        porringer check --plugin pip
        porringer check --plugin pip --plugin pipx --prereleases
        porringer check --global
        porringer check --runtime 3.14
        porringer check --all-runtimes
    """
    configuration = context.ensure_object(ConsoleConfiguration)

    if sum((global_, runtime is not None, all_runtimes)) > 1:
        configuration.output.error(
            "Use only one of --global, --runtime and --all-runtimes"
        )
        raise typer.Exit(EXIT_FAILURE)

    if runtime is not None or all_runtimes:
        report = asyncio.run(
            PackageCommands.inventory(
                runtimes=set(runtime) if runtime is not None else None,
                plugin_names=set(plugin) if plugin else None,
                include_global=all_runtimes,
                include_updates=True,
                include_prereleases=include_prereleases,
            )
        )
        unmatched = [
            w
            for w in runtime or []
            if not any(runtime_tag_matches(r.tag, w) for r in report.runtimes)
        ]
        if unmatched:
            known = ", ".join(sorted({r.tag for r in report.runtimes})) or "none"
            configuration.output.error(
                f"No runtime matches {', '.join(unmatched)} (available: {known})"
            )
            raise typer.Exit(EXIT_FAILURE)
        _show(as_json, report, lambda: _display_inventory(configuration, report))
        if not report.success:
            raise typer.Exit(EXIT_FAILURE)
        return

    if global_:
        if include_prereleases:
            configuration.output.error(
                "--prereleases doesn't apply with --global; set include_prereleases on the entry instead"
            )
            raise typer.Exit(EXIT_FAILURE)
        api = create_api(configuration)
        path = global_manifest_or_exit(configuration, api)
        managed = asyncio.run(
            api.tool.check_updates(
                paths=[path], plugin_names=set(plugin) if plugin else None
            )
        )
        _show(as_json, managed, lambda: _display_managed(configuration, managed))
        if not managed.success:
            raise typer.Exit(EXIT_FAILURE)
        return

    params = CheckParameters(
        plugin_names=set(plugin) if plugin else None,
        include_prereleases=include_prereleases,
    )

    results = asyncio.run(PackageCommands.check_updates(params))
    _show(as_json, results, lambda: _display_results(configuration, results))
