"""Tests for frozen sync plans and progressive inspection events."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.sync import SyncCommands
from porringer.schema import (
    ActionCompletedEvent,
    ActionInspectedEvent,
    ActionRef,
    InspectionMode,
    ManifestFailedEvent,
    ManifestLoadedEvent,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    progress_event_snapshot,
)
from porringer.utility.exception import PlanStaleError
from tests.unit.test_bootstrap_actions import _make_pipx


@pytest.fixture(name='plugins')
def fixture_plugins() -> DiscoveredPlugins:
    """An available pipx and nothing else."""
    return DiscoveredPlugins(
        environments={'pipx': _make_pipx(available=True)},
        project_environments={},
        scm_environments={},
    )


@pytest.fixture(name='executed')
def fixture_executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the packages a run executes instead of installing them."""
    executed: list[str] = []

    async def executor(
        preview: SetupResults,
        _parameters: Any,
        *,
        event_queue: Any,
        manifest_index: int,
        **_: Any,
    ) -> SetupResults:
        event_queue.put_nowait(ManifestLoadedEvent(manifest=preview, manifest_index=manifest_index))
        for index, action in zip(preview.action_indices, preview.actions, strict=True):
            executed.append(str(action.package))
            ref = ActionRef.from_indices(manifest_index, index)
            result = SetupActionResult(action=action, success=True, ref=ref)
            event_queue.put_nowait(ActionCompletedEvent(action=action, result=result, action_ref=ref))
        return preview

    monkeypatch.setattr('porringer.backend.command.sync.execute_manifest', executor)
    return executed


def _write(path: Path, tools: list[str]) -> Path:
    path.write_text(json.dumps({'version': '1', 'tools': {'python': tools}}), encoding='utf-8')
    return path


def _params(*paths: str | Path) -> SetupParameters:
    return SetupParameters(paths=list(paths), inspection_mode=InspectionMode.FAST, fail_fast=False)


