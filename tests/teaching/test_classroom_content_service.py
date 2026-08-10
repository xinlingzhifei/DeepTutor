from __future__ import annotations

import hashlib
import json
import tempfile
from types import SimpleNamespace

import pytest

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import TicketScopeError
from tests.teaching_contract_fixtures import valid_classroom_document


def _context(user_id: str, role: str | None = None) -> TenantContext:
    permissions = (
        permissions_for_roles(
            {role},
            scope_type="class",
            scope_id="class-a",
            tenant_id="tenant-a",
        )
        if role is not None
        else frozenset()
    )
    return TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id=user_id,
        permissions=permissions,
    )


def _read_content(content) -> bytes:
    try:
        return content.stream.read()
    finally:
        content.close()


@pytest.mark.asyncio
async def test_verified_content_spools_large_chunks_without_materializing_bytes(
    monkeypatch,
) -> None:
    from deeptutor.teaching.services import classroom_content
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentService,
        ContentArtifactReceipt,
    )

    chunks = (
        b"a" * 600_000,
        b"b" * 600_000,
        b"c" * 257,
    )
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    receipt = ContentArtifactReceipt(
        artifact_kind="media",
        media_id="media-a",
        relative_name="media/large.bin",
        object_key="private/large-object",
        sha256=digest.hexdigest(),
        size_bytes=sum(len(chunk) for chunk in chunks),
        mime_type="application/octet-stream",
    )

    class Store:
        async def open(self, object_key):
            assert object_key == "private/large-object"

            async def stream():
                for chunk in chunks:
                    yield chunk

            return stream()

    class Stores:
        async def store_for_tenant(self, tenant_id):
            assert tenant_id == "tenant-a"
            return Store()

    def forbid_bytes_materialization(*_args, **_kwargs):
        raise AssertionError("verified content must not be materialized with bytes()")

    monkeypatch.setattr(classroom_content, "bytes", forbid_bytes_materialization, raising=False)
    service = ClassroomContentService(
        repository=object(),
        stores=Stores(),
        ticket_service=None,
    )

    spool = await service._read("tenant-a", receipt)

    assert spool.tell() == 0
    assert spool._rolled is True
    assert spool.read(3) == b"aaa"
    spool.seek(600_000)
    assert spool.read(3) == b"bbb"
    spool.close()
    assert spool.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["size", "hash"])
async def test_verified_content_closes_spool_on_receipt_mismatch(
    monkeypatch,
    mismatch: str,
) -> None:
    from deeptutor.teaching.services import classroom_content
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentIntegrityError,
        ClassroomContentService,
        ContentArtifactReceipt,
    )

    payload = b"verified-content"
    created: list[object] = []
    upstream_closed: list[bool] = []

    def tracking_spool(*args, **kwargs):
        spool = tempfile.SpooledTemporaryFile(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(
        classroom_content,
        "tempfile",
        SimpleNamespace(SpooledTemporaryFile=tracking_spool),
        raising=False,
    )
    receipt = ContentArtifactReceipt(
        artifact_kind="media",
        media_id="media-a",
        relative_name="media/a.bin",
        object_key="private/a",
        sha256=("0" * 64 if mismatch == "hash" else hashlib.sha256(payload).hexdigest()),
        size_bytes=(len(payload) - 1 if mismatch == "size" else len(payload)),
        mime_type="application/octet-stream",
    )

    class Store:
        async def open(self, _object_key):
            async def stream():
                try:
                    yield payload
                finally:
                    upstream_closed.append(True)

            return stream()

    class Stores:
        async def store_for_tenant(self, _tenant_id):
            return Store()

    service = ClassroomContentService(
        repository=object(),
        stores=Stores(),
        ticket_service=None,
    )

    with pytest.raises(ClassroomContentIntegrityError):
        await service._read("tenant-a", receipt)

    assert len(created) == 1
    assert created[0].closed
    assert upstream_closed == [True]


@pytest.mark.asyncio
async def test_sql_content_repository_rejects_mismatched_tenant_schema() -> None:
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentAccessDenied,
        SqlAlchemyClassroomContentRepository,
    )

    repository = SqlAlchemyClassroomContentRepository(engine=object())
    forged = TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_other",
        user_id="student-a",
        permissions=frozenset(),
    )

    with pytest.raises(ClassroomContentAccessDenied, match="schema"):
        await repository.get_session(forged, "session-a")


