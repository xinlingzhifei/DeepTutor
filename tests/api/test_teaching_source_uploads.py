from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
import pytest
from starlette.datastructures import Headers, UploadFile

from deeptutor.api.routers import teaching_catalog as teaching_router
from deeptutor.multi_user.models import KnowledgeResource
from deeptutor.teaching.artifacts import StoredArtifact
from deeptutor.teaching.object_store import ObjectStoreConfigurationError
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.sources import (
    NewKnowledgeSnapshot,
    NewUpload,
    SourceConflictError,
    SourceEntitlementDeniedError,
    SourceNotFoundError,
    SourceRecord,
    UploadRecord,
)
from deeptutor.teaching.services import sources as source_service_module
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _binding_id(snapshot_id: str, course_id: str, class_id: str | None) -> str:
    import hashlib

    payload = "\0".join(("tenant-a", snapshot_id, course_id, class_id or "")).encode()
    return f"source-binding-{hashlib.sha256(payload).hexdigest()}"


class _SourceRepository:
    def __init__(self) -> None:
        self.records: dict[str, SourceRecord] = {}
        self.uploads: dict[str, UploadRecord] = {}
        self.snapshot_sources: dict[str, tuple[str, str]] = {}
        self.knowledge_snapshots: list[NewKnowledgeSnapshot] = []
        self.fail_create: Exception | None = None
        self.fail_after_persist: BaseException | None = None
        self.create_calls = 0
        self.valid_classes = {"class-a": "course-a", "class-b": "course-b"}
        self.knowledge_entitled = True
        self.knowledge_entitlements: set[tuple[str, str]] | None = None
        self.entitlement_calls: list[tuple[str, str | None]] = []

    def _validate_target(self, course_id: str, class_id: str | None) -> None:
        if course_id not in {"course-a", "course-b"}:
            raise SourceNotFoundError("course not found")
        if class_id is not None and self.valid_classes.get(class_id) != course_id:
            raise SourceNotFoundError("class not found in course")

    async def validate_target(self, course_id: str, class_id: str | None) -> None:
        self._validate_target(course_id, class_id)

    async def is_knowledge_resource_entitled(
        self,
        resource_id: str,
        resource_owner_id: str | None = None,
    ) -> bool:
        self.entitlement_calls.append((resource_id, resource_owner_id))
        if self.knowledge_entitlements is not None:
            return (resource_id, resource_owner_id) in self.knowledge_entitlements
        return self.knowledge_entitled

    async def list_bindings(self, course_ids, class_ids):
        return tuple(
            record
            for record in self.records.values()
            if (course_ids is None and class_ids is None)
            or (course_ids is not None and record.course_id in course_ids)
            or (class_ids is not None and record.class_id in class_ids)
        )

    async def get_binding(self, binding_id):
        try:
            return self.records[binding_id]
        except KeyError as exc:
            raise SourceNotFoundError("source binding not found") from exc

    async def find_upload_by_sha256(self, sha256):
        return self.uploads.get(sha256)

    async def bind_existing_upload(self, upload, *, binding_id, course_id, class_id, actor_id):
        self._validate_target(course_id, class_id)
        record = SourceRecord(
            binding_id=binding_id,
            source_type="pdf",
            source_id=upload.upload_id,
            filename=upload.filename,
            sha256=upload.sha256,
            size_bytes=upload.size_bytes,
            course_id=course_id,
            class_id=class_id,
        )
        self.records.setdefault(binding_id, record)
        return self.records[binding_id]

    async def create_upload_binding(
        self,
        upload: NewUpload,
        *,
        binding_id,
        course_id,
        class_id,
        actor_id,
        permission_sha256,
    ):
        self.create_calls += 1
        if self.fail_create is not None:
            raise self.fail_create
        self._validate_target(course_id, class_id)
        existing = self.uploads.get(upload.sha256)
        if existing is not None:
            actual_binding_id = _binding_id(existing.snapshot_id, course_id, class_id)
            return (
                await self.bind_existing_upload(
                    existing,
                    binding_id=actual_binding_id,
                    course_id=course_id,
                    class_id=class_id,
                    actor_id=actor_id,
                ),
                False,
            )
        saved = UploadRecord(
            upload_id=upload.upload_id,
            snapshot_id=upload.snapshot_id,
            filename=upload.filename,
            object_key=upload.object_key,
            sha256=upload.sha256,
            size_bytes=upload.size_bytes,
        )
        self.uploads[upload.sha256] = saved
        self.snapshot_sources[upload.snapshot_id] = ("pdf", upload.upload_id)
        record = SourceRecord(
            binding_id=binding_id,
            source_type="pdf",
            source_id=upload.upload_id,
            filename=upload.filename,
            sha256=upload.sha256,
            size_bytes=upload.size_bytes,
            course_id=course_id,
            class_id=class_id,
        )
        self.records[binding_id] = record
        if self.fail_after_persist is not None:
            raise self.fail_after_persist
        return record, True

    async def bind_knowledge_resource(
        self,
        snapshot: NewKnowledgeSnapshot,
        *,
        binding_id,
        course_id,
        class_id,
        actor_id,
    ):
        self._validate_target(course_id, class_id)
        self.entitlement_calls.append((snapshot.resource_id, snapshot.resource_owner_id))
        if self.knowledge_entitlements is not None:
            entitled = (
                snapshot.resource_id,
                snapshot.resource_owner_id,
            ) in self.knowledge_entitlements
        else:
            entitled = self.knowledge_entitled
        if not entitled:
            raise SourceEntitlementDeniedError(
                "knowledge resource is not entitled to this tenant"
            )
        self.knowledge_snapshots.append(snapshot)
        record = SourceRecord(
            binding_id=binding_id,
            source_type="knowledge_base",
            source_id=snapshot.resource_id,
            filename=None,
            sha256=snapshot.content_sha256,
            size_bytes=None,
            course_id=course_id,
            class_id=class_id,
        )
        self.records.setdefault(binding_id, record)
        return self.records[binding_id]

    async def delete_binding(self, binding_id):
        try:
            del self.records[binding_id]
        except KeyError as exc:
            raise SourceNotFoundError("source binding not found") from exc


