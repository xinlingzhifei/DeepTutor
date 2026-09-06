from __future__ import annotations

import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="requires native Linux with independent publisher and writer UIDs",
)

_WRITER = 1000
_ACTOR = r"""
import errno, json, os, sys
assert os.getresuid() == (1000, 1000, 1000)
assert os.getresgid() == (1000, 1000, 1000)
assert os.getgroups() == []
with open('/proc/self/status') as status:
    cap = next(line.split()[1] for line in status if line.startswith('CapEff:'))
assert int(cap, 16) == 0
parent, case, action, entry = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
def create(name, body):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body)
if action in ('swap', 'same-bytes', 'symlink'):
    os.rename('receipt.json', 'displaced.json', src_dir_fd=parent, dst_dir_fd=parent)
    if action == 'symlink':
        os.symlink('sentinel', 'foreign.json', dir_fd=parent)
    else:
        create('foreign.json', b'owned\x00\xff' if action == 'same-bytes' else b'foreign\x00\xff')
    os.rename('foreign.json', 'receipt.json', src_dir_fd=parent, dst_dir_fd=parent)
elif action == 'occupy':
    create('receipt.json', b'occupant\x00\xff')
elif action == 'deny-private':
    assert os.stat('recovery', dir_fd=case, follow_symlinks=False).st_uid == 0
    attempts = (
        lambda: os.open('recovery', os.O_RDONLY | os.O_DIRECTORY, dir_fd=case),
        lambda: os.open('recovery/' + entry, os.O_RDONLY, dir_fd=case),
        lambda: os.open('recovery/' + entry, os.O_WRONLY, dir_fd=case),
        lambda: os.unlink('recovery/' + entry, dir_fd=case),
        lambda: os.rename('receipt.json', 'recovery/' + entry,
                          src_dir_fd=parent, dst_dir_fd=case),
    )
    for attempt in attempts:
        try:
            leaked = attempt()
        except OSError as exc:
            assert exc.errno in (errno.EACCES, errno.EPERM)
        else:
            if isinstance(leaked, int): os.close(leaked)
            raise AssertionError('writer accessed publisher recovery')
else:
    raise AssertionError(action)
print(json.dumps({'uid': os.getresuid(), 'action': action, 'ok': True}))
"""


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "receipt_recovery.py"
    assert path.is_file(), "the independent-identity receipt recovery component is missing"
    specification = importlib.util.spec_from_file_location("_receipt_recovery_contract", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _identity(path):
    details = path.lstat()
    return details.st_dev, details.st_ino


@pytest.fixture
def case(tmp_path):
    # Run this file with a private basetemp below trusted ancestors, not /tmp.
    tmp_path.chmod(0o755)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    os.chown(shared, _WRITER, _WRITER)
    recovery = tmp_path / "recovery"
    recovery.mkdir(mode=0o700)
    target = shared / "receipt.json"
    target.write_bytes(b"owned\x00\xff")
    (shared / "sentinel").write_bytes(b"unrelated sentinel")
    os.link(target, tmp_path / "owned-recovery-link")
    parent_fd = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
    case_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        yield tmp_path, shared, recovery, target, parent_fd, case_fd, original_fd
    finally:
        for descriptor in (original_fd, case_fd, parent_fd):
            os.close(descriptor)


def _actor(case, action, entry=""):
    completed = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", "-c", _ACTOR, str(case[4]), str(case[5]), action, entry],
        user=_WRITER,
        group=_WRITER,
        extra_groups=(),
        pass_fds=(case[4], case[5]),
        close_fds=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"uid": [1000, 1000, 1000], "action": action, "ok": True}


def _remove(recovery, case, *, expected_fd=None):
    recovery.remove_owned(
        case[4],
        case[3].name,
        expected_fd=case[6] if expected_fd is None else expected_fd,
        source_path=case[3],
    )


def _journals(case):
    return [json.loads(path.read_bytes()) for path in case[2].glob("*.recovery.json")]


def test_owned_removal_uses_original_pin_and_retains_recovery_record(case):
    module = _module()
    duplicate = os.dup(case[6])
    try:
        with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
            _remove(recovery, case, expected_fd=duplicate)
        assert not case[3].exists()
        assert os.fstat(duplicate).st_ino == (case[0] / "owned-recovery-link").stat().st_ino
        assert (case[0] / "owned-recovery-link").read_bytes() == b"owned\x00\xff"
        records = _journals(case)
        assert len(records) == 1 and records[0]["state"] == "removed"
        assert not Path(records[0]["recoveryPath"]).exists()
    finally:
        os.close(duplicate)