@pytest.mark.asyncio
async def test_content_service_pins_tickets_and_reads_exact_receipts_without_urls() -> None:
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentAccessDenied,
        ClassroomContentService,
        ContentArtifactReceipt,
        SessionContentAccess,
        VersionContentRecord,
    )

    payload = valid_classroom_document()
    payload["classroom_version_id"] = "source-version-a"
    media_body = b"m" * 128
    payload["media_manifest"][0]["sha256"] = hashlib.sha256(media_body).hexdigest()
    payload["media_manifest"][0]["size_bytes"] = len(media_body)
    document = ClassroomDocument.model_validate(payload)
    document_body = canonical_json_bytes(document)
    document_receipt = ContentArtifactReceipt(
        artifact_kind="dsl_json",
        media_id=None,
        relative_name="classroom.json",
        object_key="private/document-object-key",
        sha256=hashlib.sha256(document_body).hexdigest(),
        size_bytes=len(document_body),
        mime_type="application/json",
    )
    media_receipt = ContentArtifactReceipt(
        artifact_kind="media",
        media_id=None,
        relative_name="media/voice.mp3",
        object_key="private/media-object-key",
        sha256=hashlib.sha256(media_body).hexdigest(),
        size_bytes=len(media_body),
        mime_type="audio/mpeg",
    )
    version = VersionContentRecord(
        tenant_id="tenant-a",
        version_id="version-a",
        source_version_id="source-version-a",
        classroom_id="asset-a",
        owner_id="teacher-owner",
        course_id="course-a",
        class_id="class-a",
        document=document_receipt,
        media=(media_receipt,),
    )
    active_session = SessionContentAccess(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="student-a",
        classroom_version_id="version-a",
        status="active",
    )

    class Repository:
        async def get_version(self, context, version_id):
            assert context.tenant_id == "tenant-a"
            return version if version_id == "version-a" else None

        async def get_session(self, context, session_id):
            assert context.tenant_id == "tenant-a"
            return active_session if session_id == "session-a" else None

        async def get_export(self, context, export_id):
            return None

    class Store:
        async def open(self, object_key):
            bodies = {
                "private/document-object-key": document_body,
                "private/media-object-key": media_body,
            }
            body = bodies[object_key]

            async def chunks():
                yield body

            return chunks()

    class Stores:
        async def store_for_tenant(self, tenant_id):
            assert tenant_id == "tenant-a"
            return Store()

    class Tickets:
        def __init__(self) -> None:
            self.issued: list[dict[str, object]] = []

        def issue(self, **claims):
            self.issued.append(claims)
            return "read-ticket"

        def verify_read(self, token, **expected):
            if token == "wrong-ticket":
                raise TicketScopeError("wrong resource")
            assert expected["expected_version_id"] == "version-a"
            return type(
                "Claims",
                (),
                {
                    "session_id": "session-a",
                    "tenant_id": "tenant-a",
                    "user_id": "student-a",
                    "classroom_version_id": "version-a",
                },
            )()

    tickets = Tickets()
    service = ClassroomContentService(
        repository=Repository(),
        stores=Stores(),
        ticket_service=tickets,
    )
    student = _context("student-a")

    issued = await service.issue_read_ticket(
        student,
        session_id="session-a",
        action="classroom.media.read",
        resource_id="media-1",
    )
    delivered_document = await service.open_document(
        student,
        version_id="version-a",
        token="document-ticket",
    )
    delivered_media = await service.open_media(
        student,
        version_id="version-a",
        media_id="media-1",
        token="media-ticket",
    )

    assert issued == "read-ticket"
    assert tickets.issued[0] == {
        "tenant_id": "tenant-a",
        "user_id": "student-a",
        "session_id": "session-a",
        "classroom_version_id": "version-a",
        "allowed_action": "classroom.media.read",
        "resource_id": "media-1",
        "ttl_seconds": 60,
    }
    delivered_document_body = _read_content(delivered_document)
    delivered_media_body = _read_content(delivered_media)
    rendered = json.loads(delivered_document_body)
    assert rendered["classroomVersionId"] == "source-version-a"
    assert rendered["exportManifest"] == []
    assert rendered["mediaManifest"][0]["temporaryDownloadPath"] == (
        "/api/v1/classroom-versions/version-a/media/media-1"
    )
    assert "private/" not in delivered_document_body.decode()
    assert "/api/yfeistai/" not in delivered_document_body.decode()
    assert delivered_media_body == media_body

    owner_document = await service.open_document(
        _context("teacher-owner"),
        version_id="version-a",
        token=None,
    )
    assert owner_document.mime_type == "application/json"
    owner_document.close()
    teacher = _context("teacher-other", "teacher")
    teacher_media = await service.open_media(
        teacher,
        version_id="version-a",
        media_id="media-1",
        token=None,
    )
    assert _read_content(teacher_media) == media_body
    with pytest.raises(ClassroomContentAccessDenied):
        await service.open_document(
            _context("student-b"),
            version_id="version-a",
            token=None,
        )
    with pytest.raises(TicketScopeError):
        await service.open_media(
            teacher,
            version_id="version-a",
            media_id="media-1",
            token="wrong-ticket",
        )


@pytest.mark.asyncio
async def test_generated_version_rejects_document_bound_to_another_version() -> None:
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentIntegrityError,
        ClassroomContentService,
        ContentArtifactReceipt,
        VersionContentRecord,
    )

    payload = valid_classroom_document()
    payload["classroom_version_id"] = "wrong-version"
    body = canonical_json_bytes(ClassroomDocument.model_validate(payload))
    receipt = ContentArtifactReceipt(
        artifact_kind="dsl_json",
        media_id=None,
        relative_name="classroom.json",
        object_key="document-key",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        mime_type="application/json",
    )
    record = VersionContentRecord(
        tenant_id="tenant-a",
        version_id="version-a",
        source_version_id=None,
        classroom_id="asset-a",
        owner_id="teacher-owner",
        course_id="course-a",
        class_id="class-a",
        document=receipt,
        media=(),
    )

    class Repository:
        async def get_version(self, _context, _version_id):
            return record

    class Stores:
        async def store_for_tenant(self, _tenant_id):
            class Store:
                async def open(self, _key):
                    async def chunks():
                        yield body

                    return chunks()

            return Store()

    service = ClassroomContentService(
        repository=Repository(),
        stores=Stores(),
        ticket_service=object(),
    )

    with pytest.raises(ClassroomContentIntegrityError, match="version"):
        await service.load_version_document(_context("teacher-owner"), "version-a")
