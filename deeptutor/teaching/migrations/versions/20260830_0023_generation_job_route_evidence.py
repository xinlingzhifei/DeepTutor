"""Persist immutable generation-job routing evidence."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260830_0023"
down_revision: str | None = "20260828_0022"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def _tenant_schema() -> str:
    return context.get_x_argument(as_dictionary=True)["tenant_schema"]


def _quoted_tenant_schema() -> str:
    connection = op.get_bind()
    return connection.dialect.identifier_preparer.quote_schema(_tenant_schema())


def _sync_tenant_schema_revision(source_revision: str, target_revision: str) -> None:
    tenant_schema = _tenant_schema()
    connection = op.get_bind()
    state = (
        connection.execute(
            sa.text(
                """
                SELECT revision, status
                FROM platform.tenant_schema_states
                WHERE schema_name = :tenant_schema
                FOR UPDATE
                """
            ),
            {"tenant_schema": tenant_schema},
        )
        .mappings()
        .one_or_none()
    )
    if state is not None and (state["status"] != "active" or state["revision"] != source_revision):
        raise RuntimeError("tenant schema state revision does not match migration source")
    result = connection.execute(
        sa.text(
            """
            UPDATE platform.tenant_schema_states
            SET revision = :target_revision,
                verified_at = now(),
                updated_at = now()
            WHERE schema_name = :tenant_schema
              AND status = 'active'
              AND revision = :source_revision
            """
        ),
        {
            "source_revision": source_revision,
            "target_revision": target_revision,
            "tenant_schema": tenant_schema,
        },
    )
    if state is not None and result.rowcount != 1:
        raise RuntimeError("tenant schema revision update was lost")


def _create_platform_append_only_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION platform.reject_generation_route_attempt_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'append-only generation route attempt';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER generation_route_attempts_append_only_truncate
            BEFORE TRUNCATE ON platform.generation_route_attempts
            FOR EACH STATEMENT
            EXECUTE FUNCTION platform.reject_generation_route_attempt_mutation()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER generation_route_attempts_append_only
            BEFORE UPDATE OR DELETE ON platform.generation_route_attempts
            FOR EACH ROW
            EXECUTE FUNCTION platform.reject_generation_route_attempt_mutation()
            """
        )
    )


