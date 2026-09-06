"""Descriptor-pinned receipt recovery under a separately trusted publisher UID.

The caller supplies operator-owned policy, not worker environment overrides.
All processes/code with the publisher identity are trusted. External writers
must have a different unprivileged UID and no means to acquire publisher rights.
This module does not create users, elevate privileges, or authenticate a caller's
deployment policy. It never treats pathname bytes or an inode tuple as an owned
file capability: the original regular-file descriptor must remain open.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import sys
from types import TracebackType
import uuid


class ReceiptRecoveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        recovery_path: Path | None,
        journal_path: Path,
        location_confirmed: bool = True,
    ) -> None:
        self.recovery_path = recovery_path
        self.journal_path = journal_path
        self.location_confirmed = location_confirmed
        detail = ""
        if recovery_path is not None:
            label = (
                "recovery retained at" if location_confirmed else "recovery location unconfirmed:"
            )
            detail = f"; {label} {recovery_path}"
        journal_label = (
            "recovery journal" if location_confirmed else "recovery journal path unconfirmed"
        )
        super().__init__(f"{message}{detail}; {journal_label}: {journal_path}")


def _identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _rename_no_replace(source_fd: int, source: str, target_fd: int, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "receipt recovery requires renameat2")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "receipt recovery no-replace rename failed")


def _publisher_identity(worker_uid: int) -> int:
    if sys.platform != "linux":
        raise ValueError("receipt recovery requires Linux publisher isolation")
    publisher = os.geteuid()
    if (
        type(worker_uid) is not int
        or not 0 < worker_uid < 2**32 - 1
        or worker_uid == publisher
        or os.getresuid() != (publisher, publisher, publisher)
    ):
        raise ValueError("receipt recovery requires independent publisher and worker identities")
    return publisher


class PosixReceiptRecovery:
    def __init__(self, root: Path, worker_uid: int, publisher_uid: int) -> None:
        self.root = root
        self.worker_uid = worker_uid
        self.publisher_uid = publisher_uid
        self._directories: list[tuple[int, tuple[int, int]]] = []

    @classmethod
    def open(cls, root: Path, *, worker_uid: int) -> PosixReceiptRecovery:
        publisher = _publisher_identity(worker_uid)
        path = Path(root)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError("receipt recovery root must be an explicit absolute directory")
        boundary = cls(path, worker_uid, publisher)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open("/", flags)
            boundary._directories.append((descriptor, _identity(os.fstat(descriptor))))
            for name in path.parts[1:]:
                boundary._check_directory(descriptor, final=False)
                descriptor = os.open(name, flags, dir_fd=descriptor)
                boundary._directories.append((descriptor, _identity(os.fstat(descriptor))))
            boundary._check_directory(descriptor, final=True)
            return boundary
        except BaseException as original:
            boundary._close_preserving(original)
            if isinstance(original, OSError):
                raise ValueError("receipt recovery directory cannot be safely opened") from original
            raise

    def _check_directory(self, descriptor: int, *, final: bool) -> None:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid not in (0, self.publisher_uid)
            or details.st_mode & 0o022
            or (
                final
                and (details.st_uid != self.publisher_uid or stat.S_IMODE(details.st_mode) != 0o700)
            )
        ):
            raise ValueError("receipt recovery directory is not private to the publisher")

    def _assert_open(self) -> int:
        if not self._directories or _publisher_identity(self.worker_uid) != self.publisher_uid:
            raise ValueError("receipt recovery publisher boundary is closed or changed")
        for index, (descriptor, identity) in enumerate(self._directories):
            self._check_directory(descriptor, final=index == len(self._directories) - 1)
            if _identity(os.fstat(descriptor)) != identity:
                raise ValueError("receipt recovery directory identity changed")
            if index:
                try:
                    named = os.stat(
                        self.root.parts[index],
                        dir_fd=self._directories[index - 1][0],
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise ValueError("receipt recovery directory binding changed") from exc
                if not stat.S_ISDIR(named.st_mode) or _identity(named) != identity:
                    raise ValueError("receipt recovery directory binding changed")
        return self._directories[-1][0]

    def _write_journal(self, name: str, record: dict[str, object], *, initial: bool) -> None:
        parent_fd = self._assert_open()
        staged_name = name if initial else f"{name}.{uuid.uuid4().hex}.pending"
        descriptor = os.open(
            staged_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if not initial:
            # Both names are inside the publisher-only directory. An interrupted
            # update retains the previous prepared record and any pending file.
            os.replace(staged_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)

    def remove_owned(
        self,
        parent_fd: int,
        name: str,
        *,
        expected_fd: int,
        source_path: Path,
    ) -> None:
        recovery_fd = self._assert_open()
        source_path = Path(source_path)
        if (
            not name
            or name in (".", "..")
            or "/" in name
            or "\x00" in name
            or not source_path.is_absolute()
            or ".." in source_path.parts
            or source_path.name != name
        ):
            raise ValueError("receipt recovery source name/path is invalid")
        source_parent = os.fstat(parent_fd)
        if not stat.S_ISDIR(source_parent.st_mode):
            raise ValueError("receipt recovery source descriptor is not a directory")
        try:
            original = os.fstat(expected_fd)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("receipt recovery original file pin is unavailable") from exc
        if not stat.S_ISREG(original.st_mode):
            raise ValueError("receipt recovery original file pin is not regular")
        if not source_parent.st_dev == original.st_dev == os.fstat(recovery_fd).st_dev:
            raise ValueError("receipt recovery must use the same filesystem as the pinned file")

        operation = uuid.uuid4().hex
        entry_name = f"{operation}.entry"
        journal_name = f"{operation}.recovery.json"
        recovery_path = self.root / entry_name
        journal_path = self.root / journal_name
        record: dict[str, object] = {
            "schemaVersion": 1,
            "operationId": operation,
            "state": "prepared",
            "publisherUid": self.publisher_uid,
            "workerUid": self.worker_uid,
            "sourcePathHint": str(source_path),
            "sourceParentIdentity": list(_identity(source_parent)),
            "expectedIdentity": list(_identity(original)),
            "recoveryRootIdentity": list(_identity(os.fstat(recovery_fd))),
            "recoveryPath": str(recovery_path),
        }
        self._write_journal(journal_name, record, initial=True)

        phase = "admission"
        try:
            self._assert_open()
            phase = "detach"
            _rename_no_replace(parent_fd, name, recovery_fd, entry_name)
            phase = "inspect"
            os.fsync(parent_fd)
            os.fsync(recovery_fd)
            moved = os.stat(entry_name, dir_fd=recovery_fd, follow_symlinks=False)
            if stat.S_ISREG(moved.st_mode) and _identity(moved) == _identity(original):
                # The original FD keeps its inode allocated. Only this private
                # name is unlinked; the competing source name is never unlinked.
                self._assert_open()
                phase = "unlink"
                os.unlink(entry_name, dir_fd=recovery_fd)
                os.fsync(recovery_fd)
                record["state"] = "removed"
                self._write_journal(journal_name, record, initial=False)
                return
            record["observedIdentity"] = list(_identity(moved))
            phase = "restore"
            _rename_no_replace(recovery_fd, entry_name, parent_fd, name)
            phase = "record-restoration"
            os.fsync(parent_fd)
            os.fsync(recovery_fd)
            record["state"] = "restored"
            self._write_journal(journal_name, record, initial=False)
            raise ReceiptRecoveryError(
                "foreign receipt was preserved and restored",
                recovery_path=None,
                journal_path=journal_path,
            )
        except ReceiptRecoveryError:
            raise
        except BaseException as original_error:
            # A native operation may have succeeded before an exception reached
            # Python. Keep the prepared record and inspect only the private name.
            inspection_error = None
            try:
                os.stat(entry_name, dir_fd=recovery_fd, follow_symlinks=False)
            except FileNotFoundError:
                retained = None
            except OSError as exc:
                inspection_error = exc
                retained = recovery_path
            else:
                retained = recovery_path
            binding_error = None
            try:
                self._assert_open()
            except (OSError, ValueError) as exc:
                binding_error = exc
            if (
                phase == "detach"
                and isinstance(original_error, FileNotFoundError)
                and retained is None
                and binding_error is None
            ):
                record["state"] = "absent"
                self._write_journal(journal_name, record, initial=False)
                return
            location_confirmed = inspection_error is None and binding_error is None
            diagnostic = ReceiptRecoveryError(
                f"receipt recovery did not complete during {phase}",
                recovery_path=retained,
                journal_path=journal_path,
                location_confirmed=location_confirmed,
            )
            if phase == "restore" and retained is not None and location_confirmed:
                record["state"] = "retained"
                try:
                    self._write_journal(journal_name, record, initial=False)
                except BaseException as journal_error:
                    if not isinstance(journal_error, Exception):
                        journal_error.add_note(str(diagnostic))
                        raise
                    original_error.add_note(f"recovery journal update also failed: {journal_error}")
            if inspection_error is not None:
                diagnostic.add_note(f"recovery location inspection also failed: {inspection_error}")
            if binding_error is not None:
                diagnostic.add_note(f"recovery pathname binding also failed: {binding_error}")
            if not isinstance(original_error, Exception):
                original_error.add_note(str(diagnostic))
                raise
            raise diagnostic from original_error

    def close(self) -> None:
        failures = []
        while self._directories:
            descriptor, _identity_value = self._directories.pop()
            try:
                os.close(descriptor)
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def _close_preserving(self, original: BaseException | None) -> None:
        try:
            self.close()
        except OSError as close_error:
            if original is None:
                raise
            original.add_note(f"receipt recovery directory close failed: {close_error}")

    def __enter__(self) -> PosixReceiptRecovery:
        self._assert_open()
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close_preserving(error)
