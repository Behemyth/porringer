"""CLI commands that change what is installed and edit the manifest together: add, remove, upgrade."""

import asyncio
from collections.abc import Coroutine
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from porringer.api import API
from porringer.backend.command.manifest_edit import editable_manifest_path
from porringer.console.common import EXIT_FAILURE, create_api
from porringer.console.schema import ConsoleConfiguration
from porringer.core.schema import Ecosystem, PluginKind
from porringer.schema import ManagedToolReport, PackageSpec, RangeStrategy, entry_id
from porringer.utility.exception import ManifestError, RuntimeInUseError


class EntryKind(StrEnum):
    """Manifest section an entry lives in."""

    PACKAGE = 'package'
    TOOL = 'tool'
    RUNTIME = 'runtime'


_KINDS = {
    EntryKind.PACKAGE: PluginKind.PACKAGE,
    EntryKind.TOOL: PluginKind.TOOL,
    EntryKind.RUNTIME: PluginKind.RUNTIME,
}

KindOption = Annotated[
    EntryKind,
    typer.Option('--kind', '-k', help='Manifest section: package, tool or runtime'),
]
EcosystemOption = Annotated[str, typer.Option('--ecosystem', '-e', help='Ecosystem, such as python or system')]
InstallerOption = Annotated[str | None, typer.Option('--installer', help='Installer plugin for this entry')]
RuntimeOption = Annotated[str | None, typer.Option('--runtime', help='Runtime tag the installer runs under')]
ManifestOption = Annotated[
    Path | None,
    typer.Option(
        '--manifest',
        '-m',
        help='porringer.json or its folder to edit. Defaults to your global manifest.',
    ),
]


def _manifest_path(configuration: ConsoleConfiguration, api: API, manifest: Path | None) -> Path:
    if manifest is None:
        return api.global_manifest_path
    try:
        return editable_manifest_path(manifest.resolve())
    except ManifestError as exc:
        configuration.output.error(exc.error)
        raise typer.Exit(EXIT_FAILURE) from exc


def _entry(name: str, installer: str | None, runtime: str | None) -> PackageSpec:
    return PackageSpec.model_validate({'name': name, 'installer': installer, 'runtime': runtime})


def _existing_entry_id(
    configuration: ConsoleConfiguration,
    api: API,
    path: Path,
    kind: EntryKind,
    ecosystem: str,
    name: str,
    installer: str | None,
    runtime: str | None,
) -> str:
    """Return the id of the named entry, exiting with a readable error when the manifest lacks it."""
    eid = entry_id(path, _KINDS[kind], Ecosystem(ecosystem), _entry(name, installer, runtime))
    if not any(entry.entry_id == eid for entry in api.tool.entries(manifest=path)):
        pinned = ''.join(f' {label} {value}' for label, value in (('via', installer), ('on', runtime)) if value)
        configuration.output.error(f"No {kind} entry '{name}'{pinned} for {ecosystem} in {path}")
        raise typer.Exit(EXIT_FAILURE)
    return eid


def _run(
    configuration: ConsoleConfiguration,
    call: Coroutine[Any, Any, ManagedToolReport],
    path: Path,
) -> None:
    """Run a verb, print its outcome and exit non-zero on failure."""
    try:
        report = asyncio.run(call)
    except RuntimeInUseError as exc:
        configuration.output.error(f'Runtime {exc.runtime} is still used by:')
        for dependent in exc.dependents:
            configuration.output.print(f'  {escape(dependent)}')
        configuration.output.print('[muted]Remove those first, or pass --cascade to remove them too.[/muted]')
        raise typer.Exit(EXIT_FAILURE) from exc
    except (ValueError, ManifestError) as exc:
        configuration.output.error(exc.error if isinstance(exc, ManifestError) else str(exc))
        raise typer.Exit(EXIT_FAILURE) from exc

    for failed in report.failed_paths:
        configuration.output.error(f'{failed.path}: {failed.error}')
    for result in report.results:
        label = escape(f'{result.package} via {result.plugin}' if result.plugin else str(result.package))
        if not result.success:
            configuration.output.error(f'{label}: {escape(result.message or "failed")}')
        elif result.skipped:
            configuration.output.print(f'[muted]{label}: {escape(result.message or "skipped")}[/muted]')
        else:
            configuration.output.success(label)

    if not report.success:
        configuration.output.warning(f'{path} was not changed')
        raise typer.Exit(EXIT_FAILURE)
    configuration.output.print(f'[muted]Manifest: {escape(str(path))}[/muted]')


