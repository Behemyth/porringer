"""Tests for handing a manifest's `native` section to the project's own CPPython."""

import json
import sys
from asyncio import CancelledError, Queue
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from packaging.version import Version
from pydantic import ValidationError
from rich.console import Console

from porringer.backend.command.core import execution
from porringer.backend.command.core.action_builder import build_actions, get_cli_steps, native_intent_json
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.console.command import sync as sync_command
from porringer.console.schema import ConsoleConfiguration
from porringer.core.plugin_schema.project_environment import NATIVE_APPLY_COMMAND, ProjectCommandPlan, native_apply_step
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Distribution, PluginKind, PluginParameters
from porringer.plugin.pdm.plugin import PDMEnvironment
from porringer.plugin.poetry.plugin import PoetryEnvironment
from porringer.plugin.uv.plugin import UvEnvironment
from porringer.schema import (
    ActionCompletedEvent,
    ActionProgressEvent,
    BatchSetupResults,
    SetupAction,
    SetupActionResult,
    SetupManifest,
    SetupParameters,
    SetupResults,
    progress_event_snapshot,
)
from porringer.schema.preparation import PREPARATION_REPORT_ENV
from porringer.test.mock.project_environment import MockProjectEnvironment
from porringer.utility.exception import ManifestError
from porringer.utility.utility import CommandProgress, CommandResult

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
    assert PDMEnvironment.prepare_command() == ['pdm', 'run', 'prepare']
    assert PoetryEnvironment.prepare_command() == ['poetry', 'run', 'prepare']
    assert UvEnvironment.prepare_command() == ['uv', 'run', '--no-sync', 'prepare']
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
    assert not result.project_prepared
    assert tuple(tuple(call.args[0]) for call in mock_run.await_args_list) == get_cli_steps(action, plugins)
    assert all(call.kwargs['cwd'] == root for call in mock_run.await_args_list)


def test_installer_without_run_prefix_fails_during_planning(tmp_path: Path) -> None:
    """An unsupported handoff is an error, not an incomplete preview."""
    root = _project(tmp_path)
    plugins = _plugins(_UnrunnableProject)
    with pytest.raises(ManifestError, match="'native'.*can't be applied"):
        build_actions(SetupManifest(native=_NATIVE), plugins, search_from=root)


def test_native_without_python_project_fails(tmp_path: Path) -> None:
    """Native intent must never be silently omitted when it has no project owner."""
    with pytest.raises(ManifestError, match='requires exactly one Python project sync'):
        build_actions(SetupManifest(native=_NATIVE), _plugins(), search_from=tmp_path)


@pytest.mark.parametrize(
    'project',
    [
        {'ecosystem': 'python', 'prepare': []},
        {'ecosystem': 'python', 'prepare': ['pdm', 'run', 'prepare']},
        {'ecosystem': 'python', 'prepare': False},
        {'ecosystem': 'python', 'prepare': 'true'},
        {'ecosystem': 'python', 'prepare': 1},
        {'ecosystem': 'python'},
        {'ecosystem': '', 'prepare': True},
    ],
)
def test_invalid_project_preparation_is_rejected(project: dict[str, Any]) -> None:
    """Preparation is an explicit typed request, not a manifest-supplied command."""
    with pytest.raises(ValidationError):
        SetupManifest.model_validate({'project': project})


def test_preparation_follows_sync_and_native_handoff(tmp_path: Path) -> None:
    """The preview exposes the exact argv after native source selection."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'native': _NATIVE, 'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))

    steps = get_cli_steps(action, plugins)
    assert steps[0] == ('mock-project', 'sync')
    assert steps[1][:5] == ('mock-project', 'run', *NATIVE_APPLY_COMMAND)
    assert steps[2] == ('mock-project', 'run', 'prepare')


async def test_preparation_execution_matches_preview(tmp_path: Path) -> None:
    """The project-owned argv runs in the project root without a shell."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))

    with patch(
        'porringer.backend.command.core.execution.run_command',
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=0, stdout='', stderr=''),
    ) as mock_run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert result.success, result.message
    assert result.project_prepared
    assert tuple(tuple(call.args[0]) for call in mock_run.await_args_list) == get_cli_steps(action, plugins)
    assert all(call.kwargs['cwd'] == root for call in mock_run.await_args_list)


