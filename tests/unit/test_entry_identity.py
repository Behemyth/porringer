"""Tests for stable manifest entry identity."""

import json
from pathlib import Path

from porringer.api import API
from porringer.backend.command.core.action_builder import build_actions
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.core.plugin_schema.environment import Environment
from porringer.core.schema import Ecosystem, PluginKind
from porringer.schema import (
    InspectionMode,
    PackageSpec,
    SetupManifest,
    SetupParameters,
    entry_id,
)
from tests.unit.test_bootstrap_actions import _make_pip, _make_pipx

_PY = Ecosystem('python')


def _id(
    spec: str | dict,
    manifest: Path | None = Path('a/porringer.json'),
    kind: PluginKind = PluginKind.TOOL,
) -> str:
    return entry_id(manifest, kind, _PY, PackageSpec.model_validate(spec))


def test_identity_ignores_constraint_description_and_name_spelling() -> None:
    """Editing an entry's constraint or description, or respelling its name, keeps its identity."""
    assert _id('pdm') == _id('pdm>=2.20') == _id({'name': 'PDM', 'description': 'x'}) == _id('Pdm')


def test_identity_separates_installer_runtime_kind_and_manifest() -> None:
    """Each identity component distinguishes entries."""
    variants = [
        _id('pdm'),
        _id({'name': 'pdm', 'installer': 'uv'}),
        _id({'name': 'pdm', 'runtime': '3.14'}),
        _id('pdm', kind=PluginKind.PACKAGE),
        _id('pdm', manifest=Path('b/porringer.json')),
    ]
    assert len(set(variants)) == len(variants)


def test_actions_carry_entry_ids_and_synthesized_actions_do_not() -> None:
    """Declared entries get ids; a bootstrap Porringer adds on its own does not."""
    environments: dict[str, Environment] = {
        'pipx': _make_pipx(available=False),
        'pip': _make_pip(available=True),
    }
    manifest_path = Path('a/porringer.json')

    actions = build_actions(
        SetupManifest.model_validate({'tools': {'python': ['pdm']}}),
        environments,
        manifest_path=manifest_path,
    )

    by_kind = {action.kind: action.entry_id for action in actions}
    assert by_kind == {
        PluginKind.TOOL: _id('pdm', manifest=manifest_path),
        PluginKind.PACKAGE: None,
    }


async def test_entry_ids_select_actions_across_reordering(test_api: API, tmp_path: Path) -> None:
    """Selecting by entry id finds the same entry after the manifest is reordered."""
    manifest = tmp_path / 'porringer.json'
    plugins = DiscoveredPlugins(
        environments={'pipx': _make_pipx(available=True)},
        project_environments={},
        scm_environments={},
    )

    async def selected(entries: list[str], wanted: set[str]) -> list[str | None]:
        manifest.write_text(json.dumps({'version': '1', 'tools': {'python': entries}}), encoding='utf-8')
        report = await test_api.sync.inspect(
            SetupParameters(paths=[manifest], inspection_mode=InspectionMode.FAST, entry_ids=wanted),
            plugins=plugins,
        )
        return [action.action.package_name for m in report.manifests for action in m.actions]

    ruff = _id('ruff', manifest=manifest)
    assert await selected(['pdm', 'ruff'], {ruff}) == ['ruff']
    assert await selected(['ruff>=0.15', 'pdm'], {ruff}) == ['ruff']
    assert await selected(['pdm'], set()) == []
