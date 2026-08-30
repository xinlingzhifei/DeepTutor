from __future__ import annotations

from pathlib import Path
import re


def test_generation_route_evidence_is_the_dual_scope_current_head() -> None:
    from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION

    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    )
    source = migration.read_text(encoding="utf-8")
    model_source = (
        Path(__file__).resolve().parents[2] / "deeptutor" / "teaching" / "models" / "platform.py"
    ).read_text(encoding="utf-8")
    export_source = (
        Path(__file__).resolve().parents[2] / "deeptutor" / "teaching" / "models" / "__init__.py"
    ).read_text(encoding="utf-8")

    assert TEACHING_MIGRATION_HEAD_REVISION == "20260830_0023"
    assert 'revision: str = "20260830_0023"' in source
    assert 'down_revision: str | None = "20260828_0022"' in source
    assert 'if _migration_scope() == "platform"' in source
    assert '"generation_route_attempts"' in source
    assert '"tenant_id",\n            "job_id",\n            "attempt_count"' in source
    assert '["platform.tenants.id"]' in source
    assert 'ondelete="RESTRICT"' in source
    assert "attempt_count > 0" in source
    assert "phase IN ('outline', 'content', 'export')" in source
    assert "decision IN ('selected', 'unavailable')" in source
    assert "data_plane_mode IN ('shared', 'dedicated')" in source
    assert "generation_route_attempts_append_only" in source
    assert "BEFORE UPDATE OR DELETE ON platform.generation_route_attempts" in source
    assert "generation_route_attempts_append_only_truncate" in source
    assert "BEFORE TRUNCATE ON platform.generation_route_attempts" in source
    assert "FOR EACH STATEMENT" in source
    assert 'sa.Column("data_plane_mode", sa.String(length=16), nullable=True)' in source
    assert "generation_jobs_route_binding_immutable" in source
    for binding in (
        "data_plane_mode",
        "data_plane_route_id",
        "provider_profile_id",
        "worker_pool_ref",
        "queue_ref",
    ):
        assert f"NEW.{binding} IS DISTINCT FROM OLD.{binding}" in source
    assert '_sync_tenant_schema_revision("20260828_0022", "20260830_0023")' in source
    assert '_sync_tenant_schema_revision("20260830_0023", "20260828_0022")' in source
    assert "cannot downgrade generation route evidence: durable facts exist" in source
    assert "cannot downgrade generation route evidence: job bindings exist" in source
    assert "class GenerationRouteAttempt(PlatformBase):" in model_source
    assert '__tablename__ = "generation_route_attempts"' in model_source
    assert '"GenerationRouteAttempt",' in export_source


def test_generation_route_attempt_table_rejects_zero_configuration_digests() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    model = (root / "deeptutor" / "teaching" / "models" / "platform.py").read_text(encoding="utf-8")
    zero_digest = "0" * 64

    for source in (migration, model):
        compact_source = re.sub(r'[\s"]+', "", source)
        assert f"route_config_digest<>'{zero_digest}'" in compact_source
        assert f"provider_config_digest<>'{zero_digest}'" in compact_source


def test_generation_route_evidence_fails_closed_for_legacy_nonterminal_jobs() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    tenant_upgrade = migration.split("def _upgrade_tenant()", 1)[1].split(
        "def upgrade()",
        1,
    )[0]

    assert "WHERE status NOT IN ('succeeded', 'failed', 'canceled')" in tenant_upgrade
    assert (
        tenant_upgrade.index("op.add_column(")
        < tenant_upgrade.index("SELECT EXISTS")
        < tenant_upgrade.index("op.create_check_constraint(")
        < tenant_upgrade.index("_create_tenant_binding_guard()")
        < tenant_upgrade.index("_sync_tenant_schema_revision(")
    )
    assert "raise CommandError(" in tenant_upgrade
    assert (
        '"cannot upgrade generation route evidence with legacy nonterminal jobs"' in tenant_upgrade
    )


def test_generation_route_evidence_downgrade_drops_both_append_only_triggers() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    platform_downgrade = migration.split("def _downgrade_platform()", 1)[1].split(
        "def _downgrade_tenant()",
        1,
    )[0]

    assert (
        platform_downgrade.index("DROP TRIGGER generation_route_attempts_append_only_truncate")
        < platform_downgrade.index("DROP TRIGGER generation_route_attempts_append_only ")
        < platform_downgrade.index(
            "DROP FUNCTION platform.reject_generation_route_attempt_mutation()"
        )
        < platform_downgrade.index('op.drop_table("generation_route_attempts"')
    )