class _Store:
    def __init__(self) -> None:
        self.put_calls: list[tuple[str, bytes, str, int, str]] = []
        self.deleted: list[StoredArtifact] = []
        self.fail_put = False

    async def put_verified(self, key, body, sha256, size, *, content_type):
        if self.fail_put:
            raise ObjectStoreConfigurationError("private storage/key detail")
        payload = b"".join([chunk async for chunk in body])
        self.put_calls.append((key, payload, sha256, size, content_type))
        return StoredArtifact(
            key=key,
            sha256=sha256,
            size=size,
            content_type=content_type,
            ownership_token="owned",
            revision="revision-1",
        )

    async def delete_owned(self, artifact):
        self.deleted.append(artifact)


class _StoreProvider:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.tenant_calls: list[str] = []

    async def store_for_tenant(self, tenant_id: str):
        self.tenant_calls.append(tenant_id)
        return self.store


class _KnowledgeResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.error: HTTPException | None = None
        self.resource_id = "admin:kb:math"
        self.resource_name = "math"
        self.source = "admin"
        self.generation_id = ""

    def __call__(self, reference: str, *, require_write: bool):
        self.calls.append((reference, require_write))
        if self.error is not None:
            raise self.error
        return KnowledgeResource(
            id=self.resource_id,
            name=self.resource_name,
            base_dir=Path("unused"),
            source=self.source,
            assigned=self.source == "admin",
            read_only=self.source == "admin",
            generation_id=self.generation_id,
        )


def _context(
    user_id: str = "teacher-a",
    role: str = "teacher",
    *,
    scope_type: str = "course",
    scope_id: str = "course-a",
    tenant_id: str = "tenant-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id.replace('-', '_')}",
        user_id=user_id,
        permissions=permissions_for_roles(
            {role},
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id=tenant_id,
        ),
    )


