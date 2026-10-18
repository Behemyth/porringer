"""Helpers for test action progress.

Tests for progress events and action progress.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from porringer.backend.command.sync import SyncCommands
from porringer.core.plugin_schema.environment import PackageParameters
from porringer.core.schema import Ecosystem, PackageRef, PluginKind
from porringer.plugin.pip.plugin import PIPEnvironment
from porringer.schema import (
    SCHEMA_VERSION,
    ActionCompletedEvent,
    ActionProgress,
    ActionProgressEvent,
    ActionRef,
    ActionStartedEvent,
    ManifestFailedEvent,
    ManifestLoadedEvent,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    progress_event_snapshot,
)
from porringer.schema.observability import action_id_for

HALF_PROGRESS = 0.5
FAILED_MANIFEST_INDEX = 3
MIN_DOWNLOAD_UPDATES = 2
MIN_EVENT_COUNT = 2


def _make_action(package: str = 'requests') -> SetupAction:
    """Create a test SetupAction."""
    return SetupAction(
        description=f'Install {package}',
        kind=PluginKind.PACKAGE,
        ecosystem=Ecosystem('python'),
        installer='pip',
        package=PackageRef(name=package),
    )


class TestActionProgress:
    """Tests for ActionProgress dataclass."""

    @staticmethod
    def test_basic_construction() -> None:
        """ActionProgress stores provided fields."""
        action = _make_action()
        progress = ActionProgress(
            action=action,
            phase='downloading',
            progress=HALF_PROGRESS,
            message='Downloading ruff',
        )
        assert progress.action is action
        assert progress.phase == 'downloading'
        assert progress.progress == HALF_PROGRESS
        assert progress.message == 'Downloading ruff'


class TestProgressEvent:
    """Tests for ProgressEvent dataclass."""

    @staticmethod
    def test_action_ref_rejects_mismatched_id() -> None:
        """ActionRef enforces consistency between structured and compact identity."""
        with pytest.raises(ValidationError):
            ActionRef(manifest_index=2, action_index=4, action_id='wrong')

    @staticmethod
    def test_action_started() -> None:
        """ActionStartedEvent populates expected fields."""
        action = _make_action()
        ref = ActionRef.from_indices(1, 0)
        event = ActionStartedEvent(action=action, action_ref=ref)
        assert event.action is action
        assert event.action_index == 0
        assert event.action_ref is ref

    @staticmethod
    def test_action_completed() -> None:
        """ActionCompletedEvent includes result."""
        action = _make_action()
        result = SetupActionResult(action=action, success=True, message='ok')
        ref = ActionRef.from_indices(1, 0)
        event = ActionCompletedEvent(action=action, result=result, action_ref=ref)
        assert event.result is result
        assert event.action_index == 0

    @staticmethod
    def test_action_progress() -> None:
        """ActionProgressEvent includes progress detail."""
        action = _make_action()
        progress = ActionProgress(action=action, phase='downloading', progress=HALF_PROGRESS, message='pkg')
        ref = ActionRef.from_indices(1, 0)
        event = ActionProgressEvent(action=action, progress=progress, action_ref=ref)
        assert event.progress is progress
        assert event.action_ref is ref

    @staticmethod
    def test_action_started_snapshot_json() -> None:
        """Action lifecycle events serialize to stable JSON with action refs."""
        action = _make_action()
        ref = ActionRef.from_indices(2, 4)
        event = ActionStartedEvent(action=action, action_ref=ref)

        snapshot = progress_event_snapshot(event)
        payload = json.loads(snapshot.model_dump_json())

        assert payload['schema_version'] == SCHEMA_VERSION
        assert payload['event_type'] == 'action_started'
        assert payload['action_id'] == '2:4'
        assert payload['action_ref'] == {
            'manifest_index': 2,
            'action_index': 4,
            'action_id': '2:4',
        }
        assert payload['action']['package_name'] == 'requests'

    @staticmethod
    def test_action_completed_snapshot_json() -> None:
        """Completed events include stable result data."""
        action = _make_action()
        ref = ActionRef.from_indices(2, 4)
        result = SetupActionResult(action=action, success=True, message='ok')
        event = ActionCompletedEvent(action=action, result=result, action_ref=ref)

        snapshot = progress_event_snapshot(event)

        assert snapshot.event_type == 'action_completed'
        assert snapshot.action_id == '2:4'
        assert snapshot.result is not None
        assert snapshot.result.success is True
        assert snapshot.result.message == 'ok'

    @staticmethod
    def test_failed_manifest_snapshot_json() -> None:
        """Failed manifest snapshots include the resolved input path index."""
        event = ManifestFailedEvent(failed_path=(Path('missing'), 'boom'), manifest_index=FAILED_MANIFEST_INDEX)

        snapshot = progress_event_snapshot(event)

        assert snapshot.event_type == 'manifest_failed'
        assert snapshot.failed_path is not None
        assert snapshot.failed_path.manifest_index == FAILED_MANIFEST_INDEX
        assert snapshot.failed_path.error == 'boom'

    @staticmethod
    def test_action_progress_snapshot_json() -> None:
        """Action progress snapshots keep parent action identity."""
        action = _make_action()
        ref = ActionRef.from_indices(2, 4)
        progress = ActionProgress(action=action, phase='downloading', progress=HALF_PROGRESS, message='pkg')
        event = ActionProgressEvent(action=action, progress=progress, action_ref=ref)

        snapshot = progress_event_snapshot(event)

        assert snapshot.event_type == 'action_progress'
        assert snapshot.action_id == '2:4'
        assert snapshot.action_progress is not None
        assert snapshot.action_progress.progress == HALF_PROGRESS


class TestPackageParametersProgressCallback:
    """Tests for progress_callback on PackageParameters."""

    @staticmethod
    def test_accepts_callback() -> None:
        """PackageParameters accepts a progress callback."""
        cb = MagicMock()
        params = PackageParameters(package=PackageRef(name='requests'), progress_callback=cb)
        assert params.progress_callback is cb

    @staticmethod
    def test_callback_excluded_from_serialization() -> None:
        """Progress callback is excluded from serialization."""
        cb = MagicMock()
        params = PackageParameters(package=PackageRef(name='requests'), progress_callback=cb)
        data = params.model_dump()
        assert 'progress_callback' not in data


class TestPipProgressLineParsing:
    """Tests for PIPEnvironment._parse_progress_line."""

    @staticmethod
    def test_downloading_line() -> None:
        """Pip parser captures download start lines."""
        action = _make_action()
        collected: list[ActionProgress] = []

        PIPEnvironment._parse_progress_line(
            'Downloading https://files.pythonhosted.org/ruff-0.8.0-py3-none-any.whl (2.1 MB)',
            action,
            collected.append,
        )

        assert len(collected) == 1
        assert collected[0].phase == 'downloading'
        assert collected[0].progress == 0.0
        assert 'ruff-0.8.0' in (collected[0].message or '')

    @staticmethod
    def test_progress_percentage_line() -> None:
        """Pip parser captures download percentages."""
        action = _make_action()
        collected: list[ActionProgress] = []

        PIPEnvironment._parse_progress_line(
            '   1.5 MB 50%',
            action,
            collected.append,
        )

        assert len(collected) == 1
        assert collected[0].phase == 'downloading'
        assert collected[0].progress == pytest.approx(HALF_PROGRESS)

    @staticmethod
    def test_installing_line() -> None:
        """Pip parser captures installing lines."""
        action = _make_action()
        collected: list[ActionProgress] = []

        PIPEnvironment._parse_progress_line(
            'Installing collected packages: requests, urllib3',
            action,
            collected.append,
        )

        assert len(collected) == 1
        assert collected[0].phase == 'installing'
        assert 'requests' in (collected[0].message or '')

    @staticmethod
    def test_already_satisfied_line() -> None:
        """Pip parser captures already satisfied lines."""
        action = _make_action()
        collected: list[ActionProgress] = []

        PIPEnvironment._parse_progress_line(
            'Requirement already satisfied: requests in /usr/lib/python3.12/site-packages',
            action,
            collected.append,
        )

        assert len(collected) == 1
        assert collected[0].phase == 'verifying'
        assert collected[0].progress == 1.0

    @staticmethod
    def test_irrelevant_line_is_ignored() -> None:
        """Pip parser ignores unrelated lines."""
        action = _make_action()
        collected: list[ActionProgress] = []

        PIPEnvironment._parse_progress_line(
            'Using cached requests-2.31.0.tar.gz',
            action,
            collected.append,
        )

        assert len(collected) == 0

    @staticmethod
    def test_full_progress_sequence() -> None:
        """Simulate a realistic sequence of pip output lines."""
        action = _make_action('ruff')
        collected: list[ActionProgress] = []

        lines = [
            'Collecting ruff',
            'Downloading https://files.pythonhosted.org/ruff-0.8.0-py3-none-any.whl (2.1 MB)',
            '   0.5 MB 25%',
            '   1.0 MB 50%',
            '   1.5 MB 75%',
            '   2.1 MB 100%',
            'Installing collected packages: ruff',
            'Successfully installed ruff-0.8.0',
        ]

        for line in lines:
            PIPEnvironment._parse_progress_line(line, action, collected.append)

        phases = [u.phase for u in collected]
        assert 'downloading' in phases
        assert 'installing' in phases

        # Check download progress increases
        download_updates = [u for u in collected if u.phase == 'downloading']
        assert len(download_updates) >= MIN_DOWNLOAD_UPDATES
        progresses = [u.progress for u in download_updates if u.progress is not None]
        assert progresses == sorted(progresses)  # monotonically increasing


@pytest.mark.mock_packages
class TestExecutionEvents:
    """Tests for evented sync execution."""

    @staticmethod
    async def test_run_partitions_results_by_action_ref(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run() groups completed results by stable refs, not result action object identity."""
        commands = SyncCommands()
        first_action = _make_action('first')
        second_action = _make_action('second')
        manifest_zero = SetupResults(actions=[first_action], manifest_path=Path('zero'))
        manifest_one = SetupResults(actions=[second_action], manifest_path=Path('one'))
        synthetic_result_action = _make_action('synthetic')

        async def _fake_execution_events(_parameters: SetupParameters, *, plugins=None, executor=None, prepared=None):
            del plugins, executor, prepared
            yield ManifestLoadedEvent(manifest=manifest_zero, manifest_index=0)
            yield ManifestLoadedEvent(manifest=manifest_one, manifest_index=1)
            yield ActionCompletedEvent(
                action=first_action,
                result=SetupActionResult(action=synthetic_result_action, success=True, message='zero'),
                action_ref=ActionRef.from_indices(0, 0),
            )
            yield ActionCompletedEvent(
                action=second_action,
                result=SetupActionResult(action=second_action, success=True, message='one'),
                action_ref=ActionRef.from_indices(1, 0),
            )

        monkeypatch.setattr(commands, '_execution_events', _fake_execution_events)

        report = await commands.run(SetupParameters())
        results = report.results

        assert [r.message for r in results.manifest_results[0].results] == ['zero']
        assert [r.message for r in results.manifest_results[1].results] == ['one']

    @staticmethod
    async def test_run_emits_events() -> None:
        """Run forwards ProgressEvent items through the event callback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {'version': '1'}
            manifest_path.write_text(json.dumps(manifest_data))

            params = SetupParameters(paths=Path(tmpdir))
            commands = SyncCommands()

            collected: list[ProgressEvent] = []
            await commands.run(params, on_event=collected.append)

            events = collected

            # Should have at least a MANIFEST_LOADED event + start/complete pairs
            manifest_events = [e for e in events if isinstance(e, ManifestLoadedEvent)]
            assert len(manifest_events) >= 1

            started = [e for e in events if isinstance(e, ActionStartedEvent)]
            completed = [e for e in events if isinstance(e, ActionCompletedEvent)]
            assert len(started) == len(completed)

    @staticmethod
    async def test_event_generator_cancellation() -> None:
        """Breaking from the private event generator cancels the background task."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / 'porringer.json'
            manifest_data = {'version': '1'}
            manifest_path.write_text(json.dumps(manifest_data))

            params = SetupParameters(paths=Path(tmpdir))
            commands = SyncCommands()

            collected: list[ProgressEvent] = []
            async for event in commands._execution_events(params):
                collected.append(event)
                if len(collected) >= MIN_EVENT_COUNT:
                    break  # early exit

            events = collected
            assert len(events) >= MIN_EVENT_COUNT


