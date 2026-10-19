"""Helpers for test cli.

Test the click cli.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from dirty_equals import IsPartialDict, IsStr
from packaging.version import Version
from porringer.console.command import sync as sync_command
from porringer.console.entry import app
from porringer.console.schema import ConsoleConfiguration
from porringer.core.schema import PluginKind
from porringer.schema import (
    ActionCompletedEvent,
    ActionProgress,
    ActionProgressEvent,
    ActionRef,
    ActionStartedEvent,
    BatchSetupResults,
    CheckResult,
    PackageUpdateInfo,
    PluginInfo,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SkipReason,
)
from rich.console import Console
from typer.testing import CliRunner

# A minimal fake result so that ``plugin list`` never spawns subprocesses.
_FAKE_PLUGINS = [
    PluginInfo(
        name="stub",
        kind=PluginKind.PACKAGE,
        version=Version("0.1.0"),
        installed=True,
        tool_version=None,
    )
]
EXPECTED_DUPLICATE_ACTIONS = 2


class _FakeConsole:
    """Tiny stand-in for a Rich Console that records printed renderables."""

    def __init__(self) -> None:
        """Initialize captured print calls."""
        self.printed: list[str] = []

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Capture printed text (rendered to its plain string form)."""
        self.printed.append(" ".join(str(a) for a in args))


class _FakeProgress:
    """Tiny stand-in for Rich Progress used by progress tracker tests."""

    def __init__(self) -> None:
        """Initialize captured task state."""
        self.tasks: list[dict[str, Any]] = []
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.console = _FakeConsole()

    def add_task(self, description: str, *, total: float | int) -> int:
        """Capture added tasks and return a stable integer task id."""
        task_id = len(self.tasks)
        self.tasks.append({"description": description, "total": total})
        return task_id

    def update(self, task_id: int, **kwargs: Any) -> None:
        """Capture task updates."""
        self.updates.append((task_id, kwargs))


@pytest.fixture(autouse=True)
def _reset_logger():
    """Ensure CLI tests do not leak logger handlers or propagation state."""
    logger = logging.getLogger("porringer")
    old_propagate = logger.propagate
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    yield
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = old_propagate


