"""Tests for the user's global manifest."""

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from porringer.api import API
from porringer.backend.command.core.action_builder import build_actions
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.schema import GlobalConfiguration
from porringer.console.entry import app
from porringer.console.schema import ConsoleConfiguration
from porringer.core.schema import PluginKind
from porringer.schema import InspectionMode, SetupManifest, SetupParameters
from tests.unit.test_runtime_inference import _plugins

_PYPROJECT = '[project]\nname = "demo"\nrequires-python = ">=3.14"\n'


def test_build_actions_without_inference_keeps_declared_entries_only(
    tmp_path: Path,
) -> None:
    """With inference off, a surrounding pyproject.toml adds no runtime action."""
    (tmp_path / 'pyproject.toml').write_text(_PYPROJECT, encoding='utf-8')

    inferred = build_actions(SetupManifest(), _plugins(), search_from=tmp_path)
    declared = build_actions(SetupManifest(), _plugins(), search_from=tmp_path, infer=False)

    assert [a.kind for a in inferred] == [PluginKind.RUNTIME]
    assert declared == []


@pytest.fixture(name='api')
def fixture_api(tmp_path: Path, test_local_configuration) -> API:
    """An API whose config directory sits inside a Python project."""
    (tmp_path / 'pyproject.toml').write_text(_PYPROJECT, encoding='utf-8')
    config = GlobalConfiguration(config_directory=tmp_path / 'config', data_directory=tmp_path / 'data')
    return API(test_local_configuration, config)


async def _kinds(api: API, path: Path) -> list[str | None]:
    plugins = DiscoveredPlugins(environments=_plugins(), project_environments={}, scm_environments={})
    report = await api.sync.inspect(
        SetupParameters(paths=[path], inspection_mode=InspectionMode.FAST),
        plugins=plugins,
    )
    return [action.action.kind for manifest in report.manifests for action in manifest.actions]


async def test_global_manifest_infers_nothing_from_its_location(api: API, tmp_path: Path) -> None:
    """The same manifest infers a runtime as a project manifest but not as the global one."""
    manifest = json.dumps({'version': '1'})
    api.global_manifest_path.write_text(manifest, encoding='utf-8')
    (tmp_path / 'porringer.json').write_text(manifest, encoding='utf-8')

    assert api.global_manifest_path == tmp_path / 'config' / 'porringer.json'
    assert await _kinds(api, api.global_manifest_path) == []
    assert await _kinds(api, api.global_manifest_path.parent) == []
    assert await _kinds(api, tmp_path / 'porringer.json') == [PluginKind.RUNTIME.value]


async def test_standalone_registration_is_scoped_and_nestable(api: API, tmp_path: Path) -> None:
    """Nested registration preserves the outer scope and restores inference on exit."""
    path = tmp_path / 'porringer.json'
    path.write_text('{"version": "1"}', encoding='utf-8')

    with api.sync.standalone_manifest(path):
        with api.sync.standalone_manifest(path):
            assert await _kinds(api, path) == []
        assert await _kinds(api, path) == []

    assert await _kinds(api, path) == [PluginKind.RUNTIME.value]


@pytest.fixture(name='cli_config')
def fixture_cli_config(tmp_path: Path, test_local_configuration) -> ConsoleConfiguration:
    """CLI configuration with an isolated global config directory."""
    return ConsoleConfiguration(
        console=Console(no_color=True, force_terminal=False, width=200),
        local_configuration=test_local_configuration,
        global_configuration=GlobalConfiguration(
            config_directory=tmp_path / 'config', data_directory=tmp_path / 'data'
        ),
    )


@pytest.mark.parametrize('command', [['preview'], ['install', '--yes'], ['check']])
def test_cli_global_without_manifest_says_so(cli_config: ConsoleConfiguration, command: list[str]) -> None:
    """A missing global manifest is reported, not treated as an error."""
    result = CliRunner().invoke(app, [*command, '--global'], obj=cli_config)

    assert result.exit_code == 0, result.output
    assert 'No global manifest yet' in result.output


def test_cli_global_rejects_a_target(cli_config: ConsoleConfiguration, tmp_path: Path) -> None:
    """--global and an explicit target can't both choose the manifest."""
    result = CliRunner().invoke(app, ['preview', str(tmp_path), '--global'], obj=cli_config)

    assert result.exit_code == 1
    assert '--global cannot be combined' in result.output


def test_cli_preview_global_uses_the_global_manifest(cli_config: ConsoleConfiguration, tmp_path: Path) -> None:
    """The preview command with --global inspects the global manifest's entries."""
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    (config_dir / 'porringer.json').write_text(
        json.dumps({'version': '1', 'tools': {'python': ['pdm']}}), encoding='utf-8'
    )

    result = CliRunner().invoke(app, ['preview', '--global', '--json', '--mode', 'fast'], obj=cli_config)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    packages = [action['action']['package'] for manifest in payload['manifests'] for action in manifest['actions']]
    assert packages == ['pdm']
