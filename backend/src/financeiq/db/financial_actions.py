"""Shared audit persistence; financial meaning belongs to the scenario's service.

Accounts and Imports actions are supported. Transaction elements and the
remaining action types arrive with Transactions in task 2.5.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financeiq.db.base import Base


class FinancialAction(Base):
    __tablename__ = "financial_actions"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "action_type IN ('confirm_balance', 'revoke_balance', 'confirm_history', "
            "'revoke_history', 'archive_account', 'restore_account', "
            "'import_confirm', 'import_cancel')",
            name="action_type_valid",
        ),
        CheckConstraint("plan_version > 0", name="plan_version_positive"),
        CheckConstraint("schema_version > 0", name="schema_version_positive"),
        CheckConstraint("octet_length(plan_digest) = 32", name="plan_digest_length"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        ForeignKeyConstraint(
            ["user_id", "source_attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "request_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    request_id: Mapped[UUID] = mapped_column()
    action_type: Mapped[str] = mapped_column(Text)
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_digest: Mapped[bytes] = mapped_column(LargeBinary)
    schema_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    account_id: Mapped[UUID | None] = mapped_column()
    source_attempt_id: Mapped[UUID | None] = mapped_column()
    reason_code: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, object]] = mapped_column(JSONB)
