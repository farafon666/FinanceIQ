"""Personal categories and rules; matching and lifecycle services come later."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from financeiq.db.base import Base


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("kind IN ('income', 'expense')", name="kind_valid"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint(
            "name_key <> '' AND name_key <> 'без категории'", name="name_key_valid"
        ),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "archived_at IS NULL OR (isfinite(archived_at) AND archived_at >= created_at)",
            name="archived_at_valid",
        ),
        UniqueConstraint("user_id", "id"),
        Index(
            "uq_categories_active_name",
            "user_id",
            "kind",
            "name_key",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text(collation="C"))
    kind: Mapped[str] = mapped_column(Text(collation="C"))
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("NULL")
    )


class CategorizationRule(Base):
    __tablename__ = "categorization_rules"
    __table_args__ = (
        CheckConstraint("priority > 0", name="priority_positive"),
        CheckConstraint(
            "char_length(pattern) BETWEEN 1 AND 200", name="pattern_length"
        ),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("user_id", "id"),
        UniqueConstraint(
            "user_id", "active_priority", deferrable=True, initially="DEFERRED"
        ),
        Index("ix_categorization_rules_user_id_category_id", "user_id", "category_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    category_id: Mapped[UUID] = mapped_column()
    pattern: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    active_priority: Mapped[int | None] = mapped_column(
        Integer,
        Computed("CASE WHEN is_active THEN priority ELSE NULL END", persisted=True),
    )
