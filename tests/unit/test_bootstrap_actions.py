"""Helpers for test bootstrap actions.

Tests for synthesized bootstrap actions (e.g. installing pipx via pip
before it can install a deferred TOOL like pdm).
"""

from pathlib import Path
from typing import override

from packaging.version import Version

from porringer.backend.command.core.action_builder import build_actions
from porringer.core.plugin_schema.environment import BootstrapRequirement, CheckUpdatesParameters, Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import (
    Distribution,
    Ecosystem,
    Package,
    PackageRef,
    PluginKind,
    PluginParameters,
)
from porringer.schema import SetupManifest

_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.0')))
_PY = Ecosystem('python')


class _StubPlugin(Environment):
    """Minimal Environment subclass for testing (mirrors test_deferred_resolution.py)."""

    @staticmethod
    def ecosystem() -> Ecosystem | None:
        return _PY

    @staticmethod
    def plugin_kind() -> PluginKind:
        return PluginKind.PACKAGE

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


def _make_pip(*, available: bool = True) -> _StubPlugin:
    """A 'pip' PACKAGE-kind plugin — the installer used to bootstrap pipx."""

    class _Pip(_StubPlugin):
        @staticmethod
        def plugin_kind() -> PluginKind:
            return PluginKind.PACKAGE

        @classmethod
        def is_available(cls) -> bool:
            return available

    return _Pip(_PARAMS)


def _make_pipx(*, available: bool = False) -> _StubPlugin:
    """A 'pipx' TOOL-kind plugin declaring a bootstrap_requirement on pip."""

    class _Pipx(_StubPlugin):
        @staticmethod
        def plugin_kind() -> PluginKind:
            return PluginKind.TOOL

        @classmethod
        def is_available(cls) -> bool:
            return available

        @classmethod
        @override
        def bootstrap_requirement(cls) -> BootstrapRequirement | None:
            return BootstrapRequirement(installer='pip', package=PackageRef.model_validate('pipx'))

    return _Pipx(_PARAMS)


def _make_uv_tool(*, available: bool = True) -> _StubPlugin:
    """A 'uv' TOOL-kind plugin with no bootstrap requirement."""

    class _UvTool(_StubPlugin):
        @staticmethod
        def plugin_kind() -> PluginKind:
            return PluginKind.TOOL

        @classmethod
        def is_available(cls) -> bool:
            return available

    return _UvTool(_PARAMS)


class TestBootstrapSynthesis:
    """Verify pipx-via-pip bootstrap synthesis for deferred TOOL actions."""

    @staticmethod
    def test_synthesizes_pip_install_pipx_when_deferred() -> None:
        """A deferred pdm TOOL action whose only candidate (pipx) is unavailable gets a bootstrap."""
        manifest = SetupManifest.model_validate({'tools': {'python': ['pdm']}})
        environments: dict[str, Environment] = {
            'pipx': _make_pipx(available=False),
            'pip': _make_pip(available=True),
        }

        actions = build_actions(manifest, environments)

        tool_actions = [a for a in actions if a.kind == PluginKind.TOOL]
        package_actions = [a for a in actions if a.kind == PluginKind.PACKAGE]

        assert len(tool_actions) == 1
        assert tool_actions[0].installer is None  # deferred

        assert len(package_actions) == 1
        assert package_actions[0].installer == 'pip'
        assert package_actions[0].package is not None
        assert package_actions[0].package.name == 'pipx'

    @staticmethod
    def test_no_bootstrap_when_pipx_available() -> None:
        """When pipx already resolves, no bootstrap action is synthesized."""
        manifest = SetupManifest.model_validate({'tools': {'python': ['pdm']}})
        environments: dict[str, Environment] = {
            'pipx': _make_pipx(available=True),
            'pip': _make_pip(available=True),
        }

        actions = build_actions(manifest, environments)

        assert all(a.kind != PluginKind.PACKAGE for a in actions)

    @staticmethod
    def test_preference_for_other_tool_suppresses_bootstrap() -> None:
        """preferences={'python': 'uv'} must not trigger a pipx bootstrap."""
        manifest = SetupManifest.model_validate({
            'tools': {'python': ['pdm']},
            'preferences': {'python': 'uv'},
        })
        environments: dict[str, Environment] = {
            'pipx': _make_pipx(available=False),
            'uv': _make_uv_tool(available=False),
            'pip': _make_pip(available=True),
        }

        actions = build_actions(manifest, environments)

        assert all(a.kind != PluginKind.PACKAGE for a in actions)

    @staticmethod
    def test_explicit_pipx_entry_dedupes_with_bootstrap() -> None:
        """An explicit pipx package entry prevents a duplicate synthesized action."""
        manifest = SetupManifest.model_validate({
            'packages': {'python': ['pipx']},
            'tools': {'python': ['pdm']},
        })
        environments: dict[str, Environment] = {
            'pipx': _make_pipx(available=False),
            'pip': _make_pip(available=True),
        }

        actions = build_actions(manifest, environments)

        package_actions = [
            a for a in actions if a.kind == PluginKind.PACKAGE and a.package is not None and a.package.name == 'pipx'
        ]
        assert len(package_actions) == 1

    @staticmethod
    def test_missing_bootstrap_installer_plugin_skips_synthesis() -> None:
        """When the bootstrap installer ('pip') isn't discovered, synthesis is skipped."""
        manifest = SetupManifest.model_validate({'tools': {'python': ['pdm']}})
        environments: dict[str, Environment] = {
            'pipx': _make_pipx(available=False),
        }

        actions = build_actions(manifest, environments)

        assert all(a.kind != PluginKind.PACKAGE for a in actions)