def _client(
    context: TenantContext,
    repository: _SourceRepository,
    store: _Store | None = None,
    resolver: _KnowledgeResolver | None = None,
    *,
    resource_exists: bool = True,
    raise_server_exceptions: bool = True,
) -> tuple[TestClient, _Store, _KnowledgeResolver]:
    actual_store = store or _Store()
    actual_resolver = resolver or _KnowledgeResolver()
    app = FastAPI()
    app.include_router(teaching_router.router, prefix="/api/v1/teaching")
    app.dependency_overrides[require_tenant] = lambda: context
    app.dependency_overrides[teaching_router.get_source_repository] = lambda: repository
    app.dependency_overrides[teaching_router.get_source_store_provider] = lambda: _StoreProvider(
        actual_store
    )
    app.dependency_overrides[teaching_router.get_knowledge_resolver] = lambda: actual_resolver
    knowledge_exists_dependency = getattr(
        teaching_router,
        "get_knowledge_resource_exists",
        None,
    )
    if knowledge_exists_dependency is not None:
        app.dependency_overrides[knowledge_exists_dependency] = lambda: (
            lambda _resource: resource_exists
        )
    return (
        TestClient(app, raise_server_exceptions=raise_server_exceptions),
        actual_store,
        actual_resolver,
    )


def _pdf_bytes(
    *,
    pages: int = 1,
    encrypted: bool = False,
    embedded: bool = False,
    catalog_action: bool = False,
    page_action: bool = False,
) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("secret")
    if embedded:
        writer.add_attachment("payload.txt", b"embedded")
    action = DictionaryObject(
        {
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("app.alert('x')"),
        }
    )
    if catalog_action:
        writer._root_object[NameObject("/OpenAction")] = action
    if page_action:
        writer.pages[0][NameObject("/AA")] = DictionaryObject({NameObject("/O"): action})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _active_annotation_pdf(
    *,
    subtype: str | None = None,
    payload_key: str | None = None,
    indirect: bool,
) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=72, height=72)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject(subtype or "/Text"),
            NameObject("/Rect"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(10), NumberObject(10)]
            ),
        }
    )
    if payload_key is not None:
        annotation[NameObject(payload_key)] = TextStringObject("active-payload")
    page[NameObject("/Annots")] = ArrayObject(
        [writer._add_object(annotation) if indirect else annotation]
    )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _declared_page_bomb_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    pages = writer._root_object[NameObject("/Pages")].get_object()
    pages[NameObject("/Count")] = NumberObject(2_001)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _upload(
    client: TestClient,
    payload: bytes,
    *,
    content_type: str = "application/pdf",
    course_id: str = "course-a",
    class_id: str | None = None,
):
    data = {"courseId": course_id}
    if class_id is not None:
        data["classId"] = class_id
    return client.post(
        "/api/v1/teaching/sources/pdf",
        data=data,
        files={"file": ("book.pdf", payload, content_type)},
    )


def test_spoofed_pdf_mime_with_non_pdf_body_is_rejected() -> None:
    repository = _SourceRepository()
    client, store, _ = _client(_context(), repository)

    response = _upload(client, b"not really a PDF")

    assert response.status_code == 415
    assert store.put_calls == []


def test_pdf_requires_application_pdf_mime() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, _pdf_bytes(), content_type="application/octet-stream")

    assert response.status_code == 415
    assert store.put_calls == []


def test_valid_pdf_is_stored_under_current_tenant_and_response_is_sanitized() -> None:
    repository = _SourceRepository()
    client, store, _ = _client(_context(), repository)

    response = _upload(client, _pdf_bytes(), class_id="class-a")

    assert response.status_code == 201
    body = response.json()
    assert body["sourceType"] == "pdf"
    assert body["courseId"] == "course-a"
    assert body["classId"] == "class-a"
    assert "objectKey" not in body
    assert "path" not in body
    assert len(store.put_calls) == 1
    assert store.put_calls[0][0].startswith("tenants/tenant-a/sources/upload-")
    assert store.put_calls[0][0].endswith("/source.pdf")
    assert "tenant-b" not in store.put_calls[0][0]