def add_default(
    context: typer.Context,
    spec: Annotated[
        str,
        typer.Argument(help='Package with an optional constraint, such as "pdm>=2.20"'),
    ],
    *,
    kind: KindOption,
    ecosystem: EcosystemOption,
    installer: InstallerOption = None,
    runtime: RuntimeOption = None,
    manifest: ManifestOption = None,
    install: Annotated[
        bool,
        typer.Option(
            '--install/--no-install',
            help='Install before recording; --no-install only edits',
        ),
    ] = True,
) -> None:
    """Install a package, then record it in a manifest.

    Adding an entry that is already there (same name, installer and runtime)
    updates it, for example to change its constraint or skip a version with
    "!=". Nothing is recorded if the install fails.

    Examples:
        porringer add pdm --kind tool --ecosystem python
        porringer add "ruff!=0.15.3" -k tool -e python --no-install
    """
    configuration = context.ensure_object(ConsoleConfiguration)
    api = create_api(configuration)
    path = _manifest_path(configuration, api, manifest)
    try:
        entry = _entry(spec, installer, runtime)
    except ValueError as exc:
        configuration.output.error(str(exc))
        raise typer.Exit(EXIT_FAILURE) from exc
    call = api.tool.add(entry, kind=_KINDS[kind], ecosystem=ecosystem, manifest=path, install=install)
    _run(configuration, call, path)


def remove_default(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help='Package name of the entry')],
    *,
    kind: KindOption,
    ecosystem: EcosystemOption,
    installer: InstallerOption = None,
    runtime: RuntimeOption = None,
    manifest: ManifestOption = None,
    keep_installed: Annotated[
        bool,
        typer.Option('--keep-installed', help='Stop managing the package without uninstalling it'),
    ] = False,
    cascade: Annotated[
        bool,
        typer.Option(
            '--cascade',
            help='For a runtime, also remove the entries and tools that depend on it',
        ),
    ] = False,
) -> None:
    """Uninstall a package, then drop its entry from a manifest.

    The entry stays when the uninstall fails. A runtime is only removed
    when nothing depends on it, unless --cascade is given.
    """
    configuration = context.ensure_object(ConsoleConfiguration)
    api = create_api(configuration)
    path = _manifest_path(configuration, api, manifest)
    eid = _existing_entry_id(configuration, api, path, kind, ecosystem, name, installer, runtime)
    if kind is EntryKind.RUNTIME and not keep_installed:
        for check in asyncio.run(api.tool.runtime_dependents({eid}, manifest=path)):
            if check.default:
                configuration.output.warning(
                    f'{check.runtime} is the default interpreter; commands such as py and python will use another'
                )
            if check.installed_packages:
                configuration.output.print(
                    f'[muted]Installed in {escape(check.runtime)} and removed with it: '
                    f'{escape(", ".join(check.installed_packages))}[/muted]'
                )
    call = api.tool.remove({eid}, manifest=path, keep_installed=keep_installed, cascade=cascade)
    _run(configuration, call, path)


def upgrade_default(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help='Package name of the entry')],
    *,
    kind: KindOption,
    ecosystem: EcosystemOption,
    installer: InstallerOption = None,
    runtime: RuntimeOption = None,
    manifest: ManifestOption = None,
    range_strategy: Annotated[
        RangeStrategy,
        typer.Option(
            '--range',
            help='in_range stays within the constraint; replace upgrades past it and moves the constraint up',
        ),
    ] = RangeStrategy.IN_RANGE,
) -> None:
    """Upgrade a manifest entry."""
    configuration = context.ensure_object(ConsoleConfiguration)
    api = create_api(configuration)
    path = _manifest_path(configuration, api, manifest)
    eid = _existing_entry_id(configuration, api, path, kind, ecosystem, name, installer, runtime)
    call = api.tool.upgrade({eid}, manifest=path, range_strategy=range_strategy)
    _run(configuration, call, path)
