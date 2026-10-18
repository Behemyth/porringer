"""Inspection honours per-action runtime tags."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from porringer.backend.command.core import resolution
from porringer.backend.command.core.presence import inspect_action
from porringer.backend.command.core.resolution import ResolutionContext
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Package, PluginKind
from porringer.schema import InspectionMode, SetupParameters
from tests.fixtures.factories import make_environment, setup_action

_PARAMS = SetupParameters(inspection_mode=InspectionMode.COMPLETE)
_DEFAULT = RuntimeContext(executables={'python': Path('default-python')})
_TAGGED = RuntimeContext(executables={'python': Path('python3.12')})


async def test_tagged_action_queries_tagged_interpreter() -> None:
    """Presence is checked against the tagged interpreter, not the default one."""
    env = make_environment(installed=[Package(name='requests', version='2.0')])
    action = setup_action('requests', kind=PluginKind.PACKAGE, installer='pip', runtime_tag='3.12')

    with patch.object(resolution, 'resolve_runtime_tag_override', AsyncMock(return_value=_TAGGED)):
        result = await inspect_action(
            action,
            {'pip': env},
            context=ResolutionContext(runtime_context=_DEFAULT),
            parameters=_PARAMS,
        )

    assert result.success
    assert env.packages.await_args is not None
    assert env.packages.await_args.kwargs['runtime_context'] == _TAGGED


async def test_unresolvable_tag_fails_like_execution() -> None:
    """An unresolvable tag is reported as a failure without querying packages."""
    env = make_environment()
    action = setup_action('requests', kind=PluginKind.PACKAGE, installer='pip', runtime_tag='2.7')

    with patch.object(resolution, 'resolve_runtime_tag_override', AsyncMock(return_value=None)):
        result = await inspect_action(action, {'pip': env}, context=ResolutionContext(), parameters=_PARAMS)

    assert not result.success
    assert "runtime tag '2.7'" in (result.message or '')
    env.packages.assert_not_awaited()
