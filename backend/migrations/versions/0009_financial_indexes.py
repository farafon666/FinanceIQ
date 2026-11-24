"""Index active operation reads and reverse lookup of financial history."""

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str = "0008"
branch_labels: str | None = None
depends_on: str | None = None

ACTIVE_INDEXES: tuple[tuple[str, tuple[str | sa.ColumnElement[object], ...]], ...] = (
    (
        "ix_transactions_active_list",
        (
            "user_id",
            sa.literal_column("occurred_on DESC"),
            sa.literal_column("id DESC"),
        ),
    ),
    (
        "ix_transactions_active_account_list",
        (
            "user_id",
            "account_id",
            sa.literal_column("occurred_on DESC"),
            sa.literal_column("id DESC"),
        ),
    ),
    (
        "ix_transactions_active_candidates",
        ("user_id", "account_id", "occurred_on", "amount"),
    ),
    ("ix_transactions_active_category", ("user_id", "category_id", "occurred_on")),
    ("ix_transactions_active_refunds", ("user_id", "refund_of_transaction_id")),
)
HISTORY_INDEXES = (
    ("transactions", "cancelled_by_id"),
    ("transactions", "refund_of_transaction_id"),
    ("transactions", "refund_action_id"),
    ("transfer_links", "outgoing_transaction_id"),
    ("transfer_links", "incoming_transaction_id"),
    ("transfer_links", "confirmation_action_id"),
    ("transfer_links", "ended_by_action_id"),
    ("import_movements", "result_transaction_id"),
    ("import_cancellations", "action_id"),
    ("financial_action_transactions", "transaction_id"),
)


def upgrade() -> None:
    for name, columns in ACTIVE_INDEXES:
        op.create_index(
            name,
            "transactions",
            columns,
            unique=False,
            postgresql_where=sa.text("cancelled_by_id IS NULL"),
        )
    for table, column in HISTORY_INDEXES:
        op.create_index(
            f"ix_{table}_user_id_{column}", table, ["user_id", column], unique=False
        )


def downgrade() -> None:
    for table, column in reversed(HISTORY_INDEXES):
        op.drop_index(f"ix_{table}_user_id_{column}", table_name=table)
    for name, _ in reversed(ACTIVE_INDEXES):
        op.drop_index(name, table_name="transactions")
