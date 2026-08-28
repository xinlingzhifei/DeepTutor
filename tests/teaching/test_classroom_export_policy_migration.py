from pathlib import Path


def test_classroom_export_policy_mutations_have_persisted_revisions() -> None:
    from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION

    migration = (
        Path(__file__).parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260828_0022_classroom_export_policy_cas.py"
    )

    assert TEACHING_MIGRATION_HEAD_REVISION == "20260828_0022"
    source = migration.read_text(encoding="utf-8")
    assert 'revision: str = "20260828_0022"' in source
    assert 'down_revision: str | None = "20260827_0021"' in source
    assert '"exists",' in source
    assert '"revision",' in source
    assert '"operation_id",' in source
    assert '"classroom_export_policy_operations",' in source
    assert 'sa.PrimaryKeyConstraint("operation_id"' in source
    assert '"mutation",' in source
    assert '"expected_revision",' in source
    assert '"result_revision",' in source
    assert '"result_exists",' in source
    assert '"result_allow_mp4",' in source
    assert '"AND result_allow_mp4 = allow_mp4) OR "' in source
    assert '"ck_classroom_export_policies_tombstone"' in source
    assert '"ck_classroom_export_policies_revision"' in source
    assert '"ck_classroom_export_policies_operation_id"' in source
    assert '"ck_classroom_export_policy_operations_operation_id"' in source
    assert '"ck_classroom_export_policy_operations_shape"' in source
    assert 'WHERE NOT "exists"' in source
    assert "LOCK TABLE {policy_table}, {operation_table} IN ACCESS EXCLUSIVE MODE" in source
    assert "cannot downgrade classroom export policy CAS history" in source
    assert "op.alter_column(" in source
    assert "nullable=False," in source
    assert '_sync_tenant_schema_revision("20260827_0021", "20260828_0022")' in source
    assert '_sync_tenant_schema_revision("20260828_0022", "20260827_0021")' in source
