"""Helpers for test example bootstrap.

Bootstrap example tests.

Validates that the `examples/python-bootstrap/porringer.json` manifest
produces the correct phased action plan, including deferred tool/runtime
resolution and implicit project sync.

Runtime and tool actions may have `installer=None` (deferred) when
the backing CLI tool is not on PATH — this is expected and correct.
"""

from pathlib import Path

import pytest

from porringer.backend.command.core.action_builder import parse_manifest
from porringer.core.schema import PluginKind
from porringer.schema import SetupResults

# Absolute path to the bootstrap example manifest directory
_BOOTSTRAP_DIR = Path(__file__).resolve().parents[2] / 'examples' / 'python-bootstrap'


class TestBootstrapPreview:
    """Preview the bootstrap manifest and verify its action plan."""

    @staticmethod
    @pytest.fixture(scope='class')
    def preview() -> SetupResults:
        """Parse the bootstrap manifest.

        Class-scoped: the manifest is parsed once and shared across
        every test in this class (all tests are read-only).
        """
        return parse_manifest(_BOOTSTRAP_DIR)

    @staticmethod
    def test_manifest_loads(preview: SetupResults) -> None:
        """The bootstrap manifest should load without errors."""
        assert preview is not None
        assert len(preview.actions) > 0

    @staticmethod
    def test_runtime_action_present(preview: SetupResults) -> None:
        """A RUNTIME action for Python 3.15 should be in the plan.

        The action may have `installer=None` (deferred) when no
        runtime provider (pim/pyenv) is available on the current
        platform.
        """
        runtime_actions = [a for a in preview.actions if a.kind == PluginKind.RUNTIME and a.ecosystem == 'python']
        assert len(runtime_actions) == 1
        assert runtime_actions[0].package is not None
        assert runtime_actions[0].package.name == '3.15'

    @staticmethod
    def test_package_actions_present(preview: SetupResults) -> None:
        """No manifest-declared PACKAGE entries remain.

        pipx bootstrap (installing pipx via pip) is only synthesized when
        pdm's TOOL action is deferred (pipx not yet available). When pipx
        is already on PATH, no PACKAGE action is needed at all — this
        keeps the test correct across both bootstrap and already-set-up
        environments.
        """
        tool_actions = [a for a in preview.actions if a.kind == PluginKind.TOOL and a.ecosystem == 'python']
        package_actions = [a for a in preview.actions if a.kind == PluginKind.PACKAGE and a.ecosystem == 'python']
        assert len(tool_actions) == 1

        if tool_actions[0].installer is None:
            assert len(package_actions) == 1
            assert package_actions[0].package is not None
            assert package_actions[0].package.name == 'pipx'
        else:
            assert len(package_actions) == 0

    @staticmethod
    def test_tool_actions_present(preview: SetupResults) -> None:
        """A TOOL action for pdm should be in the plan.

        The tool action may have `installer=None` (deferred) if pipx
        is not currently available — this is expected and correct.
        """
        tool_actions = [a for a in preview.actions if a.kind == PluginKind.TOOL and a.ecosystem == 'python']
        assert len(tool_actions) == 1
        assert tool_actions[0].package is not None
        assert tool_actions[0].package.name == 'pdm'

    @staticmethod
    def test_project_sync_action_present(preview: SetupResults) -> None:
        """A PROJECT action for PDM project sync should be in the plan."""
        project_actions = [a for a in preview.actions if a.kind == PluginKind.PROJECT and a.ecosystem == 'python']
        assert len(project_actions) == 1
        assert project_actions[0].installer in {'pdm', None}

    @staticmethod
    def test_scm_action_present(preview: SetupResults) -> None:
        """An SCM action for cloning the porringer repo should be in the plan."""
        scm_actions = [a for a in preview.actions if a.kind == PluginKind.SCM and a.ecosystem == 'git']
        assert len(scm_actions) == 1
        assert scm_actions[0].package is not None
        assert scm_actions[0].package.name == 'https://github.com/synodic/porringer'
        assert scm_actions[0].kind == PluginKind.SCM

    @staticmethod
    def test_all_action_phases_present(preview: SetupResults) -> None:
        """The plan should contain the runtime, tool, project, and scm phases.

        PACKAGE is not asserted unconditionally: it only appears when the
        pipx bootstrap (installing pipx via pip) is synthesized, which
        only happens when pipx itself is not yet available on this host.
        """
        kinds = []
        for a in preview.actions:
            if a.kind == PluginKind.RUNTIME:
                kinds.append('runtime')
            elif a.kind == PluginKind.PACKAGE:
                kinds.append('package')
            elif a.kind == PluginKind.TOOL:
                kinds.append('tool')
            elif a.kind == PluginKind.PROJECT:
                kinds.append('project')
            elif a.kind == PluginKind.SCM:
                kinds.append('scm')
            else:
                kinds.append('other')

        assert 'runtime' in kinds
        assert 'tool' in kinds
        assert 'project' in kinds
        assert 'scm' in kinds