def test_generation_route_attempt_writes_use_a_lease_fenced_database_entrypoint() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")

    assert "CREATE FUNCTION platform.record_generation_route_attempt(" in migration
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, platform" in migration
    assert ") OWNER TO yfeistai_migrator" in migration
    assert "FROM platform.tenants AS tenant" in migration
    assert "JOIN platform.tenant_schema_states AS schema_state" in migration
    assert "FOR KEY SHARE OF tenant, schema_state" in migration
    assert "pg_catalog.format(" in migration
    assert "%I.generation_jobs" in migration
    assert "FOR UPDATE" in migration
    assert "v_lease_token IS DISTINCT FROM p_lease_token" in migration
    assert "v_lease_expires_at > pg_catalog.clock_timestamp()" in migration
    for binding_check in (
        "v_phase IS DISTINCT FROM p_phase",
        "v_status IS DISTINCT FROM v_expected_status",
        "v_attempt_count IS DISTINCT FROM p_attempt_count",
        "v_data_plane_mode IS DISTINCT FROM p_data_plane_mode",
        "v_data_plane_route_id IS DISTINCT FROM p_data_plane_route_id",
        "v_provider_profile_id IS DISTINCT FROM p_provider_profile_id",
        "v_worker_pool_ref IS DISTINCT FROM p_worker_pool_ref",
        "v_queue_ref IS DISTINCT FROM p_queue_ref",
        "v_lease_owner IS DISTINCT FROM p_worker_id",
    ):
        assert binding_check in migration
    assert "INSERT INTO platform.generation_route_attempts" in migration
    assert "ON CONFLICT (tenant_id, job_id, attempt_count) DO NOTHING" in migration
    assert "ERRCODE = 'PGR02'" in migration
    assert "ERRCODE = 'PGR03'" in migration
    assert (
        "lease_token"
        not in migration.split(
            "INSERT INTO platform.generation_route_attempts",
            1,
        )[1].split("ON CONFLICT", 1)[0]
    )


def test_selected_generation_route_attempt_locks_active_configuration_digest_binding() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    entrypoint = migration.split(
        "CREATE FUNCTION platform.record_generation_route_attempt(",
        1,
    )[1].split("$route_attempt$", 2)[1]

    assert "FROM platform.data_plane_routes AS route" in entrypoint
    assert "JOIN platform.provider_profiles AS profile" in entrypoint
    assert "profile.id = route.provider_profile_id" in entrypoint
    assert "route.id = p_data_plane_route_id" in entrypoint
    assert "route.mode = p_data_plane_mode" in entrypoint
    assert "route.provider_profile_id = p_provider_profile_id" in entrypoint
    assert "route.worker_pool = p_worker_pool_ref" in entrypoint
    assert "route.queue_name = p_queue_ref" in entrypoint
    assert "profile.scope = p_data_plane_mode" in entrypoint
    assert "FOR SHARE OF route, profile" in entrypoint
    assert "p_data_plane_mode = 'shared'" in entrypoint
    assert "route.tenant_id IS NULL" in entrypoint
    assert "route.owner_key = 'shared'" in entrypoint
    assert "profile.tenant_id IS NULL" in entrypoint
    assert "profile.owner_key = 'shared'" in entrypoint
    assert "p_data_plane_mode = 'dedicated'" in entrypoint
    assert "route.tenant_id = p_tenant_id" in entrypoint
    assert "route.owner_key = p_tenant_id" in entrypoint
    assert "profile.tenant_id = p_tenant_id" in entrypoint
    assert "profile.owner_key = p_tenant_id" in entrypoint
    assert "p_config_revision = 'route-binding-v1'" in entrypoint
    assert "p_route_config_digest" in entrypoint
    assert "p_provider_config_digest" in entrypoint
    assert "p_decision = 'selected'" in entrypoint
    assert "route.status = 'active'" in entrypoint
    assert "route.health_status = 'healthy'" in entrypoint
    assert "profile.status = 'active'" in entrypoint
    assert "route.base_url" in entrypoint
    assert "profile.api_base_url" in entrypoint
    assert "profile.secret_ref" in entrypoint
    assert "FOR SHARE OF route, profile" in entrypoint
    assert "config_revision" in entrypoint
    assert "route_config_digest" in entrypoint
    assert "provider_config_digest" in entrypoint