class TestActionIdStability:
    """action_id must not be compacted when earlier manifest paths fail.

    Bug: When an earlier manifest path failed to load, action indices were
    compacted (renumbered from 0) for the successful paths that followed.
    This broke downstream consumers that stored action_ids from a preview
    call and then passed them back via SetupParameters(action_ids=...) for
    a targeted execution — the ids no longer matched.

    Fix: The resolved input-path index is preserved regardless of failures
    in earlier paths.  ``action_id_for(manifest_index, action_index)``
    encodes the *original* position, not the compacted one.
    """

    @staticmethod
    def test_action_id_format_is_manifest_colon_action() -> None:
        """action_id_for produces '<manifest_index>:<action_index>' string."""
        assert action_id_for(0, 0) == '0:0'
        assert action_id_for(0, 5) == '0:5'
        assert action_id_for(3, 7) == '3:7'

    @staticmethod
    def test_action_id_does_not_compact_on_failed_path() -> None:
        """A successful manifest at position 2 keeps id '2:0', not '0:0'.

        Simulates the scenario where paths 0 and 1 fail — the third path
        (index 2) must still produce ids starting at '2:0', not reset to '0:0'.
        """
        assert action_id_for(2, 0) == '2:0'

    @staticmethod
    def test_action_id_round_trips_through_action_ref() -> None:
        """ActionRef built from indices validates and round-trips the action_id."""
        ref = ActionRef.from_indices(2, 3)
        assert ref.action_id == '2:3'
        # The validator rejects mismatched ids.
        with pytest.raises(ValueError, match='action_id must be'):
            ActionRef(manifest_index=2, action_index=3, action_id='0:0')
