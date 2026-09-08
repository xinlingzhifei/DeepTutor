from __future__ import annotations

import base64
from functools import cache
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
import warnings
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
SOURCE_HEAD = "a" * 40
RELEASE_TAG = f"yfeistai-first-release-20260825-{SOURCE_HEAD[:8]}"
OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
OBSERVER_SIGNER_NAME = "observer_signer"
OBSERVER_SIGNER_REPOSITORY = "ghcr.io/xinlingzhifei/yfeistai-observer-signer"
OBSERVER_OVERLAY_NAME = "docker-compose.openmaic-shared-ingress-observer.yml"
CHECKOUT_ACTION = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
SETUP_BUILDX_ACTION = "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f"
LOGIN_ACTION = "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9"
BUILD_PUSH_ACTION = "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8"
WORKFLOW_IMAGE_REPOSITORIES = {
    "build_deeptutor": "ghcr.io/xinlingzhifei/deeptutor",
    "build_openmaic": "ghcr.io/xinlingzhifei/openmaic",
    "build_openmaic_render": "ghcr.io/xinlingzhifei/openmaic-render",
    "build_observer_signer": OBSERVER_SIGNER_REPOSITORY,
}
WORKFLOW_DIGEST_ENV = {
    "DEEPTUTOR_DIGEST": "${{ steps.build_deeptutor.outputs.digest }}",
    "OPENMAIC_DIGEST": "${{ steps.build_openmaic.outputs.digest }}",
    "OPENMAIC_RENDER_DIGEST": "${{ steps.build_openmaic_render.outputs.digest }}",
    "OBSERVER_SIGNER_DIGEST": "${{ steps.build_observer_signer.outputs.digest }}",
}
WORKFLOW_STAGING_TAG = "staging-${{ github.run_id }}-${{ github.run_attempt }}"
GITHUB_RUN_ID = "123456789"
GITHUB_WORKFLOW_REF = (
    f"{SOURCE_REPOSITORY}/.github/workflows/private-platform-images.yml@refs/tags/{RELEASE_TAG}"
)
GITHUB_WORKFLOW_PATH = ".github/workflows/private-platform-images.yml"
# REST workflow-run `path` is not the ref-qualified GITHUB_WORKFLOW_REF.
GITHUB_WORKFLOW_RUN_PATH = GITHUB_WORKFLOW_PATH
PROMOTION_ARTIFACT_NAME = f"private-platform-image-lock-{RELEASE_TAG}-{SOURCE_HEAD}"
PROMOTION_ARCHIVE_MEMBERS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    f"deploy/{OBSERVER_OVERLAY_NAME}",
    "artifacts/source_head.json",
    "artifacts/image_digests.json",
    "artifacts/promotion_intent.json",
    "artifacts/promotion_receipt.json",
)
PROMOTION_ARCHIVE_MAX_MEMBER_BYTES = 4 * 1024 * 1024
PROMOTION_ARCHIVE_MAX_TOTAL_BYTES = 8 * 1024 * 1024
PROMOTION_ARCHIVE_MAX_COMPRESSION_RATIO = 100
PUBLISH_JOURNAL_OWNER = "yfeistai-image-lock-writer"
CANDIDATE_ARGUMENTS = {
    "source_repository": SOURCE_REPOSITORY,
    "source_head": SOURCE_HEAD,
    "release_tag": RELEASE_TAG,
    "openmaic_head": OPENMAIC_HEAD,
}
REQUIRED_PLATFORM_SERVICES = {
    "gateway",
    "postgres",
    "minio",
    "minio-bootstrap",
    "teaching-migrate",
    "tenant-provisioner",
    "openmaic",
    "openmaic-render",
    "shared-data-plane-bootstrap",
    "teaching-dispatcher",
    "teaching-worker",
    "teaching-export-worker",
    "teaching-reaper",
    "learning-projector",
}


