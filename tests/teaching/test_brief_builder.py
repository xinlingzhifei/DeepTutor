from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deeptutor.teaching.brief_builder import (
    KnowledgePointSpec,
    OpenCreationNotAcknowledged,
    TeachingBriefBuilder,
    TeachingBriefSpec,
)
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.source_snapshots import (
    AuthorizedFragment,
    AuthorizedSourceReference,
    PermissionEvidence,
    SourceSnapshot,
)
from deeptutor.teaching.tenant_context import TenantContext


def _context() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id="teacher-a",
        permissions=frozenset(
            {
                ScopedPermission(
                    permission="source.use",
                    scope_type="course",
                    scope_id="course-a",
                    tenant_id="tenant-a",
                )
            }
        ),
    )


def _spec(**changes) -> TeachingBriefSpec:
    values = {
        "course_id": "course-a",
        "class_id": "class-a",
        "objective": "Explain Newton's second law",
        "grade_band": "upper_secondary",
        "audience": "introductory",
        "duration_minutes": 20,
        "classroom_mode": "full",
        "web_policy": "disabled",
        "template_id": "lesson-default",
        "template_version": "1",
        "knowledge_points": (
            KnowledgePointSpec(
                knowledge_point_id="kp-newton-2",
                title="Newton's second law",
                description="Relate net force, mass, and acceleration.",
            ),
        ),
    }
    values.update(changes)
    return TeachingBriefSpec(**values)


def _snapshot() -> SourceSnapshot:
    text = "Net force equals the rate of change of momentum."
    fragment = AuthorizedFragment.create(
        stable_source_id="user:kb:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        provider_fragment_id="chunk-7",
        text=text,
        document_id="document-7",
        page=4,
        section="Newton's second law",
    )
    reference = AuthorizedSourceReference(
        citation_id="citation-7",
        source_id=fragment.source_id,
        fragment_id=fragment.fragment_id,
        document_id="document-7",
        page=4,
        section="Newton's second law",
    )
    return SourceSnapshot.create(
        source_kind="knowledge_base",
        stable_source_id=fragment.source_id,
        source_revision="binding-v1",
        fragments=(fragment,),
        source_refs=(reference,),
        permission_summary=PermissionEvidence(
            permissions=("source.use",),
            scope_type="course",
            scope_id="course-a",
        ),
        query_sha256="1" * 64,
        retrieval_provider="llamaindex",
        retrieval_view_signature="2" * 64,
        created_at=datetime(2026, 8, 3, 8, tzinfo=timezone.utc),
        created_by="teacher-a",
    )


class _SnapshotBuilder:
    def __init__(self, snapshot: SourceSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = []

    async def from_kb(self, kb_ref, request):
        self.calls.append((kb_ref, request))
        return self.snapshot

    async def from_pdf(self, binding_id, request):
        self.calls.append((binding_id, request))
        return self.snapshot


@pytest.mark.asyncio
async def test_grounded_brief_maps_authorized_snapshot_without_changing_frozen_contract() -> None:
    snapshots = _SnapshotBuilder(_snapshot())
    builder = TeachingBriefBuilder(
        _context(),
        snapshots,
    )

    brief = await builder.from_kb(
        "user:kb:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        _spec(),
    )

    assert brief.content_mode == "source_grounded"
    assert brief.source_snapshot_sha256 == _snapshot().snapshot_sha256
    assert all(fragment.permission == "source.use" for fragment in brief.fragments)
    assert all(scene_ref.document_id for scene_ref in brief.source_refs)
    assert brief.contract.source_fragments[0].text == brief.fragments[0].text
    assert brief.contract.source_refs[0].fragment_id == brief.source_refs[0].fragment_id
    assert brief.contract.content_sha256 != "0" * 64
    assert "permission" not in brief.contract.source_fragments[0].model_dump()
    assert "document_id" not in brief.contract.source_refs[0].model_dump()


@pytest.mark.asyncio
async def test_teacher_defaults_to_source_grounded_mode() -> None:
    snapshots = _SnapshotBuilder(_snapshot())
    builder = TeachingBriefBuilder(_context(), snapshots)

    brief = await builder.from_kb("user:kb:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", _spec())

    assert brief.content_mode == "source_grounded"
    assert snapshots.calls[0][1].course_id == "course-a"


def test_open_creation_requires_explicit_acknowledgement() -> None:
    builder = TeachingBriefBuilder(_context(), _SnapshotBuilder(_snapshot()))

    with pytest.raises(OpenCreationNotAcknowledged, match="explicit"):
        builder.open_creation(
            _spec(content_mode="open_creation", open_creation_acknowledged=False)
        )

    brief = builder.open_creation(
        _spec(content_mode="open_creation", open_creation_acknowledged=True)
    )
    assert brief.content_mode == "open_creation"
    assert brief.contract.source_snapshot is None
    assert brief.contract.source_fragments == []


@pytest.mark.asyncio
async def test_pdf_brief_uses_the_same_grounded_contract_mapping() -> None:
    snapshots = _SnapshotBuilder(_snapshot())
    builder = TeachingBriefBuilder(_context(), snapshots)

    brief = await builder.from_pdf("pdf-binding-a", _spec())

    assert brief.content_mode == "source_grounded"
    assert brief.contract.source_fragments[0].text == brief.fragments[0].text
    assert snapshots.calls[0][0] == "pdf-binding-a"
    assert "Newton's second law" in snapshots.calls[0][1].query
    assert "Relate net force, mass, and acceleration." in snapshots.calls[0][1].query
