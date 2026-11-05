"""Tests for handing a manifest's `native` section to the project's own CPPython."""

import json
import logging
from asyncio import Queue
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from packaging.version import Version
from pydantic import ValidationError

from porringer.backend.command.core import execution
from porringer.backend.command.core.action_builder import build_actions, get_cli_steps, native_intent_json
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.core.plugin_schema.project_environment import NATIVE_APPLY_COMMAND, native_apply_step
from porringer.core.schema import Distribution, PluginKind, PluginParameters
from porringer.plugin.pdm.plugin import PDMEnvironment
from porringer.plugin.poetry.plugin import PoetryEnvironment
from porringer.plugin.uv.plugin import UvEnvironment
from porringer.schema import SetupAction, SetupManifest, SetupParameters
from porringer.test.mock.project_environment import MockProjectEnvironment
from porringer.utility.utility import CommandResult

_NATIVE = {'schema_version': 1, 'root': '..', 'default': 'local', 'packages': {'periapsis': {}}}
_FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'native'


class _RunnableProject(MockProjectEnvironment):
    """Available project plugin that can run commands in its environment."""

    @classmethod
    def is_available(cls) -> bool:
        return True

    @classmethod
    def run_prefix(cls) -> list[str] | None:
        return ['mock-project', 'run']


class _UnrunnableProject(_RunnableProject):
    """Available project plugin without a run command."""

    @classmethod
    def run_prefix(cls) -> list[str] | None:
        return None


def _plugins(plugin_type: type[MockProjectEnvironment] = _RunnableProject) -> DiscoveredPlugins:
    plugin = plugin_type(PluginParameters(distribution=Distribution(version=Version('0.0.0'))))
    return DiscoveredPlugins(environments={}, project_environments={'mock-project': plugin}, scm_environments={})


def _project(tmp_path: Path) -> Path:
    root = tmp_path / 'soul-engine'
    root.mkdir()
    (root / 'pyproject.toml').write_text('[project]\nname = "demo"\n', encoding='utf-8')
    return root


def _project_action(actions: list[SetupAction]) -> SetupAction:
    return next(action for action in actions if action.kind == PluginKind.PROJECT)


def test_manifest_accepts_object_and_rejects_other_shapes() -> None:
    """`native` is an opaque object owned by CPPython."""
    assert SetupManifest.model_validate({'native': _NATIVE}).native == _NATIVE
    assert SetupManifest.model_validate({}).native is None
    with pytest.raises(ValidationError):
        SetupManifest.model_validate({'native': ['periapsis']})


def test_relative_root_resolves_against_manifest_directory(tmp_path: Path) -> None:
    """A relative root becomes absolute, and the JSON is canonical."""
    intent = json.loads(native_intent_json(_NATIVE, tmp_path / 'soul-engine'))
    assert intent['root'] == str(tmp_path.resolve())
    assert native_intent_json({'b': 1, 'a': 2}, tmp_path) == '{"a":2,"b":1}'


def test_python_project_installers_have_run_prefixes() -> None:
    """Each Python project tool can run the handoff inside its environment."""
    assert PDMEnvironment.run_prefix() == ['pdm', 'run']
    assert PoetryEnvironment.run_prefix() == ['poetry', 'run']
    assert UvEnvironment.run_prefix() == ['uv', 'run', '--no-sync']
    assert native_apply_step(PDMEnvironment, '{}') == [
        'pdm',
        'run',
        *NATIVE_APPLY_COMMAND,
        '--clone',
        '--inline',
        '{}',
        '--json',
    ]


def test_without_native_nothing_changes(tmp_path: Path) -> None:
    """A manifest without `native` plans the plain project sync."""
    root = _project(tmp_path)
    plugins = _plugins()
    action = _project_action(build_actions(SetupManifest(), plugins, search_from=root))

    assert action.native_intent is None
    assert get_cli_steps(action, plugins) == (('mock-project', 'sync'),)


def test_preview_shows_the_handoff_after_sync(tmp_path: Path) -> None:
    """The Python project sync gains one step that applies the intent."""
    root = _project(tmp_path)
    plugins = _plugins()
    action = _project_action(build_actions(SetupManifest(native=_NATIVE), plugins, search_from=root))

    steps = get_cli_steps(action, plugins)
    assert steps[0] == ('mock-project', 'sync')
    assert steps[1][:5] == ('mock-project', 'run', *NATIVE_APPLY_COMMAND)
    assert steps[1][-1] == '--json'
    assert json.loads(steps[1][-2])['root'] == str(tmp_path.resolve())


async def test_execution_runs_exactly_the_previewed_steps(tmp_path: Path) -> None:
    """What the preview shows is what runs."""
    root = _project(tmp_path)
    plugins = _plugins()
    action = _project_action(build_actions(SetupManifest(native=_NATIVE), plugins, search_from=root))

    with patch(
        'porringer.backend.command.core.execution.run_command',
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=0, stdout='', stderr=''),
    ) as mock_run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert result.success, result.message
    assert tuple(tuple(call.args[0]) for call in mock_run.await_args_list) == get_cli_steps(action, plugins)
    assert all(call.kwargs['cwd'] == root for call in mock_run.await_args_list)


async def test_installer_without_run_prefix_fails_before_running(tmp_path: Path) -> None:
    """An installer that can't run the handoff fails the action instead of skipping the intent."""
    root = _project(tmp_path)
    plugins = _plugins(_UnrunnableProject)
    action = _project_action(build_actions(SetupManifest(native=_NATIVE), plugins, search_from=root))

    assert get_cli_steps(action, plugins) == (('mock-project', 'sync'),)
    with patch('porringer.backend.command.core.execution.run_command', new_callable=AsyncMock) as mock_run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert not result.success
    assert result.message is not None
    assert "'native'" in result.message
    mock_run.assert_not_awaited()


def test_native_without_python_project_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Without a Python project sync, the section is ignored with a warning."""
    with caplog.at_level(logging.WARNING):
        actions = build_actions(SetupManifest(native=_NATIVE), _plugins(), search_from=tmp_path)

    assert not any(action.kind == PluginKind.PROJECT for action in actions)
    assert "'native' section was ignored" in caplog.text


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f'soul-engine-{name}.json').read_text(encoding='utf-8'))


def test_use_and_develop_fixtures_differ_only_in_intent() -> None:
    """The two setups share every entry; only the CPPython intent and the description differ."""
    use, develop = _fixture('use'), _fixture('develop')
    shared = {key for key in use.keys() | develop.keys() if key not in {'native', 'description'}}

    assert {key: use.get(key) for key in shared} == {key: develop.get(key) for key in shared}
    assert use['native']['packages'] == develop['native']['packages']
    assert (use['native']['default'], develop['native']['default']) == ('stable', 'local')
    assert all(package.get('url') for package in develop['native']['packages'].values())


@pytest.mark.parametrize('name', ['use', 'develop'])
def test_fixture_plans_the_handoff(name: str, tmp_path: Path) -> None:
    """Each fixture plans a clone-capable handoff with the checkout root next to the project."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate(_fixture(name))
    # The mock plugin set has no system installers, so plan only the intent.
    actions = build_actions(SetupManifest(native=manifest.native), plugins, search_from=root)

    handoff = get_cli_steps(_project_action(actions), plugins)[-1]
    assert '--clone' in handoff
    intent = json.loads(handoff[handoff.index('--inline') + 1])
    assert intent['root'] == str(tmp_path.resolve())
    assert intent['default'] == _fixture(name)['native']['default']