def _load_renderer():
    module_path = ROOT / "scripts" / "render_platform_compose.py"
    assert module_path.is_file(), "platform compose renderer has not been implemented"
    spec = importlib.util.spec_from_file_location(
        "render_platform_compose_under_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_release_evidence():
    module_path = ROOT / "scripts" / "classroom_release_evidence.py"
    spec = importlib.util.spec_from_file_location(
        "classroom_release_evidence_for_promotion_artifact_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


@cache
def _platform_compose() -> dict[str, Any]:
    renderer = _load_renderer()
    return renderer.load_rendered_compose(
        "docker-compose.yml",
        "docker-compose.platform.yml",
        project_root=ROOT,
    )


@cache
def _data_plane_compose() -> dict[str, Any]:
    renderer = _load_renderer()
    tenant_hash = hashlib.sha256(b"tenant-acme").hexdigest()[:16]
    return renderer.load_rendered_compose(
        "docker-compose.data-plane.yml",
        project_root=ROOT,
        profiles=("mp4-export",),
        environment={
            "YFEISTAI_DATA_PLANE_ID": "tenant-acme",
            "YFEISTAI_DATA_PLANE_SECRET_DIR": (
                f"./data/system/secrets/data-planes/tenant_{tenant_hash}"
            ),
        },
    )


def _copy_candidate_compose_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    platform_compose = tmp_path / "docker-compose.platform.yml"
    data_plane_compose = tmp_path / "docker-compose.data-plane.yml"
    observer_overlay = tmp_path / OBSERVER_OVERLAY_NAME
    platform_compose.write_bytes((ROOT / platform_compose.name).read_bytes())
    data_plane_compose.write_bytes((ROOT / data_plane_compose.name).read_bytes())
    observer_overlay.write_bytes((ROOT / "deploy" / observer_overlay.name).read_bytes())
    return platform_compose, data_plane_compose, observer_overlay


def _write_candidate_with_promotion_receipt(tmp_path: Path) -> Path:
    renderer = _load_renderer()
    candidate_root = tmp_path / "candidate"
    deploy_root = candidate_root / "deploy"
    artifacts_root = candidate_root / "artifacts"
    deploy_root.mkdir(parents=True)
    artifacts_root.mkdir()
    platform_compose = candidate_root / "docker-compose.platform.yml"
    data_plane_compose = candidate_root / "docker-compose.data-plane.yml"
    observer_overlay = deploy_root / OBSERVER_OVERLAY_NAME
    base_compose = candidate_root / "docker-compose.yml"
    base_compose.write_bytes((ROOT / "docker-compose.yml").read_bytes())
    platform_compose.write_bytes((ROOT / platform_compose.name).read_bytes())
    data_plane_compose.write_bytes((ROOT / data_plane_compose.name).read_bytes())
    observer_overlay.write_bytes((ROOT / "deploy" / observer_overlay.name).read_bytes())
    image_digests = {
        name: "sha256:" + f"{index:064x}"
        for index, name in enumerate(
            ("deeptutor", "openmaic", "openmaic_render", OBSERVER_SIGNER_NAME),
            start=1,
        )
    }
    lock = renderer.write_image_lock(
        deploy_root / "image-lock.json",
        digest_resolver=lambda reference: (
            "sha256:" + hashlib.sha256(reference.encode("utf-8")).hexdigest()
        ),
        digest_overrides=image_digests,
        compose_paths=(base_compose, platform_compose, data_plane_compose, observer_overlay),
        **CANDIDATE_ARGUMENTS,
    )
    promotion_digests = {name: record["digest"] for name, record in lock["images"].items()}
    promotion_digests[OBSERVER_SIGNER_NAME] = lock["supportImages"][OBSERVER_SIGNER_NAME]["digest"]
    intent = {
        "schemaVersion": 1,
        "sourceRepository": SOURCE_REPOSITORY,
        "sourceHead": SOURCE_HEAD,
        "releaseTag": RELEASE_TAG,
        "openmaicHead": OPENMAIC_HEAD,
        "imageDigests": promotion_digests,
    }
    intent_body = (json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    (artifacts_root / "promotion_intent.json").write_bytes(intent_body)
    receipt = {
        "schemaVersion": 2,
        "sourceRepository": SOURCE_REPOSITORY,
        "sourceHead": SOURCE_HEAD,
        "releaseTag": RELEASE_TAG,
        "openmaicHead": OPENMAIC_HEAD,
        "githubRunId": GITHUB_RUN_ID,
        "githubRunAttempt": 1,
        "githubWorkflowRef": GITHUB_WORKFLOW_REF,
        "promotionIntentSha256": "sha256:" + hashlib.sha256(intent_body).hexdigest(),
        "imageDigests": promotion_digests,
    }
    (artifacts_root / "promotion_receipt.json").write_bytes(
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    return candidate_root


def _promotion_archive_bytes(
    candidate_root: Path,
    *,
    additional_members: tuple[tuple[str, bytes], ...] = (),
    member_overrides: dict[str, bytes] | None = None,
    member_modes: dict[str, int] | None = None,
) -> bytes:
    overrides = member_overrides or {}
    modes = member_modes or {}
    members = [
        (name, overrides.get(name, (candidate_root / Path(name)).read_bytes()))
        for name in PROMOTION_ARCHIVE_MEMBERS
    ]
    members.extend(additional_members)
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(
            buffer,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, body in members:
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = modes.get(name, 0o100644) << 16
                archive.writestr(entry, body)
    return buffer.getvalue()


def _promotion_source_compose_templates() -> dict[str, bytes]:
    return {name: (ROOT / Path(name)).read_bytes() for name in PROMOTION_ARCHIVE_MEMBERS[1:5]}


def _stub_downloaded_promotion_source_templates(
    renderer: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    templates: dict[str, bytes] | None = None,
) -> list[str]:
    source_templates = dict(templates or _promotion_source_compose_templates())
    calls: list[str] = []

    def read_source_compose_templates(source_head: str) -> dict[str, bytes]:
        calls.append(source_head)
        return dict(source_templates)

    monkeypatch.setattr(
        renderer,
        "_read_source_compose_templates",
        read_source_compose_templates,
        raising=False,
    )
    return calls


def _write_candidate_release_receipts(candidate_root: Path, tmp_path: Path) -> None:
    evidence = _load_release_evidence()
    source_root = tmp_path / "trusted-source"
    source_root.mkdir()
    release_run = {
        "runId": f"github-{GITHUB_RUN_ID}-1",
        "environmentId": "ghcr-production",
    }

    def run_git(arguments: list[str], *, cwd: Path) -> SimpleNamespace:
        assert cwd == source_root
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            stdout = SOURCE_HEAD + "\n"
        elif arguments[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
            stdout = ""
        else:
            assert arguments[-3:] == ["remote", "get-url", "origin"]
            stdout = "https://github.com/xinlingzhifei/DeepTutor.git\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    recovery_root = tmp_path / "receipt-recovery"
    recovery_root.mkdir(mode=0o700)
    worker_uid = 1000 if os.name == "nt" or os.geteuid() != 1000 else 1001
    with evidence.receipt_publication(recovery_root=recovery_root, worker_uid=worker_uid):
        source_receipt = evidence.write_source_head_receipt(
            candidate_root / "artifacts" / "source_head.json",
            candidate_root=candidate_root,
            release_run=release_run,
            source_root=source_root,
            observed_at="2026-08-25T00:00:00Z",
            git_runner=run_git,
        )
        image_receipt = evidence.write_image_digest_receipt(
            candidate_root / "artifacts" / "image_digests.json",
            candidate_root=candidate_root,
            release_run=release_run,
            observed_at="2026-08-25T00:00:00Z",
        )
    assert source_receipt["evidence"] == "source_head"
    assert image_receipt["evidence"] == "image_digests"
    assert source_receipt["releaseRun"] == release_run
    assert image_receipt["releaseRun"] == release_run


def _write_downloaded_promotion_artifact(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], Path]:
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    _write_candidate_release_receipts(candidate_root, tmp_path)
    archive_body = _promotion_archive_bytes(candidate_root)
    archive_path = tmp_path / f"{PROMOTION_ARTIFACT_NAME}.zip"
    archive_path.write_bytes(archive_body)
    actions_artifact = {
        "id": 24680,
        "name": PROMOTION_ARTIFACT_NAME,
        "size_in_bytes": len(archive_body),
        "expired": False,
        "digest": "sha256:" + hashlib.sha256(archive_body).hexdigest(),
        "workflow_run": {
            "id": int(GITHUB_RUN_ID),
            "head_branch": RELEASE_TAG,
            "head_sha": SOURCE_HEAD,
        },
    }
    actions_run = {
        "id": int(GITHUB_RUN_ID),
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_branch": RELEASE_TAG,
        "head_sha": SOURCE_HEAD,
        "path": GITHUB_WORKFLOW_RUN_PATH,
        "repository": {"full_name": SOURCE_REPOSITORY},
    }
    return archive_path, actions_artifact, actions_run, candidate_root


def _artifact_metadata_for_archive(
    original: dict[str, Any],
    archive_body: bytes,
) -> dict[str, Any]:
    return {
        **original,
        "size_in_bytes": len(archive_body),
        "digest": "sha256:" + hashlib.sha256(archive_body).hexdigest(),
        "workflow_run": dict(original["workflow_run"]),
    }


def _downloaded_promotion_cli_arguments(
    tmp_path: Path,
    archive_path: Path,
    actions_artifact: dict[str, Any],
    actions_run: dict[str, Any],
) -> list[str]:
    run_path = tmp_path / "trusted-actions-run.json"
    artifact_path = tmp_path / "trusted-actions-artifact.json"
    run_path.write_text(json.dumps(actions_run), encoding="utf-8")
    artifact_path.write_text(json.dumps(actions_artifact), encoding="utf-8")
    return [
        "--verify-downloaded-promotion-artifact",
        str(archive_path),
        "--actions-run-metadata",
        str(run_path),
        "--actions-artifact-metadata",
        str(artifact_path),
        "--source-head",
        SOURCE_HEAD,
        "--release-tag",
        RELEASE_TAG,
        "--expected-github-run-id",
        GITHUB_RUN_ID,
        "--expected-github-run-attempt",
        "1",
        "--expected-github-artifact-id",
        "24680",
    ]


def _file_snapshots(root: Path) -> dict[str, tuple[int, int, int, str]]:
    snapshots: dict[str, tuple[int, int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        details = path.stat()
        snapshots[path.relative_to(root).as_posix()] = (
            int(details.st_dev),
            int(details.st_ino),
            int(details.st_size),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return snapshots


def _tampered_promotion_member(candidate_root: Path, member: str) -> bytes:
    body = (candidate_root / member).read_bytes()
    if member == "deploy/image-lock.json":
        document = json.loads(body)
        document["candidate"]["sourceHead"] = "b" * 40
        return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    if member == "artifacts/promotion_intent.json":
        document = json.loads(body)
        document["sourceHead"] = "b" * 40
        return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if member == "artifacts/promotion_receipt.json":
        document = json.loads(body)
        document["githubRunId"] = str(int(GITHUB_RUN_ID) + 1)
        return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if member == "artifacts/source_head.json":
        document = json.loads(body)
        document["candidate"]["sourceHead"] = "b" * 40
        return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    if member == "artifacts/image_digests.json":
        document = json.loads(body)
        document["releaseRun"]["runId"] = "github-987654321-1"
        return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    text = body.decode("utf-8")
    match = re.search(r"@sha256:([0-9a-f]{64})", text)
    assert match is not None, f"{member} must contain one digest-qualified image"
    digest = match.group(1)
    drifted = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    return (text[: match.start(1)] + drifted + text[match.end(1) :]).encode("utf-8")


def _padded_compose(body: bytes, size: int, *, compressible: bool) -> bytes:
    assert len(body) < size
    prefix = body + (b"" if body.endswith(b"\n") else b"\n")
    if compressible:
        return prefix + b"# " + (b"A" * (size - len(prefix) - 2))
    padding = bytearray()
    index = 0
    while len(prefix) + len(padding) < size:
        digest = hashlib.sha256(f"promotion-padding-{index}".encode()).hexdigest()
        padding.extend(f"# {index:08x} {digest}\n".encode())
        index += 1
    return (prefix + bytes(padding))[:size]


def _unsafe_promotion_archive_bytes(candidate_root: Path, case: str) -> bytes:
    if case == "extra":
        return _promotion_archive_bytes(
            candidate_root,
            additional_members=(("unexpected.txt", b"unexpected\n"),),
        )
    if case == "duplicate":
        member = PROMOTION_ARCHIVE_MEMBERS[0]
        return _promotion_archive_bytes(
            candidate_root,
            additional_members=((member, (candidate_root / member).read_bytes()),),
        )
    if case in {"traversal", "absolute", "backslash-traversal"}:
        name = {
            "traversal": "../promotion_receipt.json",
            "absolute": "/promotion_receipt.json",
            "backslash-traversal": "..\\promotion_receipt.json",
        }[case]
        return _promotion_archive_bytes(
            candidate_root,
            additional_members=((name, b"{}\n"),),
        )
    if case == "symlink":
        member = "artifacts/promotion_receipt.json"
        return _promotion_archive_bytes(
            candidate_root,
            member_modes={member: 0o120777},
        )
    compose_members = PROMOTION_ARCHIVE_MEMBERS[1:5]
    if case == "member-size":
        member = compose_members[0]
        archive_body = _promotion_archive_bytes(
            candidate_root,
            member_overrides={
                member: _padded_compose(
                    (candidate_root / member).read_bytes(),
                    PROMOTION_ARCHIVE_MAX_MEMBER_BYTES + 1,
                    compressible=False,
                )
            },
        )
        with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
            assert archive.getinfo(member).file_size > PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
        return archive_body
    if case == "total-size":
        per_compose = PROMOTION_ARCHIVE_MAX_TOTAL_BYTES // len(compose_members) + 65536
        archive_body = _promotion_archive_bytes(
            candidate_root,
            member_overrides={
                member: _padded_compose(
                    (candidate_root / member).read_bytes(),
                    per_compose,
                    compressible=False,
                )
                for member in compose_members
            },
        )
        with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
            assert max(entry.file_size for entry in archive.infolist()) < (
                PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
            )
            assert sum(entry.file_size for entry in archive.infolist()) > (
                PROMOTION_ARCHIVE_MAX_TOTAL_BYTES
            )
        return archive_body
    assert case == "compression-ratio"
    member = compose_members[0]
    archive_body = _promotion_archive_bytes(
        candidate_root,
        member_overrides={
            member: _padded_compose(
                (candidate_root / member).read_bytes(),
                PROMOTION_ARCHIVE_MAX_MEMBER_BYTES - 1,
                compressible=True,
            )
        },
    )
    with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
        entry = archive.getinfo(member)
        assert entry.file_size < PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
        assert entry.compress_size > 0
        assert entry.file_size / entry.compress_size > PROMOTION_ARCHIVE_MAX_COMPRESSION_RATIO
    return archive_body


def _publish_journal_path(lock_path: Path) -> Path:
    return lock_path.with_name(f".{lock_path.name}.publish-journal.json")


def _write_publish_journal(
    lock_path: Path,
    originals: dict[Path, bytes | None],
    *,
    owner: str = PUBLISH_JOURNAL_OWNER,
    schema_version: int = 1,
    status: str = "in-progress",
    transaction_id: str | None = None,
) -> Path:
    journal_path = _publish_journal_path(lock_path)
    targets = []
    for path, content in originals.items():
        targets.append(
            {
                "path": str(path.resolve()),
                "existed": content is not None,
                "contentBase64": (
                    base64.b64encode(content).decode("ascii") if content is not None else None
                ),
                "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            }
        )
    journal = {
        "schemaVersion": schema_version,
        "owner": owner,
        "status": status,
        "targets": targets,
    }
    if schema_version == 2:
        journal["transactionId"] = transaction_id or ("f" * 64)
    journal_path.write_text(
        json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return journal_path


def _service_secret_sources(service: dict[str, Any]) -> set[str]:
    sources: set[str] = set()
    for secret in service.get("secrets", []):
        if isinstance(secret, str):
            sources.add(secret)
        elif isinstance(secret, dict):
            source = secret.get("source")
            if source:
                sources.add(str(source))
    return sources


def _writable_volume_targets(service: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for volume in service.get("volumes", []):
        if not isinstance(volume, dict):
            continue
        if not volume.get("read_only", False) and volume.get("target"):
            targets.add(str(volume["target"]))
    return targets


def _volume_at_target(service: dict[str, Any], target: str) -> dict[str, Any]:
    matches = [
        volume
        for volume in service.get("volumes", [])
        if isinstance(volume, dict) and volume.get("target") == target
    ]
    assert len(matches) == 1, (target, matches)
    return matches[0]


def test_platform_merge_exposes_only_gateway_ports() -> None:
    compose = _platform_compose()
    assert REQUIRED_PLATFORM_SERVICES.issubset(compose["services"])

    for name, service in compose["services"].items():
        if name != "gateway":
            assert not service.get("ports"), name

    published = {str(port["published"]) for port in compose["services"]["gateway"]["ports"]}
    assert published == {"13780", "13783"}
    assert {port["host_ip"] for port in compose["services"]["gateway"]["ports"]} == {"127.0.0.1"}


def test_gateway_publishes_through_a_non_internal_edge_network() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert compose["networks"]["platform-edge"].get("internal", False) is False
    assert set(services["gateway"]["networks"]) == {
        "platform-edge",
        "platform-internal",
    }
    for name, service in services.items():
        if name == "gateway":
            continue
        assert "platform-edge" not in service.get("networks", []), name


def test_api_waits_for_migration_and_storage_on_the_private_network() -> None:
    compose = _platform_compose()
    dependencies = compose["services"]["deeptutor"]["depends_on"]
    assert dependencies["teaching-migrate"]["condition"] == ("service_completed_successfully")
    assert dependencies["minio-bootstrap"]["condition"] == ("service_completed_successfully")

    assert compose["networks"]["platform-internal"]["internal"] is True
    for name in REQUIRED_PLATFORM_SERVICES | {"deeptutor"}:
        assert "platform-internal" in compose["services"][name]["networks"], name


def test_minio_bootstrap_enables_and_verifies_bucket_versioning() -> None:
    compose = yaml.load(
        (ROOT / "docker-compose.platform.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    command = compose["services"]["minio-bootstrap"]["command"]
    assert isinstance(command, list) and len(command) == 1
    script = command[0]

    bucket = 'bucket="platform/${YFEISTAI_OBJECT_STORE_BUCKET:-yfeistai-classrooms}"'
    create = 'mc mb --ignore-existing "$$bucket"'
    enable = 'mc version enable "$$bucket"'
    verify = 'mc version info "$$bucket" | grep -F "versioning is enabled"'

    assert bucket in script
    assert create in script
    assert enable in script
    assert verify in script
    assert script.index(create) < script.index(enable) < script.index(verify)


def test_runtime_waits_for_verified_shared_data_plane_registration() -> None:
    compose = _platform_compose()
    services = compose["services"]
    bootstrap = services["shared-data-plane-bootstrap"]

    assert bootstrap["command"] == ["scripts/bootstrap_shared_data_plane.py"]
    assert bootstrap["depends_on"]["teaching-migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert bootstrap["depends_on"]["openmaic"]["condition"] == "service_healthy"
    for name in {
        "deeptutor",
        "learning-projector",
        "teaching-dispatcher",
        "teaching-export-worker",
        "teaching-reaper",
        "teaching-worker",
        "tenant-provisioner",
    }:
        assert services[name]["depends_on"]["shared-data-plane-bootstrap"]["condition"] == (
            "service_completed_successfully"
        )


def test_only_openmaic_can_reach_the_shared_provider_egress() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert compose["networks"]["shared-provider-egress"].get("internal", False) is False
    assert not compose["networks"]["shared-provider-egress"].get("external", False)
    assert set(services["openmaic"]["networks"]) == {
        "platform-internal",
        "shared-provider-egress",
    }
    for name, service in services.items():
        if name == "openmaic":
            continue
        assert "shared-provider-egress" not in service.get("networks", []), name


def test_deeptutor_retains_application_egress_without_joining_provider_network() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert compose["networks"]["platform-service-egress"].get("internal", False) is False
    assert not compose["networks"]["platform-service-egress"].get("external", False)
    assert set(services["deeptutor"]["networks"]) == {
        "platform-internal",
        "platform-service-egress",
    }
    for name, service in services.items():
        if name == "deeptutor":
            continue
        assert "platform-service-egress" not in service.get("networks", []), name


def test_only_provisioner_combines_migration_storage_bootstrap_and_secret_write() -> None:
    compose = _platform_compose()
    services = compose["services"]
    provisioner = services["tenant-provisioner"]
    privileged_secrets = {
        "platform_database_migration_password",
        "minio_bootstrap_access_key",
        "minio_bootstrap_secret_key",
    }
    tenant_secret_directory = "/run/yfeistai/tenant-secrets"

    assert privileged_secrets.issubset(_service_secret_sources(provisioner))
    assert tenant_secret_directory in _writable_volume_targets(provisioner)

    ordinary_services = {
        "deeptutor",
        "openmaic",
        "teaching-dispatcher",
        "teaching-worker",
        "teaching-export-worker",
        "teaching-reaper",
        "learning-projector",
    }
    for name in ordinary_services:
        service = services[name]
        assert _service_secret_sources(service).isdisjoint(privileged_secrets), name
        assert tenant_secret_directory not in _writable_volume_targets(service), name


def test_database_bootstrap_secrets_are_role_scoped() -> None:
    services = _platform_compose()["services"]
    migration = services["teaching-migrate"]

    assert _service_secret_sources(migration) == {
        "platform_database_password",
        "platform_database_app_password",
        "platform_database_migration_password",
    }
    assert migration["environment"] == {
        "YFEISTAI_PLATFORM_ADMIN_PASSWORD_FILE": ("/run/secrets/platform_database_password"),
        "YFEISTAI_PLATFORM_APP_PASSWORD_FILE": ("/run/secrets/platform_database_app_password"),
        "YFEISTAI_PLATFORM_MIGRATION_USER": "yfeistai_migrator",
        "YFEISTAI_PLATFORM_MIGRATION_PASSWORD_FILE": (
            "/run/secrets/platform_database_migration_password"
        ),
    }
    assert "platform_database_password" not in _service_secret_sources(services["deeptutor"])
    for name in (
        "teaching-dispatcher",
        "teaching-worker",
        "teaching-export-worker",
        "teaching-reaper",
        "learning-projector",
    ):
        assert _service_secret_sources(services[name]) == {"platform_database_app_password"} | (
            {"openmaic_service_secret"} if "worker" in name else set()
        )


def test_data_mounts_mask_host_secret_tree_from_runtime_services() -> None:
    compose = _platform_compose()
    services = compose["services"]
    data_services = {
        "deeptutor",
        "teaching-migrate",
        "tenant-provisioner",
        "teaching-dispatcher",
        "teaching-worker",
        "teaching-export-worker",
        "teaching-reaper",
        "learning-projector",
    }

    assert not compose["volumes"]["platform-secret-mask"].get("external", False)
    for name in data_services:
        mask = _volume_at_target(services[name], "/app/data/system/secrets")
        assert mask["type"] == "volume", name
        assert mask["read_only"] is True, name
        assert mask["volume"]["nocopy"] is True, name


def test_teaching_processes_override_the_monolithic_image_entrypoint() -> None:
    services = _platform_compose()["services"]
    process_names = {
        "teaching-dispatcher": "dispatcher",
        "teaching-worker": "worker",
        "teaching-export-worker": "export-worker",
        "teaching-reaper": "reaper",
        "learning-projector": "learning-projector",
    }

    for service_name, process_name in process_names.items():
        service = services[service_name]
        assert service["entrypoint"] == [
            "python",
            "-m",
            "deeptutor.teaching.processes",
        ]
        assert service["command"] == [process_name]
        assert service["user"] == "1000:1000"
        assert service["healthcheck"]["disable"] is True

    provisioner = services["tenant-provisioner"]
    assert provisioner["entrypoint"] == [
        "python",
        "-m",
        "deeptutor.teaching.processes",
    ]
    assert provisioner["command"] == ["tenant-provisioner"]
    assert provisioner["user"] == "1000:1000"
    assert provisioner["healthcheck"]["disable"] is True

    migration = services["teaching-migrate"]
    assert migration["entrypoint"] == ["python"]
    assert migration["command"] == ["scripts/migrate_teaching.py"]
    assert migration["user"] == "1000:1000"
    assert migration["healthcheck"]["disable"] is True


def test_openmaic_images_and_worker_pools_are_separate_and_pinned() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert services["deeptutor"]["image"].startswith(
        "ghcr.io/xinlingzhifei/deeptutor:first-release@sha256:"
    )
    assert services["openmaic"]["image"].startswith(
        "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:"
    )
    assert services["openmaic-render"]["image"].startswith(
        "ghcr.io/xinlingzhifei/openmaic-render:0.3.1-0cf2a330@sha256:"
    )
    lock = json.loads((ROOT / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    render_source = lock["images"]["openmaic_render"]["source"]
    assert render_source == {
        "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
        "revision": "0cf2a330411681190e89f48e20f305345ff99f87",
        "dockerfile": "render-service/Dockerfile",
    }
    assert "build" not in services["openmaic-render"]
    assert (
        services["openmaic"]["environment"]["YFEISTAI_OPENMAIC_RENDER_ENDPOINT"]
        == "http://openmaic-render:9000"
    )

    assert services["teaching-worker"]["command"][-1] == "worker"
    assert services["teaching-export-worker"]["command"][-1] == "export-worker"
    settings = json.loads((ROOT / "deploy" / "platform.example.json").read_text(encoding="utf-8"))
    assert settings["object_store_namespace_id"] == "yfeistai-primary-minio"
    assert settings["shared_generation_limit"] == 20
    assert settings["default_tenant_generation_limit"] == 2


def test_offline_rendered_candidate_digest_references_are_used_by_platform(tmp_path: Path) -> None:
    # Synthetic digests exercise rendering only; downloaded-artifact validation is a separate gate.
    source_before = {name: (ROOT / name).read_bytes() for name in PROMOTION_ARCHIVE_MEMBERS[:5]}
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    lock = json.loads((candidate_root / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    assert lock["schemaVersion"] == 3
    assert set(lock["candidate"]["imageDigests"]) == {
        "deeptutor",
        "openmaic",
        "openmaic_render",
    }
    expected = {
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
        "pocketbase",
    }
    assert expected == set(lock["images"])

    references: dict[str, str] = {}
    for name, record in lock["images"].items():
        assert record["repository"]
        assert record["tag"]
        assert IMAGE_DIGEST_PATTERN.fullmatch(record["digest"]), name
        assert record["digest"] != "sha256:" + ("0" * 64), name
        reference = f"{record['repository']}:{record['tag']}@{record['digest']}"
        assert record["reference"] == reference
        references[name] = reference

    compose = _load_renderer().load_rendered_compose(
        "docker-compose.yml",
        "docker-compose.platform.yml",
        project_root=candidate_root,
        # Include optional PocketBase in this read-only image-reference inspection.
        profiles=("legacy-local",),
    )
    expected_images = {
        "gateway": "nginx",
        "postgres": "postgres",
        "minio": "minio",
        "minio-bootstrap": "minio_client",
        "pocketbase": "pocketbase",
        "deeptutor": "deeptutor",
        "teaching-migrate": "deeptutor",
        "tenant-provisioner": "deeptutor",
        "teaching-dispatcher": "deeptutor",
        "teaching-worker": "deeptutor",
        "teaching-export-worker": "deeptutor",
        "teaching-reaper": "deeptutor",
        "learning-projector": "deeptutor",
        "openmaic": "openmaic",
        "openmaic-render": "openmaic_render",
    }
    for service, image_name in expected_images.items():
        assert compose["services"][service]["image"] == references[image_name]
    for name, service in compose["services"].items():
        if service.get("profiles"):
            continue
        assert "@sha256:" in service["image"], name

    support_images = lock["supportImages"]
    assert set(support_images) == {OBSERVER_SIGNER_NAME}
    observer_signer = support_images[OBSERVER_SIGNER_NAME]
    assert observer_signer["repository"] == OBSERVER_SIGNER_REPOSITORY
    assert observer_signer["tag"] == lock["candidate"]["releaseTag"]
    assert IMAGE_DIGEST_PATTERN.fullmatch(observer_signer["digest"])
    assert observer_signer["digest"] != "sha256:" + ("0" * 64)
    assert observer_signer["digest"] not in set(lock["candidate"]["imageDigests"].values())
    assert observer_signer["reference"] == (
        f"{OBSERVER_SIGNER_REPOSITORY}:{observer_signer['tag']}@{observer_signer['digest']}"
    )

    observer_overlay = (candidate_root / "deploy" / OBSERVER_OVERLAY_NAME).read_text(
        encoding="utf-8"
    )
    assert observer_overlay.count(observer_signer["reference"]) == 2
    assert "${YFEISTAI_OBSERVER_SIGNER_IMAGE:" not in observer_overlay
    assert {name: (ROOT / name).read_bytes() for name in source_before} == source_before


def test_private_platform_workflow_builds_all_images_and_exports_digest_lock() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    assert workflow_path.is_file(), "private platform image workflow is missing"

    source = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    assert isinstance(workflow, dict)
    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "write",
    }

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and len(jobs) == 1
    job = next(iter(jobs.values()))
    steps = job["steps"]

    checkout = next(step for step in steps if step.get("name") == "Checkout OpenMAIC")
    assert checkout["uses"] == CHECKOUT_ACTION
    assert checkout["with"] == {
        "repository": "xinlingzhifei/OpenMAIC",
        "ref": "0cf2a330411681190e89f48e20f305345ff99f87",
        "path": "openmaic-upstream",
        "persist-credentials": False,
    }

    build_steps = [
        (index, step) for index, step in enumerate(steps) if step.get("uses") == BUILD_PUSH_ACTION
    ]
    assert len(build_steps) == 4
    builds_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, step in build_steps:
        settings = step["with"]
        tags = [line.strip() for line in str(settings["tags"]).splitlines() if line.strip()]
        assert len(tags) == 1
        assert settings["platforms"] == "linux/amd64"
        assert settings["push"] is True
        assert tags[0].startswith(f"{WORKFLOW_IMAGE_REPOSITORIES[step['id']]}:")
        builds_by_id[step["id"]] = (index, settings)

    expected_builds = {
        "build_deeptutor": {
            "context": ".",
            "file": "./Dockerfile",
            "target": "production",
        },
        "build_openmaic": {
            "context": ".",
            "file": "./integrations/openmaic/Dockerfile",
        },
        "build_openmaic_render": {
            "context": "./openmaic-upstream/render-service",
            "file": "./openmaic-upstream/render-service/Dockerfile",
        },
        "build_observer_signer": {
            "context": ".",
            "file": "./Dockerfile",
            "target": "observer-signer",
        },
    }
    assert set(builds_by_id) == set(expected_builds)
    for build_id, expected in expected_builds.items():
        settings = builds_by_id[build_id][1]
        for key, value in expected.items():
            assert settings[key] == value, build_id

    write_index, write_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if "render_platform_compose.py --write-image-lock" in str(step.get("run", ""))
    )
    assert all(index < write_index for index, _step in build_steps)
    write_command = str(write_step["run"])
    assert '--source-repository "$GITHUB_REPOSITORY"' in write_command
    assert '--source-head "$GITHUB_SHA"' in write_command
    assert '--release-tag "$GITHUB_REF_NAME"' in write_command

    verify_index, verify_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if "validate_image_lock_bindings" in str(step.get("run", ""))
    )
    assert verify_index > write_index
    verify_command = str(verify_step["run"])
    assert (
        "from scripts.render_platform_compose import validate_image_lock_bindings" in verify_command
    )
    assert "expected_candidate=" in verify_command
    assert "ghcr.io/muchobien/pocketbase:latest@" in verify_command
    assert 'image_lock["images"]["pocketbase"]' in verify_command
    assert "docker-compose.yml" in verify_command
    assert "docker-compose.platform.yml" in verify_command
    assert "docker-compose.data-plane.yml" in verify_command

    upload_index, upload_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == UPLOAD_ARTIFACT_ACTION
    )
    assert upload_index > verify_index
    artifact_paths = {
        line.strip() for line in str(upload_step["with"]["path"]).splitlines() if line.strip()
    }
    assert artifact_paths == {
        "release-candidate/deploy/image-lock.json",
        "release-candidate/docker-compose.yml",
        "release-candidate/docker-compose.platform.yml",
        "release-candidate/docker-compose.data-plane.yml",
        "release-candidate/deploy/docker-compose.openmaic-shared-ingress-observer.yml",
        "release-candidate/artifacts/source_head.json",
        "release-candidate/artifacts/image_digests.json",
        "release-candidate/artifacts/promotion_intent.json",
        "release-candidate/artifacts/promotion_receipt.json",
    }


def test_observer_runtime_target_contains_the_independent_service_and_exact_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    target = re.search(
        r"(?ms)^FROM python:3\.11-slim AS observer-signer\s*$\n"
        r"(?P<body>.*?)(?=^FROM\s|\Z)",
        dockerfile,
    )
    assert target is not None, "observer-signer Dockerfile target is missing"

    body = target.group("body")
    assert "pip install" in body
    dependencies = {
        "cryptography": "47.0.0",
        "httpx": "0.28.1",
        "pydantic": "2.13.3",
        "uvicorn": "0.44.0",
    }
    for dependency, version in dependencies.items():
        assert re.search(
            rf'(?<![A-Za-z0-9_-]){dependency}=={re.escape(version)}(?=[\s"\\])',
            body,
        ), f"observer runtime dependency must be exactly pinned: {dependency}"
    for source in (
        "scripts/openmaic_observer_attestation_signer.py",
        "scripts/openmaic_observer_service.py",
        "scripts/openmaic_smoke_contract.py",
    ):
        assert source in body, f"observer runtime source is missing: {source}"
    assert re.search(r"(?m)^USER 1000:1000\s*$", body)
    assert re.search(
        r'ENTRYPOINT\s*\[\s*"python"\s*,\s*"/app/scripts/'
        r'openmaic_observer_attestation_signer\.py"\s*\]',
        body,
    )
    for forbidden in ("supervisord", "deeptutor.api", "healthcheck.py"):
        assert forbidden not in body


def test_private_platform_workflow_rejects_non_release_refs_and_reruns_before_registry_login() -> (
    None
):
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    source = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]

    validation_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Validate immutable release candidate"
    ]
    assert len(validation_steps) == 1, "candidate validation step is missing"
    validate_index, validate_step = validation_steps[0]
    login_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == LOGIN_ACTION
    )
    first_build_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == BUILD_PUSH_ACTION
    )
    setup_buildx_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == SETUP_BUILDX_ACTION
    )

    assert validate_index < setup_buildx_index < login_index < first_build_index
    command = str(validate_step["run"])
    assert 'GITHUB_EVENT_NAME" = "push"' in command
    assert 'GITHUB_RUN_ATTEMPT" = "1"' in command
    assert 'GITHUB_REF_TYPE" = "tag"' in command
    assert "^yfeistai-first-release-[0-9]{8}-[0-9a-f]{8}$" in command
    assert "git rev-parse HEAD" in command
    assert "GITHUB_SHA" in command
    assert "GITHUB_REF_NAME" in command
    assert "workflow_dispatch:" not in source


def test_private_platform_workflow_stages_all_images_before_generating_candidate_lock() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]
    build_steps = [
        (index, step) for index, step in enumerate(steps) if step.get("uses") == BUILD_PUSH_ACTION
    ]
    write_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Record image digests"
    )

    assert len(build_steps) == 4
    for index, step in build_steps:
        tags = {line.strip() for line in str(step["with"]["tags"]).splitlines() if line.strip()}
        expected_staging_ref = f"{WORKFLOW_IMAGE_REPOSITORIES[step['id']]}:{WORKFLOW_STAGING_TAG}"
        assert tags == {expected_staging_ref}, (
            "builds must publish a unique run-scoped staging ref, never the final candidate tag"
        )
        assert "${{ github.ref_name }}" not in expected_staging_ref
        assert index < write_index

    source = workflow_path.read_text(encoding="utf-8")
    assert "ghcr.io/xinlingzhifei/deeptutor:first-release" not in source
    assert "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330" not in source
    assert "ghcr.io/xinlingzhifei/openmaic-render:0.3.1-0cf2a330" not in source


def test_private_platform_workflow_pins_third_party_actions_to_commits() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    action_refs = [str(step["uses"]) for step in job["steps"] if "uses" in step]

    assert action_refs
    assert set(action_refs) == {
        CHECKOUT_ACTION,
        UPLOAD_ARTIFACT_ACTION,
        SETUP_BUILDX_ACTION,
        LOGIN_ACTION,
        BUILD_PUSH_ACTION,
    }


def test_private_platform_workflow_writes_schema3_lock_from_all_staged_build_digests() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]
    build_ids = {step.get("id") for step in steps if step.get("uses") == BUILD_PUSH_ACTION}
    assert build_ids == {
        "build_deeptutor",
        "build_openmaic",
        "build_openmaic_render",
        "build_observer_signer",
    }
    build_indices = [
        index for index, step in enumerate(steps) if step.get("uses") == BUILD_PUSH_ACTION
    ]
    digest_guard_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Validate staged image digests"
    ]
    assert len(digest_guard_steps) == 1, "staged build digest validation is missing"
    digest_guard_index, digest_guard_step = digest_guard_steps[0]
    assert digest_guard_step.get("env") == WORKFLOW_DIGEST_ENV
    digest_guard_command = str(digest_guard_step["run"])
    assert "^sha256:[0-9a-f]{64}$" in digest_guard_command
    assert '"sha256:" + ("0" * 64)' in digest_guard_command
    assert "len(set(digests)) == len(digests)" in digest_guard_command
    for name in WORKFLOW_DIGEST_ENV:
        assert f'os.environ["{name}"]' in digest_guard_command

    write_index, write_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Record image digests"
    )
    assert all(index < digest_guard_index for index in build_indices)
    assert digest_guard_index < write_index
    assert write_step.get("env") == WORKFLOW_DIGEST_ENV, (
        "the candidate lock must be generated from the four successful build outputs"
    )
    write_command = str(write_step["run"])
    for image_name, environment_name in (
        ("deeptutor", "DEEPTUTOR_DIGEST"),
        ("openmaic", "OPENMAIC_DIGEST"),
        ("openmaic_render", "OPENMAIC_RENDER_DIGEST"),
        (OBSERVER_SIGNER_NAME, "OBSERVER_SIGNER_DIGEST"),
    ):
        assert f'--digest "{image_name}=${environment_name}"' in write_command

    verify_index, verify_step = next(
        (index, step) for index, step in enumerate(steps) if step.get("name") == "Verify image lock"
    )
    assert write_index < verify_index
    assert verify_step["env"] == WORKFLOW_DIGEST_ENV
    verify_command = str(verify_step["run"])
    assert '"imageDigests"' in verify_command
    assert '"supportImages"' in verify_command
    assert '"observer_signer"' in verify_command
    for name in WORKFLOW_DIGEST_ENV:
        assert f'os.environ["{name}"]' in verify_command


def test_private_platform_workflow_promotes_absent_final_tags_once_and_verifies_digests() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    source = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]

    lock_verify_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Verify image lock"
    )
    promotion_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Promote candidate image digests"
    ]
    assert len(promotion_steps) == 1, "digest-based candidate promotion step is missing"
    promotion_index, promotion_step = promotion_steps[0]
    assert lock_verify_index < promotion_index
    assert promotion_step.get("env") == WORKFLOW_DIGEST_ENV
    promotion_command = str(promotion_step["run"])
    assert "docker buildx imagetools inspect" in promotion_command
    assert "docker buildx imagetools create" in promotion_command
    assert "--prefer-index=false" in promotion_command
    assert "--raw" in promotion_command
    assert "sha256sum" in promotion_command
    assert 'final_ref="$repository:$GITHUB_REF_NAME"' in promotion_command
    assert 'source_ref="$repository@$digest"' in promotion_command
    assert "manifest unknown|name unknown|not found" in promotion_command
    assert "already has expected digest" not in promotion_command
    assert '[[ "$remote_digest" == "$digest" ]]' not in promotion_command
    assert "candidate tag already exists" in promotion_command.lower()
    assert "refus" in promotion_command.lower()
    assert "return 1" in promotion_command
    for repository in WORKFLOW_IMAGE_REPOSITORIES.values():
        assert repository in promotion_command
    for environment_name in WORKFLOW_DIGEST_ENV:
        assert f"${environment_name}" in promotion_command

    remote_verify_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Verify promoted candidate digests"
    ]
    assert len(remote_verify_steps) == 1, "remote promoted digest verification is missing"
    remote_verify_index, remote_verify_step = remote_verify_steps[0]
    assert promotion_index < remote_verify_index
    assert remote_verify_step.get("env") == WORKFLOW_DIGEST_ENV
    remote_verify_command = str(remote_verify_step["run"])
    assert "docker buildx imagetools inspect" in remote_verify_command
    assert "--raw" in remote_verify_command
    assert "sha256sum" in remote_verify_command
    assert 'final_ref="$repository:$GITHUB_REF_NAME"' in remote_verify_command
    assert '[[ "$remote_digest" == "$digest" ]]' in remote_verify_command
    for repository in WORKFLOW_IMAGE_REPOSITORIES.values():
        assert repository in remote_verify_command
    for environment_name in WORKFLOW_DIGEST_ENV:
        assert f"${environment_name}" in remote_verify_command

    receipt_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Record Task1 release evidence"
    )
    upload_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == UPLOAD_ARTIFACT_ACTION
    )
    assert remote_verify_index < receipt_index < upload_index

    for forbidden in (
        "docker manifest rm",
        "docker image rm",
        "oras manifest delete",
        "crane delete",
        "curl -x delete",
        "gh api --method delete",
        "gh api -x delete",
        "git commit",
        "git push",
    ):
        assert forbidden not in source.lower()


