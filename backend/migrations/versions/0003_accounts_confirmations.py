"""Create accounts, confirmations and the Accounts slice of financial audit.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("currency_code", sa.Text(collation="C"), nullable=False),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "facts_version",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        sa.CheckConstraint("currency_code = 'RUB'", name="currency_rub"),
        sa.CheckConstraint("facts_version >= 0", name="facts_version_nonnegative"),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.CheckConstraint(
            "archived_at IS NULL OR (isfinite(archived_at) AND archived_at >= created_at)",
            name="archived_at_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id"),
    )
    op.create_index(
        "ix_accounts_user_id_archived_at_id",
        "accounts",
        ["user_id", "archived_at", "id"],
    )
    op.create_table(
        "financial_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("plan_digest", sa.LargeBinary(), nullable=False),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.CheckConstraint(
            "action_type IN ('confirm_balance', 'revoke_balance', 'confirm_history', 'revoke_history', 'archive_account', 'restore_account')",
            name="action_type_valid",
        ),
        sa.CheckConstraint("plan_version > 0", name="plan_version_positive"),
        sa.CheckConstraint("schema_version > 0", name="schema_version_positive"),
        sa.CheckConstraint("octet_length(plan_digest) = 32", name="plan_digest_length"),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id"),
        sa.UniqueConstraint("user_id", "request_id"),
    )
    op.create_table(
        "balance_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("balance_date", sa.Date(), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("confirmation_action_id", sa.Uuid(), nullable=False),
        sa.Column(
            "revocation_action_id",
            sa.Uuid(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.CheckConstraint("isfinite(balance_date)", name="balance_date_finite"),
        sa.CheckConstraint(
            "balance BETWEEN -9999999999999999.99 AND 9999999999999999.99",
            name="balance_range",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "confirmation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "revocation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_balance_confirmations_active",
        "balance_confirmations",
        ["user_id", "account_id", "balance_date"],
        unique=True,
        postgresql_where=sa.text("revocation_action_id IS NULL"),
    )
    op.create_index(
        "ix_balance_confirmations_history",
        "balance_confirmations",
        ["user_id", "account_id", sa.text("balance_date DESC"), "id"],
    )
    op.create_table(
        "history_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("observed_facts_version", sa.BigInteger(), nullable=False),
        sa.Column("observed_facts_digest", sa.LargeBinary(), nullable=False),
        sa.Column("confirmation_action_id", sa.Uuid(), nullable=False),
        sa.Column(
            "revocation_action_id",
            sa.Uuid(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.CheckConstraint(
            "isfinite(start_date) AND isfinite(end_date) AND start_date <= end_date",
            name="date_range_valid",
        ),
        sa.CheckConstraint(
            "observed_facts_version >= 0", name="observed_facts_version_nonnegative"
        ),
        sa.CheckConstraint(
            "octet_length(observed_facts_digest) = 32",
            name="observed_facts_digest_length",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "confirmation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "revocation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_history_confirmations_active",
        "history_confirmations",
        ["user_id", "account_id", "start_date", "end_date"],
        unique=True,
        postgresql_where=sa.text("revocation_action_id IS NULL"),
    )
    op.create_index(
        "ix_history_confirmations_active_intervals",
        "history_confirmations",
        ["user_id", "account_id", "end_date", "start_date"],
        postgresql_where=sa.text("revocation_action_id IS NULL"),
    )
    # This row-local guard does not yet implement C-11 action semantics or root locking.
    # JSONB keeps NUMERIC exact and includes future source columns in the comparison.
    op.execute("""
        CREATE FUNCTION guard_confirmation_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW) - 'revocation_action_id')
                   IS DISTINCT FROM (to_jsonb(OLD) - 'revocation_action_id')
               OR (OLD.revocation_action_id IS NOT NULL
                   AND NEW.revocation_action_id IS DISTINCT FROM OLD.revocation_action_id)
            THEN
                RAISE EXCEPTION 'Confirmation source fields and existing revocation are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("balance_confirmations", "history_confirmations"):
        op.execute(
            f"CREATE TRIGGER guard_confirmation_update BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_confirmation_update()"
        )
    op.execute(
        "GRANT SELECT ON accounts, financial_actions, balance_confirmations, history_confirmations TO financeiq_runtime"
    )
    op.execute(
        "GRANT INSERT (id, user_id, name, currency_code) ON accounts TO financeiq_runtime"
    )
    op.execute("GRANT UPDATE (name, archived_at) ON accounts TO financeiq_runtime")
    op.execute(
        "GRANT INSERT (id, user_id, request_id, action_type, plan_version, plan_digest, "
        "schema_version, account_id, reason_code, reason, details) "
        "ON financial_actions TO financeiq_runtime"
    )
    op.execute(
        "GRANT INSERT (id, user_id, account_id, balance_date, balance, confirmation_action_id) ON balance_confirmations TO financeiq_runtime"
    )
    op.execute(
        "GRANT INSERT (id, user_id, account_id, start_date, end_date, observed_facts_version, observed_facts_digest, confirmation_action_id) ON history_confirmations TO financeiq_runtime"
    )
    op.execute(
        "GRANT UPDATE (revocation_action_id) ON balance_confirmations, history_confirmations TO financeiq_runtime"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE accounts, financial_actions, balance_confirmations, history_confirmations IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM accounts)
                OR EXISTS (SELECT 1 FROM financial_actions)
                OR EXISTS (SELECT 1 FROM balance_confirmations)
                OR EXISTS (SELECT 1 FROM history_confirmations)
            THEN
                RAISE EXCEPTION 'Cannot downgrade nonempty Accounts or financial audit tables';
            END IF;
        END $$
    """)
    op.drop_table("history_confirmations")
    op.drop_table("balance_confirmations")
    op.execute("DROP FUNCTION guard_confirmation_update()")
    op.drop_table("financial_actions")
    op.drop_table("accounts")
