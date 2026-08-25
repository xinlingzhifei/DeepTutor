from __future__ import annotations

from pathlib import Path


def test_pbl_grading_migration_is_current_tenant_head() -> None:
    from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION

    migration = (
        Path(__file__).parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260825_0020_pbl_grading_results.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert TEACHING_MIGRATION_HEAD_REVISION == "20260825_0020"
    assert 'revision: str = "20260825_0020"' in source
    assert 'down_revision: str | None = "20260825_0019"' in source
    assert '"pbl_grading_results"' in source
    assert 'sa.UniqueConstraint("event_id"' in source
    assert '"uq_pbl_grading_results_tenant_idempotency"' in source
    assert "grading_source = 'teacher_review'" in source
    assert "score IS NULL OR (score >= 0 AND score <= 1)" in source


def test_pbl_grading_model_exports_trusted_source_contract() -> None:
    from deeptutor.teaching.models import PblGradingResult

    table = PblGradingResult.__table__
    assert table.name == "pbl_grading_results"
    assert {
        "id",
        "event_id",
        "tenant_id",
        "session_id",
        "user_id",
        "classroom_version_id",
        "scene_id",
        "milestone_id",
        "knowledge_point_id",
        "rubric_sha256",
        "correctness",
        "score",
        "grading_source",
        "source_reference",
        "graded_by",
        "graded_at",
        "idempotency_key",
        "request_sha256",
    }.issubset(table.columns.keys())