def _create_platform_route_attempt_entrypoint() -> None:
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION platform.record_generation_route_attempt(
                p_tenant_id text,
                p_job_id text,
                p_phase text,
                p_attempt_count integer,
                p_data_plane_mode text,
                p_data_plane_route_id text,
                p_provider_profile_id text,
                p_worker_pool_ref text,
                p_queue_ref text,
                p_worker_id text,
                p_lease_token text,
                p_decision text,
                p_config_revision text,
                p_route_config_digest text,
                p_provider_config_digest text
            )
            RETURNS boolean
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, platform
            AS $route_attempt$
            DECLARE
                v_tenant_schema text;
                v_phase text;
                v_status text;
                v_attempt_count integer;
                v_data_plane_mode text;
                v_data_plane_route_id text;
                v_provider_profile_id text;
                v_worker_pool_ref text;
                v_queue_ref text;
                v_lease_owner text;
                v_lease_token text;
                v_lease_expires_at timestamptz;
                v_expected_status text;
                v_inserted_count integer;
            BEGIN
                IF p_tenant_id IS NULL OR pg_catalog.length(pg_catalog.btrim(p_tenant_id)) = 0
                   OR pg_catalog.length(p_tenant_id) > 64 OR p_tenant_id ~ E'[\r\n]'
                   OR p_job_id IS NULL OR pg_catalog.length(pg_catalog.btrim(p_job_id)) = 0
                   OR pg_catalog.length(p_job_id) > 64 OR p_job_id ~ E'[\r\n]'
                   OR p_attempt_count IS NULL OR p_attempt_count <= 0
                   OR p_phase IS NULL OR p_phase NOT IN ('outline', 'content', 'export')
                   OR p_data_plane_mode IS NULL
                   OR p_data_plane_mode NOT IN ('shared', 'dedicated')
                   OR p_decision IS NULL OR p_decision NOT IN ('selected', 'unavailable')
                   OR p_data_plane_route_id IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_data_plane_route_id)) = 0
                   OR pg_catalog.length(p_data_plane_route_id) > 63
                   OR p_data_plane_route_id ~ E'[\r\n]'
                   OR p_provider_profile_id IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_provider_profile_id)) = 0
                   OR pg_catalog.length(p_provider_profile_id) > 63
                   OR p_provider_profile_id ~ E'[\r\n]'
                   OR p_worker_pool_ref IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_worker_pool_ref)) = 0
                   OR pg_catalog.length(p_worker_pool_ref) > 128
                   OR p_worker_pool_ref ~ E'[\r\n]'
                   OR p_queue_ref IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_queue_ref)) = 0
                   OR pg_catalog.length(p_queue_ref) > 128 OR p_queue_ref ~ E'[\r\n]'
                   OR p_worker_id IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_worker_id)) = 0
                   OR pg_catalog.length(p_worker_id) > 128 OR p_worker_id ~ E'[\r\n]'
                   OR p_lease_token IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_lease_token)) = 0
                   OR pg_catalog.length(p_lease_token) > 64 OR p_lease_token ~ E'[\r\n]'
                   OR (
                       p_decision = 'selected'
                       AND (
                           p_config_revision IS DISTINCT FROM 'route-binding-v1'
                           OR p_route_config_digest IS NULL
                           OR p_route_config_digest !~ '^[0-9a-f]{64}$'
                           OR p_route_config_digest = pg_catalog.repeat('0', 64)
                           OR p_provider_config_digest IS NULL
                           OR p_provider_config_digest !~ '^[0-9a-f]{64}$'
                           OR p_provider_config_digest = pg_catalog.repeat('0', 64)
                       )
                   )
                   OR (
                       p_decision = 'unavailable'
                       AND (
                           p_config_revision IS NOT NULL
                           OR p_route_config_digest IS NOT NULL
                           OR p_provider_config_digest IS NOT NULL
                       )
                   )
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'PGR01',
                        MESSAGE = 'generation route attempt input is invalid';
                END IF;

                SELECT schema_state.schema_name
                INTO v_tenant_schema
                FROM platform.tenants AS tenant
                JOIN platform.tenant_schema_states AS schema_state
                  ON schema_state.tenant_id = tenant.id
                WHERE tenant.id = p_tenant_id
                  AND tenant.status = 'active'
                  AND schema_state.status = 'active'
                  AND schema_state.revision = '20260830_0023'
                FOR KEY SHARE OF tenant, schema_state;

                IF NOT FOUND OR v_tenant_schema !~ '^tenant_[0-9a-f]{16}$' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'PGR02',
                        MESSAGE = 'generation route attempt authority is unavailable';
                END IF;

                v_expected_status := CASE p_phase
                    WHEN 'outline' THEN 'generating_outline'
                    WHEN 'content' THEN 'generating_content'
                    WHEN 'export' THEN 'exporting'
                END;
                EXECUTE pg_catalog.format(
                    'SELECT phase, status, attempt_count, data_plane_mode, '
                    'data_plane_route_id, provider_profile_id, worker_pool_ref, '
                    'queue_ref, lease_owner, lease_token, lease_expires_at '
                    'FROM %I.generation_jobs '
                    'WHERE tenant_id = $1 AND id = $2 FOR UPDATE',
                    v_tenant_schema
                )
                INTO
                    v_phase,
                    v_status,
                    v_attempt_count,
                    v_data_plane_mode,
                    v_data_plane_route_id,
                    v_provider_profile_id,
                    v_worker_pool_ref,
                    v_queue_ref,
                    v_lease_owner,
                    v_lease_token,
                    v_lease_expires_at
                USING p_tenant_id, p_job_id;

                IF NOT FOUND
                   OR v_phase IS DISTINCT FROM p_phase
                   OR v_status IS DISTINCT FROM v_expected_status
                   OR v_attempt_count IS DISTINCT FROM p_attempt_count
                   OR v_data_plane_mode IS DISTINCT FROM p_data_plane_mode
                   OR v_data_plane_route_id IS DISTINCT FROM p_data_plane_route_id
                   OR v_provider_profile_id IS DISTINCT FROM p_provider_profile_id
                   OR v_worker_pool_ref IS DISTINCT FROM p_worker_pool_ref
                   OR v_queue_ref IS DISTINCT FROM p_queue_ref
                   OR v_lease_owner IS DISTINCT FROM p_worker_id
                   OR v_lease_token IS DISTINCT FROM p_lease_token
                   OR v_lease_expires_at IS NULL
                   OR NOT (v_lease_expires_at > pg_catalog.clock_timestamp())
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'PGR02',
                        MESSAGE = 'generation route attempt lease fence no longer matches';
                END IF;

                PERFORM 1
                FROM platform.data_plane_routes AS route
                JOIN platform.provider_profiles AS profile
                  ON profile.id = route.provider_profile_id
                 AND profile.scope = route.mode
                 AND profile.owner_key = route.owner_key
                WHERE route.id = p_data_plane_route_id
                  AND route.mode = p_data_plane_mode
                  AND route.provider_profile_id = p_provider_profile_id
                  AND route.worker_pool = p_worker_pool_ref
                  AND route.queue_name = p_queue_ref
                  AND profile.id = p_provider_profile_id
                  AND profile.scope = p_data_plane_mode
                  AND (
                      p_decision = 'unavailable'
                      OR (
                          p_decision = 'selected'
                          AND p_config_revision = 'route-binding-v1'
                          AND route.status = 'active'
                          AND route.health_status = 'healthy'
                          AND profile.status = 'active'
                          AND p_route_config_digest = pg_catalog.encode(
                              pg_catalog.sha256(
                                  pg_catalog.convert_to(
                                      'route-binding-v1' || E'\nroute\n'
                                      || 'S' || pg_catalog.octet_length(route.base_url)::text
                                      || ':' || route.base_url,
                                      'UTF8'
                                  )
                              ),
                              'hex'
                          )
                          AND p_provider_config_digest = pg_catalog.encode(
                              pg_catalog.sha256(
                                  pg_catalog.convert_to(
                                      'route-binding-v1' || E'\nprovider\n'
                                      || 'S'
                                      || pg_catalog.octet_length(profile.provider_type)::text
                                      || ':' || profile.provider_type
                                      || 'S'
                                      || pg_catalog.octet_length(profile.model_name)::text
                                      || ':' || profile.model_name
                                      || CASE
                                          WHEN profile.api_base_url IS NULL THEN 'N'
                                          ELSE 'S'
                                              || pg_catalog.octet_length(
                                                  profile.api_base_url
                                              )::text
                                              || ':' || profile.api_base_url
                                      END
                                      || 'S'
                                      || pg_catalog.octet_length(profile.secret_ref)::text
                                      || ':' || profile.secret_ref,
                                      'UTF8'
                                  )
                              ),
                              'hex'
                          )
                      )
                  )
                  AND (
                      (
                          p_data_plane_mode = 'shared'
                          AND route.tenant_id IS NULL
                          AND route.owner_key = 'shared'
                          AND profile.tenant_id IS NULL
                          AND profile.owner_key = 'shared'
                      )
                      OR (
                          p_data_plane_mode = 'dedicated'
                          AND route.tenant_id = p_tenant_id
                          AND route.owner_key = p_tenant_id
                          AND profile.tenant_id = p_tenant_id
                          AND profile.owner_key = p_tenant_id
                      )
                  )
                FOR SHARE OF route, profile;

                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'PGR02',
                        MESSAGE = 'generation route attempt binding no longer matches';
                END IF;

                INSERT INTO platform.generation_route_attempts (
                    tenant_id,
                    job_id,
                    attempt_count,
                    phase,
                    decision,
                    data_plane_mode,
                    data_plane_route_id,
                    provider_profile_id,
                    worker_pool_ref,
                    queue_ref,
                    worker_id,
                    config_revision,
                    route_config_digest,
                    provider_config_digest
                ) VALUES (
                    p_tenant_id,
                    p_job_id,
                    p_attempt_count,
                    p_phase,
                    p_decision,
                    p_data_plane_mode,
                    p_data_plane_route_id,
                    p_provider_profile_id,
                    p_worker_pool_ref,
                    p_queue_ref,
                    p_worker_id,
                    p_config_revision,
                    p_route_config_digest,
                    p_provider_config_digest
                )
                ON CONFLICT (tenant_id, job_id, attempt_count) DO NOTHING;
                GET DIAGNOSTICS v_inserted_count = ROW_COUNT;

                IF v_inserted_count = 0 THEN
                    PERFORM 1
                    FROM platform.generation_route_attempts AS existing
                    WHERE existing.tenant_id = p_tenant_id
                      AND existing.job_id = p_job_id
                      AND existing.attempt_count = p_attempt_count
                      AND existing.phase = p_phase
                      AND existing.decision = p_decision
                      AND existing.data_plane_mode = p_data_plane_mode
                      AND existing.data_plane_route_id = p_data_plane_route_id
                      AND existing.provider_profile_id = p_provider_profile_id
                      AND existing.worker_pool_ref = p_worker_pool_ref
                      AND existing.queue_ref = p_queue_ref
                      AND existing.worker_id = p_worker_id
                      AND existing.config_revision IS NOT DISTINCT FROM p_config_revision
                      AND existing.route_config_digest IS NOT DISTINCT FROM p_route_config_digest
                      AND existing.provider_config_digest IS NOT DISTINCT FROM
                          p_provider_config_digest;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION USING
                            ERRCODE = 'PGR03',
                            MESSAGE = 'generation route attempt conflicts with durable evidence';
                    END IF;
                END IF;
                RETURN true;
            END;
            $route_attempt$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION platform.read_generation_route_attempts(
                p_tenant_id text,
                p_job_id text,
                p_data_plane_mode text,
                p_data_plane_route_id text,
                p_provider_profile_id text,
                p_worker_pool_ref text,
                p_queue_ref text
            )
            RETURNS TABLE (
                attempt_count integer,
                phase text,
                decision text,
                data_plane_mode text,
                data_plane_route_id text,
                provider_profile_id text,
                worker_pool_ref text,
                queue_ref text,
                worker_id text,
                config_revision text,
                route_config_digest text,
                provider_config_digest text,
                created_at timestamptz
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, platform
            AS $route_attempt_read$
            DECLARE
                v_tenant_schema text;
                v_job_matches boolean;
            BEGIN
                IF p_tenant_id IS NULL OR pg_catalog.length(pg_catalog.btrim(p_tenant_id)) = 0
                   OR pg_catalog.length(p_tenant_id) > 64 OR p_tenant_id ~ E'[\r\n]'
                   OR p_job_id IS NULL OR pg_catalog.length(pg_catalog.btrim(p_job_id)) = 0
                   OR pg_catalog.length(p_job_id) > 64 OR p_job_id ~ E'[\r\n]'
                   OR p_data_plane_mode IS NULL
                   OR p_data_plane_mode NOT IN ('shared', 'dedicated')
                   OR p_data_plane_route_id IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_data_plane_route_id)) = 0
                   OR pg_catalog.length(p_data_plane_route_id) > 63
                   OR p_data_plane_route_id ~ E'[\r\n]'
                   OR p_provider_profile_id IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_provider_profile_id)) = 0
                   OR pg_catalog.length(p_provider_profile_id) > 63
                   OR p_provider_profile_id ~ E'[\r\n]'
                   OR p_worker_pool_ref IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_worker_pool_ref)) = 0
                   OR pg_catalog.length(p_worker_pool_ref) > 128
                   OR p_worker_pool_ref ~ E'[\r\n]'
                   OR p_queue_ref IS NULL
                   OR pg_catalog.length(pg_catalog.btrim(p_queue_ref)) = 0
                   OR pg_catalog.length(p_queue_ref) > 128 OR p_queue_ref ~ E'[\r\n]'
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'PGR01',
                        MESSAGE = 'generation route attempt read input is invalid';
                END IF;

                SELECT schema_state.schema_name
                INTO v_tenant_schema
                FROM platform.tenants AS tenant
                JOIN platform.tenant_schema_states AS schema_state
                  ON schema_state.tenant_id = tenant.id
                WHERE tenant.id = p_tenant_id
                  AND tenant.status = 'active'
                  AND schema_state.status = 'active'
                  AND schema_state.revision = '20260830_0023'
                FOR KEY SHARE OF tenant, schema_state;

                IF NOT FOUND OR v_tenant_schema !~ '^tenant_[0-9a-f]{16}$' THEN
                    RETURN;
                END IF;

                EXECUTE pg_catalog.format(
                    'SELECT true FROM %I.generation_jobs '
                    'WHERE tenant_id = $1 AND id = $2 '
                    'AND data_plane_mode = $3 AND data_plane_route_id = $4 '
                    'AND provider_profile_id = $5 AND worker_pool_ref = $6 '
                    'AND queue_ref = $7 FOR KEY SHARE',
                    v_tenant_schema
                )
                INTO v_job_matches
                USING
                    p_tenant_id,
                    p_job_id,
                    p_data_plane_mode,
                    p_data_plane_route_id,
                    p_provider_profile_id,
                    p_worker_pool_ref,
                    p_queue_ref;

                IF v_job_matches IS DISTINCT FROM true THEN
                    RETURN;
                END IF;

                RETURN QUERY
                SELECT
                    attempt.attempt_count,
                    attempt.phase::text,
                    attempt.decision::text,
                    attempt.data_plane_mode::text,
                    attempt.data_plane_route_id::text,
                    attempt.provider_profile_id::text,
                    attempt.worker_pool_ref::text,
                    attempt.queue_ref::text,
                    attempt.worker_id::text,
                    attempt.config_revision::text,
                    attempt.route_config_digest::text,
                    attempt.provider_config_digest::text,
                    attempt.created_at
                FROM platform.generation_route_attempts AS attempt
                WHERE attempt.tenant_id = p_tenant_id
                  AND attempt.job_id = p_job_id
                ORDER BY attempt.attempt_count;
            END;
            $route_attempt_read$
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yfeistai_app')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_roles WHERE rolname = 'yfeistai_migrator'
                   ) THEN
                    RAISE EXCEPTION
                        'generation route evidence requires yfeistai_migrator ownership';
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yfeistai_migrator') THEN
                    ALTER FUNCTION platform.record_generation_route_attempt(
                        text, text, text, integer, text, text, text, text,
                        text, text, text, text, text, text, text
                    ) OWNER TO yfeistai_migrator;
                    ALTER FUNCTION platform.read_generation_route_attempts(
                        text, text, text, text, text, text, text
                    ) OWNER TO yfeistai_migrator;
                END IF;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "platform.record_generation_route_attempt(text, text, text, integer, text, "
            "text, text, text, text, text, text, text, text, text, text) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "platform.read_generation_route_attempts(text, text, text, text, text, text, text) "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yfeistai_app') THEN
                    IF (
                        SELECT COUNT(*)
                        FROM pg_catalog.pg_proc AS procedure
                        WHERE procedure.oid IN (
                            pg_catalog.to_regprocedure(
                                'platform.record_generation_route_attempt('
                                'text, text, text, integer, text, text, text, text, '
                                'text, text, text, text, text, text, text)'
                            ),
                            pg_catalog.to_regprocedure(
                                'platform.read_generation_route_attempts('
                                'text, text, text, text, text, text, text)'
                            )
                        )
                          AND pg_catalog.pg_get_userbyid(procedure.proowner)
                              = 'yfeistai_migrator'
                    ) <> 2 THEN
                        RAISE EXCEPTION 'route evidence function ownership is invalid';
                    END IF;
                    REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
                        platform.generation_route_attempts FROM yfeistai_app;
                    GRANT EXECUTE ON FUNCTION
                        platform.record_generation_route_attempt(
                            text, text, text, integer, text, text, text, text,
                            text, text, text, text, text, text, text
                        ) TO yfeistai_app;
                    GRANT EXECUTE ON FUNCTION
                        platform.read_generation_route_attempts(
                            text, text, text, text, text, text, text
                        ) TO yfeistai_app;
                END IF;
            END;
            $$
            """
        )
    )


def _upgrade_platform() -> None:
    op.create_table(
        "generation_route_attempts",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("data_plane_mode", sa.String(length=16), nullable=False),
        sa.Column("data_plane_route_id", sa.String(length=63), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=63), nullable=False),
        sa.Column("worker_pool_ref", sa.String(length=128), nullable=False),
        sa.Column("queue_ref", sa.String(length=128), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("config_revision", sa.String(length=32), nullable=True),
        sa.Column("route_config_digest", sa.String(length=64), nullable=True),
        sa.Column("provider_config_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "job_id",
            "attempt_count",
            name="pk_generation_route_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_generation_route_attempts_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "attempt_count > 0",
            name="ck_generation_route_attempts_attempt_count",
        ),
        sa.CheckConstraint(
            "phase IN ('outline', 'content', 'export')",
            name="ck_generation_route_attempts_phase",
        ),
        sa.CheckConstraint(
            "decision IN ('selected', 'unavailable')",
            name="ck_generation_route_attempts_decision",
        ),
        sa.CheckConstraint(
            "data_plane_mode IN ('shared', 'dedicated')",
            name="ck_generation_route_attempts_data_plane_mode",
        ),
        sa.CheckConstraint(
            "length(btrim(tenant_id)) > 0 "
            "AND length(btrim(job_id)) > 0 "
            "AND length(btrim(data_plane_route_id)) > 0 "
            "AND length(btrim(provider_profile_id)) > 0 "
            "AND length(btrim(worker_pool_ref)) > 0 "
            "AND length(btrim(queue_ref)) > 0 "
            "AND length(btrim(worker_id)) > 0",
            name="ck_generation_route_attempts_bindings_not_empty",
        ),
        sa.CheckConstraint(
            "(decision = 'selected' AND config_revision = 'route-binding-v1' "
            "AND route_config_digest ~ '^[0-9a-f]{64}$' "
            "AND route_config_digest <> "
            "'0000000000000000000000000000000000000000000000000000000000000000' "
            "AND provider_config_digest ~ '^[0-9a-f]{64}$' "
            "AND provider_config_digest <> "
            "'0000000000000000000000000000000000000000000000000000000000000000') OR "
            "(decision = 'unavailable' AND config_revision IS NULL "
            "AND route_config_digest IS NULL AND provider_config_digest IS NULL)",
            name="ck_generation_route_attempts_configuration_binding",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_generation_route_attempts_tenant_created",
        "generation_route_attempts",
        ["tenant_id", "created_at"],
        schema="platform",
    )
    _create_platform_append_only_guard()
    _create_platform_route_attempt_entrypoint()


def _create_tenant_binding_guard() -> None:
    quoted_schema = _quoted_tenant_schema()
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {quoted_schema}.reject_generation_job_route_binding_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.data_plane_mode IS DISTINCT FROM OLD.data_plane_mode
                   OR NEW.data_plane_route_id IS DISTINCT FROM OLD.data_plane_route_id
                   OR NEW.provider_profile_id IS DISTINCT FROM OLD.provider_profile_id
                   OR NEW.worker_pool_ref IS DISTINCT FROM OLD.worker_pool_ref
                   OR NEW.queue_ref IS DISTINCT FROM OLD.queue_ref THEN
                    RAISE EXCEPTION 'generation job route binding is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER generation_jobs_route_binding_immutable
            BEFORE UPDATE ON {quoted_schema}.generation_jobs
            FOR EACH ROW
            EXECUTE FUNCTION {quoted_schema}.reject_generation_job_route_binding_mutation()
            """
        )
    )


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    op.add_column(
        "generation_jobs",
        sa.Column("data_plane_mode", sa.String(length=16), nullable=True),
        schema=tenant_schema,
    )
    connection = op.get_bind()
    quoted_schema = _quoted_tenant_schema()
    legacy_nonterminal_jobs = connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.generation_jobs "
            "WHERE status NOT IN ('succeeded', 'failed', 'canceled'))"
        )
    ).scalar()
    if legacy_nonterminal_jobs:
        raise CommandError("cannot upgrade generation route evidence with legacy nonterminal jobs")
    op.create_check_constraint(
        "ck_generation_jobs_data_plane_mode",
        "generation_jobs",
        "data_plane_mode IS NULL OR data_plane_mode IN ('shared', 'dedicated')",
        schema=tenant_schema,
    )
    _create_tenant_binding_guard()
    _sync_tenant_schema_revision("20260828_0022", "20260830_0023")


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        _upgrade_tenant()


