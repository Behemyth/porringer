"""Helpers for test scm from url.

Tests for synthesizing a git-clone SCM action from ``manifest.url``
when no explicit ``scm`` section is declared.
"""

from typing import override

from packaging.version import Version

from porringer.backend.command.core.action_builder import build_actions
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.core.schema import Distribution, PluginKind, PluginParameters
from porringer.schema import SetupManifest
from porringer.test.mock.scm import MockScm

_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.0')))


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


def _plugins_with_git() -> DiscoveredPlugins:
    return DiscoveredPlugins(environments={}, project_environments={}, scm_environments={'git': _AvailableGit(_PARAMS)})


class TestScmFromUrl:
    """Verify default-scm-from-url inference."""

    @staticmethod
    def test_synthesizes_clone_from_github_url() -> None:
        """A manifest with only a repo-shaped github.com url gets a clone action."""
        manifest = SetupManifest(url='https://github.com/synodic/periapsis')

        actions = build_actions(manifest, _plugins_with_git())

        scm_actions = [a for a in actions if a.kind == PluginKind.SCM]
        assert len(scm_actions) == 1
        assert scm_actions[0].package is not None
        assert scm_actions[0].package.name == 'https://github.com/synodic/periapsis'
        assert scm_actions[0].installer == 'git'

    @staticmethod
    def test_homepage_style_url_not_cloned() -> None:
        """A non-repo-shaped url (e.g. a docs/homepage page) is not synthesized."""
        manifest = SetupManifest(url='https://synodic.github.io/porringer')

        actions = build_actions(manifest, _plugins_with_git())

        assert all(a.kind != PluginKind.SCM for a in actions)

    @staticmethod
    def test_unknown_forge_not_cloned() -> None:
        """A repo-shaped path on an unrecognised host is not synthesized."""
        manifest = SetupManifest(url='https://example.com/synodic/periapsis')

        actions = build_actions(manifest, _plugins_with_git())

        assert all(a.kind != PluginKind.SCM for a in actions)

    @staticmethod
    def test_explicit_scm_entry_prevents_duplicate() -> None:
        """An explicit scm section wins; no duplicate clone is synthesized."""
        manifest = SetupManifest.model_validate({
            'url': 'https://github.com/synodic/periapsis',
            'scm': {'git': ['https://github.com/synodic/periapsis']},
        })

        actions = build_actions(manifest, _plugins_with_git())

        scm_actions = [a for a in actions if a.kind == PluginKind.SCM]
        assert len(scm_actions) == 1

    @staticmethod
    def test_no_url_no_synthesis() -> None:
        """A manifest with neither scm nor url produces no SCM action."""
        manifest = SetupManifest()

        actions = build_actions(manifest, _plugins_with_git())

        assert all(a.kind != PluginKind.SCM for a in actions)