def test_private_platform_workflow_revokes_ghcr_credentials_before_release_dependencies() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]

    login_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == LOGIN_ACTION
    )
    remote_verify_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Verify promoted candidate digests"
    )
    logout_steps = [
        (index, step) for index, step in enumerate(steps) if step.get("name") == "Log out of GHCR"
    ]
    assert len(logout_steps) == 1, "GHCR credentials must be explicitly revoked"
    logout_index, logout_step = logout_steps[0]
    dependency_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install release receipt dependencies"
    )
    receipt_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Record Task1 release evidence"
    )

    assert login_index < remote_verify_index < logout_index < dependency_index < receipt_index
    assert logout_step.get("if") == "${{ always() }}"
    assert logout_step.get("shell") == "bash"
    assert str(logout_step.get("run", "")).strip() == "docker logout ghcr.io"


def test_private_platform_workflow_rejects_existing_final_tags_before_any_build() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]

    precheck_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Reject existing final candidate tags"
    ]
    assert len(precheck_steps) == 1, "all four final tags must be absent before any build"
    precheck_index, precheck_step = precheck_steps[0]
    login_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == LOGIN_ACTION
    )
    first_build_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == BUILD_PUSH_ACTION
    )
    promotion_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Promote candidate image digests"
    )
    assert login_index < precheck_index < first_build_index < promotion_index
    assert "env" not in precheck_step

    command = str(precheck_step["run"])
    assert "set -euo pipefail" in command
    assert "docker buildx imagetools inspect --raw" in command
    assert "docker buildx imagetools create" not in command
    assert 'final_ref="$repository:$GITHUB_REF_NAME"' in command
    assert "manifest unknown|name unknown|not found" in command
    assert "candidate tag already exists" in command.lower()
    assert "refus" in command.lower()
    assert "return 1" in command
    for name, build_id in (
        ("deeptutor", "build_deeptutor"),
        ("openmaic", "build_openmaic"),
        ("openmaic-render", "build_openmaic_render"),
        ("observer-signer", "build_observer_signer"),
    ):
        repository = WORKFLOW_IMAGE_REPOSITORIES[build_id]
        assert f"reject_existing_tag {name} {repository}" in command

    assert all(
        "docker buildx imagetools create" not in str(step.get("run", ""))
        for step in steps[:promotion_index]
    )


def test_private_platform_workflow_persists_canonical_promotion_intent_before_promotion() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]

    intent_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Record promotion intent"
    ]
    assert len(intent_steps) == 1, (
        "canonical promotion intent must be persisted before the first final-tag write"
    )
    intent_index, intent_step = intent_steps[0]
    promotion_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Promote candidate image digests"
    )
    assert intent_index < promotion_index
    assert intent_step.get("env") == WORKFLOW_DIGEST_ENV

    command = str(intent_step["run"])
    assert "release-candidate/artifacts/promotion_intent.json" in command
    assert "load_image_lock" in command
    assert "promotion_image_digests" in command
    assert '"sourceRepository": os.environ["GITHUB_REPOSITORY"]' in command
    assert '"sourceHead": os.environ["GITHUB_SHA"]' in command
    assert '"releaseTag": os.environ["GITHUB_REF_NAME"]' in command
    assert f'"openmaicHead": "{OPENMAIC_HEAD}"' in command
    for image_name, digest_name in (
        ("deeptutor", "DEEPTUTOR_DIGEST"),
        ("openmaic", "OPENMAIC_DIGEST"),
        ("openmaic_render", "OPENMAIC_RENDER_DIGEST"),
        (OBSERVER_SIGNER_NAME, "OBSERVER_SIGNER_DIGEST"),
    ):
        assert f'"{image_name}": os.environ["{digest_name}"]' in command
    assert "sort_keys=True" in command
    assert 'separators=(",", ":")' in command
    assert "write_bytes" in command or "write_text" in command


def test_private_platform_workflow_records_intent_bound_receipt_after_all_remote_digests_verify() -> (
    None
):
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]

    remote_verify_index, remote_verify_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Verify promoted candidate digests"
    )
    remote_verify_command = str(remote_verify_step["run"])
    assert "docker buildx imagetools inspect --raw" in remote_verify_command
    assert "sha256sum" in remote_verify_command
    for name, build_id, digest_name in (
        ("deeptutor", "build_deeptutor", "DEEPTUTOR_DIGEST"),
        ("openmaic", "build_openmaic", "OPENMAIC_DIGEST"),
        ("openmaic-render", "build_openmaic_render", "OPENMAIC_RENDER_DIGEST"),
        ("observer-signer", "build_observer_signer", "OBSERVER_SIGNER_DIGEST"),
    ):
        repository = WORKFLOW_IMAGE_REPOSITORIES[build_id]
        assert f'verify_digest {name} {repository} "${digest_name}"' in remote_verify_command

    receipt_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Record verified promotion receipt"
    ]
    assert len(receipt_steps) == 1, (
        "a final promotion receipt must be written only after all four remote digests verify"
    )
    receipt_index, receipt_step = receipt_steps[0]
    upload_index, upload_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == UPLOAD_ARTIFACT_ACTION
    )
    assert remote_verify_index < receipt_index < upload_index
    assert receipt_step.get("env") == WORKFLOW_DIGEST_ENV

    receipt_command = str(receipt_step["run"])
    assert "release-candidate/artifacts/promotion_intent.json" in receipt_command
    assert "release-candidate/artifacts/promotion_receipt.json" in receipt_command
    assert "load_image_lock" in receipt_command
    assert "promotion_image_digests" in receipt_command
    assert "promotionIntentSha256" in receipt_command
    assert "hashlib.sha256" in receipt_command
    assert "_read_canonical_promotion_document" in receipt_command
    for image_name, digest_name in (
        ("deeptutor", "DEEPTUTOR_DIGEST"),
        ("openmaic", "OPENMAIC_DIGEST"),
        ("openmaic_render", "OPENMAIC_RENDER_DIGEST"),
        (OBSERVER_SIGNER_NAME, "OBSERVER_SIGNER_DIGEST"),
    ):
        assert f'"{image_name}": os.environ["{digest_name}"]' in receipt_command
    assert "sort_keys=True" in receipt_command
    assert 'separators=(",", ":")' in receipt_command

    artifact_paths = {
        line.strip() for line in str(upload_step["with"]["path"]).splitlines() if line.strip()
    }
    assert "release-candidate/artifacts/promotion_intent.json" in artifact_paths
    assert "release-candidate/artifacts/promotion_receipt.json" in artifact_paths


