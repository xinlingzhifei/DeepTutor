from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from functools import cache
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

ROOT = Path(__file__).resolve().parents[2]


@cache
def _module():
    path = ROOT / "scripts" / "openmaic_observer_attestation_signer.py"
    spec = importlib.util.spec_from_file_location(
        "openmaic_observer_attestation_signer_under_test",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_signing_pair(tmp_path: Path) -> tuple[Path, Path, Ed25519PrivateKey]:
    attestation_path = tmp_path / "observer-attestation.json"
    signing_key_path = tmp_path / "observer-signing-key.pem"
    private_key = Ed25519PrivateKey.generate()
    attestation_path.write_bytes(
        json.dumps(
            {
                "schemaVersion": 2,
                "producer": "openmaic-shared-ingress-observer",
                "releaseRun": {
                    "runId": "release-a",
                    "environmentId": "environment-a",
                },
                "observedAt": "2026-09-01T00:00:00Z",
                "observer": {
                    "observerId": "observer-a",
                    "observerUrl": "https://observer.example.test",
                    "sharedIngressControlUrl": (
                        "https://shared-ingress.example.test/v1/control-canaries"
                    ),
                    "candidateHealthUrl": (
                        "https://candidate.example.test/"
                        "api/v1/system/teaching-health/observer-canary"
                    ),
                    "sharedIngressUpstreamUrl": "https://upstream.example.test",
                    "internalProxyUrl": "http://openmaic-shared-ingress-observer:8000",
                    "authExpiresAt": "2026-09-01T00:10:00Z",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    signing_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    signing_key_path.chmod(0o600)
    return attestation_path, signing_key_path, private_key


def _signature_verifies(
    signature_path: Path,
    *,
    private_key: Ed25519PrivateKey,
    body: bytes,
) -> bool:
    try:
        signature = base64.b64decode(signature_path.read_bytes().strip(), validate=True)
        private_key.public_key().verify(signature, body)
    except (binascii.Error, InvalidSignature, OSError, ValueError):
        return False
    return True


def test_signer_refuses_candidate_independent_attestation_before_key_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, _private_key = _write_signing_pair(tmp_path)
    signature_path = tmp_path / "observer-attestation.sig"
    key_opened = False
    real_read_regular_file = module._read_regular_file

    def record_key_read(path: Path, **kwargs) -> bytes:
        nonlocal key_opened
        key_opened = True
        return real_read_regular_file(path, **kwargs)

    monkeypatch.setattr(module, "_read_regular_file", record_key_read)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_binding_invalid",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
            expected_release_run_id="release-a",
            expected_environment_id="environment-a",
            expected_observer_id="observer-a",
            expected_candidate_health_url=(
                "https://candidate.example.test/api/v1/system/teaching-health/observer-canary"
            ),
        )

    assert key_opened is False
    assert not signature_path.exists()


def test_signer_rejects_private_key_that_does_not_match_expected_public_key_before_signing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    contract = sys.modules["openmaic_smoke_contract"]
    attestation_path, signing_key_path, _private_key = _write_signing_pair(tmp_path)
    signature_path = tmp_path / "observer-attestation.sig"
    trusted_key = Ed25519PrivateKey.generate()
    expected_spki_sha256 = hashlib.sha256(
        trusted_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()
    report = json.loads(attestation_path.read_bytes())
    report["schemaVersion"] = 3
    report["candidateBinding"] = {
        "candidateSha256": "a" * 64,
        "imageLockSha256": "b" * 64,
    }
    report["attestationChallenge"] = contract.openmaic_observer_attestation_challenge(
        expected_spki_sha256
    )
    attestation_path.write_bytes(
        contract.canonical_openmaic_shared_ingress_observer_attestation(report)
    )
    loaded_keys: list[Ed25519PrivateKey] = []
    signed_bodies: list[bytes] = []
    staged_paths: list[Path] = []
    real_load_key = module.serialization.load_pem_private_key
    real_open = module.os.open

    class SigningKeyProbe(Ed25519PrivateKey):
        def __init__(self, key: Ed25519PrivateKey) -> None:
            self.key = key

        def public_key(self):
            return self.key.public_key()

        def private_bytes(self, encoding, format, encryption_algorithm):
            return self.key.private_bytes(encoding, format, encryption_algorithm)

        def private_bytes_raw(self):
            return self.key.private_bytes_raw()

        def __copy__(self):
            return self

        def __deepcopy__(self, memo):
            return self

        def sign(self, body: bytes) -> bytes:
            signed_bodies.append(body)
            return self.key.sign(body)

    def load_real_key_with_probe(body: bytes, password) -> Ed25519PrivateKey:
        key = real_load_key(body, password=password)
        assert isinstance(key, Ed25519PrivateKey)
        loaded_keys.append(key)
        return SigningKeyProbe(key)

    def record_staging_open(path, flags, *args, **kwargs) -> int:
        if Path(path).name.startswith(".observer-attestation.sig.staged-"):
            staged_paths.append(Path(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.serialization, "load_pem_private_key", load_real_key_with_probe)
    monkeypatch.setattr(module.os, "open", record_staging_open)

    with pytest.raises(module.ObserverAttestationSignerError) as failure:
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
            expected_release_run_id="release-a",
            expected_environment_id="environment-a",
            expected_observer_id="observer-a",
            expected_candidate_health_url=report["observer"]["candidateHealthUrl"],
            expected_candidate_sha256="a" * 64,
            expected_image_lock_sha256="b" * 64,
            expected_public_key_spki_sha256=expected_spki_sha256,
            trusted_now=datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc),
        )

    assert str(failure.value) == "observer_attestation_signing_key_invalid"
    assert len(loaded_keys) == 1
    assert signed_bodies == []
    assert staged_paths == []
    assert not signature_path.exists()


def test_signer_publishes_bound_attestation_for_expected_public_key(tmp_path: Path) -> None:
    module = _module()
    contract = sys.modules["openmaic_smoke_contract"]
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    signature_path = tmp_path / "observer-attestation.sig"
    spki_sha256 = hashlib.sha256(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()
    report = json.loads(attestation_path.read_bytes())
    report["schemaVersion"] = 3
    report["candidateBinding"] = {
        "candidateSha256": "a" * 64,
        "imageLockSha256": "b" * 64,
    }
    report["attestationChallenge"] = contract.openmaic_observer_attestation_challenge(spki_sha256)
    body = contract.canonical_openmaic_shared_ingress_observer_attestation(report)
    attestation_path.write_bytes(body)

    signature = module.sign_observer_attestation(
        attestation_path,
        signature_path,
        signing_key_path,
        expected_release_run_id="release-a",
        expected_environment_id="environment-a",
        expected_observer_id="observer-a",
        expected_candidate_health_url=report["observer"]["candidateHealthUrl"],
        expected_candidate_sha256="a" * 64,
        expected_image_lock_sha256="b" * 64,
        expected_public_key_spki_sha256=spki_sha256,
        trusted_now=datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc),
    )

    assert signature_path.read_bytes() == signature
    assert _signature_verifies(signature_path, private_key=private_key, body=body)
    assert tuple(tmp_path.glob(".observer-attestation.sig.staged-*")) == ()


def test_signer_does_not_publish_when_attestation_changes_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    original_body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    real_write = module.os.write
    changed = False

    def write_then_change_attestation(descriptor: int, body: bytes) -> int:
        nonlocal changed
        written = real_write(descriptor, body)
        if not changed:
            attestation_path.write_bytes(b'{"releaseRunId":"release-b"}\n')
            changed = True
        return written

    monkeypatch.setattr(module.os, "write", write_then_change_attestation)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_changed_during_signing",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert not signature_path.exists()
    staged_paths = tuple(tmp_path.glob(".observer-attestation.sig.staged-*"))
    assert staged_paths
    assert all(
        not _signature_verifies(
            staged_path,
            private_key=private_key,
            body=original_body,
        )
        for staged_path in staged_paths
    )


def test_signer_invalidates_signature_when_attestation_changes_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    original_body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    real_link = module.os.link

    def publish_then_change_attestation(
        staged_path: Path,
        target_path: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        real_link(
            staged_path,
            target_path,
            follow_symlinks=follow_symlinks,
        )
        attestation_path.write_bytes(b'{"releaseRunId":"release-b"}\n')

    monkeypatch.setattr(module.os, "link", publish_then_change_attestation)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_changed_during_signing",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert signature_path.exists()
    assert not _signature_verifies(
        signature_path,
        private_key=private_key,
        body=original_body,
    )
    assert not _signature_verifies(
        signature_path,
        private_key=private_key,
        body=attestation_path.read_bytes(),
    )
    staged_paths = tuple(tmp_path.glob(".observer-attestation.sig.staged-*"))
    assert staged_paths
    assert all(
        not _signature_verifies(
            staged_path,
            private_key=private_key,
            body=original_body,
        )
        for staged_path in staged_paths
    )


@pytest.mark.parametrize(
    "failure",
    ("staged_unlink", "final_identity", "final_fsync"),
)
def test_signer_never_leaves_verifiable_canonical_signature_after_post_publish_failure(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    published = False
    triggered = False
    real_link = module.os.link

    def link_then_mark_published(*args, **kwargs) -> None:
        nonlocal published
        real_link(*args, **kwargs)
        published = True

    monkeypatch.setattr(module.os, "link", link_then_mark_published)
    if failure == "staged_unlink":
        real_unlink = module.os.unlink

        def fail_first_staged_unlink(path, *args, **kwargs) -> None:
            nonlocal triggered
            if not triggered and Path(path).name.startswith(".observer-attestation.sig.staged-"):
                triggered = True
                raise OSError("staged-unlink-failure")
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(module.os, "unlink", fail_first_staged_unlink)
    elif failure == "final_identity":
        real_durable_identity = module._durable_identity
        identity_calls = 0

        def drift_final_identity(details):
            nonlocal identity_calls, triggered
            identity_calls += 1
            identity = real_durable_identity(details)
            if identity_calls == 2:
                triggered = True
                return (*identity, -1)
            return identity

        monkeypatch.setattr(module, "_durable_identity", drift_final_identity)
    else:
        real_fsync_directory = module._fsync_directory
        fsync_calls = 0

        def fail_final_fsync(path: Path) -> None:
            nonlocal fsync_calls, triggered
            fsync_calls += 1
            if fsync_calls == 2:
                triggered = True
                raise OSError("final-fsync-failure")
            real_fsync_directory(path)

        monkeypatch.setattr(module, "_fsync_directory", fail_final_fsync)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signature_publish_failed",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert published is True
    assert triggered is True
    assert not (
        signature_path.exists()
        and signature_path.stat().st_size > 0
        and _signature_verifies(
            signature_path,
            private_key=private_key,
            body=body,
        )
    )


def test_signer_invalidates_original_inode_but_reports_identity_lost_when_canonical_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    replacement_path = tmp_path / "replacement.sig"
    replacement_body = base64.b64encode(private_key.sign(body)) + b"\n"
    replacement_path.write_bytes(replacement_body)
    real_unlink = module.os.unlink
    real_replace = module.os.replace
    replaced = False

    def replace_canonical_then_fail_staged_unlink(path, *args, **kwargs) -> None:
        nonlocal replaced
        if not replaced and Path(path).name.startswith(".observer-attestation.sig.staged-"):
            real_replace(replacement_path, signature_path)
            replaced = True
            raise OSError("staged-unlink-after-canonical-replacement")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "unlink", replace_canonical_then_fail_staged_unlink)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signature_identity_lost",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert replaced is True
    assert signature_path.read_bytes() == replacement_body
    staged_paths = tuple(tmp_path.glob(".observer-attestation.sig.staged-*"))
    assert len(staged_paths) == 1
    assert staged_paths[0].read_bytes() == b""


def test_signer_uses_path_invalidation_when_publish_descriptor_close_releases_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    real_open = module.os.open
    real_close = module.os.close
    real_invalidate = module._invalidate_signature
    active_descriptors: set[int] = set()
    stale_close_attempts: list[int] = []
    stale_invalidation_attempts: list[int] = []
    staged_descriptor: int | None = None
    injected = False

    def record_open(path, flags, *args, **kwargs) -> int:
        nonlocal staged_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        active_descriptors.add(descriptor)
        if Path(path).name.startswith(".observer-attestation.sig.staged-"):
            staged_descriptor = descriptor
        return descriptor

    def close_staged_then_raise(descriptor: int) -> None:
        nonlocal injected
        if descriptor not in active_descriptors:
            stale_close_attempts.append(descriptor)
            raise OSError("stale-descriptor-close")
        real_close(descriptor)
        active_descriptors.remove(descriptor)
        if descriptor == staged_descriptor and not injected:
            injected = True
            raise OSError("publish-close-after-release")

    def record_invalidation(descriptor: int) -> None:
        try:
            module.os.fstat(descriptor)
        except OSError:
            stale_invalidation_attempts.append(descriptor)
        real_invalidate(descriptor)

    monkeypatch.setattr(module.os, "open", record_open)
    monkeypatch.setattr(module.os, "close", close_staged_then_raise)
    monkeypatch.setattr(module, "_invalidate_signature", record_invalidation)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signature_publish_failed",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert injected is True
    assert stale_invalidation_attempts == []
    assert stale_close_attempts == []
    assert signature_path.exists()
    assert not _signature_verifies(
        signature_path,
        private_key=private_key,
        body=body,
    )


def test_signer_invalidates_published_signature_when_attestation_close_releases_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    real_open = module.os.open
    real_close = module.os.close
    real_link = module.os.link
    active_descriptors: set[int] = set()
    stale_close_attempts: list[int] = []
    attestation_descriptor: int | None = None
    published = False
    injected = False

    def record_open(path, flags, *args, **kwargs) -> int:
        nonlocal attestation_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        active_descriptors.add(descriptor)
        if Path(path) == attestation_path:
            attestation_descriptor = descriptor
        return descriptor

    def link_then_mark_published(*args, **kwargs) -> None:
        nonlocal published
        real_link(*args, **kwargs)
        published = True

    def close_attestation_then_raise(descriptor: int) -> None:
        nonlocal injected
        if descriptor not in active_descriptors:
            stale_close_attempts.append(descriptor)
            raise OSError("stale-descriptor-close")
        real_close(descriptor)
        active_descriptors.remove(descriptor)
        if descriptor == attestation_descriptor and published and not injected:
            injected = True
            raise OSError("attestation-close-after-release")

    monkeypatch.setattr(module.os, "open", record_open)
    monkeypatch.setattr(module.os, "link", link_then_mark_published)
    monkeypatch.setattr(module.os, "close", close_attestation_then_raise)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signing_input_invalid",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert injected is True
    assert stale_close_attempts == []
    assert active_descriptors == set()
    assert signature_path.exists()
    assert not _signature_verifies(
        signature_path,
        private_key=private_key,
        body=body,
    )


def test_signer_reports_identity_lost_when_canonical_changes_while_publish_descriptor_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    foreign_path = tmp_path / "foreign.sig"
    foreign_path.write_bytes(b"foreign-signature\n")
    real_link = module.os.link
    path_type = type(signature_path)
    real_lstat = path_type.lstat
    published = False

    def link_then_mark_published(*args, **kwargs) -> None:
        nonlocal published
        real_link(*args, **kwargs)
        published = True

    def report_foreign_canonical(self: Path, *args, **kwargs):
        if published and self == signature_path:
            return real_lstat(foreign_path, *args, **kwargs)
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", link_then_mark_published)
    monkeypatch.setattr(path_type, "lstat", report_foreign_canonical)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signature_identity_lost",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert published is True
    assert foreign_path.read_bytes() == b"foreign-signature\n"
    assert not _signature_verifies(
        signature_path,
        private_key=private_key,
        body=body,
    )


def test_signer_reports_identity_lost_when_post_invalidation_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, _private_key = _write_signing_pair(tmp_path)
    signature_path = tmp_path / "observer-attestation.sig"
    foreign_path = tmp_path / "foreign.sig"
    foreign_body = b"foreign-signature\n"
    foreign_path.write_bytes(foreign_body)
    real_open = module.os.open
    real_link = module.os.link
    real_fstat = module.os.fstat
    real_invalidate = module._invalidate_signature
    path_type = type(signature_path)
    real_lstat = path_type.lstat
    staged_descriptor: int | None = None
    published = False
    invalidated = False

    def record_open(path, flags, *args, **kwargs) -> int:
        nonlocal staged_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path).name.startswith(".observer-attestation.sig.staged-"):
            staged_descriptor = descriptor
        return descriptor

    def link_then_mark_published(*args, **kwargs) -> None:
        nonlocal published
        real_link(*args, **kwargs)
        published = True

    def report_foreign_canonical(self: Path, *args, **kwargs):
        if published and self == signature_path:
            return real_lstat(foreign_path, *args, **kwargs)
        return real_lstat(self, *args, **kwargs)

    def invalidate_then_mark(descriptor: int) -> None:
        nonlocal invalidated
        real_invalidate(descriptor)
        if descriptor == staged_descriptor:
            invalidated = True

    def fail_post_invalidation_fstat(descriptor: int):
        if invalidated and descriptor == staged_descriptor:
            raise OSError("post-invalidation-fstat-failed")
        return real_fstat(descriptor)

    monkeypatch.setattr(module.os, "open", record_open)
    monkeypatch.setattr(module.os, "link", link_then_mark_published)
    monkeypatch.setattr(module.os, "fstat", fail_post_invalidation_fstat)
    monkeypatch.setattr(module, "_invalidate_signature", invalidate_then_mark)
    monkeypatch.setattr(path_type, "lstat", report_foreign_canonical)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signature_identity_lost",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert published is True
    assert invalidated is True
    assert foreign_path.read_bytes() == foreign_body


def test_signer_preserves_identity_lost_when_invalidation_and_close_both_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, _private_key = _write_signing_pair(tmp_path)
    signature_path = tmp_path / "observer-attestation.sig"
    real_open = module.os.open
    real_close = module.os.close
    staged_descriptor: int | None = None
    invalidation_attempted = False
    close_failed_after_release = False
    fsync_failed = False

    def record_open(path, flags, *args, **kwargs) -> int:
        nonlocal staged_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path).name.startswith(".observer-attestation.sig.staged-"):
            staged_descriptor = descriptor
        return descriptor

    def fail_published_invalidation(_descriptor: int) -> None:
        nonlocal invalidation_attempted
        invalidation_attempted = True
        raise module.ObserverAttestationSignerError(
            "observer_attestation_signature_invalidation_failed"
        )

    def fail_first_directory_sync(_path: Path) -> None:
        nonlocal fsync_failed
        if not fsync_failed:
            fsync_failed = True
            raise OSError("post-link-directory-sync-failed")

    def close_staged_then_raise(descriptor: int) -> None:
        nonlocal close_failed_after_release
        real_close(descriptor)
        if descriptor == staged_descriptor and not close_failed_after_release:
            close_failed_after_release = True
            raise OSError("staged-close-after-release")

    monkeypatch.setattr(module.os, "open", record_open)
    monkeypatch.setattr(module.os, "close", close_staged_then_raise)
    monkeypatch.setattr(module, "_invalidate_signature", fail_published_invalidation)
    monkeypatch.setattr(module, "_fsync_directory", fail_first_directory_sync)

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_signature_identity_lost",
    ):
        module.sign_observer_attestation(
            attestation_path,
            signature_path,
            signing_key_path,
        )

    assert fsync_failed is True
    assert invalidation_attempted is True
    assert close_failed_after_release is True


def test_signer_success_removes_staging_alias_and_syncs_publication_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    attestation_path, signing_key_path, private_key = _write_signing_pair(tmp_path)
    body = attestation_path.read_bytes()
    signature_path = tmp_path / "observer-attestation.sig"
    synced: list[Path] = []
    real_sync = module._fsync_directory

    def record_sync(path: Path) -> None:
        synced.append(Path(path))
        real_sync(path)

    monkeypatch.setattr(module, "_fsync_directory", record_sync)

    module.sign_observer_attestation(attestation_path, signature_path, signing_key_path)

    assert _signature_verifies(signature_path, private_key=private_key, body=body)
    assert tuple(tmp_path.glob(".observer-attestation.sig.staged-*")) == ()
    assert tmp_path in synced


def test_signer_rejects_attestation_without_release_run_environment_and_observer_binding(
    tmp_path: Path,
) -> None:
    module = _module()
    attestation_path, signing_key_path, _private_key = _write_signing_pair(tmp_path)
    signature_path = tmp_path / "observer-attestation.sig"
    attestation_path.write_bytes(b'{"releaseRunId":"release-a"}\n')

    with pytest.raises(
        module.ObserverAttestationSignerError,
        match="observer_attestation_binding_invalid",
    ):
        module.sign_observer_attestation(attestation_path, signature_path, signing_key_path)

    assert not signature_path.exists()
    assert tuple(tmp_path.glob(".observer-attestation.sig.staged-*")) == ()
