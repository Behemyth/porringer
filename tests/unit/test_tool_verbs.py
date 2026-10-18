"""Tests for the combined verbs: ``tool.add``, ``tool.remove`` and ``tool.upgrade``."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.manifest_edit import (
    ManifestDocument,
    editable_manifest_path,
    serialize_spec,
)
from porringer.console.entry import app
from porringer.console.schema import ConsoleConfiguration
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Ecosystem, Package, PluginKind
from porringer.schema import (
    ActionCompletedEvent,
    ActionRef,
    ManifestLoadedEvent,
    PackageSpec,
    RangeStrategy,
    SetupActionResult,
    SetupResults,
    entry_id,
)
from porringer.utility.exception import ManifestError
from tests.fixtures.factories import make_environment

_PY = Ecosystem('python')


class _Runs:
    """Fake manifest executors that record what ran and fail chosen packages."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.log: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        monkeypatch.setattr('porringer.backend.command.tool.execute_manifest', self._executor('run'))
        monkeypatch.setattr(
            'porringer.backend.command.tool.uninstall_manifest',
            self._executor('uninstall'),
        )

    def _executor(self, verb: str) -> Any:
        async def run(
            preview: SetupResults,
            _parameters: Any,
            *,
            event_queue: Any,
            manifest_index: int,
            **_: Any,
        ):
            event_queue.put_nowait(ManifestLoadedEvent(manifest=preview, manifest_index=manifest_index))
            indices = preview.action_indices or list(range(len(preview.actions)))
            for index, action in zip(indices, preview.actions, strict=True):
                assert action.package is not None
                self.log.append((verb, str(action.package)))
                ref = ActionRef.from_indices(manifest_index, index)
                result = SetupActionResult(action=action, success=action.package.name not in self.fail, ref=ref)
                event_queue.put_nowait(ActionCompletedEvent(action=action, result=result, action_ref=ref))
                preview.results.append(result)
            return preview

        return run


@pytest.fixture(name='runs')
def fixture_runs(monkeypatch: pytest.MonkeyPatch) -> _Runs:
    """Recorded fake execution."""
    return _Runs(monkeypatch)


@pytest.fixture(name='plugins')
def fixture_plugins() -> DiscoveredPlugins:
    """A pipx tool installer that reports pdm 2.30 installed."""
    pipx = make_environment(
        kind=PluginKind.TOOL,
        tool_name='pipx',
        installed=[Package(name='pdm', version='2.30')],
    )
    type(pipx).bootstrap_requirement = MagicMock(return_value=None)
    return DiscoveredPlugins(
        environments={'pipx': pipx},
        project_environments={},
        scm_environments={},
        runtime_context=RuntimeContext(),
    )


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _id(path: Path, spec: str) -> str:
    return entry_id(path, PluginKind.TOOL, _PY, PackageSpec.model_validate(spec))


async def test_add_installs_then_records_in_the_global_manifest(
    test_api: API, runs: _Runs, plugins: DiscoveredPlugins
) -> None:
    """The add verb installs first and creates the global manifest with the entry."""
    report = await test_api.tool.add('pdm>=2', kind=PluginKind.TOOL, ecosystem='python', plugins=plugins)

    assert report.success
    assert runs.log == [('run', 'pdm>=2')]
    assert [result.entry_id for result in report.results] == [_id(test_api.global_manifest_path, 'pdm')]
    assert _read(test_api.global_manifest_path) == {
        'version': '1',
        'tools': {'python': ['pdm>=2']},
    }


async def test_add_writes_nothing_when_the_install_fails(
    test_api: API, runs: _Runs, plugins: DiscoveredPlugins
) -> None:
    """A failed install leaves the manifest untouched."""
    runs.fail.add('pdm')

    report = await test_api.tool.add('pdm', kind=PluginKind.TOOL, ecosystem='python', plugins=plugins)

    assert not report.success
    assert not test_api.global_manifest_path.exists()