class TestCLI:
    """Tests for the typer CLI."""

    @staticmethod
    def test_version(test_config) -> None:
        """Verifies the version command works."""
        runner = CliRunner()
        result = runner.invoke(app, ["--version"], obj=test_config)

        assert result.exit_code == 0

    @staticmethod
    def test_doc_browser_import_is_lazy() -> None:
        """The optional browser module loads only when first used in a fresh process."""
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import porringer.console.command.doc as doc; "
                'assert "webbrowser" not in sys.modules; '
                "assert callable(doc.webbrowser.open); "
                'assert "webbrowser" in sys.modules',
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    @pytest.mark.parametrize("opened", [True, False])
    def test_doc_open_browser(test_config, opened: bool) -> None:
        """The optional browser command preserves success and failure reporting."""
        with patch(
            "porringer.console.command.doc.webbrowser.open", return_value=opened
        ) as browser_open:
            result = CliRunner().invoke(app, ["doc", "--open"], obj=test_config)

        browser_open.assert_called_once_with("https://synodic.github.io/porringer/")
        assert result.exit_code == (0 if opened else 1)

    @staticmethod
    def test_plugin_list_shows_output(test_config) -> None:
        """``plugin list`` prints discovered plugins by default."""
        runner = CliRunner()
        with patch(
            "porringer.console.command.plugin.PluginCommands.list",
            new=AsyncMock(return_value=_FAKE_PLUGINS),
        ):
            result = runner.invoke(app, ["plugin", "list"], obj=test_config)

        assert result.exit_code == 0
        assert "stub" in result.output

    @staticmethod
    def test_plugin_list_json(test_config) -> None:
        """``plugin list --json`` emits each plugin as structured data."""
        with patch(
            "porringer.console.command.plugin.PluginCommands.list",
            new=AsyncMock(return_value=_FAKE_PLUGINS),
        ):
            result = CliRunner().invoke(
                app, ["plugin", "list", "--json"], obj=test_config
            )

        assert result.exit_code == 0
        plugins = json.loads(result.output)
        assert [plugin["name"] for plugin in plugins] == [
            plugin.name for plugin in _FAKE_PLUGINS
        ]
        assert plugins[0]["version"] == str(_FAKE_PLUGINS[0].version)

    @staticmethod
    def test_check_json(test_config) -> None:
        """``check --json`` emits each plugin's update results."""
        results = [
            CheckResult(
                plugin="pip",
                packages=[
                    PackageUpdateInfo(
                        "ruff", Version("0.1"), Version("0.2"), update_available=True
                    )
                ],
            )
        ]
        with patch(
            "porringer.console.command.check.PackageCommands.check_updates",
            new=AsyncMock(return_value=results),
        ):
            result = CliRunner().invoke(app, ["check", "--json"], obj=test_config)

        assert result.exit_code == 0
        assert json.loads(result.output) == [
            {
                "plugin": "pip",
                "packages": [
                    {
                        "name": "ruff",
                        "current_version": "0.1",
                        "latest_version": "0.2",
                        "update_available": True,
                    }
                ],
                "error": None,
            }
        ]
        """``--quiet`` suppresses informational plugin listing output."""
        runner = CliRunner()
        with patch(
            "porringer.console.command.plugin.PluginCommands.list",
            new=AsyncMock(return_value=_FAKE_PLUGINS),
        ):
            result = runner.invoke(app, ["--quiet", "plugin", "list"], obj=test_config)

        assert result.exit_code == 0
        assert "stub" not in result.output

    @staticmethod
    def test_no_color_flag_is_accepted(test_config) -> None:
        """``--no-color`` runs without error and still produces output."""
        runner = CliRunner()
        with patch(
            "porringer.console.command.plugin.PluginCommands.list",
            new=AsyncMock(return_value=_FAKE_PLUGINS),
        ):
            result = runner.invoke(
                app, ["--no-color", "plugin", "list"], obj=test_config
            )

        assert result.exit_code == 0
        assert "stub" in result.output

    @staticmethod
    def test_env_info_json_without_plugins(
        monkeypatch: pytest.MonkeyPatch, test_config
    ) -> None:
        """``env info --json --no-plugins`` emits diagnostics without plugin discovery."""
        monkeypatch.setenv(
            "PIPX_HOME", str(test_config.local_configuration.cache_directory / "pipx")
        )
        runner = CliRunner()

        with patch("porringer.console.command.env.ensure_system_path") as sync_path:
            result = runner.invoke(
                app, ["env", "info", "--json", "--no-plugins"], obj=test_config
            )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["system"]["porringer_version"]
        assert payload["system"]["python_executable"]
        assert payload["directories"]["cache"] == str(
            test_config.local_configuration.cache_directory
        )
        assert payload["environment"]["PIPX_HOME"] == str(
            test_config.local_configuration.cache_directory / "pipx"
        )
        assert payload["plugins"] == []
        sync_path.assert_called_once()
        assert payload == IsPartialDict(
            directories=IsPartialDict(
                cache=str(test_config.local_configuration.cache_directory)
            ),
            environment=IsPartialDict(
                PIPX_HOME=str(test_config.local_configuration.cache_directory / "pipx")
            ),
            path=IsPartialDict(added_entries=[]),
            plugins=[],
            runtime_context={},
            system=IsPartialDict(
                frozen=False,
                porringer_version=IsStr(min_length=1),
                python_executable=IsStr(min_length=1),
                system=IsStr(min_length=1),
            ),
        )
        assert payload["path"]["before"] == payload["path"]["after"]


class TestSyncProgressTracker:
    """Tests for sync CLI progress bookkeeping."""

    @staticmethod
    def test_duplicate_action_labels_use_action_refs() -> None:
        """Actions with the same display label do not collide in progress state."""
        progress = _FakeProgress()
        state = sync_command._ProgressState(total_actions=EXPECTED_DUPLICATE_ACTIONS)
        tracker = sync_command._ProgressTracker(
            progress=cast(Any, progress),
            setup_params=SetupParameters(),
            state=state,
        )
        first_ref = ActionRef.from_indices(0, 0)
        second_ref = ActionRef.from_indices(0, 1)
        first_action = SetupAction(description="same label")
        second_action = SetupAction(description="same label")

        tracker.handle_progress_event(
            ActionStartedEvent(action=first_action, action_ref=first_ref)
        )
        tracker.handle_progress_event(
            ActionStartedEvent(action=second_action, action_ref=second_ref)
        )

        assert set(state.active_tasks) == {"0:0", "0:1"}
        assert len(progress.tasks) == EXPECTED_DUPLICATE_ACTIONS

        tracker.handle_progress_event(
            ActionCompletedEvent(
                action=first_action,
                result=SetupActionResult(action=first_action, success=True),
                action_ref=first_ref,
            )
        )
        tracker.handle_progress_event(
            ActionCompletedEvent(
                action=second_action,
                result=SetupActionResult(action=second_action, success=True),
                action_ref=second_ref,
            )
        )

        assert state.active_tasks == {}
        assert state.completed == EXPECTED_DUPLICATE_ACTIONS


