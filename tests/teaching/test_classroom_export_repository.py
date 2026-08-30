from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from deeptutor.teaching.contracts import (
    ClassroomDocument,
    ExportPolicy,
    ExportRequest,
    canonical_json_bytes,
)
from deeptutor.teaching.models.classrooms import (
    ClassroomExport,
    ClassroomExportPolicy,
    ClassroomExportPolicyOperation,
    ClassroomPublicationMaterialization,
)
from deeptutor.teaching.repositories import exports as exports_repository
from deeptutor.teaching.repositories.exports import (
    SqlAlchemyClassroomExportRepository,
    _source_media,
)
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    SqlAlchemyGenerationJobRepository,
)
from tests.teaching.test_contracts import valid_classroom_document


def test_published_version_media_comes_from_exact_finalized_receipts() -> None:
    body = b"ID3-published-media"
    payload = valid_classroom_document()
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    manifest[0]["sha256"] = hashlib.sha256(body).hexdigest()
    manifest[0]["size_bytes"] = len(body)
    document = ClassroomDocument.model_validate(payload)
    confirmed = canonical_json_bytes(
        [
            {
                "relativeName": "classroom.json",
                "objectKey": "tenants/tenant-a/classrooms/asset-a/versions/2/classroom.json",
                "sha256": "a" * 64,
                "sizeBytes": 100,
                "mimeType": "application/json",
                "artifactKind": "dsl_json",
                "mediaId": None,
            },
            {
                "relativeName": document.media_manifest[0].relative_path,
                "objectKey": "tenants/tenant-a/classrooms/asset-a/versions/2/media/voice.mp3",
                "sha256": hashlib.sha256(body).hexdigest(),
                "sizeBytes": len(body),
                "mimeType": "audio/mpeg",
                "artifactKind": "media",
                "mediaId": document.media_manifest[0].media_id,
            },
        ]
    ).decode()
    publication = ClassroomPublicationMaterialization(
        status="finalized",
        confirmed_artifacts=confirmed,
    )

    media = _source_media(document, publication=publication)

    assert len(media) == 1
    assert media[0].media_id == document.media_manifest[0].media_id
    assert media[0].object_key.endswith("/media/voice.mp3")


class _EmptyResult:
    def one_or_none(self):
        return None


class _StatementCaptureSession:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()


class _PolicySession:
    def __init__(
        self,
        existing: ClassroomExportPolicy | None = None,
        *,
        operations: dict[str, object] | None = None,
        flush_error: BaseException | None = None,
    ) -> None:
        self.existing = existing
        self.operations = operations or {}
        self.flush_error = flush_error
        self.added: list[object] = []
        self.deleted: list[ClassroomExportPolicy] = []
        self.statements = []
        self.flushed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self

    async def scalar(self, statement):
        self.statements.append(statement)
        if "classroom_export_policy_operations" in str(statement):
            for operation_id, operation in self.operations.items():
                if operation_id in str(statement.compile(compile_kwargs={"literal_binds": True})):
                    return operation
            return None
        return self.existing

    def add(self, model: object) -> None:
        self.added.append(model)
        if isinstance(model, ClassroomExportPolicyOperation):
            self.operations[model.operation_id] = model

    async def delete(self, model: ClassroomExportPolicy) -> None:
        self.deleted.append(model)

    async def flush(self) -> None:
        self.flushed = True
        if self.flush_error is not None:
            raise self.flush_error


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", ["_draft_parts", "_version_parts"])
async def test_export_source_queries_exclude_student_classroom_assets(loader: str) -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    session = _StatementCaptureSession()

    assert await getattr(repository, loader)(session, "source-a") is None

    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "student_classroom_assets" in sql
    assert "NOT (EXISTS" in sql


