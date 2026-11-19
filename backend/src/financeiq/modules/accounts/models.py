"""Accounts and immutable, explicitly revocable balance/history confirmations."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from financeiq.db.base import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("currency_code = 'RUB'", name="currency_rub"),
        CheckConstraint("facts_version >= 0", name="facts_version_nonnegative"),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "archived_at IS NULL OR (isfinite(archived_at) AND archived_at >= created_at)",
            name="archived_at_valid",
        ),
        UniqueConstraint("user_id", "id"),
        Index("ix_accounts_user_id_archived_at_id", "user_id", "archived_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text)
    currency_code: Mapped[str] = mapped_column(Text(collation="C"))
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("NULL")
    )
    facts_version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class BalanceConfirmation(Base):
    __tablename__ = "balance_confirmations"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint("isfinite(balance_date)", name="balance_date_finite"),
        CheckConstraint(
            "balance BETWEEN -9999999999999999.99 AND 9999999999999999.99",
            name="balance_range",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "confirmation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "revocation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index(
            "uq_balance_confirmations_active",
            "user_id",
            "account_id",
            "balance_date",
            unique=True,
            postgresql_where=text("revocation_action_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    account_id: Mapped[UUID] = mapped_column()
    balance_date: Mapped[date] = mapped_column(Date)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    confirmation_action_id: Mapped[UUID] = mapped_column()
    revocation_action_id: Mapped[UUID | None] = mapped_column(
        server_default=text("NULL")
    )


Index(
    "ix_balance_confirmations_history",
    BalanceConfirmation.user_id,
    BalanceConfirmation.account_id,
    BalanceConfirmation.balance_date.desc(),
    BalanceConfirmation.id,
)


class HistoryConfirmation(Base):
    __tablename__ = "history_confirmations"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "isfinite(start_date) AND isfinite(end_date) AND start_date <= end_date",
            name="date_range_valid",
        ),
        CheckConstraint(
            "observed_facts_version >= 0", name="observed_facts_version_nonnegative"
        ),
        CheckConstraint(
            "octet_length(observed_facts_digest) = 32",
            name="observed_facts_digest_length",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "confirmation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "revocation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index(
            "uq_history_confirmations_active",
            "user_id",
            "account_id",
            "start_date",
            "end_date",
            unique=True,
            postgresql_where=text("revocation_action_id IS NULL"),
        ),
        Index(
            "ix_history_confirmations_active_intervals",
            "user_id",
            "account_id",
            "end_date",
            "start_date",
            postgresql_where=text("revocation_action_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    account_id: Mapped[UUID] = mapped_column()
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    observed_facts_version: Mapped[int] = mapped_column(BigInteger)
    observed_facts_digest: Mapped[bytes] = mapped_column(LargeBinary)
    confirmation_action_id: Mapped[UUID] = mapped_column()
    revocation_action_id: Mapped[UUID | None] = mapped_column(
        server_default=text("NULL")
    )