async def test_add_again_updates_the_entry_in_place(test_api: API, runs: _Runs, tmp_path: Path) -> None:
    """Re-adding changes the constraint where it is, keeping order, $schema and other fields."""
    manifest = tmp_path / 'porringer.json'
    original = {
        '$schema': 'https://synodic.github.io/porringer/schema.json',
        'version': '1',
        'tools': {'python': [{'name': 'ruff', 'description': 'Linter'}, 'pdm']},
    }
    manifest.write_text(json.dumps(original, indent=2), encoding='utf-8')

    await test_api.tool.add(
        'ruff>=0.15',
        kind=PluginKind.TOOL,
        ecosystem='python',
        manifest=manifest,
        install=False,
    )

    data = _read(manifest)
    assert list(data) == ['$schema', 'version', 'tools']
    assert data['tools']['python'] == [
        {'name': 'ruff>=0.15', 'description': 'Linter'},
        'pdm',
    ]
    assert manifest.read_text(encoding='utf-8').startswith('{\n  "$schema"')
    assert runs.log == []


async def test_remove_keeps_the_entry_when_uninstall_fails(
    test_api: API, runs: _Runs, plugins: DiscoveredPlugins
) -> None:
    """A failed uninstall leaves the entry; a successful one drops it."""
    path = test_api.global_manifest_path
    path.write_text(
        json.dumps({'version': '1', 'tools': {'python': ['pdm', 'ruff']}}),
        encoding='utf-8',
    )
    runs.fail.add('pdm')

    failed = await test_api.tool.remove({_id(path, 'pdm')}, plugins=plugins)
    removed = await test_api.tool.remove({_id(path, 'ruff')}, plugins=plugins)

    assert not failed.success
    assert removed.success
    assert runs.log == [('uninstall', 'pdm'), ('uninstall', 'ruff')]
    assert _read(path)['tools'] == {'python': ['pdm']}


async def test_remove_keep_installed_only_edits(test_api: API, runs: _Runs) -> None:
    """keep_installed never uninstalls, and an emptied section disappears."""
    path = test_api.global_manifest_path
    path.write_text(json.dumps({'version': '1', 'tools': {'python': ['pdm']}}), encoding='utf-8')

    await test_api.tool.remove({_id(path, 'pdm')}, keep_installed=True)

    assert runs.log == []
    assert _read(path) == {'version': '1'}


async def test_remove_rejects_unknown_entries(test_api: API) -> None:
    """Ids that aren't in the manifest are refused before anything runs."""
    test_api.global_manifest_path.write_text(json.dumps({'version': '1'}), encoding='utf-8')

    with pytest.raises(ValueError, match='has no entries with ids: nope'):
        await test_api.tool.remove({'nope'})


async def test_upgrade_replace_moves_the_constraint_after_success(
    test_api: API, runs: _Runs, plugins: DiscoveredPlugins
) -> None:
    """REPLACE upgrades without the constraint, then raises it to the installed version."""
    path = test_api.global_manifest_path
    path.write_text(
        json.dumps({'version': '1', 'tools': {'python': ['pdm>=2.0']}}),
        encoding='utf-8',
    )

    report = await test_api.tool.upgrade({_id(path, 'pdm')}, range_strategy=RangeStrategy.REPLACE, plugins=plugins)

    assert report.success
    assert runs.log == [('run', 'pdm')]
    assert _read(path)['tools'] == {'python': ['pdm>=2.30']}


async def test_upgrade_replace_failure_and_in_range_leave_the_manifest(
    test_api: API, runs: _Runs, plugins: DiscoveredPlugins
) -> None:
    """A failed REPLACE and any IN_RANGE upgrade keep the constraint as written."""
    path = test_api.global_manifest_path
    path.write_text(
        json.dumps({'version': '1', 'tools': {'python': ['pdm>=2.0']}}),
        encoding='utf-8',
    )
    runs.fail.add('pdm')

    await test_api.tool.upgrade({_id(path, 'pdm')}, range_strategy=RangeStrategy.REPLACE, plugins=plugins)
    runs.fail.clear()
    await test_api.tool.upgrade({_id(path, 'pdm')}, plugins=plugins)

    assert runs.log == [('run', 'pdm'), ('run', 'pdm>=2.0')]
    assert _read(path)['tools'] == {'python': ['pdm>=2.0']}