@pytest.mark.asyncio
async def test_mp4_policy_replacement_creates_and_updates_the_tenant_row() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    created_session = _PolicySession()
    repository._session_factory = lambda: created_session

    created_state = await repository.replace_mp4_policy(
        allow_mp4=True,
        expected_revision="absent",
        operation_id="a" * 32,
        updated_by="platform-admin-a",
    )
    assert created_state.exists is True
    assert created_state.allow_mp4 is True
    assert created_state.revision != "absent"
    assert created_state.operation_id == "a" * 32
    assert created_session.flushed is True
    policies = [item for item in created_session.added if isinstance(item, ClassroomExportPolicy)]
    assert len(policies) == 1
    created = policies[0]
    assert (
        created.tenant_id,
        created.allow_mp4,
        created.operation_id,
        created.updated_by,
    ) == (
        "tenant-a",
        True,
        "a" * 32,
        "platform-admin-a",
    )
    assert any("FOR UPDATE" in str(statement) for statement in created_session.statements)

    replay_session = _PolicySession(created, operations=created_session.operations)
    repository._session_factory = lambda: replay_session
    replayed_state = await repository.replace_mp4_policy(
        allow_mp4=True,
        expected_revision="absent",
        operation_id="a" * 32,
        updated_by="platform-admin-a",
    )
    assert replayed_state == created_state
    assert replay_session.flushed is False

    stale_session = _PolicySession(created, operations=created_session.operations)
    repository._session_factory = lambda: stale_session
    with pytest.raises(exports_repository.ExportPolicyConflict):
        await repository.replace_mp4_policy(
            allow_mp4=False,
            expected_revision="absent",
            operation_id="b" * 32,
            updated_by="platform-admin-b",
        )
    assert (created.allow_mp4, created.updated_by) == (True, "platform-admin-a")

    updated_session = _PolicySession(created, operations=created_session.operations)
    repository._session_factory = lambda: updated_session
    updated_state = await repository.replace_mp4_policy(
        allow_mp4=False,
        expected_revision=created_state.revision,
        operation_id="c" * 32,
        updated_by="platform-admin-b",
    )
    assert updated_state.exists is True
    assert updated_state.allow_mp4 is False
    assert updated_state.revision not in {"absent", created_state.revision}
    assert updated_state.operation_id == "c" * 32
    assert not any(isinstance(item, ClassroomExportPolicy) for item in updated_session.added)
    assert updated_session.flushed is True
    assert (created.allow_mp4, created.updated_by) == (False, "platform-admin-b")
    assert created.updated_at is not None and created.updated_at.tzinfo is not None

    deleted_session = _PolicySession(created, operations=updated_session.operations)
    repository._session_factory = lambda: deleted_session
    deleted_state = await repository.delete_mp4_policy(
        expected_revision=updated_state.revision,
        operation_id="d" * 32,
        updated_by="platform-admin-b",
    )
    assert deleted_state.exists is False
    assert deleted_state.allow_mp4 is False
    assert deleted_state.revision not in {"absent", updated_state.revision}
    assert deleted_state.operation_id == "d" * 32
    assert deleted_session.deleted == []
    assert created.exists is False
    assert created.allow_mp4 is False
    assert created.operation_id == "d" * 32


@pytest.mark.asyncio
async def test_mp4_policy_operation_replay_is_historical_and_never_reapplies() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    historical_revision = "1" * 64
    tombstone_revision = "3" * 64
    operation_id = "a" * 32
    tombstone = ClassroomExportPolicy(
        tenant_id="tenant-a",
        allow_mp4=False,
        revision=tombstone_revision,
        operation_id="c" * 32,
        updated_by="platform-admin-b",
    )
    tombstone.exists = False
    historical = SimpleNamespace(
        tenant_id="tenant-a",
        operation_id=operation_id,
        mutation="replace",
        expected_revision="absent",
        allow_mp4=True,
        updated_by="platform-admin-a",
        result_exists=True,
        result_allow_mp4=True,
        result_revision=historical_revision,
        result_operation_id=operation_id,
    )
    session = _PolicySession(tombstone, operations={operation_id: historical})
    repository._session_factory = lambda: session

    replayed = await repository.replace_mp4_policy(
        allow_mp4=True,
        expected_revision="absent",
        operation_id=operation_id,
        updated_by="platform-admin-a",
    )

    assert replayed == exports_repository.ClassroomExportPolicyState(
        tenant_id="tenant-a",
        exists=True,
        allow_mp4=True,
        revision=historical_revision,
        operation_id=operation_id,
    )
    assert tombstone.exists is False
    assert tombstone.allow_mp4 is False
    assert tombstone.revision == tombstone_revision
    assert session.flushed is False


