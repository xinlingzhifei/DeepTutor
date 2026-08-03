"""Build frozen teaching contracts from authorized source snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal

from deeptutor.teaching.contracts import (
    AssessmentPlan,
    KnowledgePoint,
    MediaPolicy,
    NetworkPolicy,
    PermissionSummary,
    SafetyPolicy,
    SourceCitation,
    SourceFragment,
    SourceReference,
    TeachingBrief,
    TeachingObjective,
    TemplatePolicy,
    canonical_teaching_brief_sha256,
)
from deeptutor.teaching.contracts import (
    SourceSnapshot as ContractSourceSnapshot,
)
from deeptutor.teaching.source_snapshots import (
    AuthorizedFragment,
    AuthorizedSourceReference,
    SnapshotRequest,
    SourceSnapshot,
    SourceSnapshotBuilder,
)
from deeptutor.teaching.tenant_context import TenantContext

ContentMode = Literal["source_grounded", "open_creation"]


class OpenCreationNotAcknowledged(ValueError):
    """Open creation is blocked until the caller explicitly opts in."""


@dataclass(frozen=True, slots=True)
class KnowledgePointSpec:
    knowledge_point_id: str
    title: str
    description: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.knowledge_point_id, self.title, self.description)
        ):
            raise ValueError("knowledge point is incomplete")


@dataclass(frozen=True, slots=True)
class TeachingBriefSpec:
    course_id: str
    class_id: str
    objective: str
    grade_band: str
    audience: str
    duration_minutes: int
    classroom_mode: Literal["micro", "full"]
    web_policy: Literal["disabled", "enabled"]
    template_id: str
    template_version: str
    knowledge_points: tuple[KnowledgePointSpec, ...]
    content_mode: ContentMode | None = None
    open_creation_acknowledged: bool = False
    allowed_web_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        strings = (
            self.course_id,
            self.class_id,
            self.objective,
            self.grade_band,
            self.audience,
            self.template_id,
            self.template_version,
        )
        if not all(isinstance(value, str) and value.strip() for value in strings):
            raise ValueError("teaching brief specification is incomplete")
        if isinstance(self.duration_minutes, bool) or self.duration_minutes < 1:
            raise ValueError("teaching duration is invalid")
        if not self.knowledge_points:
            raise ValueError("teaching brief requires knowledge points")
        if self.web_policy == "disabled" and self.allowed_web_domains:
            raise ValueError("disabled web policy cannot allow domains")


@dataclass(frozen=True, slots=True)
class TeachingBriefBuildResult:
    contract: TeachingBrief
    source_snapshot_sha256: str | None
    fragments: tuple[AuthorizedFragment, ...]
    source_refs: tuple[AuthorizedSourceReference, ...]

    @property
    def content_mode(self) -> ContentMode:
        return self.contract.content_mode


def _digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _objective(spec: TeachingBriefSpec) -> TeachingObjective:
    point_ids = [point.knowledge_point_id for point in spec.knowledge_points]
    return TeachingObjective(
        objective_id=f"objective-{_digest(spec.objective, *point_ids)}",
        description=spec.objective,
        knowledge_point_ids=point_ids,
    )


def _brief_id(
    context: TenantContext,
    spec: TeachingBriefSpec,
    content_mode: ContentMode,
    snapshot_sha256: str | None,
) -> str:
    return f"brief-{_digest(context.tenant_id, spec.course_id, spec.class_id, spec.objective, content_mode, snapshot_sha256 or '')}"


def _grounding_query(spec: TeachingBriefSpec) -> str:
    return "\n".join(
        (
            spec.objective,
            *(point.title for point in spec.knowledge_points),
            *(point.description for point in spec.knowledge_points),
        )
    )


class TeachingBriefBuilder:
    """Map source authorization evidence to the immutable v1 contract."""

    def __init__(
        self,
        context: TenantContext,
        snapshots: SourceSnapshotBuilder,
    ) -> None:
        self._context = context
        self._snapshots = snapshots

    async def from_kb(
        self,
        kb_ref: str,
        spec: TeachingBriefSpec,
    ) -> TeachingBriefBuildResult:
        if spec.content_mode == "open_creation":
            raise OpenCreationNotAcknowledged(
                "open creation must use the explicit open_creation entry point"
            )
        snapshot = await self._snapshots.from_kb(
            kb_ref,
            SnapshotRequest(
                course_id=spec.course_id,
                class_id=spec.class_id,
                query=_grounding_query(spec),
            ),
        )
        return self._grounded(spec, snapshot)

    async def from_pdf(
        self,
        binding_id: str,
        spec: TeachingBriefSpec,
    ) -> TeachingBriefBuildResult:
        if spec.content_mode == "open_creation":
            raise OpenCreationNotAcknowledged(
                "open creation must use the explicit open_creation entry point"
            )
        snapshot = await self._snapshots.from_pdf(
            binding_id,
            SnapshotRequest(
                course_id=spec.course_id,
                class_id=spec.class_id,
                query=_grounding_query(spec),
            ),
        )
        return self._grounded(spec, snapshot)

    def open_creation(self, spec: TeachingBriefSpec) -> TeachingBriefBuildResult:
        if spec.content_mode != "open_creation" or not spec.open_creation_acknowledged:
            raise OpenCreationNotAcknowledged(
                "open creation requires an explicit acknowledgement"
            )
        return self._build(spec, "open_creation", None)

    def _grounded(
        self,
        spec: TeachingBriefSpec,
        snapshot: SourceSnapshot,
    ) -> TeachingBriefBuildResult:
        return self._build(spec, "source_grounded", snapshot)

    def _build(
        self,
        spec: TeachingBriefSpec,
        content_mode: ContentMode,
        snapshot: SourceSnapshot | None,
    ) -> TeachingBriefBuildResult:
        fragments = snapshot.fragments if snapshot is not None else ()
        source_refs = snapshot.source_refs if snapshot is not None else ()
        contract_fragments = [
            SourceFragment(
                fragment_id=fragment.fragment_id,
                source_id=fragment.source_id,
                text=fragment.text,
                content_sha256=fragment.content_sha256,
            )
            for fragment in fragments
        ]
        contract_citations = [
            SourceCitation(
                citation_id=reference.citation_id,
                source_id=reference.source_id,
                fragment_id=reference.fragment_id,
                label=reference.section
                or f"{reference.document_id}{f' p.{reference.page}' if reference.page else ''}",
            )
            for reference in source_refs
        ]
        contract_refs = [
            SourceReference(
                citation_id=reference.citation_id,
                source_id=reference.source_id,
                fragment_id=reference.fragment_id,
            )
            for reference in source_refs
        ]
        allowed_sources = list(dict.fromkeys(item.source_id for item in fragments))
        allowed_fragments = [item.fragment_id for item in fragments]
        contract = TeachingBrief(
            schema_version="1.0",
            brief_id=_brief_id(
                self._context,
                spec,
                content_mode,
                snapshot.snapshot_sha256 if snapshot is not None else None,
            ),
            brief_version=1,
            tenant_id=self._context.tenant_id,
            course_id=spec.course_id,
            target_class_id=spec.class_id,
            grade_band=spec.grade_band,
            audience_level=spec.audience,
            classroom_mode=spec.classroom_mode,
            objectives=[_objective(spec)],
            duration_minutes=spec.duration_minutes,
            knowledge_points=[
                KnowledgePoint(
                    knowledge_point_id=point.knowledge_point_id,
                    title=point.title,
                    description=point.description,
                )
                for point in spec.knowledge_points
            ],
            prerequisites=[],
            assessment=AssessmentPlan(
                methods=["discussion"],
                success_criteria=[spec.objective],
            ),
            source_snapshot=(
                ContractSourceSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    created_at=snapshot.created_at,
                    content_sha256=snapshot.snapshot_sha256,
                )
                if snapshot is not None
                else None
            ),
            source_fragments=contract_fragments,
            citations=contract_citations,
            source_refs=contract_refs,
            permission_summary=PermissionSummary(
                allowed_source_ids=allowed_sources,
                allowed_fragment_ids=allowed_fragments,
                usage_scope=(
                    f"tenant:{self._context.tenant_id}/course:{spec.course_id}/class:{spec.class_id}"
                ),
                attribution_required=snapshot is not None,
            ),
            content_mode=content_mode,
            network_policy=NetworkPolicy(
                allow_web_access=spec.web_policy == "enabled",
                allowed_domains=list(spec.allowed_web_domains),
            ),
            media_policy=MediaPolicy(
                allow_generation=True,
                allowed_mime_types=["image/png", "audio/mpeg"],
            ),
            template_policy=TemplatePolicy(
                template_id=spec.template_id,
                template_version=spec.template_version,
            ),
            safety_policy=SafetyPolicy(
                policy_id="school-default",
                blocked_categories=[],
            ),
            content_sha256="0" * 64,
        )
        contract = contract.model_copy(
            update={"content_sha256": canonical_teaching_brief_sha256(contract)}
        )
        return TeachingBriefBuildResult(
            contract=contract,
            source_snapshot_sha256=(
                snapshot.snapshot_sha256 if snapshot is not None else None
            ),
            fragments=fragments,
            source_refs=source_refs,
        )


__all__ = [
    "KnowledgePointSpec",
    "OpenCreationNotAcknowledged",
    "TeachingBriefBuildResult",
    "TeachingBriefBuilder",
    "TeachingBriefSpec",
]