@pytest.mark.parametrize("action", ["swap", "same-bytes", "symlink"])
def test_competing_inode_is_restored_without_following_or_deleting_it(case, monkeypatch, action):
    module = _module()
    real_rename = module._rename_no_replace
    reached = []
    foreign_identity = None

    def swap_before_detach(source_fd, source, target_fd, target):
        nonlocal foreign_identity
        if source_fd == case[4]:
            assert not reached
            _actor(case, action)
            foreign_identity = _identity(case[3])
            reached.append(True)
        return real_rename(source_fd, source, target_fd, target)

    monkeypatch.setattr(module, "_rename_no_replace", swap_before_detach)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError, match="foreign") as caught:
            _remove(recovery, case)
    assert reached == [True]
    assert caught.value.recovery_path is None
    assert _identity(case[3]) == foreign_identity
    if action == "symlink":
        assert os.readlink(case[3]) == "sentinel"
    else:
        expected = b"owned\x00\xff" if action == "same-bytes" else b"foreign\x00\xff"
        assert case[3].read_bytes() == expected
    assert (case[1] / "sentinel").read_bytes() == b"unrelated sentinel"
    assert (case[1] / "displaced.json").read_bytes() == b"owned\x00\xff"
    assert _journals(case)[0]["state"] == "restored"


def test_restore_conflict_preserves_both_objects_and_reports_exact_path(case, monkeypatch):
    module = _module()
    real_rename = module._rename_no_replace
    reached = []
    foreign_identity = None

    def compete_at_both_boundaries(source_fd, source, target_fd, target):
        nonlocal foreign_identity
        if source_fd == case[4]:
            _actor(case, "swap")
            foreign_identity = _identity(case[3])
            reached.append("detach")
        elif target_fd == case[4]:
            _actor(case, "occupy")
            reached.append("restore")
        return real_rename(source_fd, source, target_fd, target)

    monkeypatch.setattr(module, "_rename_no_replace", compete_at_both_boundaries)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError) as caught:
            _remove(recovery, case)
    assert reached == ["detach", "restore"]
    retained = caught.value.recovery_path
    assert retained.is_absolute() and retained.parent == case[2]
    assert str(retained) in str(caught.value)
    assert _identity(retained) == foreign_identity
    assert retained.read_bytes() == b"foreign\x00\xff"
    assert case[3].read_bytes() == b"occupant\x00\xff"
    assert json.loads(caught.value.journal_path.read_bytes())["state"] == "retained"


def test_private_unlink_cannot_touch_new_source_or_be_replaced_by_writer(case, monkeypatch):
    module = _module()
    real_unlink = os.unlink
    reached = []

    def actor_at_private_unlink(name, *, dir_fd=None):
        assert dir_fd != case[4], "must not unlink a name in the competing writer's directory"
        _actor(case, "occupy")
        _actor(case, "deny-private", str(name))
        reached.append(True)
        return real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "unlink", actor_at_private_unlink)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        _remove(recovery, case)
    assert reached == [True]
    assert case[3].read_bytes() == b"occupant\x00\xff"
    assert _journals(case)[0]["state"] == "removed"


@pytest.mark.parametrize("unsafe", ["worker-owner", "group-write", "link", "ancestor-write"])
def test_unsafe_recovery_path_is_rejected_before_source_mutation(case, unsafe):
    module = _module()
    before = _identity(case[3]), case[3].read_bytes()
    root = case[2]
    if unsafe == "worker-owner":
        os.chown(root, _WRITER, _WRITER)
    elif unsafe == "group-write":
        root.chmod(0o770)
    elif unsafe == "link":
        root = case[0] / "recovery-link"
        root.symlink_to(case[2], target_is_directory=True)
    else:
        case[0].chmod(0o777)
    with pytest.raises((ValueError, OSError), match="recovery|publisher"):
        module.PosixReceiptRecovery.open(root, worker_uid=_WRITER)
    assert (_identity(case[3]), case[3].read_bytes()) == before
    assert not list(case[2].iterdir())


@pytest.mark.parametrize("worker_uid", [0, -1, True, "1000"])
def test_invalid_worker_principal_is_rejected_without_output(case, worker_uid):
    module = _module()
    with pytest.raises(ValueError, match="identity|principal|worker"):
        module.PosixReceiptRecovery.open(case[2], worker_uid=worker_uid)
    assert case[3].read_bytes() == b"owned\x00\xff"
    assert not list(case[2].iterdir())


