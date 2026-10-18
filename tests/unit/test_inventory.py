"""Tests for the read-only runtime and package inventory."""

from pathlib import Path
from typing import override

import pytest
from typer.testing import CliRunner

from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.package import PackageCommands
from porringer.console.entry import app
from porringer.core.plugin_schema.environment import (
    CheckUpdatesParameters,
    Environment,
    PackageScope,
)
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.plugin_schema.runtime import runtime_tag_matches as tag_matches
from porringer.core.schema import Package, PluginKind
from porringer.schema import InventoryStatus
from tests.fixtures.mock_plugins import MOCK_DIST, MockPythonEnv, MockRuntimeProvider

_PY314 = Path('/py/3.14/python')
_PY313 = Path('/py/3.13/python')


class _Provider(MockRuntimeProvider):
    """Resolves 3.14 (default) and 3.13 (installed by this provider)."""

    @staticmethod
    @override
    def plugin_kind() -> PluginKind:
        return PluginKind.RUNTIME

    @override
    def query_availability(self, runtime_context: RuntimeContext | None = None) -> bool:
        return True

    @override
    async def available_tags(self) -> list[str]:
        return ['3.13', '3.14']

    @override
    async def resolve_executable(self, tag: str) -> Path | None:
        return {'3.14': _PY314, '3.13': _PY313}[tag]

    @override
    async def default_tag(self) -> str | None:
        return '3.14'

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        return [Package(name='3.13', version='3.13.5')]


class _Pip(MockPythonEnv):
    """Lists requests in 3.14 and fails in 3.13."""

    @override
    def query_availability(self, runtime_context: RuntimeContext | None = None) -> bool:
        return True

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        assert runtime_context is not None
        if runtime_context.get('python') == _PY313:
            raise RuntimeError('pip is broken here')
        return [
            Package(name='requests', version='2.0'),
            Package(name='rich', version='14.0'),
        ]

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        return [
            Package(name='requests', version='2.1'),
            Package(name='rich', version='14.0'),
        ]


class _Pipx(_Pip):
    """One global tool store."""

    @staticmethod
    @override
    def plugin_kind() -> PluginKind:
        return PluginKind.TOOL

    @classmethod
    @override
    def package_scope(cls) -> PackageScope:
        return PackageScope.GLOBAL

    @override
    async def packages(
        self,
        *,
        project_path: Path | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> list[Package]:
        return [Package(name='pdm', version='2.30')]


def _plugins(**extra: Environment) -> DiscoveredPlugins:
    environments: dict[str, Environment] = {
        'pim': _Provider(MOCK_DIST),
        'pip': _Pip(MOCK_DIST),
        'pipx': _Pipx(MOCK_DIST),
        **extra,
    }
    return DiscoveredPlugins(environments=environments, project_environments={}, scm_environments={})


async def test_runtimes_carry_default_and_managed_flags() -> None:
    """Every resolvable tag is listed, flagged as default and as installed by the provider."""
    report = await PackageCommands.inventory(plugins=_plugins())

    assert [(r.tag, r.executable, r.default, r.managed) for r in report.runtimes] == [
        ('3.14', _PY314, True, False),
        ('3.13', _PY313, False, True),
    ]


async def test_runtime_stores_per_interpreter_and_global_stores_once() -> None:
    """The pip plugin appears per interpreter, a failing one as an error, and pipx once."""
    report = await PackageCommands.inventory(plugins=_plugins())

    summary = [
        (
            loc.plugin,
            loc.runtime.tag if loc.runtime else None,
            loc.status,
            [p.name for p in loc.packages],
        )
        for loc in report.locations
    ]
    assert summary == [
        ('pip', '3.14', InventoryStatus.OK, ['requests', 'rich']),
        ('pip', '3.13', InventoryStatus.ERROR, []),
        ('pipx', None, InventoryStatus.OK, ['pdm']),
    ]
    assert not report.success


async def test_runtime_filter_without_global_stores() -> None:
    """Selecting a runtime lists only that interpreter's packages."""
    report = await PackageCommands.inventory(plugins=_plugins(), runtimes={'3.14'}, include_global=False)

    assert [(loc.plugin, loc.runtime.tag if loc.runtime else None) for loc in report.locations] == [('pip', '3.14')]
    assert report.success


async def test_updates_mark_only_newer_versions() -> None:
    """Latest versions are attached only when newer than what's installed."""
    report = await PackageCommands.inventory(
        plugins=_plugins(),
        runtimes={'3.14'},
        include_global=False,
        include_updates=True,
    )

    assert {p.name: p.latest_version for p in report.locations[0].packages} == {
        'requests': '2.1',
        'rich': None,
    }


async def test_an_interpreter_seen_by_two_providers_is_listed_once() -> None:
    """Two providers resolving the same executable produce one location per plugin."""
    report = await PackageCommands.inventory(plugins=_plugins(pyenv=_Provider(MOCK_DIST)), runtimes={'3.14'})

    assert [(loc.plugin, loc.runtime.provider if loc.runtime else None) for loc in report.locations] == [
        ('pip', 'pim'),
        ('pipx', None),
    ]


@pytest.mark.parametrize(
    ('tag', 'wanted', 'selected'),
    [
        ('3.14', '3.14', True),
        ('3.14-64', '3.14', True),
        ('3.14-64', '3.14-64', True),
        ('3.14', '3.1', False),
    ],
)
def test_short_tags_select_architecture_variants(tag: str, wanted: str, selected: bool) -> None:
    """A version tag selects that version's architecture-qualified variants."""
    assert tag_matches(tag, wanted) is selected


@pytest.mark.parametrize(
    'flags',
    [['--global', '--runtime', '3.14'], ['--runtime', '3.14', '--all-runtimes']],
)
def test_cli_runtime_flags_are_exclusive(flags: list[str], test_config) -> None:
    """--global, --runtime and --all-runtimes can't be combined."""
    result = CliRunner().invoke(app, ['check', *flags], obj=test_config)

    assert result.exit_code == 1
    assert 'Use only one of' in result.output
