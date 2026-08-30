from __future__ import annotations

from datetime import datetime, timezone
from functools import cache
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


@cache
def _backup_module():
    path = ROOT / "scripts" / "backup_teaching.py"
    spec = importlib.util.spec_from_file_location("backup_teaching_for_source_provenance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def _module():
    path = ROOT / "scripts" / "produce_backup_restore_source_provenance.py"
    assert path.is_file(), "backup/restore source provenance producer is missing"
    spec = importlib.util.spec_from_file_location(
        "backup_restore_source_provenance_under_test",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": "c" * 40,
        "releaseTag": "yfeistai-first-release-20260831-cccccccc",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "d" * 64,
            "openmaic": "sha256:" + "e" * 64,
            "openmaic_render": "sha256:" + "f" * 64,
        },
    }


def _release_run() -> dict[str, str]:
    return {
        "runId": "run-backup-restore-01",
        "environmentId": "environment-backup-restore-01",
    }


def _backup_directory(tmp_path: Path) -> Path:
    backup = _backup_module()
    source_root = tmp_path / "source"
    source_root.mkdir(exist_ok=True)
    directory = source_root / "source-backup"
    directory.mkdir()
    database_dump = directory / "database.dump"
    database_dump.write_bytes(b"source database dump")
    backup.write_backup_manifest(
        directory,
        database_dump=database_dump,
        database_identity_sha256="1" * 64,
        object_inventory=(),
        source_object_store_namespace_id="source-namespace",
        source_object_store_bucket="source-bucket",
        source_object_store_identity_sha256="2" * 64,
        platform_schema_revision="20260831_0001",
        schema_revisions={"tenant-a": "20260831_0001"},
        classroom_versions_count=1,
        learning_events_count=2,
        created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    return directory


def _output_path(tmp_path: Path) -> Path:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(exist_ok=True)
    return evidence_root / "source-provenance.json"


def test_source_provenance_producer_binds_verified_archive_candidate_and_release_run(
    tmp_path: Path,
) -> None:
    producer = _module()
    candidate = _candidate()
    release_run = _release_run()
    backup_directory = _backup_directory(tmp_path)
    output = _output_path(tmp_path)

    result = producer.produce_backup_restore_source_provenance(
        producer.SourceProvenanceConfig(
            candidate=candidate,
            release_run=release_run,
            backup_directory=backup_directory,
            output_path=output,
        )
    )

    verified = _backup_module().load_verified_backup(backup_directory)
    expected = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": release_run,
        "source": {
            "manifestSha256": verified.manifest_sha256,
            "archiveFingerprintSha256": verified.archive_fingerprint_sha256,
            "databaseIdentitySha256": verified.manifest.database.identity_sha256,
            "objectStoreIdentitySha256": verified.manifest.source_object_store_identity_sha256,
        },
    }
    assert result == output.resolve()
    assert output.read_bytes() == _canonical(expected)
    assert hashlib.sha256(output.read_bytes()).hexdigest() != "0" * 64


def test_source_provenance_producer_rechecks_archive_and_publishes_nothing_on_drift(
    tmp_path: Path,
) -> None:
    producer = _module()
    backup_directory = _backup_directory(tmp_path)
    output = _output_path(tmp_path)
    backup = _backup_module().load_verified_backup(backup_directory)
    rechecked: list[object] = []

    def reject_drift(value: object) -> object:
        rechecked.append(value)
        raise ValueError("backup changed after verification")

    with pytest.raises(ValueError, match="backup changed after verification"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=backup_directory,
                output_path=output,
            ),
            verified_backup_loader=lambda _path: backup,
            verified_backup_rechecker=reject_drift,
        )

    assert rechecked == [backup]
    assert not output.exists()


def test_source_provenance_producer_never_overwrites_existing_output(tmp_path: Path) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    original = b"pre-existing audit evidence\n"
    output.write_bytes(original)

    with pytest.raises(FileExistsError):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )

    assert output.read_bytes() == original