class TestSyncProgressOutputTail:
    """Tests for surfacing buffered subprocess output when an action fails."""

    @staticmethod
    def test_failed_action_prints_buffered_output_tail() -> None:
        """Streamed output lines are printed under a failed action."""
        progress = _FakeProgress()
        state = sync_command._ProgressState(total_actions=1)
        tracker = sync_command._ProgressTracker(
            progress=cast(Any, progress),
            setup_params=SetupParameters(),
            state=state,
        )
        ref = ActionRef.from_indices(0, 0)
        action = SetupAction(description="pdm install")

        tracker.handle_progress_event(ActionStartedEvent(action=action, action_ref=ref))
        tracker.handle_progress_event(
            ActionProgressEvent(
                action=action,
                action_ref=ref,
                progress=ActionProgress(
                    action=action,
                    phase="install",
                    output="Lockfile hash mismatch",
                    channel="stderr",
                ),
            )
        )
        tracker.handle_progress_event(
            ActionProgressEvent(
                action=action,
                action_ref=ref,
                progress=ActionProgress(
                    action=action,
                    phase="install",
                    output="run pdm lock",
                    channel="stderr",
                ),
            )
        )
        tracker.handle_progress_event(
            ActionCompletedEvent(
                action=action,
                result=SetupActionResult(
                    action=action,
                    success=False,
                    message="Project install failed (exit 1)",
                ),
                action_ref=ref,
            )
        )

        printed = "\n".join(progress.console.printed)
        assert "Lockfile hash mismatch" in printed
        assert "run pdm lock" in printed
        assert ref.action_id not in state.output_tails

    @staticmethod
    def test_successful_action_does_not_print_output_tail() -> None:
        """Buffered output is discarded (not printed) when the action succeeds."""
        progress = _FakeProgress()
        state = sync_command._ProgressState(total_actions=1)
        tracker = sync_command._ProgressTracker(
            progress=cast(Any, progress),
            setup_params=SetupParameters(),
            state=state,
        )
        ref = ActionRef.from_indices(0, 0)
        action = SetupAction(description="pdm install")

        tracker.handle_progress_event(ActionStartedEvent(action=action, action_ref=ref))
        tracker.handle_progress_event(
            ActionProgressEvent(
                action=action,
                action_ref=ref,
                progress=ActionProgress(
                    action=action,
                    phase="install",
                    output="Resolving dependencies...",
                    channel="stdout",
                ),
            )
        )
        tracker.handle_progress_event(
            ActionCompletedEvent(
                action=action,
                result=SetupActionResult(action=action, success=True),
                action_ref=ref,
            )
        )

        assert progress.console.printed == []
        assert state.output_tails == {}

    @staticmethod
    def test_active_action_shows_latest_output_and_elapsed_time() -> None:
        """Active actions show bounded output context and elapsed time."""
        progress = _FakeProgress()
        state = sync_command._ProgressState(total_actions=1)
        tracker = sync_command._ProgressTracker(
            progress=cast(Any, progress),
            setup_params=SetupParameters(),
            state=state,
        )
        ref = ActionRef.from_indices(0, 0)
        action = SetupAction(description="pdm install")

        with patch(
            "porringer.console.command.sync.time.monotonic", side_effect=[100.0, 107.0]
        ):
            tracker.handle_progress_event(
                ActionStartedEvent(action=action, action_ref=ref)
            )
            tracker.handle_progress_event(
                ActionProgressEvent(
                    action=action,
                    action_ref=ref,
                    progress=ActionProgress(
                        action=action, phase="project", output="Resolving dependencies"
                    ),
                )
            )

        updated_description = progress.updates[-1][1]["description"]
        assert "7s" in updated_description
        assert "Resolving dependencies" in updated_description

    @staticmethod
    def test_verbose_action_streams_raw_output_with_channel() -> None:
        """Verbose mode prints each subprocess line with its channel."""
        progress = _FakeProgress()
        state = sync_command._ProgressState(total_actions=1, verbose_output=True)
        tracker = sync_command._ProgressTracker(
            progress=cast(Any, progress),
            setup_params=SetupParameters(),
            state=state,
        )
        ref = ActionRef.from_indices(0, 0)
        action = SetupAction(description="pdm install")

        tracker.handle_progress_event(ActionStartedEvent(action=action, action_ref=ref))
        tracker.handle_progress_event(
            ActionProgressEvent(
                action=action,
                action_ref=ref,
                progress=ActionProgress(
                    action=action,
                    phase="project",
                    output="Installing package [1/2]",
                    channel="stdout",
                ),
            )
        )

        printed = "\n".join(progress.console.printed)
        assert "pdm install [stdout]" in printed
        assert "Installing package [1/2]" in printed