def test_promotion_receipt_verifier_binds_exact_actions_run_and_candidate_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)

    receipt = renderer.validate_promotion_receipt(
        candidate_root,
        expected_github_run_id=GITHUB_RUN_ID,
        expected_github_run_attempt=1,
    )

    assert receipt["sourceHead"] == SOURCE_HEAD
    assert receipt["releaseTag"] == RELEASE_TAG
    assert receipt["githubWorkflowRef"] == GITHUB_WORKFLOW_REF
    assert set(receipt["imageDigests"]) == {
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
        "pocketbase",
        OBSERVER_SIGNER_NAME,
    }
    assert (
        renderer.main(
            [
                "--verify-promotion-receipt",
                "--candidate-root",
                str(candidate_root),
                "--expected-github-run-id",
                GITHUB_RUN_ID,
                "--expected-github-run-attempt",
                "1",
            ]
        )
        == 0
    )
    assert (
        capsys.readouterr()
        .out.strip()
        .endswith(
            "artifacts\\promotion_receipt.json"
            if sys.platform == "win32"
            else "artifacts/promotion_receipt.json"
        )
    )

    receipt_path = candidate_root / "artifacts" / "promotion_receipt.json"
    drifted = json.loads(receipt_path.read_text(encoding="utf-8"))
    drifted["githubRunId"] = "987654321"
    receipt_path.write_bytes(
        (json.dumps(drifted, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    with pytest.raises(ValueError, match="GitHub Actions run"):
        renderer.validate_promotion_receipt(
            candidate_root,
            expected_github_run_id=GITHUB_RUN_ID,
            expected_github_run_attempt=1,
        )


def test_source_compose_templates_disable_git_replace_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    git = "trusted-git"
    templates = {
        name: f"# source template: {name}\n".encode() for name in PROMOTION_ARCHIVE_MEMBERS[1:5]
    }
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        operation = command[-2]
        object_spec = command[-1]
        repository_path = object_spec.partition(":")[2]
        body = templates[repository_path]
        stdout = f"{len(body)}\n".encode() if operation == "-s" else body
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(renderer.shutil, "which", lambda name: git if name == "git" else None)
    monkeypatch.setattr(renderer.subprocess, "run", run)

    assert renderer._read_source_compose_templates(SOURCE_HEAD) == templates
    assert calls == [
        command
        for name in PROMOTION_ARCHIVE_MEMBERS[1:5]
        for command in (
            [git, "--no-replace-objects", "cat-file", "-s", f"{SOURCE_HEAD}:{name}"],
            [git, "--no-replace-objects", "cat-file", "blob", f"{SOURCE_HEAD}:{name}"],
        )
    ]


def test_downloaded_promotion_artifact_requires_trusted_actions_run_and_artifact_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )

    def unexpected_directory_verification(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("downloaded artifacts must be verified directly from the archive")

    def unexpected_extraction(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("downloaded artifacts must not be extracted")

    monkeypatch.setattr(renderer, "validate_promotion_receipt", unexpected_directory_verification)
    monkeypatch.setattr(zipfile.ZipFile, "extract", unexpected_extraction)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", unexpected_extraction)
    with zipfile.ZipFile(io.BytesIO(archive_path.read_bytes())) as archive:
        assert tuple(entry.filename for entry in archive.infolist()) == PROMOTION_ARCHIVE_MEMBERS
        assert {entry.compress_type for entry in archive.infolist()} == {zipfile.ZIP_DEFLATED}
    before = _file_snapshots(tmp_path)

    receipt = renderer.validate_downloaded_promotion_artifact(
        archive_path,
        actions_artifact=actions_artifact,
        actions_run=actions_run,
    )

    assert receipt["sourceHead"] == SOURCE_HEAD
    assert receipt["releaseTag"] == RELEASE_TAG
    assert receipt["githubRunId"] == GITHUB_RUN_ID
    assert receipt["githubWorkflowRef"] == GITHUB_WORKFLOW_REF
    assert actions_run["head_branch"] == RELEASE_TAG
    assert actions_run["path"] == GITHUB_WORKFLOW_RUN_PATH
    assert _file_snapshots(tmp_path) == before


@pytest.mark.parametrize("metadata_size", (None, 1024 * 1024), ids=("normal", "one-mib"))
def test_downloaded_promotion_cli_prints_only_validated_receipt_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metadata_size: int | None,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    arguments = _downloaded_promotion_cli_arguments(
        tmp_path, archive_path, actions_artifact, actions_run
    )
    if metadata_size is not None:
        for flag in ("--actions-run-metadata", "--actions-artifact-metadata"):
            path = Path(arguments[arguments.index(flag) + 1])
            body = path.read_bytes()
            path.write_bytes(body + b" " * (metadata_size - len(body)))
    expected_receipt = json.loads(
        (candidate_root / "artifacts" / "promotion_receipt.json").read_bytes()
    )
    source_calls = _stub_downloaded_promotion_source_templates(renderer, monkeypatch)

    def forbidden_side_effect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("CLI must not extract, use the directory verifier, or spawn commands")

    monkeypatch.setattr(renderer, "validate_promotion_receipt", forbidden_side_effect)
    monkeypatch.setattr(renderer.subprocess, "run", forbidden_side_effect)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden_side_effect)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden_side_effect)
    before = _file_snapshots(tmp_path)
    before_paths = set(tmp_path.rglob("*"))

    assert renderer.main(arguments) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected_receipt
    assert captured.err == ""
    assert source_calls == [SOURCE_HEAD]
    assert _file_snapshots(tmp_path) == before
    assert set(tmp_path.rglob("*")) == before_paths


@pytest.mark.parametrize(
    "missing_flag",
    (
        "--actions-run-metadata",
        "--actions-artifact-metadata",
        "--source-head",
        "--release-tag",
        "--expected-github-run-id",
        "--expected-github-run-attempt",
        "--expected-github-artifact-id",
    ),
)
def test_downloaded_promotion_cli_requires_external_metadata_and_independent_selectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing_flag: str,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    arguments = _downloaded_promotion_cli_arguments(
        tmp_path, archive_path, actions_artifact, actions_run
    )
    index = arguments.index(missing_flag)
    del arguments[index : index + 2]
    source_calls = _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    before = _file_snapshots(tmp_path)

    with pytest.raises(ValueError, match="required"):
        renderer.main(arguments)

    assert capsys.readouterr().out == ""
    assert source_calls == []
    assert _file_snapshots(tmp_path) == before


@pytest.mark.parametrize(
    ("flag", "unexpected_value"),
    (
        ("--source-head", "b" * 40),
        ("--release-tag", f"yfeistai-first-release-20260826-{SOURCE_HEAD[:8]}"),
        ("--expected-github-run-id", str(int(GITHUB_RUN_ID) + 1)),
        ("--expected-github-run-attempt", "2"),
        ("--expected-github-artifact-id", "24681"),
    ),
    ids=("source-head", "release-tag", "run-id", "run-attempt", "artifact-id"),
)
def test_downloaded_promotion_cli_rejects_selector_drift_before_archive_or_source_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    unexpected_value: str,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    arguments = _downloaded_promotion_cli_arguments(
        tmp_path, archive_path, actions_artifact, actions_run
    )
    arguments[arguments.index(flag) + 1] = unexpected_value
    source_calls = _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    read_fixed_file_bytes = renderer._read_fixed_file_bytes

    def forbid_archive_read(path: Path, **kwargs: Any) -> Any:
        assert Path(path) != archive_path, "independent selectors must be checked before ZIP I/O"
        return read_fixed_file_bytes(path, **kwargs)

    monkeypatch.setattr(renderer, "_read_fixed_file_bytes", forbid_archive_read)
    before = _file_snapshots(tmp_path)

    with pytest.raises(ValueError):
        renderer.main(arguments)

    assert capsys.readouterr().out == ""
    assert source_calls == []
    assert _file_snapshots(tmp_path) == before


@pytest.mark.parametrize(
    ("metadata_kind", "case"),
    (
        ("run", "malformed"),
        ("artifact", "not-object"),
        ("run", "missing"),
        ("artifact", "missing"),
        ("run", "oversized"),
        ("artifact", "oversized"),
    ),
)
def test_downloaded_promotion_cli_rejects_invalid_external_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metadata_kind: str,
    case: str,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    arguments = _downloaded_promotion_cli_arguments(
        tmp_path, archive_path, actions_artifact, actions_run
    )
    index = arguments.index(f"--actions-{metadata_kind}-metadata") + 1
    metadata_path = Path(arguments[index])
    if case == "malformed":
        metadata_path.write_bytes(b'{"id":')
    elif case == "not-object":
        metadata_path.write_bytes(b"[]")
    elif case == "missing":
        arguments[index] = str(tmp_path / "absent-actions-metadata.json")
    else:
        assert case == "oversized"
        body = metadata_path.read_bytes()
        metadata_path.write_bytes(body + b" " * (1024 * 1024 + 1 - len(body)))
    source_calls = _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    before = _file_snapshots(tmp_path)

    with pytest.raises(ValueError):
        renderer.main(arguments)

    assert capsys.readouterr().out == ""
    assert source_calls == []
    assert _file_snapshots(tmp_path) == before


@pytest.mark.parametrize("metadata_kind", ("run", "artifact"))
def test_downloaded_promotion_cli_rejects_symlinked_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metadata_kind: str,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    arguments = _downloaded_promotion_cli_arguments(
        tmp_path, archive_path, actions_artifact, actions_run
    )
    index = arguments.index(f"--actions-{metadata_kind}-metadata") + 1
    link_path = tmp_path / "linked-actions-metadata.json"
    try:
        link_path.symlink_to(Path(arguments[index]))
    except OSError as error:
        pytest.skip(f"file symlink unavailable: {error}")
    arguments[index] = str(link_path)
    source_calls = _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    before = _file_snapshots(tmp_path)

    with pytest.raises(ValueError):
        renderer.main(arguments)

    assert capsys.readouterr().out == ""
    assert source_calls == []
    assert _file_snapshots(tmp_path) == before


@pytest.mark.parametrize(
    "case", ("failed-run", "tampered-archive", "source-template-drift", "archive-directory")
)
def test_downloaded_promotion_cli_cannot_bypass_strict_archive_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    templates = _promotion_source_compose_templates()
    if case == "failed-run":
        actions_run["conclusion"] = "failure"
    elif case == "tampered-archive":
        member = "artifacts/promotion_receipt.json"
        archive_body = _promotion_archive_bytes(
            candidate_root,
            member_overrides={member: _tampered_promotion_member(candidate_root, member)},
        )
        archive_path.write_bytes(archive_body)
        actions_artifact = _artifact_metadata_for_archive(actions_artifact, archive_body)
    elif case == "source-template-drift":
        templates["docker-compose.yml"] += b"\n# trusted-source-template-drift\n"
    else:
        assert case == "archive-directory"
        archive_path = candidate_root
    arguments = _downloaded_promotion_cli_arguments(
        tmp_path, archive_path, actions_artifact, actions_run
    )
    arguments.extend(("--candidate-root", str(candidate_root)))
    source_calls = _stub_downloaded_promotion_source_templates(
        renderer, monkeypatch, templates=templates
    )
    before = _file_snapshots(tmp_path)

    with pytest.raises(ValueError):
        renderer.main(arguments)

    assert capsys.readouterr().out == ""
    assert source_calls == (
        [SOURCE_HEAD] if case in ("tampered-archive", "source-template-drift") else []
    )
    assert _file_snapshots(tmp_path) == before


def test_downloaded_promotion_cli_action_is_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = _load_renderer()

    with pytest.raises(SystemExit) as error:
        renderer.main(
            [
                "--verify-downloaded-promotion-artifact",
                "promotion.zip",
                "--verify-promotion-receipt",
            ]
        )

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert captured.out == ""
    assert "not allowed with argument" in captured.err


def test_downloaded_promotion_artifact_binds_compose_templates_to_actions_source_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    source_templates = _promotion_source_compose_templates()
    source_templates["docker-compose.yml"] += b"\n# source-head-only-template\n"
    calls = _stub_downloaded_promotion_source_templates(
        renderer,
        monkeypatch,
        templates=source_templates,
    )

    with pytest.raises(ValueError):
        renderer.validate_downloaded_promotion_artifact(
            archive_path,
            actions_artifact=actions_artifact,
            actions_run=actions_run,
        )

    assert calls == [SOURCE_HEAD]


@pytest.mark.parametrize(
    "drift",
    (
        "archive-digest",
        "artifact-name",
        "artifact-id",
        "artifact-size",
        "artifact-run-id",
        "artifact-run-head-branch",
        "artifact-run-head-sha",
        "run-id",
        "repository",
        "event",
        "status",
        "head-branch",
        "head-sha",
        "workflow-path",
        "workflow-path-tag-suffix",
        "run-attempt",
        "run-conclusion",
        "expired",
    ),
)
def test_downloaded_promotion_artifact_rejects_archive_digest_or_actions_identity_drift(
    tmp_path: Path,
    drift: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    archive_path, original_artifact, original_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    actions_artifact = {
        **original_artifact,
        "workflow_run": dict(original_artifact["workflow_run"]),
    }
    actions_run = {
        **original_run,
        "repository": dict(original_run["repository"]),
    }

    if drift == "archive-digest":
        digest = str(actions_artifact["digest"])
        actions_artifact["digest"] = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    elif drift == "artifact-name":
        actions_artifact["name"] = f"{PROMOTION_ARTIFACT_NAME}-other"
    elif drift == "artifact-id":
        actions_artifact["id"] = 0
    elif drift == "artifact-size":
        actions_artifact["size_in_bytes"] = int(actions_artifact["size_in_bytes"]) + 1
    elif drift == "artifact-run-id":
        actions_artifact["workflow_run"]["id"] = int(GITHUB_RUN_ID) + 1
    elif drift == "artifact-run-head-branch":
        actions_artifact["workflow_run"]["head_branch"] = f"{RELEASE_TAG}-other"
    elif drift == "artifact-run-head-sha":
        actions_artifact["workflow_run"]["head_sha"] = "b" * 40
    elif drift == "run-id":
        actions_run["id"] = int(GITHUB_RUN_ID) + 1
    elif drift == "repository":
        actions_run["repository"]["full_name"] = "xinlingzhifei/another-repository"
    elif drift == "event":
        actions_run["event"] = "workflow_dispatch"
    elif drift == "status":
        actions_run["status"] = "in_progress"
    elif drift == "head-branch":
        actions_run["head_branch"] = f"{RELEASE_TAG}-other"
    elif drift == "head-sha":
        actions_run["head_sha"] = "b" * 40
    elif drift == "workflow-path":
        actions_run["path"] = ".github/workflows/other.yml"
    elif drift == "workflow-path-tag-suffix":
        actions_run["path"] = f"{GITHUB_WORKFLOW_PATH}@{RELEASE_TAG}"
    elif drift == "run-attempt":
        actions_run["run_attempt"] = 2
    elif drift == "run-conclusion":
        actions_run["conclusion"] = "failure"
    else:
        assert drift == "expired"
        actions_artifact["expired"] = True

    with pytest.raises(ValueError):
        renderer.validate_downloaded_promotion_artifact(
            archive_path,
            actions_artifact=actions_artifact,
            actions_run=actions_run,
        )


@pytest.mark.parametrize(
    "member",
    (
        "deploy/image-lock.json",
        "docker-compose.yml",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
        f"deploy/{OBSERVER_OVERLAY_NAME}",
        "artifacts/source_head.json",
        "artifacts/image_digests.json",
        "artifacts/promotion_intent.json",
        "artifacts/promotion_receipt.json",
    ),
    ids=(
        "image-lock",
        "base-compose",
        "platform-compose",
        "data-plane-compose",
        "observer-compose",
        "source-head-receipt",
        "image-digests-receipt",
        "promotion-intent",
        "promotion-receipt",
    ),
)
def test_downloaded_promotion_artifact_rejects_digest_bound_semantic_member_tampering(
    tmp_path: Path,
    member: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    _archive_path, actions_artifact, actions_run, candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    archive_body = _promotion_archive_bytes(
        candidate_root,
        member_overrides={member: _tampered_promotion_member(candidate_root, member)},
    )
    archive_path = tmp_path / f"semantic-tamper-{Path(member).name}.zip"
    archive_path.write_bytes(archive_body)

    with pytest.raises(ValueError):
        renderer.validate_downloaded_promotion_artifact(
            archive_path,
            actions_artifact=_artifact_metadata_for_archive(actions_artifact, archive_body),
            actions_run=actions_run,
        )


@pytest.mark.parametrize(
    "case",
    (
        "extra",
        "duplicate",
        "traversal",
        "absolute",
        "backslash-traversal",
        "symlink",
        "member-size",
        "total-size",
        "compression-ratio",
    ),
)
def test_downloaded_promotion_artifact_rejects_unsafe_or_unbounded_zip_members(
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    _archive_path, actions_artifact, actions_run, candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    archive_body = _unsafe_promotion_archive_bytes(candidate_root, case)
    archive_path = tmp_path / f"unsafe-{case}.zip"
    archive_path.write_bytes(archive_body)

    with pytest.raises(ValueError):
        renderer.validate_downloaded_promotion_artifact(
            archive_path,
            actions_artifact=_artifact_metadata_for_archive(actions_artifact, archive_body),
            actions_run=actions_run,
        )


def test_downloaded_promotion_artifact_reads_one_fixed_archive_descriptor_during_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    _stub_downloaded_promotion_source_templates(renderer, monkeypatch)
    archive_path, actions_artifact, actions_run, _candidate_root = (
        _write_downloaded_promotion_artifact(tmp_path)
    )
    archive_key = os.path.normcase(os.path.abspath(archive_path))
    real_open = renderer.os.open
    real_read = renderer.os.read
    real_close = renderer.os.close
    real_stat = renderer.os.stat
    real_lstat = renderer.os.lstat
    real_read_bytes = Path.read_bytes
    replacement_path = tmp_path / "replacement-promotion-artifact.zip"
    replacement_path.write_bytes(b"replacement promotion artifact identity\n")
    replacement_stat = real_stat(replacement_path)
    replacement_lstat = real_lstat(replacement_path)
    descriptors: set[int] = set()
    closed: set[int] = set()
    path_swapped = False

    def is_archive(path: Any) -> bool:
        return not isinstance(path, int) and os.path.normcase(os.path.abspath(path)) == archive_key

    def forbidden_path_read(path: Path) -> bytes:
        if is_archive(path):
            raise AssertionError("promotion archive must be read through one fixed descriptor")
        return real_read_bytes(path)

    def observed_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if is_archive(path):
            descriptors.add(descriptor)
        return descriptor

    def observed_read(descriptor: int, size: int) -> bytes:
        nonlocal path_swapped
        chunk = real_read(descriptor, size)
        if descriptor in descriptors and chunk and not path_swapped:
            path_swapped = True
        return chunk

    def observed_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        details = real_stat(path, *args, **kwargs)
        if path_swapped and is_archive(path):
            return replacement_stat
        return details

    def observed_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        details = real_lstat(path, *args, **kwargs)
        if path_swapped and is_archive(path):
            return replacement_lstat
        return details

    def observed_close(descriptor: int) -> None:
        if descriptor in descriptors:
            closed.add(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(Path, "read_bytes", forbidden_path_read)
    monkeypatch.setattr(renderer.os, "open", observed_open)
    monkeypatch.setattr(renderer.os, "read", observed_read)
    monkeypatch.setattr(renderer.os, "stat", observed_stat)
    monkeypatch.setattr(renderer.os, "lstat", observed_lstat)
    monkeypatch.setattr(renderer.os, "close", observed_close)

    with pytest.raises(ValueError, match="changed"):
        renderer.validate_downloaded_promotion_artifact(
            archive_path,
            actions_artifact=actions_artifact,
            actions_run=actions_run,
        )

    assert len(descriptors) == 1
    assert descriptors == closed
    assert path_swapped is True


def test_private_platform_workflow_upload_contract_is_candidate_unique_and_api_addressable() -> (
    None
):
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]
    uploads = [step for step in steps if step.get("uses") == UPLOAD_ARTIFACT_ACTION]

    assert len(uploads) == 1
    upload = uploads[0]
    parameters = upload["with"]
    assert parameters["name"] == (
        "private-platform-image-lock-${{ github.ref_name }}-${{ github.sha }}"
    )
    assert parameters["if-no-files-found"] == "error"
    assert parameters.get("overwrite", False) in (False, "false")
    assert {line.strip() for line in str(parameters["path"]).splitlines() if line.strip()} == {
        f"release-candidate/{name}" for name in PROMOTION_ARCHIVE_MEMBERS
    }


def test_promotion_receipt_verifier_binds_uploaded_base_compose(tmp_path: Path) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    base_compose = candidate_root / "docker-compose.yml"
    lock = json.loads((candidate_root / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    pocketbase = lock["images"]["pocketbase"]
    assert pocketbase["reference"] == (
        f"ghcr.io/muchobien/pocketbase:latest@{pocketbase['digest']}"
    )
    assert pocketbase["reference"] in base_compose.read_text(encoding="utf-8")

    renderer.validate_promotion_receipt(
        candidate_root,
        expected_github_run_id=GITHUB_RUN_ID,
        expected_github_run_attempt=1,
    )
    base_compose.write_bytes(b"name: substituted-candidate\nservices: {}\n")

    with pytest.raises(ValueError):
        renderer.validate_promotion_receipt(
            candidate_root,
            expected_github_run_id=GITHUB_RUN_ID,
            expected_github_run_attempt=1,
        )


def test_promotion_receipt_verifier_binds_all_image_lock_digests(tmp_path: Path) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)

    renderer.validate_promotion_receipt(
        candidate_root,
        expected_github_run_id=GITHUB_RUN_ID,
        expected_github_run_attempt=1,
    )
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for index, name in enumerate(
        ("nginx", "postgres", "minio", "minio_client", "pocketbase"),
        start=91,
    ):
        record = lock["images"][name]
        digest = "sha256:" + f"{index:064x}"
        record["digest"] = digest
        record["reference"] = f"{record['repository']}:{record['tag']}@{digest}"
    lock_path.write_bytes(
        (json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    (candidate_root / "docker-compose.platform.yml").write_text(
        renderer._render_compose_references(
            ROOT / "docker-compose.platform.yml",
            lock["images"],
            lock["supportImages"],
        ),
        encoding="utf-8",
    )
    (candidate_root / "docker-compose.yml").write_text(
        renderer._render_compose_references(
            ROOT / "docker-compose.yml",
            lock["images"],
            lock["supportImages"],
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        renderer.validate_promotion_receipt(
            candidate_root,
            expected_github_run_id=GITHUB_RUN_ID,
            expected_github_run_attempt=1,
        )


def test_candidate_lease_allows_proof_outputs_without_changing_candidate_files(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    with renderer.validated_candidate_artifact_lease(candidate_root) as artifact:
        paths = (artifact.paths.image_lock, *(path for path, _body in artifact.compose_bytes))
        before = {
            path: (path.read_bytes(), renderer._stable_file_signature(path.stat()))
            for path in paths
        }
        for relative in ("raw/probe.json", "proof.json", "deploy/proof.json"):
            proof = candidate_root / relative
            proof.parent.mkdir(parents=True, exist_ok=True)
            proof.write_bytes(b"unrelated probe evidence\n")
            artifact.check_unchanged()
        assert {
            path: (path.read_bytes(), renderer._stable_file_signature(path.stat()))
            for path in paths
        } == before


@pytest.mark.parametrize("directory", (".", "deploy"), ids=("root", "deploy"))
@pytest.mark.parametrize("change", ("identity", "symlink", "reparse", "mode"))
@pytest.mark.parametrize("phase", ("explicit-check", "lease-exit"))
def test_candidate_lease_rejects_directory_identity_or_topology_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    change: str,
    phase: str,
) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    target = candidate_root / directory
    replacement = tmp_path / "replacement-directory"
    replacement.mkdir()
    original_lstat = Path.lstat
    details = original_lstat(target)
    replacement_details = original_lstat(replacement)
    assert details.st_ino != replacement_details.st_ino
    changed = SimpleNamespace(
        **{
            name: getattr(details, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        },
        st_file_attributes=getattr(details, "st_file_attributes", 0),
    )
    if change == "identity":
        changed.st_ino = replacement_details.st_ino
    elif change == "symlink":
        changed.st_mode = stat.S_IFLNK | stat.S_IMODE(details.st_mode)
    elif change == "reparse":
        changed.st_file_attributes |= 0x400
    else:
        changed.st_mode ^= stat.S_IWUSR
    target_key = os.path.normcase(os.path.abspath(target))

    def changed_directory(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == target_key:
            return changed
        return original_lstat(path, *args, **kwargs)

    with pytest.raises(ValueError, match="candidate artifact changed"):
        with renderer.validated_candidate_artifact_lease(candidate_root) as artifact:
            monkeypatch.setattr(Path, "lstat", changed_directory)
            if phase == "explicit-check":
                artifact.check_unchanged()
                pytest.fail("explicit check must reject before lease exit")


def test_candidate_artifact_reader_uses_fixed_no_follow_handle_against_pathname_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    document_path = tmp_path / "promotion_intent.json"
    trusted_body = b'{"schemaVersion":1,"sourceHead":"trusted"}\n'
    document_path.write_bytes(b'{"schemaVersion":1, "sourceHead":"attacker"}\n')
    real_read_bytes = Path.read_bytes
    pathname_reads = 0
    opened: dict[int, int] = {}
    descriptor_reads: set[int] = set()
    descriptor_stats: set[int] = set()
    real_open = renderer.os.open
    real_read = renderer.os.read
    real_fstat = renderer.os.fstat

    def aba_read_bytes(path: Path) -> bytes:
        nonlocal pathname_reads
        if Path(path) == document_path:
            pathname_reads += 1
            if pathname_reads == 2:
                return trusted_body
        return real_read_bytes(path)

    def observed_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if not isinstance(path, int) and Path(path) == document_path:
            opened[descriptor] = flags
        return descriptor

    def observed_read(descriptor: int, size: int) -> bytes:
        if descriptor in opened:
            descriptor_reads.add(descriptor)
        return real_read(descriptor, size)

    def observed_fstat(descriptor: int) -> Any:
        if descriptor in opened:
            descriptor_stats.add(descriptor)
        return real_fstat(descriptor)

    monkeypatch.setattr(Path, "read_bytes", aba_read_bytes)
    monkeypatch.setattr(renderer.os, "open", observed_open)
    monkeypatch.setattr(renderer.os, "read", observed_read)
    monkeypatch.setattr(renderer.os, "fstat", observed_fstat)

    with pytest.raises(ValueError, match="promotion intent"):
        renderer._read_canonical_promotion_document(
            document_path,
            label="promotion intent",
        )

    assert opened, "candidate artifacts must be opened through a fixed descriptor"
    assert set(opened).issubset(descriptor_reads)
    assert set(opened).issubset(descriptor_stats)
    no_follow = getattr(renderer.os, "O_NOFOLLOW", 0)
    if no_follow:
        assert all(flags & no_follow for flags in opened.values())


def test_promotion_receipt_verifier_holds_and_rereads_one_descriptor_per_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    protected = {
        str(path.resolve())
        for path in (
            candidate_root / "deploy" / "image-lock.json",
            candidate_root / "docker-compose.yml",
            candidate_root / "docker-compose.platform.yml",
            candidate_root / "docker-compose.data-plane.yml",
            candidate_root / "deploy" / OBSERVER_OVERLAY_NAME,
            candidate_root / "artifacts" / "promotion_intent.json",
            candidate_root / "artifacts" / "promotion_receipt.json",
        )
    }
    real_open = renderer.os.open
    real_close = renderer.os.close
    real_lseek = renderer.os.lseek
    descriptors: dict[int, str] = {}
    opens: list[str] = []
    closes: set[int] = set()
    rewind_counts: dict[int, int] = {}

    def observed_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        resolved = str(Path(path).resolve()) if not isinstance(path, int) else ""
        if resolved in protected:
            descriptors[descriptor] = resolved
            opens.append(resolved)
        return descriptor

    def observed_lseek(descriptor: int, position: int, how: int) -> int:
        if descriptor in descriptors and position == 0 and how == renderer.os.SEEK_SET:
            rewind_counts[descriptor] = rewind_counts.get(descriptor, 0) + 1
        return real_lseek(descriptor, position, how)

    def observed_close(descriptor: int) -> None:
        if descriptor in descriptors:
            closes.add(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(renderer.os, "open", observed_open)
    monkeypatch.setattr(renderer.os, "lseek", observed_lseek)
    monkeypatch.setattr(renderer.os, "close", observed_close)

    renderer.validate_promotion_receipt(
        candidate_root,
        expected_github_run_id=GITHUB_RUN_ID,
        expected_github_run_attempt=1,
    )

    assert set(opens) == protected
    assert len(opens) == len(protected)
    assert set(descriptors) == closes
    assert {rewind_counts[descriptor] for descriptor in descriptors} == {2}


def test_promotion_receipt_verifier_reverse_reread_detects_late_earlier_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    candidate_root = _write_candidate_with_promotion_receipt(tmp_path)
    lock_path = str((candidate_root / "deploy" / "image-lock.json").resolve())
    receipt_path = str((candidate_root / "artifacts" / "promotion_receipt.json").resolve())
    real_open = renderer.os.open
    real_lseek = renderer.os.lseek
    real_read = renderer.os.read
    descriptor_paths: dict[int, str] = {}
    rewind_counts: dict[int, int] = {}
    rewrite_earlier = False

    def observed_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if not isinstance(path, int):
            descriptor_paths[descriptor] = str(Path(path).resolve())
        return descriptor

    def observed_lseek(descriptor: int, position: int, how: int) -> int:
        if descriptor in descriptor_paths and position == 0 and how == renderer.os.SEEK_SET:
            rewind_counts[descriptor] = rewind_counts.get(descriptor, 0) + 1
        return real_lseek(descriptor, position, how)

    def observed_read(descriptor: int, size: int) -> bytes:
        nonlocal rewrite_earlier
        chunk = real_read(descriptor, size)
        path = descriptor_paths.get(descriptor)
        if path == receipt_path and rewind_counts.get(descriptor) == 2 and chunk:
            rewrite_earlier = True
        if path == lock_path and rewrite_earlier and rewind_counts.get(descriptor) == 2 and chunk:
            return bytes((chunk[0] ^ 1,)) + chunk[1:]
        return chunk

    monkeypatch.setattr(renderer.os, "open", observed_open)
    monkeypatch.setattr(renderer.os, "lseek", observed_lseek)
    monkeypatch.setattr(renderer.os, "read", observed_read)

    with pytest.raises(ValueError, match="changed during verification"):
        renderer.validate_promotion_receipt(
            candidate_root,
            expected_github_run_id=GITHUB_RUN_ID,
            expected_github_run_attempt=1,
        )

    assert rewrite_earlier is True


def test_private_platform_workflow_verifies_promotion_receipt_before_artifact_upload() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]

    receipt_index, receipt_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Record verified promotion receipt"
    )
    verify_index, verify_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Verify promotion receipt"
    )
    upload_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == UPLOAD_ARTIFACT_ACTION
    )
    assert receipt_index < verify_index < upload_index

    receipt_command = str(receipt_step["run"])
    for field in (
        "sourceRepository",
        "sourceHead",
        "releaseTag",
        "openmaicHead",
        "githubRunId",
        "githubRunAttempt",
        "githubWorkflowRef",
    ):
        assert f'"{field}"' in receipt_command
    assert 'os.environ["GITHUB_RUN_ID"]' in receipt_command
    assert 'int(os.environ["GITHUB_RUN_ATTEMPT"])' in receipt_command
    assert 'os.environ["GITHUB_WORKFLOW_REF"]' in receipt_command

    verify_command = str(verify_step["run"])
    assert "scripts/render_platform_compose.py" in verify_command
    assert "--verify-promotion-receipt" in verify_command
    assert '--candidate-root "$GITHUB_WORKSPACE/release-candidate"' in verify_command
    assert '--expected-github-run-id "$GITHUB_RUN_ID"' in verify_command
    assert '--expected-github-run-attempt "$GITHUB_RUN_ATTEMPT"' in verify_command


def test_private_platform_workflow_emits_task1_receipts_from_clean_source() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]

    prepare_index, prepare = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Prepare candidate evidence workspace"
    )
    checkout_openmaic_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Checkout OpenMAIC"
    )
    assert prepare_index < checkout_openmaic_index
    prepare_command = str(prepare["run"])
    assert ".git/info/exclude" in prepare_command
    assert "/openmaic-upstream/" in prepare_command
    assert "/release-candidate/" in prepare_command
    assert "release-candidate/docker-compose.yml" in prepare_command
    assert "release-candidate/docker-compose.platform.yml" in prepare_command
    assert "release-candidate/docker-compose.data-plane.yml" in prepare_command
    observer_overlay = (
        "release-candidate/deploy/docker-compose.openmaic-shared-ingress-observer.yml"
    )
    assert observer_overlay in prepare_command

    write_index, write_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Record image digests"
    )
    write_command = str(write_step["run"])
    assert "--lock-path release-candidate/deploy/image-lock.json" in write_command
    assert write_command.count("--compose-path release-candidate/") == 4
    assert "--compose-path release-candidate/docker-compose.yml" in write_command
    assert f"--compose-path {observer_overlay}" in write_command

    verify_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Verify image lock"
    )
    verify_command = str(steps[verify_index]["run"])
    assert 'Path("release-candidate/docker-compose.yml")' in verify_command
    assert f'Path("{observer_overlay}")' in verify_command
    receipt_index, receipt_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Record Task1 release evidence"
    )
    assert write_index < verify_index < receipt_index
    receipt_command = str(receipt_step["run"])
    logical_command = re.sub(r"\\\r?\n\s*", " ", receipt_command)
    assert re.search(r"classroom_release_evidence\.py [^\n]*\bsource-head\b", logical_command)
    assert re.search(r"classroom_release_evidence\.py [^\n]*\bimage-digests\b", logical_command)
    assert receipt_command.count('--candidate-root "$GITHUB_WORKSPACE/release-candidate"') == 2
    assert "--source-root ." in receipt_command
    assert "release-candidate/artifacts/source_head.json" in receipt_command
    assert "release-candidate/artifacts/image_digests.json" in receipt_command
    assert "GITHUB_RUN_ID" in receipt_command
    assert "GITHUB_RUN_ATTEMPT" in receipt_command

    upload_index, upload_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == UPLOAD_ARTIFACT_ACTION
    )
    assert receipt_index < upload_index
    artifact_paths = {
        line.strip() for line in str(upload_step["with"]["path"]).splitlines() if line.strip()
    }
    assert artifact_paths == {
        "release-candidate/deploy/image-lock.json",
        "release-candidate/docker-compose.yml",
        "release-candidate/docker-compose.platform.yml",
        "release-candidate/docker-compose.data-plane.yml",
        observer_overlay,
        "release-candidate/artifacts/source_head.json",
        "release-candidate/artifacts/image_digests.json",
        "release-candidate/artifacts/promotion_intent.json",
        "release-candidate/artifacts/promotion_receipt.json",
    }


def test_private_platform_workflow_requires_explicit_private_task1_receipt_policy() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]
    receipt_step = next(
        step for step in steps if step.get("name") == "Record Task1 release evidence"
    )
    assert receipt_step["shell"] == "bash"
    command = str(receipt_step["run"])
    logical_command = re.sub(r"\\\r?\n\s*", " ", command)
    receipt_commands = [
        shlex.split(line)
        for line in logical_command.splitlines()
        if line.strip().startswith("python scripts/classroom_release_evidence.py ")
    ]
    assert len(receipt_commands) == 2
    for arguments, subcommand in zip(
        receipt_commands, ("source-head", "image-digests"), strict=True
    ):
        assert arguments[:7] == [
            "python",
            "scripts/classroom_release_evidence.py",
            "--receipt-recovery-root",
            "$receipt_recovery_root",
            "--receipt-worker-uid",
            "$receipt_worker_uid",
            subcommand,
        ], "both Task1 producers must pass the same explicit policy before their subcommand"
    assert command.count('--receipt-recovery-root "$receipt_recovery_root"') == 2
    assert command.count('--receipt-worker-uid "$receipt_worker_uid"') == 2

    preparation = command.split("python scripts/classroom_release_evidence.py", 1)[0]
    for required in (
        "set -euo pipefail",
        "umask 077",
        'receipt_publisher_uid="$(id -u)"',
        'receipt_worker_uid="$(id -u nobody)"',
        '[[ "$receipt_worker_uid" -gt 0 ]]',
        '[[ "$receipt_worker_uid" -ne "$receipt_publisher_uid" ]]',
        'receipt_recovery_root="$(mktemp -d "$GITHUB_WORKSPACE/../.receipt-recovery.XXXXXXXX")"',
        'receipt_recovery_root="$(realpath -e "$receipt_recovery_root")"',
        'chmod 700 "$receipt_recovery_root"',
        '[[ "$(stat -c %d "$receipt_recovery_root")" == '
        '"$(stat -c %d "$GITHUB_WORKSPACE/release-candidate")" ]]',
        'python - "$receipt_recovery_root" "$receipt_worker_uid"',
        "from receipt_recovery import PosixReceiptRecovery",
        "PosixReceiptRecovery.open(Path(sys.argv[1]), worker_uid=int(sys.argv[2]))",
    ):
        assert required in preparation, (
            f"receipt policy admission is missing before publication: {required}"
        )
    for forbidden in ("/tmp", "TMPDIR", "RUNNER_TEMP", "useradd", "adduser", "|| true"):
        assert forbidden not in preparation, (
            "receipt policy must not fall back or provision accounts"
        )


@pytest.mark.skipif(sys.platform != "linux", reason="requires the native Linux workflow shell")
def test_private_platform_workflow_executes_task1_receipts_with_real_git(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/private-platform-images.yml").read_text(encoding="utf-8")
    )
    steps = next(iter(workflow["jobs"].values()))["steps"]
    receipt_step = next(
        step for step in steps if step.get("name") == "Record Task1 release evidence"
    )
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "classroom_release_evidence.py").write_bytes(
        (ROOT / "scripts/classroom_release_evidence.py").read_bytes()
    )
    template = tmp_path / "empty-git-template"
    template.mkdir()
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "scripts"))),
            "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ["PATH"])),
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_RUN_ID": GITHUB_RUN_ID,
            "GITHUB_RUN_ATTEMPT": "1",
        }
    )

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    git("init", "--quiet", f"--template={template}")
    (workspace / ".git/info").mkdir(exist_ok=True)
    (workspace / ".git/info/exclude").write_text("/release-candidate/\n", encoding="utf-8")
    git("add", "scripts/classroom_release_evidence.py")
    git(
        "-c",
        "user.name=Receipt fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--no-gpg-sign",
        "--quiet",
        "-m",
        "isolated workflow receipt fixture",
    )
    git("remote", "add", "origin", f"https://github.com/{SOURCE_REPOSITORY}.git")
    source_head = git("rev-parse", "HEAD")
    candidate_root = workspace / "release-candidate"
    for relative in PROMOTION_ARCHIVE_MEMBERS[1:5]:
        target = candidate_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    lock = _load_renderer().write_image_lock(
        candidate_root / "deploy/image-lock.json",
        digest_resolver=lambda reference: (
            "sha256:" + hashlib.sha256(reference.encode("utf-8")).hexdigest()
        ),
        compose_paths=tuple(
            candidate_root / relative for relative in PROMOTION_ARCHIVE_MEMBERS[1:5]
        ),
        source_repository=SOURCE_REPOSITORY,
        source_head=source_head,
        release_tag=f"yfeistai-first-release-20260906-{source_head[:8]}",
        openmaic_head=OPENMAIC_HEAD,
    )
    candidate_bytes = {
        relative: (candidate_root / relative).read_bytes()
        for relative in PROMOTION_ARCHIVE_MEMBERS[:5]
    }
    assert git("status", "--porcelain=v1", "--untracked-files=all") == ""

    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", str(receipt_step["run"])],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    for evidence, checks in (
        ("source_head", {"headMatches": True, "worktreeClean": True}),
        ("image_digests", {"lockMatches": True, "composeMatches": True}),
    ):
        receipt = json.loads((candidate_root / f"artifacts/{evidence}.json").read_bytes())
        assert receipt["candidate"] == lock["candidate"]
        assert receipt["candidate"]["sourceHead"] == source_head
        assert receipt["evidence"] == evidence
        assert receipt["releaseRun"] == {
            "runId": f"github-{GITHUB_RUN_ID}-1",
            "environmentId": "ghcr-production",
        }
        assert receipt["receipt"]["result"] == {
            "outcome": "pass",
            "nativeExit": 0,
            "checks": checks,
        }
    assert {path.name for path in (candidate_root / "artifacts").iterdir()} == {
        "source_head.json",
        "image_digests.json",
    }
    assert {
        relative: (candidate_root / relative).read_bytes() for relative in candidate_bytes
    } == candidate_bytes
    assert git("rev-parse", "HEAD") == source_head
    assert git("status", "--porcelain=v1", "--untracked-files=all") == ""