def test_source_provenance_cli_rejects_noncanonical_identity_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _module()
    candidate_file = tmp_path / "candidate.json"
    release_run_file = tmp_path / "release-run.json"
    candidate_file.write_text(json.dumps(_candidate(), indent=2), encoding="utf-8")
    release_run_file.write_bytes(_canonical(_release_run()))
    output = _output_path(tmp_path)

    result = producer.main(
        [
            "--candidate",
            str(candidate_file),
            "--release-run",
            str(release_run_file),
            "--backup-dir",
            str(_backup_directory(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "candidate must be canonical JSON\n"
    assert not output.exists()


def test_source_provenance_cli_rejects_symlinked_identity_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _module()
    real_candidate = tmp_path / "candidate-real.json"
    candidate_link = tmp_path / "candidate.json"
    release_run_file = tmp_path / "release-run.json"
    real_candidate.write_bytes(_canonical(_candidate()))
    candidate_link.symlink_to(real_candidate)
    release_run_file.write_bytes(_canonical(_release_run()))
    output = _output_path(tmp_path)

    result = producer.main(
        [
            "--candidate",
            str(candidate_link),
            "--release-run",
            str(release_run_file),
            "--backup-dir",
            str(_backup_directory(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "candidate path chain is unsafe\n"
    assert not output.exists()


@pytest.mark.parametrize(
    ("backup_directory", "output_path"),
    (
        (Path("relative-source"), Path("C:/absolute/source-provenance.json")),
        (Path("C:/absolute/source-backup"), Path("relative-output.json")),
    ),
)
def test_source_provenance_producer_requires_absolute_paths_before_archive_read(
    backup_directory: Path,
    output_path: Path,
) -> None:
    producer = _module()

    with pytest.raises(ValueError, match="paths must be absolute"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=backup_directory,
                output_path=output_path,
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )


def test_source_provenance_producer_requires_output_outside_source_archive(
    tmp_path: Path,
) -> None:
    producer = _module()
    backup_directory = _backup_directory(tmp_path)
    output = backup_directory / "source-provenance.json"

    with pytest.raises(ValueError, match="output must be outside the source archive"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=backup_directory,
                output_path=output,
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )

    assert not output.exists()


def test_source_provenance_producer_rejects_symlinked_source_before_archive_read(
    tmp_path: Path,
) -> None:
    producer = _module()
    backup_directory = _backup_directory(tmp_path)
    linked_backup = tmp_path / "source-backup-link"
    linked_backup.symlink_to(backup_directory, target_is_directory=True)
    output = _output_path(tmp_path)

    with pytest.raises(ValueError, match="source archive path chain is unsafe"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=linked_backup,
                output_path=output,
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )

    assert not output.exists()


def test_source_provenance_producer_rejects_symlinked_output_parent_before_archive_read(
    tmp_path: Path,
) -> None:
    producer = _module()
    real_output_parent = tmp_path / "real-evidence"
    real_output_parent.mkdir()
    linked_output_parent = tmp_path / "evidence"
    linked_output_parent.symlink_to(real_output_parent, target_is_directory=True)
    output = linked_output_parent / "source-provenance.json"

    with pytest.raises(ValueError, match="output parent path chain is unsafe"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )

    assert not (real_output_parent / output.name).exists()


def test_source_provenance_producer_uses_atomic_no_replace_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    real_link = producer.os.link
    commits: list[tuple[Path, Path, bytes]] = []

    def recording_link(source: str | Path, destination: str | Path) -> None:
        staging = Path(source)
        target = Path(destination)
        assert staging.parent == output.parent
        assert staging.name.startswith(f".{output.name}.")
        assert staging.name.endswith(".tmp")
        assert not target.exists()
        commits.append((staging, target, staging.read_bytes()))
        real_link(staging, target)

    monkeypatch.setattr(producer.os, "link", recording_link)
    producer.produce_backup_restore_source_provenance(
        producer.SourceProvenanceConfig(
            candidate=_candidate(),
            release_run=_release_run(),
            backup_directory=_backup_directory(tmp_path),
            output_path=output,
        )
    )

    assert len(commits) == 1
    staging, target, committed_body = commits[0]
    assert target == output
    assert output.read_bytes() == committed_body
    assert not staging.exists()
    assert tuple(output.parent.glob(f".{output.name}.*.tmp")) == ()


def test_source_provenance_cli_rejects_identity_file_changed_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _module()
    candidate_file = tmp_path / "candidate.json"
    release_run_file = tmp_path / "release-run.json"
    candidate_file.write_bytes(_canonical(_candidate()))
    release_run_file.write_bytes(_canonical(_release_run()))
    output = _output_path(tmp_path)
    real_fstat = producer.os.fstat
    fstat_calls = 0

    def drift_during_read(descriptor: int):
        nonlocal fstat_calls
        observed = real_fstat(descriptor)
        fstat_calls += 1
        if fstat_calls != 2:
            return observed
        return SimpleNamespace(
            st_dev=observed.st_dev,
            st_ino=observed.st_ino,
            st_mode=observed.st_mode,
            st_size=observed.st_size,
            st_mtime_ns=observed.st_mtime_ns + 1,
        )

    monkeypatch.setattr(producer.os, "fstat", drift_during_read)
    result = producer.main(
        [
            "--candidate",
            str(candidate_file),
            "--release-run",
            str(release_run_file),
            "--backup-dir",
            str(_backup_directory(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "candidate changed while reading\n"
    assert not output.exists()


def test_source_provenance_producer_reports_directory_fsync_failure_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    monkeypatch.setattr(
        producer,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("directory fsync failed")),
    )

    with pytest.raises(RuntimeError, match="committed.*directory") as caught:
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )

    assert type(caught.value).__name__ == "SourceProvenanceCommittedError"
    assert caught.value.output_path == output
    assert output.is_file()


def test_source_provenance_producer_rejects_output_parent_containing_source_archive(
    tmp_path: Path,
) -> None:
    producer = _module()
    backup_directory = _backup_directory(tmp_path)
    output = tmp_path / "source-provenance.json"

    with pytest.raises(ValueError, match="source and output paths must be disjoint"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=backup_directory,
                output_path=output,
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )

    assert not output.exists()


def test_source_provenance_producer_rejects_reparse_point_in_source_path_chain(
    tmp_path: Path,
) -> None:
    producer = _module()
    real_root = tmp_path / "real-source-root"
    real_root.mkdir()
    backup_directory = _backup_directory(real_root)
    linked_root = tmp_path / "linked-source-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="source archive path chain is unsafe"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=linked_root / backup_directory.relative_to(real_root),
                output_path=_output_path(tmp_path),
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )


def test_source_provenance_producer_rejects_reparse_point_in_output_path_chain(
    tmp_path: Path,
) -> None:
    producer = _module()
    real_root = tmp_path / "real-evidence-root"
    child = real_root / "run"
    child.mkdir(parents=True)
    linked_root = tmp_path / "linked-evidence-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    output = linked_root / "run" / "source-provenance.json"

    with pytest.raises(ValueError, match="output parent path chain is unsafe"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            ),
            verified_backup_loader=lambda _path: pytest.fail("archive must not be read"),
        )

    assert not (child / output.name).exists()


def test_source_provenance_producer_rechecks_immediately_before_commit(
    tmp_path: Path,
) -> None:
    producer = _module()
    backup_directory = _backup_directory(tmp_path)
    backup = _backup_module().load_verified_backup(backup_directory)
    output = _output_path(tmp_path)
    calls = 0

    def recheck(value: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("backup changed immediately before commit")
        return value

    with pytest.raises(ValueError, match="immediately before commit"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=backup_directory,
                output_path=output,
            ),
            verified_backup_loader=lambda _path: backup,
            verified_backup_rechecker=recheck,
        )

    assert calls == 2
    assert not output.exists()


def test_source_provenance_producer_reports_postcommit_archive_drift_as_committed(
    tmp_path: Path,
) -> None:
    producer = _module()
    backup_directory = _backup_directory(tmp_path)
    backup = _backup_module().load_verified_backup(backup_directory)
    output = _output_path(tmp_path)
    calls = 0

    def recheck(value: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("backup changed after commit")
        return value

    with pytest.raises(RuntimeError, match="committed.*archive") as caught:
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=backup_directory,
                output_path=output,
            ),
            verified_backup_loader=lambda _path: backup,
            verified_backup_rechecker=recheck,
        )

    assert type(caught.value).__name__ == "SourceProvenanceCommittedError"
    assert caught.value.output_path == output
    assert calls == 3
    assert output.is_file()


def test_source_provenance_producer_reconciles_committed_target_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    real_link = producer.os.link

    def tampering_link(source: str | Path, destination: str | Path) -> None:
        real_link(source, destination)
        Path(destination).write_bytes(b"tampered after link\n")

    monkeypatch.setattr(producer.os, "link", tampering_link)
    with pytest.raises(RuntimeError, match="committed.*target") as caught:
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )

    assert type(caught.value).__name__ == "SourceProvenanceCommittedError"
    assert caught.value.output_path == output


def test_source_provenance_producer_classifies_target_read_failure_as_committed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    real_read_bytes = producer.Path.read_bytes

    def fail_target_read(path: Path) -> bytes:
        if path == output:
            raise PermissionError("access denied")
        return real_read_bytes(path)

    monkeypatch.setattr(producer.Path, "read_bytes", fail_target_read)
    with pytest.raises(RuntimeError, match="committed.*target") as caught:
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )

    assert type(caught.value).__name__ == "SourceProvenanceCommittedError"
    assert caught.value.output_path == output


def test_source_provenance_producer_staging_cleanup_failure_does_not_reverse_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    real_unlink = producer.Path.unlink

    def fail_staging_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(f".{output.name}.") and path.name.endswith(".tmp"):
            raise OSError("staging cleanup failed")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(producer.Path, "unlink", fail_staging_unlink)
    with pytest.warns(RuntimeWarning, match="staging cleanup failed"):
        result = producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )

    assert result == output
    assert output.is_file()
    assert len(tuple(output.parent.glob(f".{output.name}.*.tmp"))) == 1


def test_source_provenance_producer_precommit_cleanup_failure_preserves_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)

    monkeypatch.setattr(
        producer.os,
        "link",
        lambda _source, _target: (_ for _ in ()).throw(FileExistsError("commit collision")),
    )
    monkeypatch.setattr(
        producer.Path,
        "unlink",
        lambda _path, *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(FileExistsError, match="commit collision") as caught:
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )

    assert any("cleanup failed" in note for note in getattr(caught.value, "__notes__", ()))
    assert not output.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership contract")
def test_source_provenance_producer_rejects_world_writable_output_parent(
    tmp_path: Path,
) -> None:
    producer = _module()
    output = _output_path(tmp_path)
    output.parent.chmod(0o777)

    with pytest.raises(ValueError, match="output parent is not restricted"):
        producer.produce_backup_restore_source_provenance(
            producer.SourceProvenanceConfig(
                candidate=_candidate(),
                release_run=_release_run(),
                backup_directory=_backup_directory(tmp_path),
                output_path=output,
            )
        )


def test_source_provenance_cli_reports_committed_failure_with_distinct_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _module()
    candidate_file = tmp_path / "candidate.json"
    release_run_file = tmp_path / "release-run.json"
    candidate_file.write_bytes(_canonical(_candidate()))
    release_run_file.write_bytes(_canonical(_release_run()))
    output = _output_path(tmp_path)
    monkeypatch.setattr(
        producer,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("directory fsync failed")),
    )

    result = producer.main(
        [
            "--candidate",
            str(candidate_file),
            "--release-run",
            str(release_run_file),
            "--backup-dir",
            str(_backup_directory(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    stderr = capsys.readouterr().err
    assert "committed" in stderr
    assert str(output) in stderr
    assert output.is_file()


def test_source_provenance_cli_rejects_reparse_point_in_identity_ancestor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _module()
    real_identity_root = tmp_path / "real-identities"
    real_identity_root.mkdir()
    candidate_file = real_identity_root / "candidate.json"
    candidate_file.write_bytes(_canonical(_candidate()))
    linked_identity_root = tmp_path / "linked-identities"
    linked_identity_root.symlink_to(real_identity_root, target_is_directory=True)
    release_run_file = tmp_path / "release-run.json"
    release_run_file.write_bytes(_canonical(_release_run()))
    output = _output_path(tmp_path)

    result = producer.main(
        [
            "--candidate",
            str(linked_identity_root / candidate_file.name),
            "--release-run",
            str(release_run_file),
            "--backup-dir",
            str(_backup_directory(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "candidate path chain is unsafe\n"
    assert not output.exists()