@pytest.mark.asyncio
async def test_mp4_policy_delete_replay_is_historical_and_never_reapplies() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    operation_id = "d" * 32
    current = ClassroomExportPolicy(
        tenant_id="tenant-a",
        exists=True,
        allow_mp4=True,
        revision="4" * 64,
        operation_id="e" * 32,
        updated_by="platform-admin-b",
    )
    historical = SimpleNamespace(
        tenant_id="tenant-a",
        operation_id=operation_id,
        mutation="delete",
        expected_revision="2" * 64,
        allow_mp4=None,
        updated_by="platform-admin-a",
        result_exists=False,
        result_allow_mp4=False,
        result_revision="3" * 64,
        result_operation_id=operation_id,
    )
    session = _PolicySession(current, operations={operation_id: historical})
    repository._session_factory = lambda: session

    replayed = await repository.delete_mp4_policy(
        expected_revision="2" * 64,
        operation_id=operation_id,
        updated_by="platform-admin-a",
    )

    assert replayed == exports_repository.ClassroomExportPolicyState(
        tenant_id="tenant-a",
        exists=False,
        allow_mp4=False,
        revision="3" * 64,
        operation_id=operation_id,
    )
    assert current.exists is True
    assert current.allow_mp4 is True
    assert current.revision == "4" * 64
    assert session.flushed is False


@pytest.mark.asyncio
async def test_mp4_policy_operation_id_is_permanently_bound_to_request_semantics() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    operation_id = "a" * 32
    tombstone = ClassroomExportPolicy(
        tenant_id="tenant-a",
        allow_mp4=False,
        revision="3" * 64,
        operation_id="c" * 32,
        updated_by="platform-admin-b",
    )
    tombstone.exists = False
    historical = SimpleNamespace(
        tenant_id="tenant-a",
        operation_id=operation_id,
        mutation="replace",
        expected_revision="absent",
        allow_mp4=True,
        updated_by="platform-admin-a",
        result_exists=True,
        result_allow_mp4=True,
        result_revision="1" * 64,
        result_operation_id=operation_id,
    )
    session = _PolicySession(tombstone, operations={operation_id: historical})
    repository._session_factory = lambda: session

    with pytest.raises(exports_repository.ExportPolicyConflict):
        await repository.replace_mp4_policy(
            allow_mp4=False,
            expected_revision=tombstone.revision,
            operation_id=operation_id,
            updated_by="platform-admin-a",
        )

    assert tombstone.exists is False
    assert tombstone.revision == "3" * 64
    assert session.flushed is False


@pytest.mark.asyncio
async def test_mp4_policy_operation_replay_fails_closed_across_tenants() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    operation_id = "a" * 32
    historical = SimpleNamespace(
        tenant_id="tenant-b",
        operation_id=operation_id,
        mutation="replace",
        expected_revision="absent",
        allow_mp4=True,
        updated_by="platform-admin-a",
        result_exists=True,
        result_allow_mp4=True,
        result_revision="1" * 64,
        result_operation_id=operation_id,
    )
    session = _PolicySession(operations={operation_id: historical})
    repository._session_factory = lambda: session

    with pytest.raises(exports_repository.ExportPolicyConflict):
        await repository.replace_mp4_policy(
            allow_mp4=True,
            expected_revision="absent",
            operation_id=operation_id,
            updated_by="platform-admin-a",
        )

    assert session.flushed is False


@pytest.mark.asyncio
async def test_mp4_policy_delete_from_absent_creates_replayable_tombstone() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    operation_id = "d" * 32
    created_session = _PolicySession()
    repository._session_factory = lambda: created_session

    deleted = await repository.delete_mp4_policy(
        expected_revision="absent",
        operation_id=operation_id,
        updated_by="platform-admin-a",
    )

    assert deleted.exists is False
    assert deleted.allow_mp4 is False
    assert deleted.revision != "absent"
    assert deleted.operation_id == operation_id
    policies = [item for item in created_session.added if isinstance(item, ClassroomExportPolicy)]
    assert len(policies) == 1
    assert policies[0].exists is False
    assert created_session.deleted == []

    operation = SimpleNamespace(
        tenant_id="tenant-a",
        operation_id=operation_id,
        mutation="delete",
        expected_revision="absent",
        allow_mp4=None,
        updated_by="platform-admin-a",
        result_exists=False,
        result_allow_mp4=False,
        result_revision=deleted.revision,
        result_operation_id=operation_id,
    )
    replay_session = _PolicySession(policies[0], operations={operation_id: operation})
    repository._session_factory = lambda: replay_session

    replayed = await repository.delete_mp4_policy(
        expected_revision="absent",
        operation_id=operation_id,
        updated_by="platform-admin-a",
    )

    assert replayed == deleted
    assert replay_session.flushed is False