def test_pdf_size_limit_is_streaming_and_inclusive(monkeypatch) -> None:
    payload = _pdf_bytes() + b"\n" * 8
    assert source_service_module.MAX_PDF_BYTES == 100 * 1024 * 1024
    repository = _SourceRepository()
    client, store, _ = _client(_context(), repository)
    monkeypatch.setattr(source_service_module, "MAX_PDF_BYTES", len(payload))

    at_limit = _upload(client, payload)
    monkeypatch.setattr(source_service_module, "MAX_PDF_BYTES", len(payload) - 1)
    over_limit = _upload(client, payload, class_id="class-a")

    assert at_limit.status_code == 201
    assert over_limit.status_code == 413
    assert len(store.put_calls) == 1


def test_pdf_with_more_than_2000_pages_is_rejected() -> None:
    assert source_service_module.MAX_PDF_PAGES == 2_000
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, _pdf_bytes(pages=2_001))

    assert response.status_code == 422
    assert store.put_calls == []


def test_pdf_declaring_more_than_2000_pages_is_rejected_before_flattening() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, _declared_page_bomb_pdf())

    assert response.status_code == 422
    assert store.put_calls == []


def test_malformed_pdf_with_valid_magic_is_rejected() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, b"%PDF-not-a-valid-document")

    assert response.status_code == 422
    assert store.put_calls == []


def test_encrypted_pdf_is_rejected() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, _pdf_bytes(encrypted=True))

    assert response.status_code == 422
    assert store.put_calls == []


def test_pdf_embedded_files_are_rejected() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, _pdf_bytes(embedded=True))

    assert response.status_code == 422
    assert store.put_calls == []


def test_pdf_catalog_and_page_actions_are_rejected() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    catalog = _upload(client, _pdf_bytes(catalog_action=True))
    page = _upload(client, _pdf_bytes(page_action=True))

    assert catalog.status_code == 422
    assert page.status_code == 422
    assert store.put_calls == []


@pytest.mark.parametrize("subtype", ["/Movie", "/Sound", "/3D"])
@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_active_annotation_subtypes_are_rejected(subtype: str, indirect: bool) -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(
        client,
        _active_annotation_pdf(subtype=subtype, indirect=indirect),
    )

    assert response.status_code == 422
    assert store.put_calls == []


@pytest.mark.parametrize("payload_key", ["/Movie", "/Sound", "/3D", "/3DA", "/3DD"])
@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_active_annotation_payload_keys_are_rejected(
    payload_key: str,
    indirect: bool,
) -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(
        client,
        _active_annotation_pdf(payload_key=payload_key, indirect=indirect),
    )

    assert response.status_code == 422
    assert store.put_calls == []


def test_pdf_sha256_deduplicates_storage_and_binding() -> None:
    repository = _SourceRepository()
    client, store, _ = _client(_context(), repository)
    payload = _pdf_bytes()

    first = _upload(client, payload)
    replay = _upload(client, payload)

    assert first.status_code == replay.status_code == 201
    assert first.json()["bindingId"] == replay.json()["bindingId"]
    assert len(store.put_calls) == 1
    assert len(repository.uploads) == 1


def test_storage_failure_creates_no_database_rows_and_hides_details() -> None:
    repository = _SourceRepository()
    store = _Store()
    store.fail_put = True
    client, _, _ = _client(_context(), repository, store)

    response = _upload(client, _pdf_bytes())

    assert response.status_code == 503
    assert "private storage/key detail" not in response.text
    assert repository.records == {}
    assert repository.create_calls == 0


def test_database_failure_cleans_up_owned_object_and_hides_object_key() -> None:
    repository = _SourceRepository()
    repository.fail_create = SourceConflictError("private DB/object key detail")
    client, store, _ = _client(_context(), repository)

    response = _upload(client, _pdf_bytes())

    assert response.status_code == 409
    assert len(store.deleted) == 1
    assert store.deleted[0].key.startswith("tenants/tenant-a/")
    assert "private DB/object key detail" not in response.text


def test_unknown_post_commit_failure_does_not_delete_retained_source_object() -> None:
    repository = _SourceRepository()
    repository.fail_after_persist = RuntimeError("post-commit read failed")
    client, store, _ = _client(
        _context(),
        repository,
        raise_server_exceptions=False,
    )

    response = _upload(client, _pdf_bytes())

    assert response.status_code == 500
    assert repository.records
    assert repository.uploads
    assert store.deleted == []