def test_entries_list_the_manifest_as_written(test_api: API) -> None:
    """Entries come back as written, with ids matching the ones results carry."""
    assert test_api.tool.entries() == ()
    path = test_api.global_manifest_path
    path.write_text(json.dumps({'version': '1', 'tools': {'python': ['pdm>=2']}}), encoding='utf-8')

    (entry,) = test_api.tool.entries()

    assert (entry.entry_id, entry.kind, entry.ecosystem, str(entry.spec.name)) == (
        _id(path, 'pdm'),
        PluginKind.TOOL,
        'python',
        'pdm>=2',
    )


def test_embedded_manifests_are_not_edited(tmp_path: Path) -> None:
    """A manifest inside pyproject.toml is left for the user to edit."""
    (tmp_path / 'pyproject.toml').write_text('[tool.porringer]\nversion = "1"\n', encoding='utf-8')

    with pytest.raises(ManifestError, match='edit it by hand'):
        editable_manifest_path(tmp_path)


_PINNED = {'name': 'pdm', 'installer': 'pipx', 'runtime': '3.14'}


@pytest.mark.parametrize(
    ('spec', 'expected'),
    [
        ('pdm', 'pdm'),
        ('cppython[cmake,git]>=0.9', 'cppython[cmake,git]>=0.9'),
        ('lodash@^4.0.0', 'lodash@^4.0.0'),
        (_PINNED, _PINNED),
    ],
)
def test_entries_round_trip_in_their_shortest_form(spec: str | dict, expected: str | dict) -> None:
    """Serialized entries parse back to the same entry and stay as short as possible."""
    serialized = serialize_spec(PackageSpec.model_validate(spec))

    assert serialized == expected
    assert PackageSpec.model_validate(serialized) == PackageSpec.model_validate(spec)


def test_document_rejects_invalid_edits(tmp_path: Path) -> None:
    """An edit that breaks the schema is refused and the file is unchanged."""
    path = tmp_path / 'porringer.json'
    path.write_text('{"version": "1"}', encoding='utf-8')
    document = ManifestDocument(path)
    document.data['unknown'] = True

    with pytest.raises(ManifestError, match='invalid'):
        document.save()
    assert path.read_text(encoding='utf-8') == '{"version": "1"}'


def test_cli_add_and_remove_edit_the_named_manifest(tmp_path: Path, test_local_configuration) -> None:
    """The CLI records and drops an entry without installing, and names unknown entries readably."""
    manifest = tmp_path / 'porringer.json'
    manifest.write_text(json.dumps({'version': '1', 'tools': {'python': ['pdm']}}), encoding='utf-8')
    config = ConsoleConfiguration(
        console=Console(no_color=True, force_terminal=False, width=200),
        error_console=Console(no_color=True, force_terminal=False, width=200),
        local_configuration=test_local_configuration,
    )
    entry = ['--kind', 'tool', '--ecosystem', 'python', '--manifest', str(tmp_path)]
    runner = CliRunner()

    added = runner.invoke(app, ['add', 'ruff!=0.15.3', *entry, '--no-install'], obj=config)
    after_add = _read(manifest)['tools']
    removed = runner.invoke(app, ['remove', 'ruff', *entry, '--keep-installed'], obj=config)
    missing = runner.invoke(app, ['remove', 'nothere', *entry, '--keep-installed'], obj=config)

    assert added.exit_code == 0, added.output
    assert after_add == {'python': ['pdm', 'ruff!=0.15.3']}
    assert removed.exit_code == 0, removed.output
    assert _read(manifest)['tools'] == {'python': ['pdm']}
    assert missing.exit_code == 1
    assert "No tool entry 'nothere' for python" in missing.output