@pytest.mark.asyncio
async def test_mp4_policy_concurrent_insert_replays_the_same_operation() -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    committed = ClassroomExportPolicy(
        tenant_id="tenant-a",
        exists=True,
        allow_mp4=True,
        revision="d" * 64,
        operation_id="a" * 32,
        updated_by="platform-admin-a",
    )
    committed_operation = SimpleNamespace(
        tenant_id="tenant-a",
        operation_id="a" * 32,
        mutation="replace",
        expected_revision="absent",
        allow_mp4=True,
        updated_by="platform-admin-a",
        result_exists=True,
        result_allow_mp4=True,
        result_revision="d" * 64,
        result_operation_id="a" * 32,
    )
    collision = IntegrityError(
        "insert classroom export policy",
        {},
        RuntimeError("UNIQUE constraint failed: classroom_export_policies.tenant_id"),
    )
    sessions = iter(
        (
            _PolicySession(flush_error=collision),
            _PolicySession(committed, operations={"a" * 32: committed_operation}),
        )
    )
    repository._session_factory = lambda: next(sessions)

    replayed = await repository.replace_mp4_policy(
        allow_mp4=True,
        expected_revision="absent",
        operation_id="a" * 32,
        updated_by="platform-admin-a",
    )

    assert replayed == exports_repository.ClassroomExportPolicyState(
        tenant_id="tenant-a",
        exists=True,
        allow_mp4=True,
        revision="d" * 64,
        operation_id="a" * 32,
    )


def _job_request(
    exported: ClassroomExport,
    *,
    document_sha256: str | None = None,
) -> GenerationJobRequest:
    request = ExportRequest(
        schema_version="1.0",
        tenant_id="tenant-a",
        job_id=exported.id,
        idempotency_key=exported.id,
        classroom_document_sha256=(document_sha256 or exported.input_document_sha256 or ""),
        media_manifest_sha256=exported.input_media_manifest_sha256 or "",
        format="pptx",
        language="zh-CN",
        export_policy=ExportPolicy(
            include_source_attribution=True,
            allow_external_links=False,
        ),
    )
    payload = canonical_json_bytes(request).decode()
    return GenerationJobRequest(
        tenant_id="tenant-a",
        job_id=exported.id,
        job_kind="export",
        phase="export",
        export_format="pptx",
        priority="teacher",
        quota_units=1,
        actor_id="teacher-a",
        owner_id="teacher-a",
        visibility="private",
        request_id=exported.id,
        idempotency_key=exported.id,
        request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload=payload,
    )


@pytest.mark.asyncio
async def test_atomic_job_binding_compares_the_frozen_request_to_export_pins() -> None:
    exported = ClassroomExport(
        id="export-fixed",
        tenant_id="tenant-a",
        classroom_id="asset-a",
        classroom_version_id="version-a",
        classroom_draft_id=None,
        draft_revision=None,
        generation_job_id=None,
        export_format="pptx",
        input_document_sha256="a" * 64,
        input_media_manifest_sha256="b" * 64,
        idempotency_key="public-key",
        request_sha256="c" * 64,
        input_manifest_object_key="tenants/tenant-a/export-inputs/export-fixed/manifest.json",
        input_manifest_sha256="d" * 64,
        status="input_ready",
        created_by="teacher-a",
    )

    class Session:
        async def scalar(self, _statement):
            return exported

    with pytest.raises(ValueError, match="does not match"):
        await SqlAlchemyGenerationJobRepository._bind_export_job(
            Session(),  # type: ignore[arg-type]
            _job_request(exported, document_sha256="e" * 64),
            exported.id,
        )
    assert exported.generation_job_id is None

    await SqlAlchemyGenerationJobRepository._bind_export_job(
        Session(),  # type: ignore[arg-type]
        _job_request(exported),
        exported.id,
    )
    assert exported.generation_job_id == exported.id
    assert exported.status == "quota_reserved"
