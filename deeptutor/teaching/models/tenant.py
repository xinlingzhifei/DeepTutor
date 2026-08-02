"""ORM models stored in a runtime-translated tenant schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, MetaData, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TenantBase(DeclarativeBase):
    """Declarative base using the logical schema translated per connection."""

    metadata = MetaData(schema="tenant", naming_convention=_NAMING_CONVENTION)


class Course(TenantBase):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_courses_status", "status"),)


class TeachingClass(TenantBase):
    __tablename__ = "classes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant.courses.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("id", "course_id", name="uq_classes_id_course"),
        Index("ix_classes_course_id", "course_id"),
    )


class Enrollment(TenantBase):
    __tablename__ = "enrollments"

    class_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant.classes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    learner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_enrollments_learner_id", "learner_id"),)