def test_cancelled_commit_outcome_does_not_delete_retained_source_object() -> None:
    repository = _SourceRepository()
    repository.fail_after_persist = asyncio.CancelledError()
    store = _Store()
    service = source_service_module.SourceService(
        repository,
        _StoreProvider(store),
        _KnowledgeResolver(),
        lambda _resource: True,
    )
    upload = UploadFile(
        BytesIO(_pdf_bytes()),
        filename="book.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.upload_pdf(
                _context(),
                upload=upload,
                course_id="course-a",
                class_id=None,
            )
        )

    assert repository.records
    assert repository.uploads
    assert store.deleted == []


def test_teacher_lists_only_sources_in_granted_scope() -> None:
    repository = _SourceRepository()
    repository.records = {
        "binding-a": SourceRecord(
            "binding-a", "knowledge_base", "admin:kb:a", None, "a" * 64, None, "course-a", None
        ),
        "binding-b": SourceRecord(
            "binding-b", "knowledge_base", "admin:kb:b", None, "b" * 64, None, "course-b", None
        ),
    }
    client, _, _ = _client(_context(), repository)

    response = client.get("/api/v1/teaching/sources")

    assert response.status_code == 200
    assert [item["bindingId"] for item in response.json()["items"]] == ["binding-a"]
    assert all("objectKey" not in item for item in response.json()["items"])


def test_class_scoped_teacher_lists_only_sources_bound_to_that_class() -> None:
    repository = _SourceRepository()
    repository.records = {
        "class-a-binding": SourceRecord(
            "class-a-binding",
            "knowledge_base",
            "admin:kb:a",
            None,
            "a" * 64,
            None,
            None,
            "class-a",
        ),
        "course-a-binding": SourceRecord(
            "course-a-binding",
            "knowledge_base",
            "admin:kb:course",
            None,
            "c" * 64,
            None,
            "course-a",
            None,
        ),
    }
    client, _, _ = _client(
        _context(scope_type="class", scope_id="class-a"),
        repository,
    )

    response = client.get("/api/v1/teaching/sources")

    assert response.status_code == 200
    assert [item["bindingId"] for item in response.json()["items"]] == ["class-a-binding"]


def test_student_cannot_list_teaching_sources() -> None:
    client, _, _ = _client(
        _context(
            user_id="student-a",
            role="student",
            scope_type="class",
            scope_id="class-a",
        ),
        _SourceRepository(),
    )

    response = client.get("/api/v1/teaching/sources")

    assert response.status_code == 403


def test_knowledge_binding_requires_user_and_organization_authorization() -> None:
    repository = _SourceRepository()
    resolver = _KnowledgeResolver()
    client, _, _ = _client(_context(), repository, resolver=resolver)

    org_denied = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "admin:kb:math", "courseId": "course-b"},
    )
    assert org_denied.status_code == 403
    assert resolver.calls == []

    resolver.error = HTTPException(status_code=403, detail="not assigned")
    user_denied = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "admin:kb:math", "courseId": "course-a"},
    )
    assert user_denied.status_code == 403
    assert repository.knowledge_snapshots == []


def test_same_user_requires_independent_knowledge_entitlement_per_tenant() -> None:
    resolver = _KnowledgeResolver()
    entitled_repository = _SourceRepository()
    denied_repository = _SourceRepository()
    denied_repository.knowledge_entitled = False
    entitled_client, _, _ = _client(
        _context(tenant_id="tenant-a"),
        entitled_repository,
        resolver=resolver,
    )
    denied_client, _, _ = _client(
        _context(tenant_id="tenant-b"),
        denied_repository,
        resolver=resolver,
    )

    entitled = entitled_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "admin:kb:math", "courseId": "course-a"},
    )
    denied = denied_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "admin:kb:math", "courseId": "course-a"},
    )

    assert entitled.status_code == 201
    assert denied.status_code == 403
    assert denied_repository.knowledge_snapshots == []


