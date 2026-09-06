from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.scripts.test_classroom_release_evidence import _load_evidence_module

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="requires native Linux with independent publisher and writer UIDs",
)

_WRITER = 1000
_FOREIGN_BODY = b"another publisher's payload\x00\xff"
_ACTOR = r"""
import json, os, sys
assert os.getresuid() == (1000, 1000, 1000)
assert os.getresgid() == (1000, 1000, 1000)
assert os.getgroups() == []
with open('/proc/self/status') as status:
    cap = next(line.split()[1] for line in status if line.startswith('CapEff:'))
assert int(cap, 16) == 0
parent, action, receipt = int(sys.argv[1]), sys.argv[2], sys.argv[3]
assert os.fstat(parent).st_uid == 1000
assert os.fstat(parent).st_mode & 0o700 == 0o700
if action == 'prepare':
    fd = os.open('replacement.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 0o600, dir_fd=parent)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(b"another publisher's payload\x00\xff")
elif action == 'swap':
    os.rename(receipt, 'displaced-owned.json', src_dir_fd=parent, dst_dir_fd=parent)
    os.rename('replacement.json', receipt, src_dir_fd=parent, dst_dir_fd=parent)
else:
    raise AssertionError(action)
print(json.dumps({'uid': os.getresuid(), 'action': action, 'ok': True}))
"""


def _actor(parent_fd: int, action: str, receipt: str) -> None:
    completed = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", "-c", _ACTOR, str(parent_fd), action, receipt],
        user=_WRITER,
        group=_WRITER,
        extra_groups=(),
        pass_fds=(parent_fd,),
        close_fds=True,
        cwd="/",
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "uid": [_WRITER, _WRITER, _WRITER],
        "action": action,
        "ok": True,
    }


@pytest.mark.parametrize("cleanup", ("manifest", "classroom"))
@pytest.mark.parametrize("replace_before_remove", (False, True), ids=("owned", "replacement"))
def test_posix_receipt_cleanup_preserves_replacement_at_remove_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
    replace_before_remove: bool,
) -> None:
    module = _load_evidence_module()
    recovery_module = sys.modules[module.PosixReceiptRecovery.__module__]
    # The native helper supplies basetemp below trusted ancestors, not /tmp.
    recovery_root = tmp_path / "recovery"
    recovery_root.mkdir(mode=0o700)
    recovery_identity = module._file_identity(recovery_root.stat())
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    os.chown(root, _WRITER, _WRITER)
    for name in ("raw", "runtime", "deploy"):
        (root / name).mkdir(mode=0o700)
        os.chown(root / name, _WRITER, _WRITER)
    if cleanup == "manifest":
        boundary = module._ManifestPublicationBoundary.open(root)
        parent = root
        parent_key = "bundle"
    else:
        boundary = module._GatewayPublicationBoundary.open(root, root)
        boundary.ensure_publication_parents()
        parent = root / "runtime"
        parent_key = "runtime"
    parent_fd = int(boundary.leases[parent_key].handle)
    parent_identity = module._file_identity(os.fstat(parent_fd))
    target = parent / "owned-receipt.json"
    owned_backup = tmp_path / "owned-recovery-link"
    foreign_backup = tmp_path / "foreign-recovery-link"
    displaced_owned = parent / "displaced-owned.json"
    try:
        with module.receipt_publication(recovery_root=recovery_root, worker_uid=_WRITER):
            original_handle, owned_identity, owned_body = module._create_classroom_json_staging(
                boundary,
                parent_key=parent_key,
                path=target,
                document={"payload": "original transaction payload\x00\xff"},
            )
            original_handle.close()
            assert original_handle.closed
            # Retain both original objects even if a cleanup regression destroys a name.
            os.link(target, owned_backup)
            _actor(parent_fd, "prepare", target.name)
            foreign = parent / "replacement.json"
            foreign_identity = module._file_identity(foreign.stat())
            assert foreign.stat().st_uid == _WRITER
            os.link(foreign, foreign_backup)
            real_rename = recovery_module._rename_no_replace
            reached = []

            def detach_after_replacement(source_fd, source, target_fd, destination):
                # The publication scope owns a duplicate of the original parent FD.
                if (
                    source == target.name
                    and module._file_identity(os.fstat(source_fd)) == parent_identity
                ):
                    assert not reached
                    assert module._file_identity(os.fstat(target_fd)) == recovery_identity
                    assert module._file_identity(target.stat()) == owned_identity
                    if replace_before_remove:
                        _actor(source_fd, "swap", source)
                        assert module._file_identity(target.stat()) == foreign_identity
                    reached.append(replace_before_remove)
                return real_rename(source_fd, source, target_fd, destination)

            monkeypatch.setattr(recovery_module, "_rename_no_replace", detach_after_replacement)
            error = None
            try:
                if cleanup == "manifest":
                    module._remove_manifest_staging(
                        boundary, target, expected_identity=owned_identity
                    )
                else:
                    module._remove_classroom_entries(
                        boundary, {target: owned_identity}, label="test owned receipt"
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                error = exc
            assert reached == [replace_before_remove], "must reach the actual detach boundary"
            assert (owned_backup.read_bytes(), module._file_identity(owned_backup.stat())) == (
                owned_body,
                owned_identity,
            )
            assert (foreign_backup.read_bytes(), module._file_identity(foreign_backup.stat())) == (
                _FOREIGN_BODY,
                foreign_identity,
            )
            if replace_before_remove:
                assert target.exists(), "cleanup deleted the competing writer's replacement"
                assert (target.read_bytes(), module._file_identity(target.stat())) == (
                    _FOREIGN_BODY,
                    foreign_identity,
                )
                assert (
                    displaced_owned.read_bytes(),
                    module._file_identity(displaced_owned.stat()),
                ) == (owned_body, owned_identity)
                assert error is not None, "replacement must not be treated as owned cleanup"
            else:
                assert error is None
                assert not target.exists()
            records = [
                json.loads(path.read_bytes()) for path in recovery_root.glob("*.recovery.json")
            ]
            assert len(records) == 1
            assert records[0]["state"] == ("restored" if replace_before_remove else "removed")
            assert records[0]["expectedIdentity"] == list(owned_identity)
            assert not Path(records[0]["recoveryPath"]).exists()
    finally:
        boundary.close()