def test_private_platform_workflow_installs_receipt_dependencies_before_recording() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]

    install_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Install release receipt dependencies"
    ]
    assert len(install_steps) == 1, "release receipt dependencies must be installed exactly once"
    install_index, install_step = install_steps[0]
    receipt_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Record Task1 release evidence"
    )
    assert install_index < receipt_index

    command = str(install_step["run"])
    assert "python -m pip install" in command
    assert "--disable-pip-version-check" in command
    assert "--no-input" in command
    assert "--only-binary=:all:" in command
    assert "python-pptx==1.0.2" in command
    assert "pydantic==2.13.3" in command
    assert "PyYAML==6.0.3" in command
    assert "cryptography==47.0.0" in command


def test_image_lock_writer_binds_source_head_release_tag_and_custom_digests(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    platform_compose, data_plane_compose, observer_overlay = _copy_candidate_compose_files(tmp_path)
    calls: list[str] = []

    def resolve(reference: str) -> str:
        calls.append(reference)
        return "sha256:" + f"{len(calls):064x}"

    written = renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=(platform_compose, data_plane_compose, observer_overlay),
        source_repository=SOURCE_REPOSITORY,
        source_head=SOURCE_HEAD,
        release_tag=RELEASE_TAG,
        openmaic_head=OPENMAIC_HEAD,
    )

    assert written["schemaVersion"] == 3
    assert written["candidate"] == {
        "sourceRepository": SOURCE_REPOSITORY,
        "sourceHead": SOURCE_HEAD,
        "releaseTag": RELEASE_TAG,
        "openmaicHead": OPENMAIC_HEAD,
        "imageDigests": {
            name: written["images"][name]["digest"]
            for name in ("deeptutor", "openmaic", "openmaic_render")
        },
    }
    for name in ("deeptutor", "openmaic", "openmaic_render"):
        record = written["images"][name]
        assert record["tag"] == RELEASE_TAG
        assert record["reference"] == (f"{record['repository']}:{RELEASE_TAG}@{record['digest']}")
    observer_signer = written["supportImages"][OBSERVER_SIGNER_NAME]
    assert observer_signer["repository"] == OBSERVER_SIGNER_REPOSITORY
    assert observer_signer["tag"] == RELEASE_TAG
    assert observer_signer["digest"] not in written["candidate"]["imageDigests"].values()
    assert observer_signer["reference"] == (
        f"{OBSERVER_SIGNER_REPOSITORY}:{RELEASE_TAG}@{observer_signer['digest']}"
    )
    assert len(calls) == 9
    assert {
        f"ghcr.io/xinlingzhifei/deeptutor:{RELEASE_TAG}",
        f"ghcr.io/xinlingzhifei/openmaic:{RELEASE_TAG}",
        f"ghcr.io/xinlingzhifei/openmaic-render:{RELEASE_TAG}",
        f"{OBSERVER_SIGNER_REPOSITORY}:{RELEASE_TAG}",
        "ghcr.io/muchobien/pocketbase:latest",
    }.issubset(calls)
    assert (
        renderer.load_image_lock(
            output_path,
            expected_candidate={
                "sourceRepository": SOURCE_REPOSITORY,
                "sourceHead": SOURCE_HEAD,
                "releaseTag": RELEASE_TAG,
                "openmaicHead": OPENMAIC_HEAD,
            },
        )
        == written
    )


