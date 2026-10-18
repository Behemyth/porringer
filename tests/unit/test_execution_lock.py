"""Tests for the per-API sync execution lock."""

import asyncio

from porringer.backend.command.sync import SyncCommands
from porringer.schema import (
    BatchSetupResults,
    ProgressEvent,
    SetupParameters,
    SyncRunReport,
)
from porringer.utility.exception import PorringerBusyError


class _ProbeSync(SyncCommands):
    """SyncCommands with a controllable run body."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()

    async def _run_unlocked(self, parameters, *, plugins, on_event, executor, prepared=None) -> SyncRunReport:
        del executor, prepared
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if on_event is not None:
                await on_event(object())
            await self.release.wait()
        finally:
            self.active -= 1
        return SyncRunReport(results=BatchSetupResults())


async def test_reentrant_run_raises_busy() -> None:
    """Starting a run from on_event of the same instance raises instead of deadlocking."""
    sync = _ProbeSync()
    sync.release.set()
    errors: list[BaseException] = []

    async def _on_event(_event: ProgressEvent) -> None:
        try:
            await sync.run(SetupParameters())
        except PorringerBusyError as exc:
            errors.append(exc)

    await sync.run(SetupParameters(), on_event=_on_event)

    assert len(errors) == 1


async def test_concurrent_runs_are_serialized() -> None:
    """Two tasks running on one instance never overlap."""
    sync = _ProbeSync()
    first = asyncio.create_task(sync.run(SetupParameters()))
    second = asyncio.create_task(sync.run(SetupParameters()))
    await asyncio.sleep(0.01)

    assert sync.active == 1
    sync.release.set()
    await asyncio.gather(first, second)

    assert sync.max_active == 1


async def test_separate_instances_do_not_block() -> None:
    """The lock is per instance, so a nested run on another instance is allowed."""
    outer = _ProbeSync()
    inner = _ProbeSync()
    outer.release.set()
    inner.release.set()

    async def _on_event(_event: ProgressEvent) -> None:
        await inner.run(SetupParameters())

    await outer.run(SetupParameters(), on_event=_on_event)

    assert inner.max_active == 1


def test_instance_survives_sequential_event_loops() -> None:
    """One instance can be reused across separate ``asyncio.run`` calls."""
    sync = _ProbeSync()
    for _ in range(2):
        sync.release = asyncio.Event()
        sync.release.set()
        asyncio.run(sync.run(SetupParameters()))