async def test_plan_runs_what_was_previewed_without_reloading(
    test_api: API,
    tmp_path: Path,
    plugins: DiscoveredPlugins,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan runs its frozen actions; the manifests are not loaded again."""
    manifest = _write(tmp_path / 'porringer.json', ['pdm', 'ruff'])
    plan = await test_api.sync.plan(_params(manifest), plugins=plugins)
    planned = [action.action.package_name for action in plan.report.manifests[0].actions]
    assert planned == ['pdm', 'ruff']

    def _no_reload(*_: Any, **__: Any) -> Any:
        raise AssertionError('a plan must not reload its manifests')

    monkeypatch.setattr(SyncCommands, '_load_manifests', _no_reload)
    report = await test_api.sync.run(plan=plan, plugins=plugins)

    assert executed == planned
    assert [str(result.action.package) for result in report.results.manifest_results[0].results] == planned
    assert plan.consumed
    assert plan.manifests[0].results == []


async def test_concurrent_runs_consume_a_plan_once(
    test_api: API, tmp_path: Path, plugins: DiscoveredPlugins, executed: list[str]
) -> None:
    """A run queued behind another run must recheck whether the plan was consumed."""
    plan = await test_api.sync.plan(_params(_write(tmp_path / 'porringer.json', ['pdm'])), plugins=plugins)

    results = await asyncio.gather(
        test_api.sync.run(plan=plan, plugins=plugins),
        test_api.sync.run(plan=plan, plugins=plugins),
        return_exceptions=True,
    )

    assert executed == ['pdm']
    errors = [result for result in results if isinstance(result, ValueError)]
    assert len(errors) == 1
    assert 'already run or discarded' in str(errors[0])
    assert plan.consumed


@pytest.mark.parametrize('change', ['edit', 'delete'])
async def test_changed_manifest_makes_plan_stale(
    test_api: API,
    tmp_path: Path,
    plugins: DiscoveredPlugins,
    executed: list[str],
    change: str,
) -> None:
    """Editing or deleting a planned manifest refuses the run and runs nothing."""
    manifest = _write(tmp_path / 'porringer.json', ['pdm'])
    plan = await test_api.sync.plan(_params(manifest), plugins=plugins)

    if change == 'edit':
        _write(manifest, ['pdm', 'ruff'])
    else:
        manifest.unlink()

    with pytest.raises(PlanStaleError) as caught:
        await test_api.sync.run(plan=plan, plugins=plugins)

    assert caught.value.changed == (manifest.resolve(),)
    assert executed == []
    assert plan.consumed


async def test_host_pyproject_change_makes_plan_stale(
    test_api: API, tmp_path: Path, plugins: DiscoveredPlugins, executed: list[str]
) -> None:
    """The pyproject.toml beside a manifest is part of the plan."""
    manifest = _write(tmp_path / 'porringer.json', ['pdm'])
    host = tmp_path / 'pyproject.toml'
    host.write_text('[project]\nname = "demo"\n', encoding='utf-8')
    plan = await test_api.sync.plan(_params(manifest), plugins=plugins)
    assert host.resolve() in plan.fingerprints

    host.write_text('[project]\nname = "renamed"\n', encoding='utf-8')

    with pytest.raises(PlanStaleError) as caught:
        await test_api.sync.run(plan=plan, plugins=plugins)
    assert caught.value.changed == (host.resolve(),)
    assert executed == []


async def test_remote_plan_downloads_once(
    test_api: API,
    tmp_path: Path,
    plugins: DiscoveredPlugins,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote manifest is downloaded when planning and removed after the run."""
    downloads: list[str] = []

    async def download(urls: list[str], tmp_dir: Path, *, max_concurrency: int = 8) -> list[Path]:
        del max_concurrency
        downloads.extend(urls)
        return [_write(tmp_dir / 'porringer.json', ['pdm'])]

    monkeypatch.setattr(SyncCommands, '_download_urls', staticmethod(download))
    plan = await test_api.sync.plan(_params('https://example.invalid/porringer.json'), plugins=plugins)
    directory = plan.download_directory
    assert directory is not None
    assert directory.is_dir()

    await test_api.sync.run(plan=plan, plugins=plugins)

    assert downloads == ['https://example.invalid/porringer.json']
    assert executed == ['pdm']
    assert not directory.exists()


async def test_inspect_and_discard_release_downloads(
    test_api: API, plugins: DiscoveredPlugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspect leaves nothing behind, and discard consumes a plan."""
    directories: list[Path] = []

    async def download(urls: list[str], tmp_dir: Path, *, max_concurrency: int = 8) -> list[Path]:
        del urls, max_concurrency
        directories.append(tmp_dir)
        return [_write(tmp_dir / 'porringer.json', ['pdm'])]

    monkeypatch.setattr(SyncCommands, '_download_urls', staticmethod(download))
    params = _params('https://example.invalid/porringer.json')

    await test_api.sync.inspect(params, plugins=plugins)
    plan = await test_api.sync.plan(params, plugins=plugins)
    plan.discard()

    assert [directory.exists() for directory in directories] == [False, False]
    with pytest.raises(ValueError, match='already run or discarded'):
        await test_api.sync.run(plan=plan, plugins=plugins)


async def test_run_takes_parameters_or_a_plan(test_api: API, tmp_path: Path, plugins: DiscoveredPlugins) -> None:
    """Exactly one of parameters and plan is accepted."""
    plan = await test_api.sync.plan(_params(_write(tmp_path / 'porringer.json', ['pdm'])), plugins=plugins)
    with pytest.raises(ValueError, match='not both'):
        await test_api.sync.run(_params(tmp_path), plan=plan)
    with pytest.raises(ValueError, match='parameters or a plan'):
        await test_api.sync.run()
    plan.discard()


async def test_plan_reports_inspection_progressively(test_api: API, tmp_path: Path, plugins: DiscoveredPlugins) -> None:
    """Plugins, then failures, then every manifest's load, then each manifest's actions."""
    first = _write(tmp_path / 'a.json', ['pdm', 'ruff'])
    second = _write(tmp_path / 'b.json', ['ruff'])
    events: list[object] = []

    plan = await test_api.sync.plan(
        _params(tmp_path / 'missing.json', first, second),
        plugins=plugins,
        on_event=events.append,
    )
    plan.discard()

    assert [type(event).__name__ for event in events] == [
        'PluginsDiscoveredEvent',
        'ManifestFailedEvent',
        'ManifestLoadedEvent',
        'ManifestLoadedEvent',
        'ActionInspectedEvent',
        'ActionInspectedEvent',
        'ActionInspectedEvent',
    ]
    assert isinstance(events[1], ManifestFailedEvent)
    inspected = [event for event in events if isinstance(event, ActionInspectedEvent)]
    reported = [action.action_id for m in plan.report.manifests for action in m.actions]
    assert [event.action_ref.action_id for event in inspected] == reported
    assert [str(event.action.package) for event in inspected] == ['pdm', 'ruff', 'ruff']
    assert [event.result.ref for event in inspected] == [event.action_ref for event in inspected]
    assert all(event.result.action is event.action for event in inspected)


async def test_inspected_event_snapshot(test_api: API, tmp_path: Path, plugins: DiscoveredPlugins) -> None:
    """The new event serializes under schema version 2."""
    events: list[object] = []
    await test_api.sync.inspect(
        _params(_write(tmp_path / 'porringer.json', ['pdm'])),
        plugins=plugins,
        on_event=events.append,
    )
    event = next(event for event in events if isinstance(event, ActionInspectedEvent))

    snapshot = progress_event_snapshot(event).model_dump(mode='json')

    assert snapshot['schema_version'] == '2'
    assert snapshot['event_type'] == 'action_inspected'
    assert snapshot['action_id'] == '0:0'
    assert snapshot['inspection']['action']['package_name'] == 'pdm'