@pytest.mark.parametrize(
    "colliding_names",
    (
        ("deeptutor", "openmaic"),
        ("deeptutor", "openmaic_render"),
        ("openmaic", "openmaic_render"),
    ),
)
def test_image_lock_writer_rejects_resolved_workload_collision_before_content_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    colliding_names: tuple[str, str],
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    output_path.write_bytes(b'{"sentinel": true}\n')
    base_compose = tmp_path / "docker-compose.yml"
    base_compose.write_bytes((ROOT / base_compose.name).read_bytes())
    compose_paths = (base_compose, *_copy_candidate_compose_files(tmp_path))
    protected = {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in (*compose_paths, output_path)
    }
    colliding_references = {
        f"{renderer.IMAGE_SPECS[name]['repository']}:{RELEASE_TAG}" for name in colliding_names
    }
    resolved: list[str] = []
    staged: list[Path] = []
    original_stage = renderer._stage_text

    def resolve(reference: str) -> str:
        resolved.append(reference)
        return "sha256:" + (
            "a" * 64
            if reference in colliding_references
            else hashlib.sha256(reference.encode("utf-8")).hexdigest()
        )

    def stage(path: Path, content: str):
        staged.append(path)
        return original_stage(path, content)

    monkeypatch.setattr(renderer, "_stage_text", stage)
    failure = None
    try:
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )
    except ValueError as error:
        failure = error

    assert colliding_references.issubset(resolved)
    assert failure is not None, f"accepted colliding workload digests after {len(staged)} writes"
    assert "distinct" in str(failure)
    assert staged == []
    assert {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino) for path in protected
    } == protected


def test_image_lock_writer_rejects_zero_source_head_before_digest_resolution(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)

    def reject_digest_resolution(reference: str) -> str:
        pytest.fail(f"digest resolution started for invalid candidate: {reference}")

    with pytest.raises(ValueError, match="image lock candidate source head is invalid"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=reject_digest_resolution,
            compose_paths=compose_paths,
            source_repository=SOURCE_REPOSITORY,
            source_head="0" * 40,
            release_tag=f"{RELEASE_TAG.rsplit('-', maxsplit=1)[0]}-00000000",
            openmaic_head=OPENMAIC_HEAD,
        )

    assert not output_path.exists()


