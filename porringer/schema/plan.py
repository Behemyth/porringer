"""Frozen sync plans: exactly what was previewed, ready to run."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from porringer.schema.execution import SetupParameters, SetupResults
from porringer.schema.inspection import SyncInspectionReport


@dataclass(slots=True)
class SyncPlan:
    """The result of ``api.sync.plan``: the inspected actions, frozen for ``api.sync.run(plan=...)``.

    Running a plan executes these actions as previewed, without reloading
    the manifests. Each file the plan was built from is fingerprinted;
    if any changed since, ``run`` raises ``PlanStaleError`` instead.
    Remote manifests are downloaded once, when the plan is made.

    A plan runs once. Call :meth:`discard` to release it without running.
    """

    parameters: SetupParameters
    """The parameters the plan was made with, local paths for any downloaded manifests."""

    report: SyncInspectionReport
    """The inspection report, as ``api.sync.inspect`` returns it."""

    manifests: tuple[SetupResults, ...] = ()
    """Each loaded manifest's actions, in inspection order."""

    failed_paths: tuple[tuple[int, Path, str], ...] = ()
    """``(manifest_index, path, error)`` for each input that could not be loaded."""

    fingerprints: dict[Path, str] = field(default_factory=dict)
    """SHA-256 of each file the plan depends on; empty for files that didn't exist."""

    download_directory: Path | None = None
    """Where remote manifests were downloaded, removed when the plan is run or discarded."""

    consumed: bool = False
    """Whether the plan has been run or discarded."""

    @property
    def success(self) -> bool:
        """Whether inspection found every action runnable."""
        return self.report.success

    def discard(self) -> None:
        """Release the plan and its downloaded manifests without running it."""
        self.consumed = True
        if self.download_directory is not None:
            shutil.rmtree(self.download_directory, ignore_errors=True)
            self.download_directory = None