def test_unavailable_original_pin_never_authorizes_pathname_removal(case):
    module = _module()
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises((ValueError, OSError), match="pin|descriptor|file"):
            _remove(recovery, case, expected_fd=-1)
    assert case[3].read_bytes() == b"owned\x00\xff"
    assert not list(case[2].iterdir())


def test_post_detach_failure_keeps_entry_and_prepared_recovery_record(case, monkeypatch):
    module = _module()
    real_rename = module._rename_no_replace
    before = _identity(case[3])

    def detach_then_fail(*arguments):
        real_rename(*arguments)
        raise OSError("injected failure after real atomic detach")

    monkeypatch.setattr(module, "_rename_no_replace", detach_then_fail)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError) as caught:
            _remove(recovery, case)
    retained = caught.value.recovery_path
    assert retained.is_absolute() and retained.parent == case[2]
    assert _identity(retained) == before
    assert retained.read_bytes() == b"owned\x00\xff"
    record = json.loads(caught.value.journal_path.read_bytes())
    assert record["state"] == "prepared" and record["recoveryPath"] == str(retained)
    assert not case[3].exists()


def test_missing_journal_after_foreign_restore_is_not_reported_as_success(case, monkeypatch):
    module = _module()
    real_write = module.PosixReceiptRecovery._write_journal
    _actor(case, "swap")
    foreign_identity = _identity(case[3])
    reached = []

    def missing_after_restore(self, name, record, *, initial):
        if record["state"] == "restored":
            reached.append(True)
            raise FileNotFoundError("injected missing final journal")
        return real_write(self, name, record, initial=initial)

    monkeypatch.setattr(module.PosixReceiptRecovery, "_write_journal", missing_after_restore)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError):
            _remove(recovery, case)
    assert reached == [True]
    assert _identity(case[3]) == foreign_identity
    assert case[3].read_bytes() == b"foreign\x00\xff"
    assert _journals(case)[0]["state"] == "prepared"


def test_exception_after_native_restore_does_not_report_nonexistent_recovery_path(
    case, monkeypatch
):
    module = _module()
    real_rename = module._rename_no_replace
    _actor(case, "swap")
    foreign_identity = _identity(case[3])
    reached = []

    def restore_then_fail(source_fd, source, target_fd, target):
        real_rename(source_fd, source, target_fd, target)
        if target_fd == case[4]:
            reached.append(True)
            raise OSError("injected error after successful restore")

    monkeypatch.setattr(module, "_rename_no_replace", restore_then_fail)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError) as caught:
            _remove(recovery, case)
    assert reached == [True]
    assert caught.value.recovery_path is None
    assert caught.value.journal_path.is_file()
    assert _identity(case[3]) == foreign_identity
    assert case[3].read_bytes() == b"foreign\x00\xff"
    assert _journals(case)[0]["state"] == "prepared"


def test_failed_private_inspection_reports_uncertain_location_and_original_failure(
    case, monkeypatch
):
    module = _module()
    real_stat = os.stat
    reached = []

    def unavailable_private_stat(path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None and str(path).endswith(".entry"):
            reached.append(True)
            raise OSError(errno.EIO, "injected private inspection I/O failure")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(module.os, "stat", unavailable_private_stat)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError, match="unconfirmed") as caught:
            _remove(recovery, case)
    assert len(reached) >= 2
    retained = caught.value.recovery_path
    assert retained.is_absolute() and retained.parent == case[2]
    assert retained.read_bytes() == b"owned\x00\xff"
    assert caught.value.journal_path.read_bytes()
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_after_detach_preserves_control_flow_and_recovery_diagnostic(
    case, monkeypatch, interruption
):
    module = _module()
    real_rename = module._rename_no_replace

    def interrupt_after_detach(*arguments):
        real_rename(*arguments)
        raise interruption("injected publication interruption")

    monkeypatch.setattr(module, "_rename_no_replace", interrupt_after_detach)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(interruption) as caught:
            _remove(recovery, case)
    record = _journals(case)[0]
    retained = Path(record["recoveryPath"])
    assert retained.read_bytes() == b"owned\x00\xff"
    assert any(str(retained) in note for note in caught.value.__notes__)
    assert record["state"] == "prepared"


def test_initial_absent_source_does_not_touch_displaced_original(case):
    module = _module()
    displaced = case[1] / "already-moved.json"
    os.rename(case[3], displaced)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        _remove(recovery, case)
    assert displaced.read_bytes() == b"owned\x00\xff"
    assert _journals(case)[0]["state"] == "absent"