def test_personal_knowledge_entitlement_is_scoped_to_resource_owner() -> None:
    repository = _SourceRepository()
    repository.knowledge_entitlements = {("user:kb:course-a", "alice")}
    alice_resolver = _KnowledgeResolver()
    alice_resolver.resource_id = "user:kb:course-a"
    alice_resolver.resource_name = "course-a"
    alice_resolver.source = "user"
    bob_resolver = _KnowledgeResolver()
    bob_resolver.resource_id = "user:kb:course-a"
    bob_resolver.resource_name = "course-a"
    bob_resolver.source = "user"
    alice_client, _, _ = _client(
        _context(user_id="alice"),
        repository,
        resolver=alice_resolver,
    )
    bob_client, _, _ = _client(
        _context(user_id="bob"),
        repository,
        resolver=bob_resolver,
    )

    alice = alice_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "course-a", "courseId": "course-a"},
    )
    bob = bob_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "course-a", "courseId": "course-a"},
    )

    assert alice.status_code == 201
    assert bob.status_code == 403
    assert repository.entitlement_calls == [
        ("user:kb:course-a", "alice"),
        ("user:kb:course-a", "bob"),
    ]
    assert len(repository.records) == 1
    assert [snapshot.resource_owner_id for snapshot in repository.knowledge_snapshots] == ["alice"]


def test_recreated_kb_does_not_inherit_old_generation_entitlement() -> None:
    first_generation = "11111111-1111-4111-8111-111111111111"
    second_generation = "22222222-2222-4222-8222-222222222222"
    first_id = f"user:kb:{first_generation}"
    second_id = f"user:kb:{second_generation}"
    repository = _SourceRepository()
    repository.knowledge_entitlements = {(first_id, "alice")}
    resolver = _KnowledgeResolver()
    resolver.resource_id = first_id
    resolver.resource_name = "course-a"
    resolver.source = "user"
    resolver.generation_id = first_generation
    client, _, _ = _client(
        _context(user_id="alice"),
        repository,
        resolver=resolver,
    )

    first = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "user:kb:course-a", "courseId": "course-a"},
    )
    resolver.resource_id = second_id
    resolver.generation_id = second_generation
    recreated = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "user:kb:course-a", "courseId": "course-a"},
    )

    assert first.status_code == 201
    assert recreated.status_code == 403
    assert repository.entitlement_calls == [(first_id, "alice"), (second_id, "alice")]


def test_admin_knowledge_entitlement_uses_shared_workspace_owner() -> None:
    repository = _SourceRepository()
    repository.knowledge_entitlements = {("admin:kb:math", "admin-workspace")}
    alice_client, _, _ = _client(_context(user_id="alice"), repository)
    bob_client, _, _ = _client(_context(user_id="bob"), repository)

    alice = alice_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "math", "courseId": "course-a"},
    )
    bob = bob_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "math", "courseId": "course-a"},
    )

    assert alice.status_code == bob.status_code == 201
    assert repository.entitlement_calls == [
        ("admin:kb:math", "admin-workspace"),
        ("admin:kb:math", "admin-workspace"),
    ]
    assert {snapshot.resource_owner_id for snapshot in repository.knowledge_snapshots} == {
        "admin-workspace"
    }
    assert len({snapshot.snapshot_id for snapshot in repository.knowledge_snapshots}) == 1
    assert len({snapshot.content_sha256 for snapshot in repository.knowledge_snapshots}) == 1
    assert len({snapshot.permission_sha256 for snapshot in repository.knowledge_snapshots}) == 1


