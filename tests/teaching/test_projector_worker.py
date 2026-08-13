from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_worker_projects_one_claim_from_an_active_tenant() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    event = ProjectionEvent(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="quiz.graded",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={},
    )
    claim = ProjectionClaim(event=event, lease_owner="worker-a", lease_token="token-a")

    class Repository:
        def __init__(self) -> None:
            self.projected = []
            self.heartbeats = 0

        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, tenant_id, *, owner, lease_seconds):
            assert (tenant_id, owner, lease_seconds) == ("tenant-a", "worker-a", 60)
            return claim

        async def heartbeat(self, claimed, *, lease_seconds):
            assert claimed is claim
            self.heartbeats += 1

        async def project(self, claimed, *, document):
            self.projected.append((claimed, document))

    class Documents:
        async def load_version_document(self, tenant_id, version_id):
            assert (tenant_id, version_id) == ("tenant-a", "version-a")
            return "immutable-document"

    repository = Repository()
    worker = LearningProjectionWorker(
        repository=repository,
        documents=Documents(),
        worker_id="worker-a",
    )

    assert await worker.run_once() is True
    assert repository.projected == [(claim, "immutable-document")]
    assert repository.heartbeats == 2


@pytest.mark.asyncio
async def test_deterministic_rejection_tolerates_lease_loss_before_quarantine() -> None:
    from deeptutor.teaching.projector_worker import (
        LearningProjectionWorker,
        ProjectionClaim,
        ProjectionLeaseLost,
    )
    from deeptutor.teaching.projectors.mastery import (
        DeterministicProjectionError,
        ProjectionEvent,
    )

    event = ProjectionEvent(
        event_id="event-invalid",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="classroom.started",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id=None,
        knowledge_point_id=None,
        payload={},
    )
    claim = ProjectionClaim(event=event, lease_owner="worker-a", lease_token="token-a")

    class Repository:
        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, tenant_id, *, owner, lease_seconds):
            return claim

        async def heartbeat(self, claimed, *, lease_seconds):
            return None

        async def project(self, claimed, *, document):
            raise DeterministicProjectionError("invalid_scoring")

        async def quarantine(self, claimed, *, reason_code):
            assert reason_code == "invalid_scoring"
            raise ProjectionLeaseLost()

    class Documents:
        async def load_version_document(self, tenant_id, version_id):
            raise AssertionError("non-quiz events do not load a document")

    worker = LearningProjectionWorker(
        repository=Repository(),
        documents=Documents(),
        worker_id="worker-a",
    )

    assert await worker.run_once() is True


@pytest.mark.asyncio
async def test_worker_round_robins_active_tenants_instead_of_starving_later_tenants() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    def claim_for(tenant_id: str) -> ProjectionClaim:
        return ProjectionClaim(
            event=ProjectionEvent(
                event_id=f"event-{tenant_id}",
                tenant_id=tenant_id,
                session_id=f"session-{tenant_id}",
                user_id="student-a",
                classroom_version_id="version-a",
                seq=1,
                event_type="classroom.started",
                occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
                scene_id=None,
                knowledge_point_id=None,
                payload={},
            ),
            lease_owner="worker-a",
            lease_token=f"token-{tenant_id}",
        )

    class Repository:
        def __init__(self) -> None:
            self.projected: list[str] = []

        async def active_tenant_ids(self):
            return ("tenant-a", "tenant-b")

        async def claim(self, tenant_id, *, owner, lease_seconds):
            return claim_for(tenant_id)

        async def heartbeat(self, claimed, *, lease_seconds):
            return None

        async def project(self, claimed, *, document):
            self.projected.append(claimed.event.tenant_id)

    class Documents:
        async def load_version_document(self, tenant_id, version_id):
            raise AssertionError("non-quiz events do not load a document")

    repository = Repository()
    worker = LearningProjectionWorker(
        repository=repository,
        documents=Documents(),
        worker_id="worker-a",
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert repository.projected == ["tenant-a", "tenant-b"]


@pytest.mark.asyncio
async def test_worker_heartbeats_during_slow_document_load_to_prevent_reclaim() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    event = ProjectionEvent(
        event_id="event-slow",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="quiz.graded",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={},
    )

    class Repository:
        def __init__(self) -> None:
            self.claimed = ProjectionClaim(
                event=event,
                lease_owner="worker-a",
                lease_token="token-a",
            )
            self.expires_at = 0.0
            self.projected = False

        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, tenant_id, *, owner, lease_seconds):
            now = asyncio.get_running_loop().time()
            if owner == "worker-a":
                self.expires_at = now + lease_seconds
                return self.claimed
            if now >= self.expires_at:
                return ProjectionClaim(
                    event=event,
                    lease_owner=owner,
                    lease_token="token-b",
                )
            return None

        async def heartbeat(self, claimed, *, lease_seconds):
            assert claimed is self.claimed
            self.expires_at = asyncio.get_running_loop().time() + lease_seconds

        async def project(self, claimed, *, document):
            assert claimed is self.claimed
            self.projected = True

    class Documents:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def load_version_document(self, tenant_id, version_id):
            self.started.set()
            await self.release.wait()
            return "immutable-document"

    repository = Repository()
    documents = Documents()
    worker = LearningProjectionWorker(
        repository=repository,
        documents=documents,
        worker_id="worker-a",
        lease_seconds=1,
    )

    work = asyncio.create_task(worker.run_once())
    try:
        await documents.started.wait()
        await asyncio.sleep(1.2)
        assert await repository.claim("tenant-a", owner="worker-b", lease_seconds=1) is None
    finally:
        documents.release.set()
        await work
    assert repository.projected is True