@pytest.mark.parametrize(
    ('report', 'diagnostic'),
    [
        (None, 'preparation.metadata_unavailable'),
        ('{"password":"do-not-emit"}', 'preparation.metadata_invalid'),
        ('{}\n{}', 'preparation.metadata_invalid'),
        ('{"backend":"cmake","provider":"conan"}', 'preparation.metadata_invalid'),
        ('{"schema_version":"2","backend":"cmake","provider":"conan"}', 'preparation.metadata_invalid'),
        (
            json.dumps({
                'schema_version': '1',
                'identity': 'actual-selection',
                'backend': 'cmake',
                'provider': 'conan',
            }),
            None,
        ),
    ],
)
async def test_preparation_metadata_is_explicit(tmp_path: Path, report: str | None, diagnostic: str | None) -> None:
    """Only the dedicated file from successful preparation supplies native facts."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    report_paths: list[Path] = []

    def command(_args: list[str], **kwargs: Any) -> CommandResult:
        if environment := kwargs.get('env'):
            path = Path(environment[PREPARATION_REPORT_ENV])
            report_paths.append(path)
            assert path.is_absolute()
            assert path.parent == root
            if report is not None:
                path.write_text(report, encoding='utf-8')
        return CommandResult(0, 'CPPYTHON_PREPARATION_V1:{"backend":"ignored-log"}', '')

    with patch(
        'porringer.backend.command.core.execution.run_command',
        new_callable=AsyncMock,
        side_effect=command,
    ) as run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )
    assert result.success == (diagnostic in {None, 'preparation.metadata_unavailable'})
    assert result.project_prepared
    assert 'env' not in run.await_args_list[0].kwargs
    assert report_paths
    assert not report_paths[0].exists()
    if diagnostic:
        assert result.preparation_metadata is None
        assert result.preparation_diagnostics[0].code == diagnostic
        assert 'do-not-emit' not in result.preparation_diagnostics[0].model_dump_json()
    else:
        assert result.preparation_metadata is not None
        assert result.preparation_metadata.identity == 'actual-selection'
        assert result.preparation_metadata.provider == 'conan'
        assert result.preparation_metadata.dependency_outputs is None
    snapshot = progress_event_snapshot(ActionCompletedEvent(action=action, result=result))
    assert snapshot.result is not None
    assert snapshot.result.preparation_metadata == result.preparation_metadata


@pytest.mark.parametrize('error', [None, TimeoutError(), CancelledError()])
async def test_failed_or_interrupted_preparation_never_reads_report(
    tmp_path: Path, error: BaseException | None
) -> None:
    """Report cleanup covers process failure, timeout and cancellation, without consuming failed output."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    report_paths: list[Path] = []

    def command(_args: list[str], **kwargs: Any) -> CommandResult:
        if environment := kwargs.get('env'):
            path = Path(environment[PREPARATION_REPORT_ENV])
            report_paths.append(path)
            path.write_text('invalid report that must never be read', encoding='utf-8')
            if error is not None:
                raise error
            return CommandResult(1, '', 'preparation failed')
        return CommandResult(0, '', '')

    with (
        patch('porringer.backend.command.core.execution.run_command', side_effect=command),
        patch('porringer.backend.command.core.execution._preparation_metadata') as read_report,
    ):
        if isinstance(error, CancelledError):
            with pytest.raises(CancelledError):
                await execution._execute_project_install(
                    action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
                )
        else:
            result = await execution._execute_project_install(
                action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
            )
            assert not result.success
            assert not result.project_prepared
        read_report.assert_not_called()
    assert report_paths
    assert not report_paths[0].exists()


def test_preparation_report_read_error_is_explicit(tmp_path: Path) -> None:
    """Unreadable reports are errors rather than old-version compatibility warnings."""
    with patch.object(Path, 'read_text', side_effect=PermissionError('private-details')):
        metadata, diagnostics = execution._preparation_metadata(tmp_path / 'report.json')
    assert metadata is None
    assert diagnostics[0].code == 'preparation.metadata_read_failed'
    assert diagnostics[0].severity == 'error'
    assert 'private-details' not in diagnostics[0].model_dump_json()


@pytest.mark.parametrize('error', [None, TimeoutError(), CancelledError()])
async def test_report_cleanup_errors_do_not_replace_primary_failure(
    tmp_path: Path, error: BaseException | None
) -> None:
    """Cleanup failures surface, but the original timeout or cancellation remains authoritative."""
    action = SetupAction(description='Prepare')
    with (
        patch(
            'porringer.backend.command.core.execution.run_command',
            new_callable=AsyncMock,
            side_effect=error,
            return_value=CommandResult(0, '', ''),
        ),
        patch.object(Path, 'unlink', side_effect=PermissionError('cleanup failed')),
    ):
        if error is None:
            with pytest.raises(PermissionError, match='cleanup failed'):
                await execution._run_preparation_command(
                    ['prepare'],
                    progress=CommandProgress(action=action, callback=lambda _: None),
                    cwd=tmp_path,
                    timeout_seconds=None,
                )
        else:
            with pytest.raises(type(error)) as caught:
                await execution._run_preparation_command(
                    ['prepare'],
                    progress=CommandProgress(action=action, callback=lambda _: None),
                    cwd=tmp_path,
                    timeout_seconds=None,
                )
            assert caught.value is error
            assert 'Could not remove the native preparation report.' in error.__notes__


