"""Tests for temporary-file ownership in asynchronous downloads."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO, cast

import aiohttp
import pytest

from porringer.schema import DownloadParameters, HashAlgorithm
from porringer.utility import download as download_utility


class _Content:
    """Minimal async response body."""

    def __init__(self, chunks: tuple[bytes, ...] = ()) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _Response:
    """Minimal aiohttp response context manager."""

    def __init__(self, *, content_length: int | None = None, error: Exception | None = None) -> None:
        self.headers = {} if content_length is None else {'Content-Length': str(content_length)}
        self.content = _Content()
        self._error = error

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


class _Session:
    """Minimal borrowed session returning one response."""

    def __init__(self, response: _Response) -> None:
        self._response = response

    def get(self, *_args: object, **_kwargs: object) -> _Response:
        return self._response


def _state(tmp_path: Path, *, expected_size: int | None = None) -> download_utility._DownloadState:
    (tmp_path / 'archive.bin').write_bytes(b'original')
    return download_utility._DownloadState(
        parameters=DownloadParameters(
            url='https://example.invalid/archive.bin',
            destination=tmp_path / 'archive.bin',
            expected_size=expected_size,
        ),
        expected_algorithm=None,
        expected_digest=None,
        progress_callback=None,
    )


def _temporary_files(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob('.download_*.tmp'))


@pytest.fixture
def opened_files(monkeypatch: pytest.MonkeyPatch) -> list[BinaryIO]:
    """Record actual handles so unlink success cannot hide a descriptor leak."""
    handles: list[BinaryIO] = []
    fdopen = os.fdopen

    def tracked_fdopen(descriptor: int, mode: str) -> BinaryIO:
        handle = cast(BinaryIO, fdopen(descriptor, mode))
        handles.append(handle)
        return handle

    monkeypatch.setattr(download_utility.os, 'fdopen', tracked_fdopen)
    return handles


def _assert_failed_attempt_cleaned_up(tmp_path: Path, handles: list[BinaryIO]) -> None:
    assert len(handles) == 1
    assert handles[0].closed
    assert _temporary_files(tmp_path) == []
    assert (tmp_path / 'archive.bin').read_bytes() == b'original'


async def test_http_failure_closes_and_removes_temporary_file(tmp_path: Path, opened_files: list[BinaryIO]) -> None:
    """An error before body iteration leaves no open temporary descriptor."""
    error = aiohttp.ClientConnectionError('connection failed')

    with pytest.raises(aiohttp.ClientConnectionError):
        await download_utility._download_attempt(
            cast(aiohttp.ClientSession, _Session(_Response(error=error))),
            _state(tmp_path),
            None,
        )

    _assert_failed_attempt_cleaned_up(tmp_path, opened_files)


async def test_header_size_mismatch_closes_and_removes_temporary_file(
    tmp_path: Path, opened_files: list[BinaryIO]
) -> None:
    """A header rejection closes the file before cleanup unlinks it."""
    result = await download_utility._download_attempt(
        cast(aiohttp.ClientSession, _Session(_Response(content_length=2))),
        _state(tmp_path, expected_size=1),
        None,
    )

    assert result.success is False
    assert result.message == 'Size mismatch: expected 1, got 2'
    _assert_failed_attempt_cleaned_up(tmp_path, opened_files)


async def test_verified_download_replaces_destination(tmp_path: Path, opened_files: list[BinaryIO]) -> None:
    """A successful transfer writes all chunks and replaces the old destination."""
    payload = b'new archive'
    state = _state(tmp_path, expected_size=len(payload))
    state.expected_algorithm = HashAlgorithm.SHA256
    state.expected_digest = hashlib.sha256(payload).hexdigest()
    response = _Response(content_length=len(payload))
    response.content = _Content((b'new ', b'archive'))

    result = await download_utility._download_attempt(cast(aiohttp.ClientSession, _Session(response)), state, None)

    assert result.success
    assert result.verified
    assert result.size == len(payload)
    assert result.path == state.parameters.destination
    assert await asyncio.to_thread(state.parameters.destination.read_bytes) == payload
    assert len(opened_files) == 1
    assert opened_files[0].closed
    assert _temporary_files(tmp_path) == []


async def test_cancelled_write_finishes_before_cancellation_propagates() -> None:
    """Cancellation waits for an in-flight worker-thread write to settle."""
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    class BlockingWriter:
        completed = False

        def write(self, _chunk: bytes) -> None:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=5):
                raise TimeoutError('Test did not release the write thread')
            self.completed = True

    writer = BlockingWriter()
    task = asyncio.create_task(download_utility._write_chunk(writer, b'data'))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert task.cancelled()
    assert writer.completed is True
