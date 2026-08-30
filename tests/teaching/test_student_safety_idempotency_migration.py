from __future__ import annotations

from pathlib import Path


def test_student_safety_idempotency_migration_is_current_tenant_head() -> None:
    from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION

    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260827_0021_student_safety_idempotency.py"
    )
    source = migration.read_text(encoding="utf-8")
    upgrade = source.split("def _upgrade_tenant()", 1)[1].split("def upgrade()", 1)[0]
    downgrade = source.split("def _downgrade_tenant()", 1)[1].split("def downgrade()", 1)[0]

    assert TEACHING_MIGRATION_HEAD_REVISION == "20260830_0023"
    assert 'revision: str = "20260827_0021"' in source
    assert 'down_revision: str | None = "20260825_0020"' in source
    assert (
        upgrade.index("op.add_column(")
        < upgrade.index("UPDATE {assessments_table}")
        < upgrade.index("op.alter_column(")
        < upgrade.index("op.create_check_constraint(")
        < upgrade.index("_sync_tenant_schema_revision(")
    )
    assert "EXTRACT(EPOCH FROM (expires_at - reviewed_at))" in upgrade
    assert "requested_expires_at = expires_at" in upgrade
    assert '"valid_for_seconds > 0"' in upgrade
    assert '"expires_at <= requested_expires_at"' in upgrade
    assert "LOCK TABLE" in downgrade
    assert "expires_at <> requested_expires_at" in downgrade
    assert downgrade.index("SELECT EXISTS") < downgrade.index("op.drop_column(")


def test_student_safety_model_keeps_original_request_duration() -> None:
    from deeptutor.teaching.models import StudentSafetyAssessmentRecord

    table = StudentSafetyAssessmentRecord.__table__
    assert table.c.valid_for_seconds.nullable is False
    assert table.c.requested_expires_at.nullable is False
    assert {constraint.name for constraint in table.constraints if constraint.name is not None} >= {
        "ck_student_safety_assessments_valid_for_seconds",
        "ck_student_safety_assessments_supersession_window",
    }
