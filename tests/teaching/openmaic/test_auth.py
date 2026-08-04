from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pydantic import SecretStr
import pytest

from deeptutor.teaching.openmaic.auth import (
    MAX_CLOCK_SKEW_SECONDS,
    MountedServiceSecretResolver,
    PrehashedServiceRequest,
    ServiceRequest,
    ServiceSecretAccessDenied,
    ServiceSecretUnavailable,
    canonical_prehashed_service_request,
    canonical_service_request,
    read_service_secret,
    sign_service_request,
    signed_prehashed_service_headers,
    signed_service_headers,
    verify_service_request,
)
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection


def test_service_signature_matches_the_overlay_seven_line_contract() -> None:
    request = ServiceRequest(
        method="post",
        path="/api/yfeistai/v1/outlines",
        tenant_id="tenant-a",
        job_id="job-1",
        timestamp=1_770_000_000,
        idempotency_key="idem-1",
        body='{"message":"你好"}'.encode(),
    )

    assert canonical_service_request(request) == (
        "POST\n"
        "/api/yfeistai/v1/outlines\n"
        "tenant-a\n"
        "job-1\n"
        "1770000000\n"
        "idem-1\n"
        "c9e2af2cd43caf5443003f9a156dfef55714f86fe7720635e2fdb6879aecb3ec"
    )
    assert (
        sign_service_request(
            request,
            SecretStr("shared-service-secret"),
        )
        == "a6342464248da52628354a8a3d1a4836fde14da5d5efc5733d6300f40cce5bf7"
    )


def test_signed_headers_bind_every_overlay_auth_field() -> None:
    request = ServiceRequest(
        method="GET",
        path="/api/yfeistai/v1/outlines/job-1",
        tenant_id="tenant-a",
        job_id="job-1",
        timestamp=1_770_000_001,
        idempotency_key="",
        body=b"",
    )

    headers = signed_service_headers(request, SecretStr("service-secret"))

    assert headers == {
        "x-yfeistai-tenant-id": "tenant-a",
        "x-yfeistai-job-id": "job-1",
        "x-yfeistai-timestamp": "1770000001",
        "x-yfeistai-idempotency-key": "",
        "x-yfeistai-signature": sign_service_request(
            request,
            SecretStr("service-secret"),
        ),
    }


def test_prehashed_signature_binds_stream_digest_without_buffering_the_body() -> None:
    request = PrehashedServiceRequest(
        method="PUT",
        path="/api/yfeistai/v1/export-inputs/job-1/files/file-1",
        tenant_id="tenant-a",
        job_id="job-1",
        timestamp=1_770_000_001,
        idempotency_key="idem-1",
        body_sha256="a" * 64,
    )

    assert canonical_prehashed_service_request(request) == (
        "PUT\n"
        "/api/yfeistai/v1/export-inputs/job-1/files/file-1\n"
        "tenant-a\n"
        "job-1\n"
        "1770000001\n"
        "idem-1\n"
        f"{'a' * 64}"
    )
    headers = signed_prehashed_service_headers(
        request,
        SecretStr("service-secret"),
    )

    assert headers["x-yfeistai-content-sha256"] == "a" * 64
    assert len(headers["x-yfeistai-signature"]) == 64


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64])
def test_prehashed_signature_rejects_a_noncanonical_digest(digest: str) -> None:
    request = PrehashedServiceRequest(
        method="PUT",
        path="/api/yfeistai/v1/export-inputs/job-1/files/file-1",
        tenant_id="tenant-a",
        job_id="job-1",
        timestamp=1_770_000_001,
        idempotency_key="idem-1",
        body_sha256=digest,
    )

    with pytest.raises(ValueError):
        canonical_prehashed_service_request(request)


def test_service_signature_uses_the_overlay_sixty_second_validity_window() -> None:
    request = ServiceRequest(
        method="GET",
        path="/api/yfeistai/v1/outlines/job-1",
        tenant_id="tenant-a",
        job_id="job-1",
        timestamp=1_770_000_000,
        idempotency_key="",
        body=b"",
    )
    signature = sign_service_request(request, SecretStr("service-secret"))

    assert MAX_CLOCK_SKEW_SECONDS == 60
    assert verify_service_request(
        request,
        signature,
        SecretStr("service-secret"),
        now_seconds=request.timestamp + 60,
    )
    assert not verify_service_request(
        request,
        signature,
        SecretStr("service-secret"),
        now_seconds=request.timestamp + 61,
    )
    assert not verify_service_request(
        replace(request, body=b"tampered"),
        signature,
        SecretStr("service-secret"),
        now_seconds=request.timestamp,
    )