def _downgrade_platform() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE platform.generation_route_attempts IN ACCESS EXCLUSIVE MODE")
    )
    connection.execute(sa.text("LOCK TABLE platform.tenant_schema_states IN SHARE MODE"))
    durable_attempts = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM platform.generation_route_attempts)")
    ).scalar()
    if durable_attempts:
        raise CommandError("cannot downgrade generation route evidence: durable facts exist")
    current_tenants = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM platform.tenant_schema_states
                WHERE revision = '20260830_0023'
            )
            """
        )
    ).scalar()
    physically_upgraded_tenants = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema ~ '^tenant_[0-9a-f]{16}$'
                  AND table_name = 'generation_jobs'
                  AND column_name = 'data_plane_mode'
            )
            """
        )
    ).scalar()
    if current_tenants or physically_upgraded_tenants:
        raise CommandError("downgrade tenant schemas before generation route evidence")
    op.execute(
        sa.text(
            "DROP FUNCTION platform.read_generation_route_attempts("
            "text, text, text, text, text, text, text)"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION platform.record_generation_route_attempt("
            "text, text, text, integer, text, text, text, text, text, text, text, text, "
            "text, text, text)"
        )
    )
    op.drop_index(
        "ix_generation_route_attempts_tenant_created",
        table_name="generation_route_attempts",
        schema="platform",
    )
    op.execute(
        sa.text(
            "DROP TRIGGER generation_route_attempts_append_only_truncate "
            "ON platform.generation_route_attempts"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER generation_route_attempts_append_only "
            "ON platform.generation_route_attempts"
        )
    )
    op.execute(sa.text("DROP FUNCTION platform.reject_generation_route_attempt_mutation()"))
    op.drop_table("generation_route_attempts", schema="platform")


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = _quoted_tenant_schema()
    connection = op.get_bind()
    connection.execute(
        sa.text(f"LOCK TABLE {quoted_schema}.generation_jobs IN ACCESS EXCLUSIVE MODE")
    )
    durable_bindings = connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.generation_jobs "
            "WHERE data_plane_mode IS NOT NULL)"
        )
    ).scalar()
    if durable_bindings:
        raise CommandError("cannot downgrade generation route evidence: job bindings exist")
    op.execute(
        sa.text(
            f"DROP TRIGGER generation_jobs_route_binding_immutable "
            f"ON {quoted_schema}.generation_jobs"
        )
    )
    op.execute(
        sa.text(f"DROP FUNCTION {quoted_schema}.reject_generation_job_route_binding_mutation()")
    )
    op.drop_constraint(
        "ck_generation_jobs_data_plane_mode",
        "generation_jobs",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_column("generation_jobs", "data_plane_mode", schema=tenant_schema)
    _sync_tenant_schema_revision("20260830_0023", "20260828_0022")


def downgrade() -> None:
    if _migration_scope() == "platform":
        _downgrade_platform()
    else:
        _downgrade_tenant()
