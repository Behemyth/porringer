"""Helpers for test action ordering.

Tests for the canonical phase ordering (RUNTIME, PACKAGE, TOOL, SCM,
PROJECT) that ``build_actions()`` sorts its returned action list into,
so the displayed plan always matches execution order.
"""

import tempfile
from pathlib import Path
from typing import override

from packaging.version import Version

from porringer.backend.command.core.action_builder import PHASE_ORDER, build_actions
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.core.plugin_schema.environment import CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Distribution, Ecosystem, Package, PackageRef, PluginKind, PluginParameters
from porringer.schema import SetupManifest
from porringer.test.mock.project_environment import MockProjectEnvironment
from porringer.test.mock.scm import MockScm

_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.0')))
_PY = Ecosystem('python')


class _StubEnv(Environment):
    """Minimal Environment subclass usable for any PluginKind."""

    _kind: PluginKind = PluginKind.PACKAGE

    @staticmethod
    def ecosystem() -> Ecosystem | None:
        return _PY

    @classmethod
    def plugin_kind(cls) -> PluginKind:
        return cls._kind

    @staticmethod
    def is_supported() -> bool:
        return True

    @classmethod
    def is_available(cls) -> bool:
        return True

    @override
    def install_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        return ['stub', 'install', package.name]

    @override
    def uninstall_command(self, package: PackageRef, *, runtime_context: RuntimeContext | None = None) -> list[str]:
        return ['stub', 'uninstall', package.name]

    @override
    def upgrade_command(
        self, package: PackageRef, *, include_prereleases: bool = False, runtime_context: RuntimeContext | None = None
    ) -> list[str]:
        return ['stub', 'upgrade', package.name]

    @override
    async def packages(
        self, *, project_path: Path | None = None, runtime_context: RuntimeContext | None = None
    ) -> list[Package]:
        return []

    @override
    async def check_updates(self, params: CheckUpdatesParameters) -> list[Package]:
        return []


def _make(kind: PluginKind) -> _StubEnv:
    class _Env(_StubEnv):
        _kind = kind

    return _Env(_PARAMS)


class _AvailableGit(MockScm):
    """MockScm with tool_name='git' and forced availability."""

    @classmethod
    @override
    def tool_name(cls) -> str:
        return 'git'

    @classmethod
    @override
    def is_available(cls) -> bool:
        return True


class _AvailablePdm(MockProjectEnvironment):
    """Project plugin with pdm.lock evidence, forced availability."""

    _project_evidence_files = ('pdm.lock',)

    @classmethod
    @override
    def tool_name(cls) -> str:
        return 'pdm'

    @classmethod
    @override
    def is_available(cls) -> bool:
        return True


class TestActionOrdering:
    """Verify the returned action list is sorted into canonical phase order."""

    @staticmethod
    def test_all_five_kinds_sorted_into_phase_order() -> None:
        """A manifest touching every kind is returned in RUNTIME/PACKAGE/TOOL/SCM/PROJECT order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / 'pyproject.toml').write_text('[project]\nname = "demo"\n', encoding='utf-8')
            (tmp_path / 'pdm.lock').write_text('', encoding='utf-8')

            # Declare manifest sections in a deliberately "wrong" order
            # (tools before packages, scm before runtimes) to prove the
            # sort — not declaration order — determines the result.
            manifest = SetupManifest.model_validate({
                'url': 'https://github.com/synodic/periapsis',
                'tools': {'python': ['pdm-tool']},
                'runtimes': {'python': ['3.14']},
                'packages': {'python': ['ruff']},
            })

            plugins = DiscoveredPlugins(
                environments={
                    'pip': _make(PluginKind.PACKAGE),
                    'pipx': _make(PluginKind.TOOL),
                    'pim': _make(PluginKind.RUNTIME),
                },
                project_environments={'pdm': _AvailablePdm(_PARAMS)},
                scm_environments={'git': _AvailableGit(_PARAMS)},
            )

            actions = build_actions(manifest, plugins, search_from=tmp_path)

            kinds = [a.kind for a in actions]
            expected_order = [k for k in PHASE_ORDER if k in set(kinds)]
            # Every kind's first occurrence must appear in PHASE_ORDER order.
            seen_order = list(dict.fromkeys(kinds))
            assert seen_order == expected_order