class TestSyncFinalOutput:
    """Tests for concise post-execution output."""

    @staticmethod
    def test_final_output_shows_duration_and_collapses_skips() -> None:
        """Executed actions show duration while skipped actions become a count."""
        configuration = ConsoleConfiguration(console=Console(record=True))
        executed = SetupAction(description="pdm install")
        skipped = SetupAction(description="Install ruff")
        results = BatchSetupResults(
            manifest_results=[
                SetupResults(
                    manifest_path=Path("porringer.json"),
                    results=[
                        SetupActionResult(
                            action=executed, success=True, duration_seconds=12.3
                        ),
                        SetupActionResult(
                            action=skipped,
                            success=True,
                            skipped=True,
                            skip_reason=SkipReason.ALREADY_INSTALLED,
                        ),
                    ],
                )
            ]
        )

        sync_command._display_results(configuration, results)

        output = configuration.console.export_text()
        assert "pdm install" in output
        assert "(12.3s)" in output
        assert "1 action(s) skipped" in output
        assert "Install ruff" not in output

    @staticmethod
    def test_final_output_identifies_failed_command_step() -> None:
        """Failed multi-step actions name the command step that failed."""
        configuration = ConsoleConfiguration(console=Console(record=True))
        action = SetupAction(description="poetry install")
        results = BatchSetupResults(
            manifest_results=[
                SetupResults(
                    manifest_path=Path("porringer.json"),
                    results=[
                        SetupActionResult(
                            action=action,
                            success=False,
                            message="Project install failed via poetry (exit 1)",
                            duration_seconds=3.2,
                            cli_command=("poetry", "install"),
                            cli_steps=(
                                ("poetry", "env", "use", "python"),
                                ("poetry", "install"),
                            ),
                            failed_step_index=2,
                        )
                    ],
                )
            ]
        )

        sync_command._display_results(configuration, results)

        output = configuration.console.export_text()
        assert "[2 steps]" in output
        assert "Failed step 2: poetry install" in output
        assert "Project install failed via poetry (exit 1)" in output


class TestCLILoggingLevels:
    """Verify --verbose and --debug flags wire to logger.setLevel."""

    @pytest.fixture(autouse=True)
    @staticmethod
    def _mock_plugin_list():
        """Bypass real plugin discovery so the tests run in milliseconds."""
        with patch(
            "porringer.backend.command.plugin.PluginCommands.list",
            new_callable=AsyncMock,
        ) as plugin_list:
            plugin_list.return_value = _FAKE_PLUGINS
            yield

    @staticmethod
    def test_default_level_is_warning() -> None:
        """With no flags the porringer logger is set to WARNING."""
        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list"])

        assert result.exit_code == 0

        logger = logging.getLogger("porringer")
        assert logger.level == logging.WARNING

    @staticmethod
    def test_verbose_sets_info() -> None:
        """A single -v sets the logger to INFO."""
        runner = CliRunner()
        result = runner.invoke(app, ["-v", "plugin", "list"])

        assert result.exit_code == 0

        logger = logging.getLogger("porringer")
        assert logger.level == logging.INFO

    @staticmethod
    def test_double_verbose_sets_debug() -> None:
        """-vv sets the logger to DEBUG."""
        runner = CliRunner()
        result = runner.invoke(app, ["-vv", "plugin", "list"])

        assert result.exit_code == 0

        logger = logging.getLogger("porringer")
        assert logger.level == logging.DEBUG

    @staticmethod
    def test_debug_flag_sets_debug() -> None:
        """--debug sets the logger to DEBUG."""
        runner = CliRunner()
        result = runner.invoke(app, ["--debug", "plugin", "list"])

        assert result.exit_code == 0

        logger = logging.getLogger("porringer")
        assert logger.level == logging.DEBUG