@pytest.mark.parametrize(
    "case",
    (
        "source-repository",
        "source-head",
        "release-tag",
        "openmaic-head",
        "image-digests",
        "extra-field",
    ),
)
def test_image_lock_loader_rejects_candidate_binding_drift(
    tmp_path: Path,
    case: str,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    platform_compose, data_plane_compose, observer_overlay = _copy_candidate_compose_files(tmp_path)
    digest_calls = 0

    def resolve(reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=(platform_compose, data_plane_compose, observer_overlay),
        source_repository=SOURCE_REPOSITORY,
        source_head=SOURCE_HEAD,
        release_tag=RELEASE_TAG,
        openmaic_head=OPENMAIC_HEAD,
    )
    document = json.loads(output_path.read_text(encoding="utf-8"))
    if case == "source-repository":
        document["candidate"]["sourceRepository"] = "example/Other"
    elif case == "source-head":
        document["candidate"]["sourceHead"] = "b" * 40
    elif case == "release-tag":
        document["candidate"]["releaseTag"] = "yfeistai-first-release-20260825-bbbbbbbb"
    elif case == "openmaic-head":
        document["candidate"]["openmaicHead"] = "c" * 40
    elif case == "image-digests":
        document["candidate"]["imageDigests"]["deeptutor"] = "sha256:" + "9" * 64
    else:
        document["candidate"]["unexpected"] = True
    output_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate"):
        renderer.load_image_lock(
            output_path,
            expected_candidate={
                "sourceRepository": SOURCE_REPOSITORY,
                "sourceHead": SOURCE_HEAD,
                "releaseTag": RELEASE_TAG,
                "openmaicHead": OPENMAIC_HEAD,
            },
        )


@pytest.mark.parametrize(
    "case",
    (
        "missing-support-images",
        "missing-observer-signer",
        "zero-digest",
        "workload-digest-collision",
    ),
)
def test_image_lock_loader_rejects_invalid_observer_signer_support_image(
    tmp_path: Path,
    case: str,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    digest_calls = 0

    def resolve(reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    document = renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=compose_paths,
        **CANDIDATE_ARGUMENTS,
    )
    assert document["schemaVersion"] == 3
    assert renderer.load_image_lock(output_path) == document

    if case == "missing-support-images":
        document.pop("supportImages")
    elif case == "missing-observer-signer":
        document["supportImages"].pop(OBSERVER_SIGNER_NAME)
    else:
        signer = document["supportImages"][OBSERVER_SIGNER_NAME]
        signer["digest"] = (
            "sha256:" + ("0" * 64)
            if case == "zero-digest"
            else document["candidate"]["imageDigests"]["deeptutor"]
        )
        signer["reference"] = f"{signer['repository']}:{signer['tag']}@{signer['digest']}"
    output_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        renderer.load_image_lock(output_path)


def test_image_lock_writer_updates_compose_atomically_from_registry_digests(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    platform_compose, data_plane_compose, observer_overlay = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    platform_before = platform_compose.read_bytes()
    data_plane_before = data_plane_compose.read_bytes()
    observer_before = observer_overlay.read_bytes()

    partial_calls: list[str] = []

    def resolve_until_missing(reference: str) -> str | None:
        partial_calls.append(reference)
        if reference == f"{OBSERVER_SIGNER_REPOSITORY}:{RELEASE_TAG}":
            return None
        return "sha256:" + f"{len(partial_calls):064x}"

    with pytest.raises(ValueError, match="registry digest"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve_until_missing,
            compose_paths=(platform_compose, data_plane_compose, observer_overlay),
            **CANDIDATE_ARGUMENTS,
        )
    assert f"{OBSERVER_SIGNER_REPOSITORY}:{RELEASE_TAG}" in partial_calls
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"sentinel": True}
    assert platform_compose.read_bytes() == platform_before
    assert data_plane_compose.read_bytes() == data_plane_before
    assert observer_overlay.read_bytes() == observer_before

    calls: list[str] = []

    def resolve(reference: str) -> str:
        calls.append(reference)
        return "sha256:" + f"{len(calls):064x}"

    written = renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=(platform_compose, data_plane_compose, observer_overlay),
        **CANDIDATE_ARGUMENTS,
    )
    assert set(written["images"]) == {
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
        "pocketbase",
    }
    assert set(written["supportImages"]) == {OBSERVER_SIGNER_NAME}
    assert len(calls) == 9
    assert json.loads(output_path.read_text(encoding="utf-8")) == written
    assert (
        renderer.image_reference("nginx", lock_path=output_path)
        == (written["images"]["nginx"]["reference"])
    )

    platform_text = platform_compose.read_text(encoding="utf-8")
    data_plane_text = data_plane_compose.read_text(encoding="utf-8")
    observer_text = observer_overlay.read_text(encoding="utf-8")
    for name, record in written["images"].items():
        if name == "pocketbase":
            continue
        assert record["reference"] in platform_text, name
    for name in ("openmaic", "openmaic_render"):
        assert written["images"][name]["reference"] in data_plane_text
    assert written["images"]["deeptutor"]["reference"] not in observer_text
    assert observer_text.count(written["supportImages"][OBSERVER_SIGNER_NAME]["reference"]) == 2
    assert "${YFEISTAI_OBSERVER_SIGNER_IMAGE:" not in observer_text
    assert "sha256:" + ("0" * 64) not in platform_text
    assert "sha256:" + ("0" * 64) not in data_plane_text
    assert "sha256:" + ("0" * 64) not in observer_text


def test_image_lock_writer_uses_explicit_candidate_digests_without_resolving_candidate_tags(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    explicit = {
        "deeptutor": "sha256:" + "a" * 64,
        "openmaic": "sha256:" + "b" * 64,
        "openmaic_render": "sha256:" + "c" * 64,
        OBSERVER_SIGNER_NAME: "sha256:" + "d" * 64,
    }
    resolved: list[str] = []

    def resolve(reference: str) -> str:
        resolved.append(reference)
        return "sha256:" + f"{len(resolved) + 4:064x}"

    written = renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        digest_overrides=explicit,
        compose_paths=compose_paths,
        **CANDIDATE_ARGUMENTS,
    )

    assert resolved == [
        "nginx:1.29.8-alpine3.23",
        "postgres:16.14-alpine3.24",
        "minio/minio:RELEASE.2025-04-22T22-12-26Z",
        "minio/mc:RELEASE.2025-04-16T18-13-26Z",
        "ghcr.io/muchobien/pocketbase:latest",
    ]
    assert written["candidate"]["imageDigests"] == {
        name: explicit[name] for name in ("deeptutor", "openmaic", "openmaic_render")
    }
    assert (
        written["supportImages"][OBSERVER_SIGNER_NAME]["digest"] == explicit[OBSERVER_SIGNER_NAME]
    )


@pytest.mark.parametrize(
    "digest_arguments",
    (
        ("deeptutor=sha256:" + "a" * 64,),
        (
            "deeptutor=sha256:" + "a" * 64,
            "deeptutor=sha256:" + "b" * 64,
            "openmaic=sha256:" + "c" * 64,
            "openmaic_render=sha256:" + "d" * 64,
            OBSERVER_SIGNER_NAME + "=sha256:" + "e" * 64,
        ),
        (
            "deeptutor=sha256:" + "a" * 64,
            "openmaic=sha256:" + "b" * 64,
            "openmaic_render=sha256:" + "c" * 64,
            "unknown=sha256:" + "d" * 64,
        ),
        (
            "deeptutor=sha256:" + "0" * 64,
            "openmaic=sha256:" + "b" * 64,
            "openmaic_render=sha256:" + "c" * 64,
            OBSERVER_SIGNER_NAME + "=sha256:" + "d" * 64,
        ),
    ),
)
def test_image_lock_cli_rejects_incomplete_duplicate_or_invalid_explicit_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    digest_arguments: tuple[str, ...],
) -> None:
    renderer = _load_renderer()
    called = False

    def unexpected_write(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid explicit digests must be rejected before publication")

    monkeypatch.setattr(renderer, "write_image_lock", unexpected_write)
    arguments = [
        "--write-image-lock",
        "--lock-path",
        str(tmp_path / "image-lock.json"),
        "--compose-path",
        str(tmp_path / "docker-compose.yml"),
        "--compose-path",
        str(tmp_path / "docker-compose.platform.yml"),
        "--compose-path",
        str(tmp_path / "docker-compose.data-plane.yml"),
        "--compose-path",
        str(tmp_path / OBSERVER_OVERLAY_NAME),
        "--source-repository",
        SOURCE_REPOSITORY,
        "--source-head",
        SOURCE_HEAD,
        "--release-tag",
        RELEASE_TAG,
    ]
    for digest in digest_arguments:
        arguments.extend(("--digest", digest))

    with pytest.raises(ValueError, match="digest"):
        renderer.main(arguments)
    assert called is False


def test_image_lock_cli_updates_both_production_compose_files(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    captured: dict[str, Any] = {}

    def write_image_lock(
        path: Path,
        *,
        compose_paths: tuple[Path, ...],
        source_repository: str,
        source_head: str,
        release_tag: str,
        openmaic_head: str,
    ) -> dict[str, Any]:
        captured["path"] = path
        captured["compose_paths"] = compose_paths
        captured["candidate"] = {
            "source_repository": source_repository,
            "source_head": source_head,
            "release_tag": release_tag,
            "openmaic_head": openmaic_head,
        }
        return {}

    monkeypatch.setattr(renderer, "write_image_lock", write_image_lock)

    assert (
        renderer.main(
            [
                "--write-image-lock",
                "--lock-path",
                str(output_path),
                "--source-repository",
                SOURCE_REPOSITORY,
                "--source-head",
                SOURCE_HEAD,
                "--release-tag",
                RELEASE_TAG,
                "--openmaic-head",
                OPENMAIC_HEAD,
            ]
        )
        == 0
    )
    assert captured == {
        "path": output_path,
        "compose_paths": (
            ROOT / "docker-compose.yml",
            ROOT / "docker-compose.platform.yml",
            ROOT / "docker-compose.data-plane.yml",
            ROOT / "deploy" / OBSERVER_OVERLAY_NAME,
        ),
        "candidate": CANDIDATE_ARGUMENTS,
    }
    assert capsys.readouterr().out == f"{output_path}\n"


def test_image_lock_cli_accepts_explicit_candidate_compose_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    candidate_root = tmp_path / "release-candidate"
    output_path = candidate_root / "deploy" / "image-lock.json"
    compose_paths = (
        candidate_root / "docker-compose.yml",
        candidate_root / "docker-compose.platform.yml",
        candidate_root / "docker-compose.data-plane.yml",
        candidate_root / "deploy" / OBSERVER_OVERLAY_NAME,
    )
    captured: dict[str, Any] = {}

    def write_image_lock(path: Path, **arguments: Any) -> dict[str, Any]:
        captured["path"] = path
        captured.update(arguments)
        return {}

    monkeypatch.setattr(renderer, "write_image_lock", write_image_lock)

    assert (
        renderer.main(
            [
                "--write-image-lock",
                "--lock-path",
                str(output_path),
                "--compose-path",
                str(compose_paths[0]),
                "--compose-path",
                str(compose_paths[1]),
                "--compose-path",
                str(compose_paths[2]),
                "--compose-path",
                str(compose_paths[3]),
                "--source-repository",
                SOURCE_REPOSITORY,
                "--source-head",
                SOURCE_HEAD,
                "--release-tag",
                RELEASE_TAG,
            ]
        )
        == 0
    )
    assert captured["path"] == output_path
    assert captured["compose_paths"] == compose_paths


def test_stage_text_removes_and_syncs_temporary_file_after_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    target = tmp_path / "image-lock.json"
    synced: list[Path] = []

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated stage fsync failure")

    monkeypatch.setattr(renderer.os, "fsync", fail_fsync)
    monkeypatch.setattr(
        renderer,
        "_sync_directory",
        lambda parent: synced.append(Path(parent)),
    )

    caught: BaseException | None = None
    try:
        renderer._stage_text(target, '{"candidate": true}\n')
    except BaseException as error:
        caught = error

    residual = list(tmp_path.glob(".image-lock.json.*.tmp"))
    for staged_path in residual:
        staged_path.unlink(missing_ok=True)

    assert isinstance(caught, OSError)
    assert "simulated stage fsync failure" in str(caught)
    assert residual == []
    assert len(synced) == 2
    assert synced[0].parent == tmp_path
    assert synced[0].name.startswith(".yfeistai-staged-cleanup-")
    assert synced[1] == tmp_path


def test_publish_journal_create_syncs_temp_cleanup_after_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    output_path.write_bytes(b"original image lock\n")
    journal_path = _publish_journal_path(output_path)
    synced: list[Path] = []

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError("simulated journal link failure")

    monkeypatch.setattr(renderer.os, "link", fail_link)
    monkeypatch.setattr(
        renderer,
        "_sync_directory",
        lambda parent: synced.append(Path(parent)),
    )

    with pytest.raises(OSError, match="simulated journal link failure"):
        renderer._create_publish_journal(
            output_path,
            (output_path,),
            {output_path: output_path.read_bytes()},
            transaction_id="a" * 64,
        )

    residual = list(tmp_path.glob(".*.tmp"))
    for staged_path in residual:
        staged_path.unlink(missing_ok=True)

    assert not journal_path.exists()
    assert residual == []
    assert len(synced) == 2
    assert synced[0].parent == tmp_path
    assert synced[0].name.startswith(".yfeistai-staged-cleanup-")
    assert synced[1] == tmp_path


def test_image_lock_writer_syncs_staged_cleanup_after_journal_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    directories = {name: tmp_path / name for name in ("platform", "data", "observer", "lock")}
    for directory in directories.values():
        directory.mkdir()
    platform = directories["platform"] / "docker-compose.platform.yml"
    data_plane = directories["data"] / "docker-compose.data-plane.yml"
    observer = directories["observer"] / OBSERVER_OVERLAY_NAME
    output_path = directories["lock"] / "image-lock.json"
    platform.write_bytes((ROOT / platform.name).read_bytes())
    data_plane.write_bytes((ROOT / data_plane.name).read_bytes())
    observer.write_bytes((ROOT / "deploy" / observer.name).read_bytes())
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    targets = (platform, data_plane, observer, output_path)
    originals = {path: path.read_bytes() for path in targets}
    synced: list[Path] = []
    digest_calls = 0

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def fail_journal_creation(*_args, **_kwargs) -> None:
        raise OSError("simulated journal creation failure")

    monkeypatch.setattr(renderer, "_create_publish_journal", fail_journal_creation)
    monkeypatch.setattr(
        renderer,
        "_sync_directory",
        lambda parent: synced.append(Path(parent)),
    )

    with pytest.raises(OSError, match="simulated journal creation failure"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=(platform, data_plane, observer),
            **CANDIDATE_ARGUMENTS,
        )

    residual = list(tmp_path.rglob(".*.tmp"))
    for staged_path in residual:
        staged_path.unlink(missing_ok=True)

    assert digest_calls == 9
    assert residual == []
    assert {path: path.read_bytes() for path in targets} == originals
    expected_parents = {path.parent for path in targets}
    parent_syncs = [path for path in synced if path in expected_parents]
    quarantine_syncs = [path for path in synced if path not in expected_parents]
    assert set(parent_syncs) == expected_parents
    assert len(parent_syncs) == len(expected_parents)
    assert len(quarantine_syncs) == len(expected_parents)
    assert {path.parent for path in quarantine_syncs} == expected_parents
    assert all(path.name.startswith(".yfeistai-staged-cleanup-") for path in quarantine_syncs)


def test_image_lock_writer_preserves_publish_and_staged_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    digest_calls = 0

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def fail_journal_creation(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated primary publish failure")

    real_unlink = Path.unlink
    real_rmdir = Path.rmdir
    cleanup_failed = False

    def fail_one_staged_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal cleanup_failed
        if path.suffix == ".tmp" and not cleanup_failed:
            cleanup_failed = True
            raise OSError("simulated staged cleanup failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(renderer, "_create_publish_journal", fail_journal_creation)
    monkeypatch.setattr(renderer, "_sync_directory", lambda _parent: None)
    monkeypatch.setattr(Path, "unlink", fail_one_staged_unlink)

    caught: BaseException | None = None
    try:
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )
    except BaseException as error:
        caught = error
    finally:
        for staged_path in tmp_path.rglob(".*.tmp"):
            real_unlink(staged_path, missing_ok=True)
        for quarantine in sorted(
            tmp_path.rglob(".yfeistai-staged-cleanup-*"),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            if quarantine.is_dir():
                real_rmdir(quarantine)

    assert digest_calls == 9
    assert cleanup_failed is True
    grouped = (
        caught if isinstance(caught, BaseExceptionGroup) else getattr(caught, "__cause__", None)
    )
    assert isinstance(grouped, BaseExceptionGroup)

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(grouped)
    assert any("simulated primary publish failure" in str(error) for error in failures)
    assert any("simulated staged cleanup failure" in str(error) for error in failures)


def test_image_lock_writer_reports_cleanup_topology_loss_without_unlinking_foreign_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    candidate = tmp_path / "candidate"
    displaced = tmp_path / "candidate-displaced"
    candidate.mkdir()
    output_path = candidate / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(candidate)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    digest_calls = 0
    original_staged_paths: list[Path] = []
    displaced_staged_paths: list[Path] = []
    foreign_paths: list[Path] = []
    foreign_content = b"foreign staged entry must survive\n"

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def replace_parent_after_staging(*_args, **_kwargs) -> None:
        staged_paths = tuple(sorted(candidate.glob(".*.tmp")))
        if len(staged_paths) != 4:
            raise AssertionError("all publish targets must be staged before journal creation")
        original_staged_paths.extend(path.resolve() for path in staged_paths)
        candidate.rename(displaced)
        displaced_staged_paths.extend(
            (displaced / path.relative_to(candidate)).resolve() for path in staged_paths
        )
        candidate.mkdir()
        foreign_path = candidate / staged_paths[0].name
        foreign_path.write_bytes(foreign_content)
        foreign_paths.append(foreign_path.resolve())
        raise RuntimeError("simulated parent replacement after staging")

    monkeypatch.setattr(renderer, "_create_publish_journal", replace_parent_after_staging)

    caught: BaseException | None = None
    try:
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )
    except BaseException as error:
        caught = error

    retained_staged_paths = tuple(path for path in displaced_staged_paths if path.is_file())
    foreign_exists = bool(foreign_paths) and foreign_paths[0].is_file()
    foreign_after = foreign_paths[0].read_bytes() if foreign_exists else None
    real_unlink = Path.unlink
    for staged_path in displaced_staged_paths:
        real_unlink(staged_path, missing_ok=True)

    assert digest_calls == 9
    assert len(original_staged_paths) == 4
    assert retained_staged_paths == tuple(displaced_staged_paths)
    assert foreign_exists is True
    assert foreign_after == foreign_content
    grouped = (
        caught if isinstance(caught, BaseExceptionGroup) else getattr(caught, "__cause__", None)
    )
    assert isinstance(grouped, BaseExceptionGroup)

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(grouped)
    assert any("simulated parent replacement after staging" in str(error) for error in failures)
    assert any("staged cleanup topology" in str(error).lower() for error in failures)


def test_staged_cleanup_rejects_same_name_replacement_at_unlink_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    staged_path = renderer._stage_text(tmp_path / "candidate.json", '{"candidate": true}\n')
    displaced_path = tmp_path / f"{staged_path.path.name}.displaced"
    foreign_content = b"foreign replacement at unlink boundary\n"
    real_staged_file_state = renderer._staged_file_state
    swapped = False

    def swap_after_state_check(
        candidate,
        *,
        allow_missing: bool,
        error_message: str,
    ):
        nonlocal swapped
        file_state = real_staged_file_state(
            candidate,
            allow_missing=allow_missing,
            error_message=error_message,
        )
        if allow_missing and file_state is not None and not swapped:
            candidate.path.rename(displaced_path)
            candidate.path.write_bytes(foreign_content)
            swapped = True
        return file_state

    monkeypatch.setattr(renderer, "_staged_file_state", swap_after_state_check)

    caught: BaseException | None = None
    try:
        renderer._cleanup_staged_paths((staged_path,))
    except BaseException as error:
        caught = error

    foreign_survivors = tuple(
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.read_bytes() == foreign_content
    )
    real_unlink = Path.unlink
    for cleanup_path in {displaced_path, *foreign_survivors}:
        real_unlink(cleanup_path, missing_ok=True)

    assert swapped is True
    assert foreign_survivors, "cleanup deleted a same-name foreign entry after its state check"
    assert caught is not None

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    assert any("staged cleanup topology" in str(error).lower() for error in leaves(caught))


def test_stage_text_rejects_same_name_replacement_before_path_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    output_path.write_bytes(b"original image lock\n")
    journal_path = _publish_journal_path(output_path)
    foreign_content = b"foreign replacement before staged path capture\n"
    real_capture_staged_path = renderer._capture_staged_path
    staged_paths: list[Path] = []
    displaced_paths: list[Path] = []
    swapped = False

    def swap_before_path_capture(path: Path, parent_identity, *args, **kwargs):
        nonlocal swapped
        if swapped:
            return real_capture_staged_path(path, parent_identity, *args, **kwargs)
        staged_path = Path(path).resolve()
        displaced_path = tmp_path / f"{staged_path.name}.pre-capture"
        staged_path.rename(displaced_path)
        staged_path.write_bytes(foreign_content)
        staged_paths.append(staged_path)
        displaced_paths.append(displaced_path.resolve())
        swapped = True
        return real_capture_staged_path(path, parent_identity, *args, **kwargs)

    monkeypatch.setattr(renderer, "_capture_staged_path", swap_before_path_capture)

    caught: BaseException | None = None
    try:
        renderer._create_publish_journal(
            output_path,
            (output_path,),
            {output_path: output_path.read_bytes()},
            transaction_id="a" * 64,
        )
    except BaseException as error:
        caught = error

    journal_existed = journal_path.is_file()
    journal_bytes = journal_path.read_bytes() if journal_existed else None
    real_unlink = Path.unlink
    for cleanup_path in {*staged_paths, *displaced_paths, journal_path}:
        real_unlink(cleanup_path, missing_ok=True)

    assert len(staged_paths) == 1
    assert journal_existed is False
    assert journal_bytes != foreign_content
    assert caught is not None

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(caught)
    assert any(
        "staged" in str(error).lower() and "topology" in str(error).lower() for error in failures
    )


def test_staged_cleanup_reports_quarantine_payload_after_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    staged_path = renderer._stage_text(tmp_path / "candidate.json", '{"candidate": true}\n')
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir
    failed_payloads: list[Path] = []

    def fail_quarantine_payload_unlink(path: Path, missing_ok: bool = False) -> None:
        target = Path(path)
        if target.suffix == ".tmp" and target.parent.name.startswith(".yfeistai-staged-cleanup-"):
            failed_payloads.append(target.resolve())
            raise OSError("simulated quarantine payload unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_quarantine_payload_unlink)

    caught: BaseException | None = None
    try:
        renderer._cleanup_staged_paths((staged_path,))
    except BaseException as error:
        caught = error

    payload = failed_payloads[0] if failed_payloads else None
    quarantine = payload.parent if payload is not None else None
    payload_exists = payload is not None and payload.is_file()
    quarantine_exists = quarantine is not None and quarantine.is_dir()
    quarantine_entries = (
        tuple(quarantine.iterdir()) if quarantine is not None and quarantine_exists else ()
    )
    if payload is not None:
        real_unlink(payload, missing_ok=True)
    if quarantine is not None and quarantine.is_dir():
        real_rmdir(quarantine)

    assert len(failed_payloads) == 1
    assert payload_exists is True
    assert quarantine_exists is True
    assert quarantine_entries == (payload,)
    assert caught is not None

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(caught)
    assert any("simulated quarantine payload unlink failure" in str(error) for error in failures)
    assert any(str(payload) in str(error) for error in failures)


def test_staged_cleanup_reports_empty_quarantine_after_rmdir_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    staged_path = renderer._stage_text(tmp_path / "candidate.json", '{"candidate": true}\n')
    real_rmdir = Path.rmdir
    failed_quarantines: list[Path] = []

    def fail_quarantine_rmdir(path: Path) -> None:
        target = Path(path)
        if target.name.startswith(".yfeistai-staged-cleanup-"):
            failed_quarantines.append(target.resolve())
            raise OSError("simulated quarantine rmdir failure")
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_quarantine_rmdir)

    caught: BaseException | None = None
    try:
        renderer._cleanup_staged_paths((staged_path,))
    except BaseException as error:
        caught = error

    quarantine = failed_quarantines[0] if failed_quarantines else None
    quarantine_exists = quarantine is not None and quarantine.is_dir()
    quarantine_entries = (
        tuple(quarantine.iterdir()) if quarantine is not None and quarantine_exists else ()
    )
    payload_path = quarantine / staged_path.path.name if quarantine is not None else None
    payload_exists = payload_path is not None and payload_path.exists()
    if quarantine is not None and quarantine.is_dir():
        real_rmdir(quarantine)

    assert len(failed_quarantines) == 1
    assert quarantine_exists is True
    assert quarantine_entries == ()
    assert payload_exists is False
    assert caught is not None

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(caught)
    assert any("simulated quarantine rmdir failure" in str(error) for error in failures)
    assert any(str(quarantine) in str(error) for error in failures)
    assert all(payload_path is None or str(payload_path) not in str(error) for error in failures)


def test_staged_cleanup_preserves_quarantined_foreign_entry_when_restore_name_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    staged_path = renderer._stage_text(tmp_path / "candidate.json", '{"candidate": true}\n')
    displaced_path = tmp_path / f"{staged_path.path.name}.displaced"
    foreign_content = b"foreign replacement moved into quarantine\n"
    concurrent_content = b"concurrent replacement at original name\n"
    real_staged_file_state = renderer._staged_file_state
    real_link = renderer.os.link
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir
    quarantined_paths: list[Path] = []
    swapped = False

    def swap_after_state_check(
        candidate,
        *,
        allow_missing: bool,
        error_message: str,
    ):
        nonlocal swapped
        file_state = real_staged_file_state(
            candidate,
            allow_missing=allow_missing,
            error_message=error_message,
        )
        if allow_missing and file_state is not None and not swapped:
            candidate.path.rename(displaced_path)
            candidate.path.write_bytes(foreign_content)
            swapped = True
        return file_state

    def rebuild_original_then_link(source: Path, target: Path, *args, **kwargs) -> None:
        quarantined_paths.append(Path(source).resolve())
        Path(target).write_bytes(concurrent_content)
        real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(renderer, "_staged_file_state", swap_after_state_check)
    monkeypatch.setattr(renderer.os, "link", rebuild_original_then_link)

    caught: BaseException | None = None
    try:
        renderer._cleanup_staged_paths((staged_path,))
    except BaseException as error:
        caught = error

    quarantined_path = quarantined_paths[0] if quarantined_paths else None
    quarantine = quarantined_path.parent if quarantined_path is not None else None
    quarantined_exists = quarantined_path is not None and quarantined_path.is_file()
    quarantined_bytes = quarantined_path.read_bytes() if quarantined_exists else None
    original_bytes = staged_path.path.read_bytes() if staged_path.path.is_file() else None
    for cleanup_path in (staged_path.path, displaced_path, quarantined_path):
        if cleanup_path is not None:
            real_unlink(cleanup_path, missing_ok=True)
    if quarantine is not None and quarantine.is_dir():
        real_rmdir(quarantine)

    assert swapped is True
    assert len(quarantined_paths) == 1
    assert quarantined_exists is True
    assert quarantined_bytes == foreign_content
    assert original_bytes == concurrent_content
    assert caught is not None

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(caught)
    assert any(isinstance(error, FileExistsError) for error in failures)
    assert any(str(quarantined_path) in str(error) for error in failures)


def test_staged_cleanup_reports_empty_quarantine_when_payload_disappears_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    staged_path = renderer._stage_text(tmp_path / "candidate.json", '{"candidate": true}\n')
    displaced_path = tmp_path / f"{staged_path.path.name}.post-rename"
    real_target_file_state = renderer._target_file_state
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir
    quarantined_paths: list[Path] = []

    def remove_quarantine_payload_before_state(path: Path):
        target = Path(path)
        if (
            not quarantined_paths
            and target.parent.name.startswith(".yfeistai-staged-cleanup-")
            and target.is_file()
        ):
            quarantined_paths.append(target.resolve())
            target.rename(displaced_path)
        return real_target_file_state(path)

    monkeypatch.setattr(renderer, "_target_file_state", remove_quarantine_payload_before_state)

    caught: BaseException | None = None
    try:
        renderer._cleanup_staged_paths((staged_path,))
    except BaseException as error:
        caught = error

    quarantined_path = quarantined_paths[0] if quarantined_paths else None
    quarantine = quarantined_path.parent if quarantined_path is not None else None
    quarantine_exists = quarantine is not None and quarantine.is_dir()
    quarantine_entries = (
        tuple(quarantine.iterdir()) if quarantine is not None and quarantine_exists else ()
    )
    payload_exists = quarantined_path is not None and quarantined_path.exists()
    displaced_exists = displaced_path.is_file()
    real_unlink(displaced_path, missing_ok=True)
    if quarantine is not None and quarantine.is_dir():
        real_rmdir(quarantine)

    assert len(quarantined_paths) == 1
    assert displaced_exists is True
    assert quarantine_exists is True
    assert quarantine_entries == ()
    assert payload_exists is False
    assert caught is not None

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    failures = leaves(caught)
    assert any(str(quarantine) in str(error) for error in failures)
    assert all(str(quarantined_path) not in str(error) for error in failures)


def test_image_lock_writer_cleans_staged_files_when_publish_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    platform_compose, data_plane_compose, observer_overlay = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    originals = {
        path: path.read_bytes()
        for path in (output_path, platform_compose, data_plane_compose, observer_overlay)
    }
    real_replace = renderer.os.replace

    calls = 0

    def resolve(reference: str) -> str:
        nonlocal calls
        calls += 1
        return "sha256:" + f"{calls:064x}"

    def fail_replace(source: Path, target: Path) -> None:
        if Path(target) in originals:
            raise OSError("simulated publish failure")
        real_replace(source, target)

    monkeypatch.setattr(renderer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated publish failure"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=(platform_compose, data_plane_compose, observer_overlay),
            **CANDIDATE_ARGUMENTS,
        )
    assert calls == 9
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_image_lock_writer_rolls_back_every_file_after_late_publish_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    real_replace = renderer.os.replace
    partial_publications: list[str] = []

    for failure_index in (2, 3, 4):
        case_dir = tmp_path / f"replace-{failure_index}"
        case_dir.mkdir()
        output_path = case_dir / "image-lock.json"
        platform_compose, data_plane_compose, observer_overlay = _copy_candidate_compose_files(
            case_dir
        )
        output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
        originals = {
            path: path.read_bytes()
            for path in (output_path, platform_compose, data_plane_compose, observer_overlay)
        }

        digest_calls = 0
        replace_calls = 0

        def resolve(reference: str) -> str:
            nonlocal digest_calls
            digest_calls += 1
            return "sha256:" + f"{digest_calls:064x}"

        def fail_late_replace(source: Path, target: Path) -> None:
            nonlocal replace_calls
            if Path(target) in originals:
                replace_calls += 1
                if replace_calls == failure_index:
                    raise OSError(f"simulated publish failure {failure_index}")
            real_replace(source, target)

        monkeypatch.setattr(renderer.os, "replace", fail_late_replace)

        with pytest.raises(
            OSError,
            match=f"simulated publish failure {failure_index}",
        ):
            renderer.write_image_lock(
                output_path,
                digest_resolver=resolve,
                compose_paths=(platform_compose, data_plane_compose, observer_overlay),
                **CANDIDATE_ARGUMENTS,
            )

        assert digest_calls == 9
        assert replace_calls == failure_index
        for path, original in originals.items():
            if path.read_bytes() != original:
                partial_publications.append(
                    f"replace {failure_index} partially published {path.name}"
                )
        assert list(case_dir.glob(".*.tmp")) == []

    assert partial_publications == []


def test_image_lock_writer_rolls_back_from_memory_if_active_journal_is_corrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (*compose_paths, output_path)}
    journal_path = _publish_journal_path(output_path)
    real_replace = renderer.os.replace
    replace_calls = 0
    digest_calls = 0

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def corrupt_journal_before_failure(source: Path, target: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            journal_path.write_text("{corrupt-active-journal\n", encoding="utf-8")
            raise OSError("simulated publish failure after journal corruption")
        real_replace(source, target)

    monkeypatch.setattr(renderer.os, "replace", corrupt_journal_before_failure)

    with pytest.raises(RuntimeError, match="journal integrity"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    for path, original in originals.items():
        assert path.read_bytes() == original
    assert journal_path.read_text(encoding="utf-8") == "{corrupt-active-journal\n"


def test_image_lock_writer_marks_committed_before_retiring_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    real_retire = renderer._retire_publish_journal
    observed_statuses: list[str] = []
    digest_calls = 0

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def observe_retire(journal_path: Path) -> None:
        observed_statuses.append(json.loads(journal_path.read_text(encoding="utf-8"))["status"])
        real_retire(journal_path)

    monkeypatch.setattr(renderer, "_retire_publish_journal", observe_retire)

    renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=compose_paths,
        **CANDIDATE_ARGUMENTS,
    )

    assert observed_statuses == ["committed"]


def test_image_lock_writer_cleans_committed_journal_without_restoring_old_targets(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": "committed"}\n', encoding="utf-8")
    committed = {path: path.read_bytes() for path in (*compose_paths, output_path)}
    old = {path: b"old-" + content for path, content in committed.items()}
    journal_path = _write_publish_journal(
        output_path,
        old,
        schema_version=2,
        status="committed",
    )

    class StopAfterCleanup(BaseException):
        pass

    def stop_after_cleanup(_reference: str) -> str:
        for path, content in committed.items():
            assert path.read_bytes() == content
        assert not journal_path.exists()
        raise StopAfterCleanup

    with pytest.raises(StopAfterCleanup):
        renderer.write_image_lock(
            output_path,
            digest_resolver=stop_after_cleanup,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )


def test_image_lock_writer_does_not_overwrite_foreign_target_after_failed_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    victim = compose_paths[0]
    foreign_content = b"foreign-writer-content\n"
    digest_calls = 0

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def foreign_replace_failure(_source: Path, target: Path) -> None:
        Path(target).write_bytes(foreign_content)
        raise OSError("simulated replace failure after foreign mutation")

    monkeypatch.setattr(renderer.os, "replace", foreign_replace_failure)

    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    assert victim.read_bytes() == foreign_content
    assert _publish_journal_path(output_path).exists()


def test_image_lock_validator_rejects_owned_in_progress_publish_journal(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    digest_calls = 0

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=compose_paths,
        **CANDIDATE_ARGUMENTS,
    )
    candidate_paths = (*compose_paths, output_path)
    originals = {path: path.read_bytes() for path in candidate_paths}
    _write_publish_journal(output_path, originals)

    with pytest.raises(ValueError, match="publish journal"):
        renderer.validate_image_lock_bindings(
            output_path,
            compose_paths=compose_paths,
        )


def test_image_lock_writer_recovers_partial_publish_before_starting_next_publish(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    candidate_paths = (*compose_paths, output_path)
    originals = {path: path.read_bytes() for path in candidate_paths}
    journal_path = _write_publish_journal(output_path, originals)

    compose_paths[0].write_bytes(b"partially published platform compose\n")
    compose_paths[1].write_bytes(b"partially published data-plane compose\n")

    def stop_after_recovery(_reference: str) -> str:
        for path, original in originals.items():
            assert path.read_bytes() == original, f"{path.name} was not recovered before republish"
        assert not journal_path.exists(), "recovered journal must be retired before republish"
        raise RuntimeError("stop after crash recovery")

    with pytest.raises(RuntimeError, match="stop after crash recovery"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=stop_after_recovery,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    for path, original in originals.items():
        assert path.read_bytes() == original
    assert not journal_path.exists()


@pytest.mark.parametrize("journal_case", ("corrupt", "unowned", "boolean-schema"))
def test_image_lock_writer_refuses_untrusted_publish_journal_without_candidate_changes(
    tmp_path: Path,
    journal_case: str,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    candidate_paths = (*compose_paths, output_path)
    originals = {path: path.read_bytes() for path in candidate_paths}
    journal_path = _publish_journal_path(output_path)
    if journal_case == "corrupt":
        journal_path.write_text("{not-json\n", encoding="utf-8")
    else:
        _write_publish_journal(output_path, originals, owner="another-writer")
        if journal_case == "boolean-schema":
            document = json.loads(journal_path.read_text(encoding="utf-8"))
            document["owner"] = PUBLISH_JOURNAL_OWNER
            document["schemaVersion"] = True
            journal_path.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
    journal_before = journal_path.read_bytes()
    resolver_called = False

    def unexpected_resolve(_reference: str) -> str:
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("untrusted journal must be refused before digest resolution")

    with pytest.raises(ValueError, match="publish journal"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=unexpected_resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    assert resolver_called is False
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert journal_path.read_bytes() == journal_before


def test_image_lock_writer_refuses_a_second_writer_while_the_first_journal_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (*compose_paths, output_path)}

    class StopFirstWriter(BaseException):
        pass

    real_link = renderer.os.link
    interleaved = False
    second_error: BaseException | None = None
    second_resolver_called = False
    first_digest_calls = 0

    def resolve_first_writer(_reference: str) -> str:
        nonlocal first_digest_calls
        first_digest_calls += 1
        return "sha256:" + f"{first_digest_calls:064x}"

    def unexpected_second_resolve(_reference: str) -> str:
        nonlocal second_resolver_called
        second_resolver_called = True
        raise AssertionError("second writer escaped the active publish lease")

    def link_then_interleave(source: Path, target: Path) -> None:
        nonlocal interleaved, second_error
        real_link(source, target)
        if interleaved:
            return
        interleaved = True
        try:
            renderer.write_image_lock(
                output_path,
                digest_resolver=unexpected_second_resolve,
                compose_paths=compose_paths,
                **CANDIDATE_ARGUMENTS,
            )
        except BaseException as error:
            second_error = error
        raise StopFirstWriter

    monkeypatch.setattr(renderer.os, "link", link_then_interleave)

    with pytest.raises(StopFirstWriter):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve_first_writer,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    assert first_digest_calls == 9
    assert second_resolver_called is False
    assert isinstance(second_error, ValueError)
    assert re.search(r"publish (transaction|lease).*(active|busy)", str(second_error))
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert not _publish_journal_path(output_path).exists()
    assert list(tmp_path.rglob(".*.tmp")) == []


def test_image_lock_writer_locks_shared_compose_targets_across_distinct_outputs(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    compose_paths = _copy_candidate_compose_files(tmp_path)
    first_output = tmp_path / "image-lock-a.json"
    second_output = tmp_path / "image-lock-b.json"
    first_output.write_text('{"sentinel": "first"}\n', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (*compose_paths, first_output)}
    second_error: BaseException | None = None
    second_resolver_called = False
    interleaved = False
    first_digest_calls = 0

    class StopFirstWriter(BaseException):
        pass

    def unexpected_second_resolve(_reference: str) -> str:
        nonlocal second_resolver_called
        second_resolver_called = True
        raise AssertionError("shared Compose targets escaped the active publish lease")

    def interleave_distinct_output(_reference: str) -> str:
        nonlocal first_digest_calls, interleaved, second_error
        first_digest_calls += 1
        if not interleaved:
            interleaved = True
            try:
                renderer.write_image_lock(
                    second_output,
                    digest_resolver=unexpected_second_resolve,
                    compose_paths=compose_paths,
                    **CANDIDATE_ARGUMENTS,
                )
            except BaseException as error:
                second_error = error
            raise StopFirstWriter
        return "sha256:" + f"{first_digest_calls:064x}"

    with pytest.raises(StopFirstWriter):
        renderer.write_image_lock(
            first_output,
            digest_resolver=interleave_distinct_output,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    assert first_digest_calls == 1
    assert second_resolver_called is False
    assert isinstance(second_error, ValueError)
    assert re.search(r"publish (transaction|lease).*(active|busy)", str(second_error))
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert not second_output.exists()
    assert not _publish_journal_path(first_output).exists()
    assert not _publish_journal_path(second_output).exists()


def test_image_lock_writer_rejects_publish_parent_identity_change_before_staging(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    output_path = candidate / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(candidate)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in (*compose_paths, output_path)}
    displaced = tmp_path / "candidate-original"
    digest_calls = 0

    def replace_parent_then_resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        if digest_calls == 1:
            candidate.rename(displaced)
            candidate.mkdir()
            for name, content in originals.items():
                (candidate / name).write_bytes(content)
        return "sha256:" + f"{digest_calls:064x}"

    with pytest.raises(ValueError, match="publish target topology changed"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=replace_parent_then_resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    for name, original in originals.items():
        assert (displaced / name).read_bytes() == original
        assert (candidate / name).read_bytes() == original
    assert not _publish_journal_path(output_path).exists()


def test_image_lock_writer_rejects_in_place_target_content_change_before_staging(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    compose_paths = _copy_candidate_compose_files(tmp_path)
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    protected = {path: path.read_bytes() for path in (*compose_paths, output_path)}
    victim = compose_paths[0]
    replacement = b"x" * len(protected[victim])
    digest_calls = 0

    def rewrite_in_place_then_resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        if digest_calls == 1:
            victim.write_bytes(replacement)
        return "sha256:" + f"{digest_calls:064x}"

    with pytest.raises(ValueError, match="publish target topology changed"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=rewrite_in_place_then_resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    assert victim.read_bytes() == replacement
    for path, original in protected.items():
        if path != victim:
            assert path.read_bytes() == original
    assert not _publish_journal_path(output_path).exists()


@pytest.mark.parametrize("linked_component", ("leaf-link", "parent-link"))
def test_image_lock_writer_rejects_linked_publish_components_before_digest_resolution(
    tmp_path: Path,
    linked_component: str,
) -> None:
    renderer = _load_renderer()
    real_root = tmp_path / "real"
    real_root.mkdir()
    real_compose_paths = _copy_candidate_compose_files(real_root)
    real_output_path = real_root / "image-lock.json"
    real_output_path.write_text('{"sentinel": true}\n', encoding="utf-8")

    if linked_component == "leaf-link":
        external = tmp_path / "external-image-lock.json"
        external.write_bytes(real_output_path.read_bytes())
        linked_output_path = real_root / "linked-image-lock.json"
        try:
            linked_output_path.symlink_to(external)
        except OSError as error:
            pytest.skip(f"file symlink unavailable: {error}")
        output_path = linked_output_path
        compose_paths = real_compose_paths
    else:
        linked_root = tmp_path / "linked"
        try:
            linked_root.symlink_to(real_root, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlink unavailable: {error}")
        output_path = linked_root / real_output_path.name
        compose_paths = tuple(linked_root / path.name for path in real_compose_paths)

    protected = {path: path.read_bytes() for path in (*real_compose_paths, real_output_path)}
    resolver_called = False

    def unexpected_resolve(_reference: str) -> str:
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("linked topology reached digest resolution")

    with pytest.raises(ValueError, match="linked publish target"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=unexpected_resolve,
            compose_paths=compose_paths,
            **CANDIDATE_ARGUMENTS,
        )

    assert resolver_called is False
    for path, original in protected.items():
        assert path.read_bytes() == original


def test_image_lock_writer_flushes_all_target_directories_before_retiring_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    directories = {name: tmp_path / name for name in ("platform", "data", "observer", "lock")}
    for directory in directories.values():
        directory.mkdir()
    platform = directories["platform"] / "docker-compose.platform.yml"
    data_plane = directories["data"] / "docker-compose.data-plane.yml"
    observer = directories["observer"] / OBSERVER_OVERLAY_NAME
    lock = directories["lock"] / "image-lock.json"
    platform.write_bytes((ROOT / platform.name).read_bytes())
    data_plane.write_bytes((ROOT / data_plane.name).read_bytes())
    observer.write_bytes((ROOT / "deploy" / observer.name).read_bytes())
    lock.write_text('{"sentinel": true}\n', encoding="utf-8")
    targets = (platform, data_plane, observer, lock)
    events: list[tuple[str, Path]] = []
    retire_snapshot: list[tuple[str, Path]] = []
    digest_calls = 0
    real_replace = renderer.os.replace
    real_retire = renderer._retire_publish_journal

    def resolve(_reference: str) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "sha256:" + f"{digest_calls:064x}"

    def observed_replace(source: Path, target: Path) -> None:
        real_replace(source, target)
        events.append(("replace", Path(target)))

    def observed_sync(directory: Path) -> None:
        events.append(("sync", Path(directory).resolve()))

    def observed_retire(journal_path: Path) -> None:
        retire_snapshot.extend(events)
        real_retire(journal_path)

    monkeypatch.setattr(renderer.os, "replace", observed_replace)
    monkeypatch.setattr(renderer, "_sync_directory", observed_sync)
    monkeypatch.setattr(renderer, "_retire_publish_journal", observed_retire)

    renderer.write_image_lock(
        lock,
        digest_resolver=resolve,
        compose_paths=(platform, data_plane, observer),
        **CANDIDATE_ARGUMENTS,
    )

    missing: list[str] = []
    for parent in {path.parent.resolve() for path in targets}:
        last_replace = max(
            index
            for index, event in enumerate(retire_snapshot)
            if event[0] == "replace" and event[1].parent.resolve() == parent
        )
        if not any(event == ("sync", parent) for event in retire_snapshot[last_replace + 1 :]):
            missing.append(str(parent))
    assert missing == [], f"missing durable parent barriers before journal retirement: {missing}"


def test_registry_digest_hashes_raw_remote_manifest_bytes_without_local_image(
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    raw_manifest = (
        b'{\n  "schemaVersion": 2,\n'
        b'  "mediaType": "application/vnd.oci.image.index.v1+json",\n'
        b'  "manifests": []\n}\n'
    )
    digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    commands: list[list[str]] = []
    subprocess_options: list[dict[str, Any]] = []

    def inspect_remote_manifest(command: list[str], **_kwargs) -> SimpleNamespace:
        commands.append(command)
        subprocess_options.append(_kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=raw_manifest,
            stderr=b"",
        )

    monkeypatch.setattr(renderer.shutil, "which", lambda executable: "docker.exe")
    monkeypatch.setattr(renderer.subprocess, "run", inspect_remote_manifest)

    reference = "registry.example/yfeistai/deeptutor:first-release"
    resolved = renderer._registry_digest(reference)
    assert commands == [
        [
            "docker.exe",
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            reference,
        ]
    ]
    assert not subprocess_options[0].get("text", False)
    assert resolved == digest


def test_data_plane_is_project_scoped_private_and_uses_dedicated_secrets() -> None:
    compose = _data_plane_compose()
    assert set(compose["services"]) == {"openmaic", "openmaic-render"}
    for service in compose["services"].values():
        assert not service.get("ports")
    assert set(compose["services"]["openmaic"]["networks"]) == {
        "tenant-data-plane",
        "tenant-provider-egress",
    }
    assert set(compose["services"]["openmaic-render"]["networks"]) == {"tenant-data-plane"}
    assert compose["services"]["openmaic-render"]["profiles"] == ["mp4-export"]
    assert compose["networks"]["tenant-data-plane"]["internal"] is True
    assert not compose["networks"]["tenant-data-plane"].get("external", False)
    assert compose["networks"]["tenant-provider-egress"].get("internal", False) is False
    assert not compose["networks"]["tenant-provider-egress"].get("external", False)

    secret = compose["secrets"]["openmaic_service_secret"]
    tenant_hash = hashlib.sha256(b"tenant-acme").hexdigest()[:16]
    assert f"tenant_{tenant_hash}" in secret["file"]
    assert _service_secret_sources(compose["services"]["openmaic"]) == {"openmaic_service_secret"}
