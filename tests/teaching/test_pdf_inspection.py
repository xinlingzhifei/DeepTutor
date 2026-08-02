from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
import threading
import zlib

import pytest

from deeptutor.teaching.services import sources as source_service_module
from tests.pdf_inspection_worker import blocking_pdf_worker, silent_pdf_worker


async def _wait_for_path(
    path: Path,
    task: asyncio.Task[None] | None = None,
) -> None:
    for _ in range(3_000):
        if path.exists() and path.stat().st_size:
            return
        if task is not None and task.done():
            await task
            raise AssertionError("PDF inspection ended without writing its worker marker")
        await asyncio.sleep(0.01)
    raise AssertionError(f"worker marker was not created: {path}")


def _worker_is_active(pid: int) -> bool:
    return any(child.pid == pid for child in multiprocessing.active_children())


def _compressed_object_bomb_pdf(*, expanded_mebibytes: int = 512) -> bytes:
    parts = [b"%PDF-1.7\n"]
    offsets: dict[int, int] = {}

    def add_object(number: int, body: bytes) -> None:
        offsets[number] = sum(map(len, parts))
        parts.extend([f"{number} 0 obj\n".encode(), body, b"\nendobj\n"])

    add_object(1, b"<< /Type /Catalog /Pages 5 0 R >>")
    compressor = zlib.compressobj(level=9)
    compressed = compressor.compress(
        b"5 0 << /Type /Pages /Kids [6 0 R] /Count 1 /Padding ("
    )
    for _ in range(expanded_mebibytes):
        compressed += compressor.compress(b"A" * (1024 * 1024))
    compressed += compressor.compress(b") >>") + compressor.flush()
    add_object(
        3,
        (
            f"<< /Type /ObjStm /N 1 /First 4 /Length {len(compressed)} "
            "/Filter /FlateDecode >>\nstream\n"
        ).encode()
        + compressed
        + b"\nendstream",
    )
    add_object(6, b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 72 72] >>")
    offsets[2] = sum(map(len, parts))
    entries = (
        (0, 0, 65535),
        (1, offsets[1], 0),
        (1, offsets[2], 0),
        (1, offsets[3], 0),
        (0, 0, 0),
        (2, 3, 0),
        (1, offsets[6], 0),
    )
    xref = b"".join(
        bytes([kind]) + field_two.to_bytes(4, "big") + field_three.to_bytes(2, "big")
        for kind, field_two, field_three in entries
    )
    parts.extend(
        [
            b"2 0 obj\n",
            (
                f"<< /Type /XRef /Size 7 /Root 1 0 R /W [1 4 2] "
                f"/Length {len(xref)} >>\nstream\n"
            ).encode()
            + xref
            + b"\nendstream",
            b"\nendobj\n",
            f"startxref\n{offsets[2]}\n%%EOF\n".encode(),
        ]
    )
    return b"".join(parts)


@pytest.mark.asyncio
async def test_pdf_inspection_timeout_terminates_and_reaps_child(tmp_path: Path) -> None:
    runner = getattr(source_service_module, "_run_pdf_inspection_process", None)
    assert runner is not None, "PDF parsing must run in a killable child process"
    pdf_path = tmp_path / "blocked.pdf"
    pdf_path.write_bytes(b"%PDF-blocked")

    with pytest.raises(source_service_module.InvalidPdfSourceError, match="timed out"):
        await runner(
            pdf_path,
            timeout_seconds=3,
            worker=blocking_pdf_worker,
        )

    pid_path = Path(f"{pdf_path}.pid")
    await _wait_for_path(pid_path)
    pid = int(pid_path.read_text(encoding="ascii"))
    assert not _worker_is_active(pid)


@pytest.mark.asyncio
async def test_pdf_inspection_cancellation_terminates_and_reaps_child(tmp_path: Path) -> None:
    runner = getattr(source_service_module, "_run_pdf_inspection_process", None)
    assert runner is not None, "PDF parsing must run in a killable child process"
    pdf_path = tmp_path / "cancelled.pdf"
    pdf_path.write_bytes(b"%PDF-cancelled")
    task = asyncio.create_task(
        runner(
            pdf_path,
            timeout_seconds=60,
            worker=blocking_pdf_worker,
        )
    )
    pid_path = Path(f"{pdf_path}.pid")
    await _wait_for_path(pid_path, task)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pid = int(pid_path.read_text(encoding="ascii"))
    assert not _worker_is_active(pid)


@pytest.mark.asyncio
async def test_pdf_inspection_concurrency_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = source_service_module._run_pdf_inspection_process
    monkeypatch.setattr(
        source_service_module,
        "_PDF_INSPECTION_SLOTS",
        threading.BoundedSemaphore(value=1),
    )
    first_path = tmp_path / "first.pdf"
    second_path = tmp_path / "second.pdf"
    first_path.write_bytes(b"%PDF-first")
    second_path.write_bytes(b"%PDF-second")
    first_task = asyncio.create_task(
        runner(first_path, timeout_seconds=60, worker=blocking_pdf_worker)
    )
    second_task: asyncio.Task[None] | None = None
    try:
        await _wait_for_path(Path(f"{first_path}.pid"), first_task)
        second_task = asyncio.create_task(
            runner(second_path, timeout_seconds=60, worker=blocking_pdf_worker)
        )
        await asyncio.sleep(0.25)
        assert not Path(f"{second_path}.pid").exists()

        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        await _wait_for_path(Path(f"{second_path}.pid"), second_task)
    finally:
        if not first_task.done():
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
        if second_task is not None and not second_task.done():
            second_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_task
    assert not any(
        child.name.startswith("yfeistai-pdf-inspector")
        for child in multiprocessing.active_children()
    )


@pytest.mark.asyncio
async def test_pdf_inspection_worker_exiting_without_result_is_rejected(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "silent.pdf"
    pdf_path.write_bytes(b"%PDF-silent")

    with pytest.raises(
        source_service_module.InvalidPdfSourceError,
        match="could not be safely parsed",
    ):
        await source_service_module._run_pdf_inspection_process(
            pdf_path,
            timeout_seconds=30,
            worker=silent_pdf_worker,
        )


@pytest.mark.asyncio
async def test_compressed_object_bomb_is_contained_by_worker_memory_limit(
    tmp_path: Path,
) -> None:
    runner = getattr(source_service_module, "_run_pdf_inspection_process", None)
    assert runner is not None, "PDF parsing must run in a resource-limited child process"
    pdf_path = tmp_path / "object-bomb.pdf"
    pdf_path.write_bytes(_compressed_object_bomb_pdf())

    with pytest.raises(source_service_module.InvalidPdfSourceError):
        await runner(pdf_path, timeout_seconds=5)

    assert not any(
        child.name.startswith("yfeistai-pdf-inspector")
        for child in multiprocessing.active_children()
    )


def test_compressed_object_stream_fixture_is_otherwise_a_valid_pdf(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "small-object-stream.pdf"
    pdf_path.write_bytes(_compressed_object_bomb_pdf(expanded_mebibytes=1))

    source_service_module._validate_pdf_structure(
        str(pdf_path),
        source_service_module.MAX_PDF_PAGES,
        source_service_module._MAX_OBJECTS_INSPECTED,
    )