async def test_handoff_commands_remain_as_previewed_if_plugin_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native and preparation commands are fixed when the action is planned."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'native': _NATIVE, 'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    preview = get_cli_steps(action, plugins)
    monkeypatch.setattr(_RunnableProject, 'run_prefix', classmethod(lambda cls: ['different-tool', 'run']))

    with patch(
        'porringer.backend.command.core.execution.run_command',
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=0, stdout='', stderr=''),
    ) as run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert result.success, result.message
    assert tuple(tuple(call.args[0]) for call in run.await_args_list) == preview
    assert get_cli_steps(action, plugins) == preview


async def test_real_project_subprocesses_match_preview(tmp_path: Path) -> None:
    """Run the previewed sync, native handoff, and preparation as real subprocesses."""
    root = _project(tmp_path)

    def command(label: str) -> list[str]:
        script = f"from pathlib import Path; Path('steps.log').open('a').write('{label}\\n')"
        if label == 'prepare':
            report = json.dumps({
                'schema_version': '1',
                'identity': 'actual-subprocess-selection',
                'backend': 'cmake',
                'provider': 'conan',
                'dependency_outputs': [str(root / 'provider-output')],
            })
            script += (
                f'; import os; report_path = Path(os.environ[{PREPARATION_REPORT_ENV!r}])'
                f"; assert report_path.is_absolute(); report_path.write_text({report!r}, encoding='utf-8')"
                "; print('CPPYTHON_PREPARATION_V1:ordinary-log-not-a-report')"
                "; print('\\n'.join('later output' for _ in range(250)))"
            )
        return [sys.executable, '-c', script]

    class _ScriptedProject(_RunnableProject):
        @classmethod
        def command_plan(
            cls, search_from: Path, *, runtime_context: RuntimeContext | None = None
        ) -> ProjectCommandPlan:
            argv = command('sync')
            return ProjectCommandPlan(directory=search_from, argv=argv, steps=[argv])

        @classmethod
        def run_prefix(cls) -> list[str]:
            return command('native')

        @classmethod
        def prepare_command(cls) -> list[str]:
            return command('prepare')

    plugins = _plugins(_ScriptedProject)
    manifest = SetupManifest.model_validate({'native': _NATIVE, 'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    preview = get_cli_steps(action, plugins)

    events: Queue = Queue()
    result = await execution._execute_project_install(
        action, plugins.project_environments, root, SetupParameters(), event_queue=events
    )

    assert result.success, result.message
    assert result.project_prepared
    assert result.cli_steps == preview
    assert (root / 'steps.log').read_text(encoding='utf-8').splitlines() == ['sync', 'native', 'prepare']
    assert result.preparation_metadata is not None
    assert result.preparation_metadata.identity == 'actual-subprocess-selection'
    assert result.preparation_metadata.provider == 'conan'
    assert result.preparation_metadata.dependency_outputs == (root / 'provider-output',)
    snapshot = progress_event_snapshot(ActionCompletedEvent(action=action, result=result))
    assert snapshot.result is not None
    assert snapshot.result.model_dump(mode='json')['project_prepared'] is True
    assert snapshot.result.preparation_metadata == result.preparation_metadata
    assert not list(root.glob('.porringer-preparation-*.json'))
    output = []
    while not events.empty():
        event = events.get_nowait()
        if isinstance(event, ActionProgressEvent):
            output.append(event.progress.output)
    assert 'CPPYTHON_PREPARATION_V1:ordinary-log-not-a-report' in output


def test_final_output_distinguishes_prepared_projects() -> None:
    """A project is called prepared only when its preparation completed."""
    configuration = ConsoleConfiguration(console=Console(record=True))
    action = SetupAction(description='Sync project')
    results = BatchSetupResults(
        manifest_results=[SetupResults(results=[SetupActionResult(action=action, success=True, project_prepared=True)])]
    )

    sync_command._display_results(configuration, results)

    assert '1 project(s) prepared' in configuration.console.export_text()


async def test_failed_sync_does_not_run_preparation(tmp_path: Path) -> None:
    """A failed project sync stops before running its preparation step."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))

    with patch(
        'porringer.backend.command.core.execution.run_command',
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=1, stdout='', stderr='sync failed'),
    ) as mock_run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert not result.success
    assert not result.project_prepared
    assert result.failed_step_index == 1
    mock_run.assert_awaited_once()


async def test_failed_preparation_is_not_reported_as_prepared(tmp_path: Path) -> None:
    """A successful sync does not turn a failed preparation into a success."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    results = [
        CommandResult(returncode=0, stdout='', stderr=''),
        CommandResult(returncode=1, stdout='', stderr='preparation failed'),
    ]

    with patch(
        'porringer.backend.command.core.execution.run_command', new_callable=AsyncMock, side_effect=results
    ) as run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert not result.success
    assert not result.project_prepared
    assert result.failed_step_index == len(results)
    assert run.await_count == len(results)


def test_preparation_without_matching_project_fails(tmp_path: Path) -> None:
    """Never claim setup succeeded if the required project step has no owner."""
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})
    with pytest.raises(ManifestError, match='requires exactly one'):
        build_actions(manifest, _plugins(), search_from=tmp_path)