def test_verifier_returns_false_for_malformed_signed_fields() -> None:
    request = ServiceRequest(
        method="GET",
        path="/api/yfeistai/v1/outlines/job-1",
        tenant_id="tenant-a",
        job_id="job-1",
        timestamp="not-an-integer",  # type: ignore[arg-type]
        idempotency_key="",
        body=b"",
    )

    assert not verify_service_request(
        request,
        "0" * 64,
        SecretStr("service-secret"),
        now_seconds=1_770_000_000,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "relative"),
        ("tenant_id", "tenant-a\nforged"),
        ("job_id", ""),
        ("idempotency_key", ""),
    ],
)
def test_write_signature_rejects_noncanonical_or_missing_fields(
    field: str,
    value: str,
) -> None:
    values = {
        "method": "POST",
        "path": "/api/yfeistai/v1/outlines",
        "tenant_id": "tenant-a",
        "job_id": "job-1",
        "timestamp": 1_770_000_000,
        "idempotency_key": "idem-1",
        "body": b"{}",
    }
    values[field] = value

    with pytest.raises(ValueError):
        canonical_service_request(ServiceRequest(**values))


def test_service_secret_is_read_from_a_nonsymlink_mounted_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "openmaic_service_secret"
    secret_file.write_text("mounted-secret\n", encoding="utf-8")

    secret = read_service_secret(secret_file)

    assert secret.get_secret_value() == "mounted-secret"
    assert "mounted-secret" not in repr(secret)


def test_invalid_utf8_secret_is_not_retained_in_the_exception_chain(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "openmaic_service_secret"
    secret_file.write_bytes(b"\xffRAW_SECRET_SENTINEL")

    with pytest.raises(ServiceSecretUnavailable) as captured:
        read_service_secret(secret_file)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_service_secret_rejects_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("must-not-follow", encoding="utf-8")
    link = tmp_path / "openmaic_service_secret"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ServiceSecretUnavailable):
        read_service_secret(link)


def test_service_secret_rejects_a_symlinked_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "real-mount"
    target.mkdir()
    (target / "openmaic_service_secret").write_text(
        "must-not-follow",
        encoding="utf-8",
    )
    link = tmp_path / "linked-mount"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ServiceSecretUnavailable):
        read_service_secret(link / "openmaic_service_secret")


def test_mounted_service_secret_is_bound_to_the_worker_route(tmp_path: Path) -> None:
    secret_file = tmp_path / "openmaic_service_secret"
    secret_file.write_text("route-service-secret", encoding="utf-8")
    resolver = MountedServiceSecretResolver(
        secret_file,
        runtime_mode="shared",
        runtime_route_id="shared-primary",
    )

    secret = resolver.resolve(
        DataPlaneSelection(
            tenant_id="tenant-standard",
            route_ref="shared-primary",
            provider_profile_ref="platform-default",
            mode="shared",
            worker_pool_ref="shared-generation",
            queue_ref="openmaic.shared",
        )
    )

    assert secret.get_secret_value() == "route-service-secret"


def test_dedicated_service_secret_rejects_cross_tenant_before_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[Path] = []

    def tracked_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        raise AssertionError("cross-tenant selection must not read the mount")

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    resolver = MountedServiceSecretResolver(
        tmp_path / "openmaic_service_secret",
        runtime_mode="dedicated",
        runtime_route_id="dedicated-tenant-a",
        runtime_tenant_id="tenant-a",
    )

    with pytest.raises(ServiceSecretAccessDenied):
        resolver.resolve(
            DataPlaneSelection(
                tenant_id="tenant-b",
                route_ref="dedicated-tenant-a",
                provider_profile_ref="provider-tenant-b",
                mode="dedicated",
                worker_pool_ref="generation-tenant-b",
                queue_ref="openmaic.tenant-b",
            )
        )

    assert reads == []