def test_unsupported_no_replace_has_no_copy_or_delete_fallback(case, monkeypatch):
    module = _module()

    def unavailable(*_arguments):
        raise OSError(errno.ENOSYS, "no native renameat2")

    monkeypatch.setattr(module, "_rename_no_replace", unavailable)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError) as caught:
            _remove(recovery, case)
    assert caught.value.recovery_path is None
    assert case[3].read_bytes() == b"owned\x00\xff"
    assert _journals(case)[0]["state"] == "prepared"


def test_real_cross_filesystem_source_is_rejected_without_mutation(case):
    module = _module()
    other_filesystem = Path("/dev/shm")
    if not other_filesystem.is_dir() or other_filesystem.stat().st_dev == case[2].stat().st_dev:
        pytest.skip("a second native filesystem is unavailable")
    other = Path(tempfile.mkdtemp(prefix="yfeistai-receipt-cross-device-", dir=other_filesystem))
    target = other / "receipt.json"
    target.write_bytes(b"other filesystem owned fixture")
    parent_fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
    original_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
            with pytest.raises(ValueError, match="same filesystem"):
                recovery.remove_owned(
                    parent_fd, target.name, expected_fd=original_fd, source_path=target
                )
        assert target.read_bytes() == b"other filesystem owned fixture"
        assert not list(case[2].iterdir())
    finally:
        os.close(original_fd)
        os.close(parent_fd)


@pytest.mark.parametrize("rotated", ["root", "ancestor"])
def test_recovery_directory_rotation_is_rejected_before_any_mutation(case, rotated):
    module = _module()
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        if rotated == "root":
            moved = case[2].with_name("recovery-old")
            os.rename(case[2], moved)
            case[2].mkdir(mode=0o700)
            original_target = case[3]
            moved_recovery = moved
        else:
            moved = case[0].with_name(case[0].name + "-old")
            os.rename(case[0], moved)
            case[0].mkdir(mode=0o755)
            case[2].mkdir(mode=0o700)
            original_target = moved / "shared" / case[3].name
            moved_recovery = moved / "recovery"
        with pytest.raises(ValueError, match="recovery.*changed"):
            _remove(recovery, case)
    assert original_target.read_bytes() == b"owned\x00\xff"
    assert not list(case[2].iterdir()) and not list(moved_recovery.iterdir())


def test_rotation_after_detach_marks_path_unconfirmed_and_records_root_identity(case, monkeypatch):
    module = _module()
    real_rename = module._rename_no_replace
    moved_root = case[2].with_name("recovery-relocated")
    root_identity = _identity(case[2])

    def rotate_after_detach(*arguments):
        real_rename(*arguments)
        os.rename(case[2], moved_root)
        case[2].mkdir(mode=0o700)

    monkeypatch.setattr(module, "_rename_no_replace", rotate_after_detach)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(module.ReceiptRecoveryError, match="unconfirmed") as caught:
            _remove(recovery, case)
    assert caught.value.location_confirmed is False
    assert not list(case[2].iterdir())
    entries = list(moved_root.glob("*.entry"))
    assert len(entries) == 1 and entries[0].read_bytes() == b"owned\x00\xff"
    records = [json.loads(path.read_bytes()) for path in moved_root.glob("*.recovery.json")]
    assert len(records) == 1 and records[0]["state"] == "prepared"
    assert records[0]["recoveryRootIdentity"] == list(root_identity)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_recording_restore_conflict_is_not_swallowed(case, monkeypatch, interruption):
    module = _module()
    real_rename = module._rename_no_replace
    real_write = module.PosixReceiptRecovery._write_journal
    _actor(case, "swap")

    def occupy_before_restore(source_fd, source, target_fd, target):
        if target_fd == case[4]:
            _actor(case, "occupy")
        return real_rename(source_fd, source, target_fd, target)

    def interrupt_retained_journal(self, name, record, *, initial):
        if record["state"] == "retained":
            raise interruption("injected interruption recording conflict")
        return real_write(self, name, record, initial=initial)

    monkeypatch.setattr(module, "_rename_no_replace", occupy_before_restore)
    monkeypatch.setattr(module.PosixReceiptRecovery, "_write_journal", interrupt_retained_journal)
    with module.PosixReceiptRecovery.open(case[2], worker_uid=_WRITER) as recovery:
        with pytest.raises(interruption) as caught:
            _remove(recovery, case)
    record = _journals(case)[0]
    retained = Path(record["recoveryPath"])
    assert retained.read_bytes() == b"foreign\x00\xff"
    assert case[3].read_bytes() == b"occupant\x00\xff"
    assert any(str(retained) in note for note in caught.value.__notes__)