def test_preparation_without_installer_capability_fails_during_planning(tmp_path: Path) -> None:
    """An unsupported preparation command must not be omitted from preview."""
    root = _project(tmp_path)
    manifest = SetupManifest.model_validate({'project': {'ecosystem': 'python', 'prepare': True}})

    with pytest.raises(ManifestError, match='cannot provide a preparation command'):
        build_actions(manifest, _plugins(_UnrunnableProject), search_from=root)


async def test_failed_native_handoff_does_not_run_preparation(tmp_path: Path) -> None:
    """A failed native selection stops before project preparation."""
    root = _project(tmp_path)
    plugins = _plugins()
    manifest = SetupManifest.model_validate({'native': _NATIVE, 'project': {'ecosystem': 'python', 'prepare': True}})
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    results = [
        CommandResult(returncode=0, stdout='', stderr=''),
        CommandResult(returncode=1, stdout='', stderr='native handoff failed'),
    ]

    with patch(
        'porringer.backend.command.core.execution.run_command', new_callable=AsyncMock, side_effect=results
    ) as run:
        result = await execution._execute_project_install(
            action, plugins.project_environments, root, SetupParameters(), event_queue=Queue()
        )

    assert not result.success
    assert not result.project_prepared
    assert result.failed_step_index == len(results)
    assert run.await_count == len(results)


@pytest.mark.parametrize(
    'sections',
    [
        {'project': {'ecosystem': 'python', 'prepare': True}},
        {'native': _NATIVE},
        {'native': _NATIVE, 'project': {'ecosystem': 'python', 'prepare': True}},
    ],
)
def test_deferred_project_keeps_previewed_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sections: dict[str, Any]
) -> None:
    """Installing a different tool must not change the authorized project handoff."""
    root = _project(tmp_path)
    (root / 'mock-project.lock').touch()

    class _DeferredProject(_RunnableProject):
        _project_evidence_files = ('mock-project.lock',)

        @classmethod
        def is_available(cls) -> bool:
            return False

    class _AlternateProject(_RunnableProject):
        @classmethod
        def tool_name(cls) -> str:
            return 'alternate'

        @classmethod
        def run_prefix(cls) -> list[str]:
            return ['alternate', 'run']

    params = PluginParameters(distribution=Distribution(version=Version('0.0.0')))
    plugins = DiscoveredPlugins(
        environments={},
        project_environments={
            'mock-project': _DeferredProject(params),
            'alternate': _AlternateProject(params),
        },
        scm_environments={},
    )
    manifest = SetupManifest.model_validate(sections)
    action = _project_action(build_actions(manifest, plugins, search_from=root))
    assert action.installer is None
    preview = get_cli_steps(action, plugins)
    assert preview[0] == ('mock-project', 'sync')
    if 'native' in sections:
        assert preview[1][:5] == ('mock-project', 'run', *NATIVE_APPLY_COMMAND)
    if 'project' in sections:
        assert preview[-1] == ('mock-project', 'run', 'prepare')

    actions = [action]
    execution.resolve_deferred_actions(actions, plugins)
    assert actions[0].installer is None
    monkeypatch.setattr(_DeferredProject, 'is_available', classmethod(lambda cls: True))
    execution.resolve_deferred_actions(actions, plugins)
    assert actions[0].installer == 'mock-project'
    assert get_cli_steps(actions[0], plugins) == preview


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
