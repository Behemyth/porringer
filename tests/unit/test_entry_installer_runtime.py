"""Tests for per-entry ``installer`` and ``runtime`` manifest fields."""

import pytest
from pydantic import ValidationError

from porringer.backend.command.core.action_builder import build_actions
from porringer.core.plugin_schema.environment import Environment
from porringer.core.schema import PluginKind
from porringer.schema import SetupManifest
from porringer.utility.exception import ManifestError, ManifestValidationCode
from tests.unit.test_bootstrap_actions import _make_pip, _make_pipx, _make_uv_tool


def _tool_actions(manifest: dict, environments: dict[str, Environment]) -> list:
    actions = build_actions(SetupManifest.model_validate(manifest), environments)
    return [action for action in actions if action.kind == PluginKind.TOOL]


def test_entry_installer_overrides_section_choice() -> None:
    """An entry's installer is used even when another candidate would be chosen."""
    environments: dict[str, Environment] = {
        'pipx': _make_pipx(available=True),
        'uv': _make_uv_tool(available=True),
    }

    tools = _tool_actions(
        {'tools': {'python': [{'name': 'pdm', 'installer': 'uv'}, 'ruff']}},
        environments,
    )

    assert [(str(action.package), action.installer) for action in tools] == [
        ('pdm', 'uv'),
        ('ruff', 'pipx'),
    ]


def test_entry_runtime_becomes_runtime_tag() -> None:
    """``runtime`` maps onto the action's runtime tag."""
    environments: dict[str, Environment] = {'pipx': _make_pipx(available=True)}

    tools = _tool_actions(
        {'tools': {'python': [{'name': 'pdm', 'runtime': '3.14'}, 'ruff']}},
        environments,
    )

    assert [action.runtime_tag for action in tools] == ['3.14', None]


def test_installer_from_another_kind_is_rejected() -> None:
    """A package installer can't be pinned on a tool entry."""
    environments: dict[str, Environment] = {
        'pipx': _make_pipx(available=True),
        'pip': _make_pip(available=True),
    }

    with pytest.raises(ManifestError, match="installer 'pip' does not install tools for 'python'") as caught:
        _tool_actions({'tools': {'python': [{'name': 'pdm', 'installer': 'pip'}]}}, environments)

    assert caught.value.code == ManifestValidationCode.UNKNOWN_PLUGIN


def test_unavailable_pinned_installer_is_kept_and_bootstrapped() -> None:
    """A pinned installer that isn't installed yet stays pinned and gets its prerequisite installed."""
    environments: dict[str, Environment] = {
        'pipx': _make_pipx(available=False),
        'uv': _make_uv_tool(available=True),
        'pip': _make_pip(available=True),
    }

    actions = build_actions(
        SetupManifest.model_validate({'tools': {'python': [{'name': 'pdm', 'installer': 'pipx'}]}}),
        environments,
    )

    tool = next(action for action in actions if action.kind == PluginKind.TOOL)
    bootstrap = [action for action in actions if action.kind == PluginKind.PACKAGE]
    assert tool.installer == 'pipx'
    assert [(str(action.package), action.installer) for action in bootstrap] == [('pipx', 'pip')]


@pytest.mark.parametrize('section', ['runtimes', 'scm'])
def test_runtime_is_rejected_outside_packages_and_tools(section: str) -> None:
    """``runtime`` has no meaning on runtime or SCM entries."""
    with pytest.raises(ValidationError, match="'runtime' applies only to packages and tools"):
        SetupManifest.model_validate({section: {'python': [{'name': 'x', 'runtime': '3.14'}]}})