def test_personal_knowledge_owner_partitions_snapshot_identity() -> None:
    repository = _SourceRepository()
    repository.knowledge_entitlements = {
        ("user:kb:course-a", "alice"),
        ("user:kb:course-a", "bob"),
    }
    alice_resolver = _KnowledgeResolver()
    alice_resolver.resource_id = "user:kb:course-a"
    alice_resolver.resource_name = "course-a"
    alice_resolver.source = "user"
    bob_resolver = _KnowledgeResolver()
    bob_resolver.resource_id = "user:kb:course-a"
    bob_resolver.resource_name = "course-a"
    bob_resolver.source = "user"
    alice_client, _, _ = _client(
        _context(user_id="alice"),
        repository,
        resolver=alice_resolver,
    )
    bob_client, _, _ = _client(
        _context(user_id="bob"),
        repository,
        resolver=bob_resolver,
    )

    alice = alice_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "course-a", "courseId": "course-a"},
    )
    bob = bob_client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "course-a", "courseId": "course-a"},
    )

    assert alice.status_code == bob.status_code == 201
    assert alice.json()["bindingId"] != bob.json()["bindingId"]
    snapshots = repository.knowledge_snapshots
    assert {snapshot.resource_owner_id for snapshot in snapshots} == {"alice", "bob"}
    assert len({snapshot.snapshot_id for snapshot in snapshots}) == 2
    assert len({snapshot.content_sha256 for snapshot in snapshots}) == 2
    assert len({snapshot.permission_sha256 for snapshot in snapshots}) == 2


def test_stale_assigned_knowledge_resource_fails_closed() -> None:
    repository = _SourceRepository()
    resolver = _KnowledgeResolver()
    client, _, _ = _client(
        _context(),
        repository,
        resolver=resolver,
        resource_exists=False,
    )

    response = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "admin:kb:math", "courseId": "course-a"},
    )

    assert response.status_code == 404
    assert repository.knowledge_snapshots == []


def test_knowledge_binding_stores_only_resolved_stable_resource_id() -> None:
    repository = _SourceRepository()
    resolver = _KnowledgeResolver()
    client, _, _ = _client(_context(), repository, resolver=resolver)

    response = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "math-alias", "courseId": "course-a"},
    )

    assert response.status_code == 201
    assert response.json()["sourceId"] == "admin:kb:math"
    assert resolver.calls == [("math-alias", False)]
    snapshot = repository.knowledge_snapshots[0]
    assert snapshot.resource_id == "admin:kb:math"
    assert not hasattr(snapshot, "content")


def test_invalid_resolved_knowledge_identity_is_rejected_without_database_write() -> None:
    repository = _SourceRepository()
    resolver = _KnowledgeResolver()
    resolver.resource_id = "x" * 129
    client, _, _ = _client(_context(), repository, resolver=resolver)

    response = client.post(
        "/api/v1/teaching/sources/bind",
        json={"knowledgeResourceId": "math-alias", "courseId": "course-a"},
    )

    assert response.status_code == 422
    assert repository.knowledge_snapshots == []


def test_forged_class_course_ancestry_is_rejected() -> None:
    client, store, _ = _client(_context(), _SourceRepository())

    response = _upload(client, _pdf_bytes(), course_id="course-a", class_id="class-b")

    assert response.status_code == 404
    assert store.put_calls == []
    assert store.deleted == []


def test_source_deletion_requires_binding_scope_and_current_tenant() -> None:
    repository = _SourceRepository()
    repository.records["binding-b"] = SourceRecord(
        "binding-b", "knowledge_base", "admin:kb:b", None, "b" * 64, None, "course-b", None
    )
    repository.records["binding-a"] = SourceRecord(
        "binding-a", "knowledge_base", "admin:kb:a", None, "a" * 64, None, "course-a", None
    )
    client, _, _ = _client(_context(), repository)

    denied = client.delete("/api/v1/teaching/sources/binding-b")
    allowed = client.delete("/api/v1/teaching/sources/binding-a")
    other_tenant = client.delete("/api/v1/teaching/sources/tenant-b-binding")

    assert denied.status_code == 403
    assert allowed.status_code == 204
    assert other_tenant.status_code == 404


def test_tenant_id_cannot_be_injected_into_source_binding() -> None:
    repository = _SourceRepository()
    client, _, resolver = _client(_context(), repository)

    response = client.post(
        "/api/v1/teaching/sources/bind",
        json={
            "knowledgeResourceId": "admin:kb:math",
            "courseId": "course-a",
            "tenantId": "tenant-b",
        },
    )

    assert response.status_code == 422
    assert resolver.calls == []
    assert repository.records == {}