def test_generation_route_attempt_reads_use_a_narrow_database_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    repository = (root / "deeptutor" / "teaching" / "repositories" / "data_planes.py").read_text(
        encoding="utf-8"
    )

    assert "CREATE FUNCTION platform.read_generation_route_attempts(" in migration
    read_entrypoint = migration.split(
        "CREATE FUNCTION platform.read_generation_route_attempts(",
        1,
    )[1].split("$route_attempt_read$", 2)[1]
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, platform" in migration
    assert "%I.generation_jobs" in read_entrypoint
    assert "p_tenant_id" in read_entrypoint
    assert "p_job_id" in read_entrypoint
    for binding in (
        "p_data_plane_mode",
        "p_data_plane_route_id",
        "p_provider_profile_id",
        "p_worker_pool_ref",
        "p_queue_ref",
    ):
        assert binding in read_entrypoint
    assert "RETURN QUERY" in read_entrypoint
    assert "ORDER BY attempt.attempt_count" in read_entrypoint
    assert "SELECT * FROM platform.read_generation_route_attempts(" in repository
    audit_method = repository.split("async def resolve_job_route_audit(", 1)[1].split(
        "async def resolve_bound_profile(", 1
    )[0]
    assert "select(GenerationRouteAttempt)" not in audit_method


def test_platform_downgrade_detects_physically_upgraded_untracked_tenant_schemas() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    platform_downgrade = migration.split("def _downgrade_platform()", 1)[1].split(
        "def _downgrade_tenant()",
        1,
    )[0]

    assert "FROM information_schema.columns" in platform_downgrade
    assert "table_schema ~ '^tenant_[0-9a-f]{16}$'" in platform_downgrade
    assert "table_name = 'generation_jobs'" in platform_downgrade
    assert "column_name = 'data_plane_mode'" in platform_downgrade
    assert "downgrade tenant schemas before generation route evidence" in platform_downgrade


def test_generation_route_attempt_table_write_is_not_granted_to_the_app_role() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")
    migration_script = (root / "scripts" / "migrate_teaching.py").read_text(encoding="utf-8")

    assert "REVOKE ALL ON FUNCTION" in migration
    assert re.search(
        r"platform\.record_generation_route_attempt\(\s*text,\s*text,\s*text,"
        r"\s*integer,\s*text,\s*text,\s*text,\s*text,\s*text,\s*text,\s*text,"
        r"\s*text,\s*text,\s*text,\s*text\s*\)",
        migration,
    )
    assert "FROM PUBLIC" in migration
    assert "GRANT EXECUTE ON FUNCTION" in migration
    assert "TO yfeistai_app" in migration
    assert re.search(
        r"REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE\s+"
        r"platform\.generation_route_attempts FROM yfeistai_app",
        migration,
    )
    assert "_ROUTE_ATTEMPT_FUNCTION_SIGNATURE" in migration_script
    assert "platform.record_generation_route_attempt(" in migration_script
    assert "_ROUTE_ATTEMPT_READ_FUNCTION_SIGNATURE" in migration_script
    assert "platform.read_generation_route_attempts(" in migration_script
    assert "REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE" in migration_script
    signature_loop = (
        "for signature in (\n"
        "        _ROUTE_ATTEMPT_FUNCTION_SIGNATURE,\n"
        "        _ROUTE_ATTEMPT_READ_FUNCTION_SIGNATURE,\n"
        "    ):"
    )
    assert migration_script.count(signature_loop) == 2
    owner = 'text(f"ALTER FUNCTION {signature} OWNER TO yfeistai_migrator")'
    revoke = 'text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")'
    grant = 'text(f"GRANT EXECUTE ON FUNCTION {signature} TO yfeistai_app")'
    assert owner in migration_script
    assert migration_script.index(owner) < migration_script.index(revoke)
    assert migration_script.index(owner) < migration_script.index(grant)


def test_direct_alembic_refuses_app_execution_without_migrator_owned_definers() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260830_0023_generation_job_route_evidence.py"
    ).read_text(encoding="utf-8")

    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yfeistai_app')" in migration
    assert re.search(
        r"AND NOT EXISTS\s*\(\s*SELECT 1 FROM pg_roles "
        r"WHERE rolname = 'yfeistai_migrator'\s*\)",
        migration,
    )
    assert "generation route evidence requires yfeistai_migrator ownership" in migration
    assert "pg_catalog.pg_get_userbyid(procedure.proowner)" in migration
    assert "procedure.oid IN (" in migration
    assert "pg_catalog.to_regprocedure(" in migration
    assert "'platform.record_generation_route_attempt('" in migration
    assert "'platform.read_generation_route_attempts('" in migration
    assert "SELECT COUNT(*)" in migration
    assert ") <> 2 THEN" in migration
    assert "route evidence function ownership is invalid" in migration
    assert migration.index(") OWNER TO yfeistai_migrator;") < migration.index(
        "GRANT EXECUTE ON FUNCTION"
    )
