"""Plugin integration for plugin.

Plugin implementation for Poetry project environment.
"""

from typing import override

from porringer.core.plugin_schema.project_environment import ProjectCommandPlan, ProjectEnvironment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import Ecosystem


class PoetryEnvironment(ProjectEnvironment):
    """Project environment managed by Poetry.

    Delegates venv creation, dependency resolution, and lock-file
    synchronisation to `poetry install`.

    Overrides `command_plan()` because Poetry requires a separate
    `poetry env use <path>` step to select a non-default interpreter,
    unlike PDM/uv which accept `--python` inline.
    """

    _project_evidence_files = ('poetry.lock',)
    _pyproject_tool_tables = (('tool', 'poetry'),)

    @staticmethod
    @override
    def ecosystem() -> Ecosystem:
        """Poetry belongs to the `python` ecosystem."""
        return Ecosystem('python')

    @classmethod
    @override
    def consumed_runtime_kind(cls) -> str:
        """Poetry consumes a Python runtime."""
        return 'python'

    @classmethod
    @override
    def tool_name(cls) -> str:
        """Poetry wraps the `poetry` CLI."""
        return 'poetry'

    @classmethod
    @override
    def run_prefix(cls) -> list[str]:
        """`poetry run` runs commands in the project environment."""
        return ['poetry', 'run']

    @classmethod
    @override
    def command_plan(cls, search_from, *, runtime_context: RuntimeContext | None = None) -> ProjectCommandPlan:
        """Build Poetry's sync plan, including runtime selection when needed."""
        directory = cls.resolve_project_root(search_from) or search_from
        steps: list[list[str]] = []
        if runtime_context is not None:
            exe = runtime_context.get(cls.consumed_runtime_kind())
            if exe is not None:
                steps.append(['poetry', 'env', 'use', str(exe)])
        steps.append(['poetry', cls._install_verb])
        return ProjectCommandPlan(directory=directory, argv=steps[-1], steps=steps)