@pytest.mark.asyncio
async def test_worker_stops_document_load_when_periodic_heartbeat_loses_lease() -> None:
    from deeptutor.teaching.projector_worker import (
        LearningProjectionWorker,
        ProjectionClaim,
        ProjectionLeaseLost,
    )
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    claim = ProjectionClaim(
        event=ProjectionEvent(
            event_id="event-lost",
            tenant_id="tenant-a",
            session_id="session-a",
            user_id="student-a",
            classroom_version_id="version-a",
            seq=1,
            event_type="quiz.graded",
            occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            scene_id="quiz-scene",
            knowledge_point_id="kp-1",
            payload={},
        ),
        lease_owner="worker-a",
        lease_token="token-a",
    )

    class Repository:
        def __init__(self) -> None:
            self.heartbeats = 0
            self.projected = False

        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, tenant_id, *, owner, lease_seconds):
            return claim

        async def heartbeat(self, claimed, *, lease_seconds):
            self.heartbeats += 1
            if self.heartbeats > 1:
                raise ProjectionLeaseLost("lease was reclaimed")

        async def project(self, claimed, *, document):
            self.projected = True

    class Documents:
        def __init__(self) -> None:
            self.cancelled = False

        async def load_version_document(self, tenant_id, version_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    repository = Repository()
    documents = Documents()
    worker = LearningProjectionWorker(
        repository=repository,
        documents=documents,
        worker_id="worker-a",
        lease_seconds=1,
    )

    assert await asyncio.wait_for(worker.run_once(), timeout=1) is True
    assert documents.cancelled is True
    assert repository.projected is False


@pytest.mark.asyncio
async def test_worker_propagates_cancellation_during_document_load() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    claim = ProjectionClaim(
        event=ProjectionEvent(
            event_id="event-cancelled",
            tenant_id="tenant-a",
            session_id="session-a",
            user_id="student-a",
            classroom_version_id="version-a",
            seq=1,
            event_type="quiz.graded",
            occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            scene_id="quiz-scene",
            knowledge_point_id="kp-1",
            payload={},
        ),
        lease_owner="worker-a",
        lease_token="token-a",
    )

    class Repository:
        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, tenant_id, *, owner, lease_seconds):
            return claim

        async def heartbeat(self, claimed, *, lease_seconds):
            return None

        async def project(self, claimed, *, document):
            raise AssertionError("cancelled work must not project")

    class Documents:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def load_version_document(self, tenant_id, version_id):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    documents = Documents()
    worker = LearningProjectionWorker(
        repository=Repository(),
        documents=documents,
        worker_id="worker-a",
        lease_seconds=1,
    )
    task = asyncio.create_task(worker.run_once())
    await documents.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert documents.cancelled is True


def test_mastery_advisory_lock_key_is_stable_and_tenant_isolated() -> None:
    from deeptutor.teaching.projector_worker import _mastery_lock_key

    assert _mastery_lock_key("tenant-a", "student-a", "kp-1") == _mastery_lock_key(
        "tenant-a",
        "student-a",
        "kp-1",
    )
    assert _mastery_lock_key("tenant-a", "student-a", "kp-1") != _mastery_lock_key(
        "tenant-b",
        "student-a",
        "kp-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "expected_action", "expected_code"),
    [
        (
            lambda: __import__(
                "deeptutor.teaching.projectors.mastery",
                fromlist=["DeterministicProjectionError"],
            ).DeterministicProjectionError("classroom_document_integrity_invalid"),
            "quarantine",
            "classroom_document_integrity_invalid",
        ),
        (
            lambda: __import__(
                "deeptutor.teaching.services.classroom_content",
                fromlist=["ClassroomContentUnavailable"],
            ).ClassroomContentUnavailable("temporary storage outage"),
            "retry",
            "transient_classroom_document_unavailable",
        ),
    ],
)
async def test_worker_classifies_document_loader_failures(
    error_factory,
    expected_action: str,
    expected_code: str,
) -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    claim = ProjectionClaim(
        event=ProjectionEvent(
            event_id="event-document-failure",
            tenant_id="tenant-a",
            session_id="session-a",
            user_id="student-a",
            classroom_version_id="version-a",
            seq=1,
            event_type="quiz.graded",
            occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            scene_id="quiz-scene",
            knowledge_point_id="kp-1",
            payload={},
        ),
        lease_owner="worker-a",
        lease_token="token-a",
    )

    class Repository:
        def __init__(self) -> None:
            self.actions: list[tuple[str, str]] = []

        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, tenant_id, *, owner, lease_seconds):
            return claim

        async def heartbeat(self, claimed, *, lease_seconds):
            return None

        async def project(self, claimed, *, document):
            raise AssertionError("failed document loads must not project")

        async def quarantine(self, claimed, *, reason_code):
            self.actions.append(("quarantine", reason_code))

        async def retry(self, claimed, *, error_code):
            self.actions.append(("retry", error_code))

    class Documents:
        async def load_version_document(self, tenant_id, version_id):
            raise error_factory()

    repository = Repository()
    worker = LearningProjectionWorker(
        repository=repository,
        documents=Documents(),
        worker_id="worker-a",
    )

    assert await worker.run_once() is True
    assert repository.actions == [(expected_action, expected_code)]
