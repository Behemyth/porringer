"""Tests for removing runtimes safely: dependents, cascade and NOT_MANAGED."""

import json
from pathlib import Path
from typing import override
from unittest.mock import AsyncMock

import pytest

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.core.resolution import resolve_uninstall_operation
from porringer.core.plugin_schema.environment import Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Ecosystem, Package, PackageRef, PluginKind
from porringer.plugin.pipx.plugin import PIPXEnvironment
from porringer.schema import PackageSpec, ResultStatus, SetupAction, SkipReason, entry_id
from porringer.schema.execution import Skip
from porringer.utility.exception import RuntimeInUseError
from tests.fixtures.mock_plugins import MOCK_DIST
from tests.unit.test_inventory import _PY314, _Pip, _Pipx, _Provider
from tests.unit.test_tool_verbs import _Runs

_PY = Ecosystem('python')


@pytest.fixture(name='runs')
def fixture_runs(monkeypatch: pytest.MonkeyPatch) -> _Runs:
    """Recorded fake execution."""
    return _Runs(monkeypatch)


class _PipxOn314(_Pipx):
    """A pipx whose black venv was built on 3.14."""

    @override
    async def runtime_dependents(self, executable: Path) -> list[str]:
        return ['black'] if executable == _PY314 else []


def _plugins(pipx: Environment) -> DiscoveredPlugins:
    environments: dict[str, Environment] = {
        'pim': _Provider(MOCK_DIST),
        'pip': _Pip(MOCK_DIST),
        'pipx': pipx,
    }
    return DiscoveredPlugins(
        environments=environments,
        project_environments={},
        scm_environments={},
        runtime_context=RuntimeContext(),
    )


def _write_global(api: API) -> tuple[str, str]:
    """A global manifest with Python 3.14 and pdm running on it; returns their entry ids."""
    path = api.global_manifest_path
    data = {
        'version': '1',
        'runtimes': {'python': ['3.14']},
        'tools': {'python': [{'name': 'pdm', 'runtime': '3.14'}]},
    }
    path.write_text(json.dumps(data), encoding='utf-8')
    runtime = entry_id(path, PluginKind.RUNTIME, _PY, PackageSpec.model_validate('3.14'))
    pdm = entry_id(
        path,
        PluginKind.TOOL,
        _PY,
        PackageSpec.model_validate({'name': 'pdm', 'runtime': '3.14'}),
    )
    return runtime, pdm


async def test_dependents_report_entries_tools_default_and_contents(
    test_api: API,
) -> None:
    """The check lists entries on the runtime, tools built on it, and what goes with it."""
    runtime, pdm = _write_global(test_api)

    (check,) = await test_api.tool.runtime_dependents({runtime}, plugins=_plugins(_PipxOn314(MOCK_DIST)))

    assert check.executable == _PY314
    assert check.default
    assert [(d.entry_id, d.plugin, d.package) for d in check.dependents] == [
        (pdm, None, None),
        (None, 'pipx', 'black'),
    ]
    assert check.installed_packages == ('requests', 'rich')


async def test_removal_is_refused_while_anything_depends_on_the_runtime(test_api: API, runs: _Runs) -> None:
    """Nothing is uninstalled or edited when dependents remain."""
    runtime, _pdm = _write_global(test_api)
    before = test_api.global_manifest_path.read_text(encoding='utf-8')

    with pytest.raises(RuntimeInUseError, match='black \\(pipx\\)') as caught:
        await test_api.tool.remove({runtime}, plugins=_plugins(_PipxOn314(MOCK_DIST)))

    assert caught.value.runtime == '3.14'
    assert caught.value.dependents[0].startswith('pdm (tools.python)')
    assert runs.log == []
    assert test_api.global_manifest_path.read_text(encoding='utf-8') == before


async def test_cascade_removes_dependents_first(test_api: API, runs: _Runs) -> None:
    """With cascade, dependent entries and tools go first, then the runtime and its entry."""
    runtime, _pdm = _write_global(test_api)
    pipx = _PipxOn314(MOCK_DIST)
    pipx.uninstall = AsyncMock(return_value=Package(name='black'))

    report = await test_api.tool.remove({runtime}, cascade=True, plugins=_plugins(pipx))

    assert report.success
    assert runs.log == [('uninstall', 'pdm'), ('uninstall', '3.14')]
    pipx.uninstall.assert_awaited_once()
    assert json.loads(test_api.global_manifest_path.read_text(encoding='utf-8')) == {'version': '1'}


@pytest.mark.parametrize('raises', [False, True])
async def test_failed_cascade_keeps_the_runtime(test_api: API, runs: _Runs, raises: bool) -> None:
    """A dependent uninstall failure must stop runtime removal and mark the report failed."""
    runtime, _pdm = _write_global(test_api)
    pipx = _PipxOn314(MOCK_DIST)
    pipx.uninstall = AsyncMock(return_value=None, side_effect=RuntimeError('uninstall failed') if raises else None)

    report = await test_api.tool.remove({runtime}, cascade=True, plugins=_plugins(pipx))

    assert not report.success
    assert report.status is ResultStatus.FAILED
    assert runs.log == [('uninstall', 'pdm')]
    assert json.loads(test_api.global_manifest_path.read_text(encoding='utf-8'))['runtimes'] == {'python': ['3.14']}


async def test_runtime_dependents_are_scoped_to_the_ecosystem(test_api: API) -> None:
    """An identically named runtime in another ecosystem is not a dependent."""
    runtime, pdm = _write_global(test_api)
    path = test_api.global_manifest_path
    data = json.loads(path.read_text(encoding='utf-8'))
    data['tools']['node'] = [{'name': 'other-tool', 'runtime': '3.14'}]
    path.write_text(json.dumps(data), encoding='utf-8')

    (check,) = await test_api.tool.runtime_dependents({runtime}, plugins=_plugins(_Pipx(MOCK_DIST)))

    assert [dependent.entry_id for dependent in check.dependents] == [pdm]


async def test_runtime_installed_elsewhere_is_not_managed() -> None:
    """A runtime the provider can see but didn't install is skipped as NOT_MANAGED."""

    class _Elsewhere(_Provider):
        @override
        async def packages(
            self,
            *,
            project_path: Path | None = None,
            runtime_context: RuntimeContext | None = None,
        ) -> list[Package]:
            return []

    action = SetupAction(
        description='Uninstall 3.14',
        kind=PluginKind.RUNTIME,
        ecosystem=_PY,
        installer='pim',
        package=PackageRef(name='3.14'),
    )

    resolved = await resolve_uninstall_operation(action, _Elsewhere(MOCK_DIST))

    assert isinstance(resolved.operation, Skip)
    assert resolved.operation.reason is SkipReason.NOT_MANAGED


async def test_pipx_finds_venvs_built_on_an_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pipx venv belongs to the interpreter its pyvenv.cfg home points at."""
    monkeypatch.setenv('PIPX_HOME', str(tmp_path))
    base = tmp_path / 'python314'
    for name, home in (('black', base), ('ruff', tmp_path / 'python315')):
        venv = tmp_path / 'venvs' / name
        venv.mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text(f'home = {home}\nversion = 3.14.0\n', encoding='utf-8')

    assert await PIPXEnvironment(MOCK_DIST).runtime_dependents(base / 'python.exe') == ['black']
