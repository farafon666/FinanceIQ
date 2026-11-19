"""Financial facts and transfer history, protected by migration-owned guards."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from financeiq.db.base import Base


class _TransactionRecord:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )


class Transaction(_TransactionRecord, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint("isfinite(occurred_on)", name="occurred_on_finite"),
        CheckConstraint(
            "amount BETWEEN -9999999999999999.99 AND 9999999999999999.99 "
            "AND amount <> 0",
            name="amount_valid",
        ),
        CheckConstraint(
            "kind IN ('income','expense','transfer','refund','unclassified')",
            name="kind_valid",
        ),
        CheckConstraint(
            "(kind <> 'income' OR amount > 0) AND (kind <> 'expense' OR amount < 0)",
            name="kind_sign_valid",
        ),
        CheckConstraint("char_length(description) <= 2000", name="description_length"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "category_origin IN ('none','rule','manual')",
            name="category_origin_valid",
        ),
        CheckConstraint(
            "(category_origin <> 'none' OR (category_id IS NULL AND categorization_rule_id IS NULL)) "
            "AND (category_origin <> 'rule' OR (category_id IS NOT NULL AND categorization_rule_id IS NOT NULL)) "
            "AND (category_origin <> 'manual' OR categorization_rule_id IS NULL) "
            "AND (kind NOT IN ('transfer','unclassified') OR category_origin = 'none') "
            "AND (refund_of_transaction_id IS NULL OR category_origin = 'none') "
            "AND (kind <> 'refund' OR category_origin <> 'rule') "
            "AND (kind <> 'refund' OR refund_of_transaction_id IS NOT NULL OR category_origin = 'manual')",
            name="category_state_valid",
        ),
        CheckConstraint(
            "refund_of_transaction_id IS NULL OR (kind = 'refund' AND refund_of_transaction_id <> id)",
            name="refund_reference_valid",
        ),
        CheckConstraint(
            "(kind = 'refund') = (refund_action_id IS NOT NULL)",
            name="refund_action_valid",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "source_movement_id"],
            [
                "import_movements.user_id",
                "import_movements.account_id",
                "import_movements.id",
            ],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        *(
            ForeignKeyConstraint(
                ["user_id", column],
                [f"{target}.user_id", f"{target}.id"],
                ondelete="RESTRICT",
                onupdate="RESTRICT",
            )
            for column, target in (
                ("category_id", "categories"),
                ("categorization_rule_id", "categorization_rules"),
                ("refund_of_transaction_id", "transactions"),
                ("refund_action_id", "financial_actions"),
                ("last_action_id", "financial_actions"),
                ("cancelled_by_id", "import_cancellations"),
            )
        ),
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "account_id", "id"),
        UniqueConstraint("user_id", "source_movement_id"),
    )

    account_id: Mapped[UUID] = mapped_column()
    source_movement_id: Mapped[UUID] = mapped_column()
    occurred_on: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    description: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(
        Text(collation="C"), server_default=text("'unclassified'")
    )
    category_id: Mapped[UUID | None] = mapped_column()
    category_origin: Mapped[str] = mapped_column(
        Text(collation="C"), server_default=text("'none'")
    )
    categorization_rule_id: Mapped[UUID | None] = mapped_column()
    refund_of_transaction_id: Mapped[UUID | None] = mapped_column()
    refund_action_id: Mapped[UUID | None] = mapped_column()
    last_action_id: Mapped[UUID] = mapped_column()
    revision: Mapped[int] = mapped_column(
        BigInteger, server_default=text("1"), server_onupdate=FetchedValue()
    )
    cancelled_by_id: Mapped[UUID | None] = mapped_column(server_default=text("NULL"))


class TransferLink(_TransactionRecord, Base):
    __tablename__ = "transfer_links"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "outgoing_transaction_id IS NOT NULL OR incoming_transaction_id IS NOT NULL",
            name="side_required",
        ),
        CheckConstraint(
            "outgoing_transaction_id IS NULL OR incoming_transaction_id IS NULL "
            "OR outgoing_transaction_id <> incoming_transaction_id",
            name="distinct_sides",
        ),
        CheckConstraint(
            "ended_by_action_id IS NULL OR cancelled_by_id IS NULL",
            name="ending_valid",
        ),
        *(
            ForeignKeyConstraint(
                ["user_id", column],
                [f"{target}.user_id", f"{target}.id"],
                ondelete="RESTRICT",
                onupdate="RESTRICT",
            )
            for column, target in (
                ("outgoing_transaction_id", "transactions"),
                ("incoming_transaction_id", "transactions"),
                ("confirmation_action_id", "financial_actions"),
                ("ended_by_action_id", "financial_actions"),
                ("cancelled_by_id", "import_cancellations"),
            )
        ),
        *(
            Index(
                f"uq_transfer_links_active_{role}",
                "user_id",
                f"{role}_transaction_id",
                unique=True,
                postgresql_where=text(
                    "ended_by_action_id IS NULL AND cancelled_by_id IS NULL"
                ),
            )
            for role in ("outgoing", "incoming")
        ),
    )

    outgoing_transaction_id: Mapped[UUID | None] = mapped_column()
    incoming_transaction_id: Mapped[UUID | None] = mapped_column()
    confirmation_action_id: Mapped[UUID] = mapped_column()
    ended_by_action_id: Mapped[UUID | None] = mapped_column(server_default=text("NULL"))
    cancelled_by_id: Mapped[UUID | None] = mapped_column(server_default=text("NULL"))
