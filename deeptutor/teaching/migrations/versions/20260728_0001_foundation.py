"""Create the platform and tenant teaching foundations."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260728_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def _upgrade_platform() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'provisioning'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        schema="platform",
    )
    op.create_index(
        "ix_tenants_status",
        "tenants",
        ["status"],
        schema="platform",
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_memberships_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "user_id",
            name="pk_tenant_memberships",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_memberships_user_id",
        "tenant_memberships",
        ["user_id"],
        schema="platform",
    )

    op.create_table(
        "role_grants",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            [
                "platform.tenant_memberships.tenant_id",
                "platform.tenant_memberships.user_id",
            ],
            name="fk_role_grants_tenant_id_tenant_memberships",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "user_id",
            "role",
            name="pk_role_grants",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_role_grants_user_id",
        "role_grants",
        ["user_id"],
        schema="platform",
    )

    op.create_table(
        "data_plane_routes",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("schema_name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_data_plane_routes_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_data_plane_routes"),
        sa.UniqueConstraint(
            "schema_name",
            name="uq_data_plane_routes_schema_name",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_data_plane_routes_status",
        "data_plane_routes",
        ["status"],
        schema="platform",
    )

    op.create_table(
        "tenant_storage_credentials",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "access_key_fingerprint",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_storage_credentials_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            name="pk_tenant_storage_credentials",
        ),
        sa.UniqueConstraint(
            "access_key_fingerprint",
            name="uq_tenant_storage_credentials_access_key_fingerprint",
        ),
        sa.UniqueConstraint(
            "secret_ref",
            name="uq_tenant_storage_credentials_secret_ref",
        ),
        schema="platform",
    )

    op.create_table(
        "tenant_provisioning_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "operation",
            sa.String(length=32),
            server_default=sa.text("'provision'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_provisioning_jobs_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_provisioning_jobs"),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_provisioning_jobs_tenant_status",
        "tenant_provisioning_jobs",
        ["tenant_id", "status"],
        schema="platform",
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_audit_log_tenant_id_tenants",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        schema="platform",
    )
    op.create_index(
        "ix_audit_log_tenant_created",
        "audit_log",
        ["tenant_id", "created_at"],
        schema="platform",
    )


def _upgrade_tenant() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_courses"),
        schema="tenant",
    )
    op.create_index(
        "ix_courses_status",
        "courses",
        ["status"],
        schema="tenant",
    )

    op.create_table(
        "classes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["tenant.courses.id"],
            name="fk_classes_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classes"),
        schema="tenant",
    )
    op.create_index(
        "ix_classes_course_id",
        "classes",
        ["course_id"],
        schema="tenant",
    )

    op.create_table(
        "enrollments",
        sa.Column("class_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_enrollments_class_id_classes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "class_id",
            "learner_id",
            name="pk_enrollments",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_enrollments_learner_id",
        "enrollments",
        ["learner_id"],
        schema="tenant",
    )


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        _upgrade_tenant()


def _downgrade_platform() -> None:
    op.drop_index(
        "ix_audit_log_tenant_created",
        table_name="audit_log",
        schema="platform",
    )
    op.drop_table("audit_log", schema="platform")
    op.drop_index(
        "ix_tenant_provisioning_jobs_tenant_status",
        table_name="tenant_provisioning_jobs",
        schema="platform",
    )
    op.drop_table("tenant_provisioning_jobs", schema="platform")
    op.drop_table("tenant_storage_credentials", schema="platform")
    op.drop_index(
        "ix_data_plane_routes_status",
        table_name="data_plane_routes",
        schema="platform",
    )
    op.drop_table("data_plane_routes", schema="platform")
    op.drop_index(
        "ix_role_grants_user_id",
        table_name="role_grants",
        schema="platform",
    )
    op.drop_table("role_grants", schema="platform")
    op.drop_index(
        "ix_tenant_memberships_user_id",
        table_name="tenant_memberships",
        schema="platform",
    )
    op.drop_table("tenant_memberships", schema="platform")
    op.drop_index(
        "ix_tenants_status",
        table_name="tenants",
        schema="platform",
    )
    op.drop_table("tenants", schema="platform")


def _downgrade_tenant() -> None:
    op.drop_index(
        "ix_enrollments_learner_id",
        table_name="enrollments",
        schema="tenant",
    )
    op.drop_table("enrollments", schema="tenant")
    op.drop_index(
        "ix_classes_course_id",
        table_name="classes",
        schema="tenant",
    )
    op.drop_table("classes", schema="tenant")
    op.drop_index(
        "ix_courses_status",
        table_name="courses",
        schema="tenant",
    )
    op.drop_table("courses", schema="tenant")


def downgrade() -> None:
    if _migration_scope() == "platform":
        _downgrade_platform()
    else:
        _downgrade_tenant()
