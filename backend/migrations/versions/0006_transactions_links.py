"""Create financial facts, transfer history and audit elements.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("import_movements", sa.Column("result_transaction_id", sa.Uuid()))
    # Validation deliberately rejects old link decisions without a real result.
    # Transactional DDL leaves the old revision and all data intact on failure.
    op.create_check_constraint(
        "decision_result_valid",
        "import_movements",
        "(decision <> 'pending' OR result_transaction_id IS NULL) "
        "AND (decision <> 'link' OR result_transaction_id IS NOT NULL)",
    )
    op.create_table(
        "transactions",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("source_movement_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Text(collation="C"),
            server_default=sa.text("'unclassified'"),
            nullable=False,
        ),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column(
            "category_origin",
            sa.Text(collation="C"),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column("categorization_rule_id", sa.Uuid(), nullable=True),
        sa.Column("refund_of_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("refund_action_id", sa.Uuid(), nullable=True),
        sa.Column("last_action_id", sa.Uuid(), nullable=False),
        sa.Column(
            "revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "cancelled_by_id", sa.Uuid(), server_default=sa.text("NULL"), nullable=True
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(category_origin <> 'none' OR (category_id IS NULL AND categorization_rule_id IS NULL)) AND (category_origin <> 'rule' OR (category_id IS NOT NULL AND categorization_rule_id IS NOT NULL)) AND (category_origin <> 'manual' OR categorization_rule_id IS NULL) AND (kind NOT IN ('transfer','unclassified') OR category_origin = 'none') AND (refund_of_transaction_id IS NULL OR category_origin = 'none') AND (kind <> 'refund' OR category_origin <> 'rule') AND (kind <> 'refund' OR refund_of_transaction_id IS NOT NULL OR category_origin = 'manual')",
            name=op.f("ck_transactions_category_state_valid"),
        ),
        sa.CheckConstraint(
            "(kind <> 'income' OR amount > 0) AND (kind <> 'expense' OR amount < 0)",
            name=op.f("ck_transactions_kind_sign_valid"),
        ),
        sa.CheckConstraint(
            "(kind = 'refund') = (refund_action_id IS NOT NULL)",
            name=op.f("ck_transactions_refund_action_valid"),
        ),
        sa.CheckConstraint(
            "category_origin IN ('none','rule','manual')",
            name=op.f("ck_transactions_category_origin_valid"),
        ),
        sa.CheckConstraint(
            "kind IN ('income','expense','transfer','refund','unclassified')",
            name=op.f("ck_transactions_kind_valid"),
        ),
        sa.CheckConstraint(
            "refund_of_transaction_id IS NULL OR (kind = 'refund' AND refund_of_transaction_id <> id)",
            name=op.f("ck_transactions_refund_reference_valid"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN -9999999999999999.99 AND 9999999999999999.99 AND amount <> 0",
            name=op.f("ck_transactions_amount_valid"),
        ),
        sa.CheckConstraint(
            "char_length(description) <= 2000",
            name=op.f("ck_transactions_description_length"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)", name=op.f("ck_transactions_created_at_finite")
        ),
        sa.CheckConstraint(
            "isfinite(occurred_on)", name=op.f("ck_transactions_occurred_on_finite")
        ),
        sa.CheckConstraint(
            "revision > 0", name=op.f("ck_transactions_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "source_movement_id"],
            [
                "import_movements.user_id",
                "import_movements.account_id",
                "import_movements.id",
            ],
            name=op.f(
                "fk_transactions_user_id_account_id_source_movement_id_import_movements"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            name=op.f("fk_transactions_user_id_account_id_accounts"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "cancelled_by_id"],
            ["import_cancellations.user_id", "import_cancellations.id"],
            name=op.f("fk_transactions_user_id_cancelled_by_id_import_cancellations"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "categorization_rule_id"],
            ["categorization_rules.user_id", "categorization_rules.id"],
            name=op.f(
                "fk_transactions_user_id_categorization_rule_id_categorization_rules"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            name=op.f("fk_transactions_user_id_category_id_categories"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "last_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f("fk_transactions_user_id_last_action_id_financial_actions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "refund_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f("fk_transactions_user_id_refund_action_id_financial_actions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "refund_of_transaction_id"],
            ["transactions.user_id", "transactions.id"],
            name=op.f("fk_transactions_user_id_refund_of_transaction_id_transactions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_transactions_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint(
            "user_id",
            "account_id",
            "id",
            name=op.f("uq_transactions_user_id_account_id_id"),
        ),
        sa.UniqueConstraint("user_id", "id", name=op.f("uq_transactions_user_id_id")),
        sa.UniqueConstraint(
            "user_id",
            "source_movement_id",
            name=op.f("uq_transactions_user_id_source_movement_id"),
        ),
    )
    op.create_table(
        "transfer_links",
        sa.Column("outgoing_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("incoming_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("confirmation_action_id", sa.Uuid(), nullable=False),
        sa.Column(
            "ended_by_action_id",
            sa.Uuid(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "cancelled_by_id", sa.Uuid(), server_default=sa.text("NULL"), nullable=True
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ended_by_action_id IS NULL OR cancelled_by_id IS NULL",
            name=op.f("ck_transfer_links_ending_valid"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)", name=op.f("ck_transfer_links_created_at_finite")
        ),
        sa.CheckConstraint(
            "outgoing_transaction_id IS NOT NULL OR incoming_transaction_id IS NOT NULL",
            name=op.f("ck_transfer_links_side_required"),
        ),
        sa.CheckConstraint(
            "outgoing_transaction_id IS NULL OR incoming_transaction_id IS NULL OR outgoing_transaction_id <> incoming_transaction_id",
            name=op.f("ck_transfer_links_distinct_sides"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "cancelled_by_id"],
            ["import_cancellations.user_id", "import_cancellations.id"],
            name=op.f("fk_transfer_links_user_id_cancelled_by_id_import_cancellations"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "confirmation_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f(
                "fk_transfer_links_user_id_confirmation_action_id_financial_actions"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "ended_by_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f("fk_transfer_links_user_id_ended_by_action_id_financial_actions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "incoming_transaction_id"],
            ["transactions.user_id", "transactions.id"],
            name=op.f("fk_transfer_links_user_id_incoming_transaction_id_transactions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "outgoing_transaction_id"],
            ["transactions.user_id", "transactions.id"],
            name=op.f("fk_transfer_links_user_id_outgoing_transaction_id_transactions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_transfer_links_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transfer_links")),
    )
    op.create_index(
        "uq_transfer_links_active_incoming",
        "transfer_links",
        ["user_id", "incoming_transaction_id"],
        unique=True,
        postgresql_where=sa.text(
            "ended_by_action_id IS NULL AND cancelled_by_id IS NULL"
        ),
    )
    op.create_index(
        "uq_transfer_links_active_outgoing",
        "transfer_links",
        ["user_id", "outgoing_transaction_id"],
        unique=True,
        postgresql_where=sa.text(
            "ended_by_action_id IS NULL AND cancelled_by_id IS NULL"
        ),
    )
    op.create_table(
        "financial_action_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column(
            "before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.CheckConstraint(
            "jsonb_typeof(after_state) = 'object'",
            name=op.f("ck_financial_action_transactions_after_state_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before_state) = 'object'",
            name=op.f("ck_financial_action_transactions_before_state_object"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)",
            name=op.f("ck_financial_action_transactions_created_at_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f(
                "fk_financial_action_transactions_user_id_action_id_financial_actions"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "transaction_id"],
            ["transactions.user_id", "transactions.id"],
            name=op.f(
                "fk_financial_action_transactions_user_id_transaction_id_transactions"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_financial_action_transactions_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_action_transactions")),
        sa.UniqueConstraint(
            "user_id",
            "action_id",
            "transaction_id",
            name=op.f(
                "uq_financial_action_transactions_user_id_action_id_transaction_id"
            ),
        ),
    )
    for columns, targets in (
        (
            ["user_id", "account_id", "result_transaction_id"],
            ["user_id", "account_id", "id"],
        ),
        (["user_id", "result_transaction_id"], ["user_id", "id"]),
    ):
        op.create_foreign_key(
            None,
            "import_movements",
            "transactions",
            columns,
            targets,
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )
    op.create_unique_constraint(
        None, "import_movements", ["user_id", "attempt_id", "result_transaction_id"]
    )
    op.drop_constraint(
        op.f("ck_financial_actions_action_type_valid"),
        "financial_actions",
        type_="check",
    )
    op.create_check_constraint(
        "action_type_valid",
        "financial_actions",
        "action_type IN ('confirm_balance','revoke_balance','confirm_history',"
        "'revoke_history','archive_account','restore_account','import_confirm','import_cancel',"
        "'set_kind','set_category','confirm_transfer','replace_transfer','unlink_transfer',"
        "'link_refund','unlink_refund','confirm_unlinked_refund')",
    )
    # Row-local history protection only. C-02--C-11 and owner roots arrive later.
    op.execute("""
        CREATE FUNCTION guard_transaction_history() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Financial facts cannot be deleted' USING ERRCODE = '23514';
            END IF;
            IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
            IF OLD.cancelled_by_id IS NOT NULL THEN
                RAISE EXCEPTION 'Cancelled financial fact is immutable' USING ERRCODE = '23514';
            END IF;
            IF (to_jsonb(NEW) - ARRAY['kind','category_id','category_origin',
                    'categorization_rule_id','refund_of_transaction_id','refund_action_id',
                    'last_action_id','revision','cancelled_by_id']) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['kind','category_id','category_origin',
                    'categorization_rule_id','refund_of_transaction_id','refund_action_id',
                    'last_action_id','revision','cancelled_by_id'])
            THEN
                RAISE EXCEPTION 'Financial source fields are immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.cancelled_by_id IS NOT NULL AND
                (to_jsonb(NEW) - ARRAY['cancelled_by_id','last_action_id','revision']) IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['cancelled_by_id','last_action_id','revision'])
            THEN
                RAISE EXCEPTION 'Cancellation cannot change classification' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER guard_transaction_history BEFORE UPDATE OR DELETE ON transactions FOR EACH ROW EXECUTE FUNCTION guard_transaction_history()"
    )
    op.execute("""
        CREATE FUNCTION guard_transfer_history() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Transfer history cannot be deleted' USING ERRCODE = '23514';
            END IF;
            IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
            IF OLD.ended_by_action_id IS NOT NULL OR OLD.cancelled_by_id IS NOT NULL
                OR (to_jsonb(NEW) - ARRAY['ended_by_action_id','cancelled_by_id']) IS DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['ended_by_action_id','cancelled_by_id'])
            THEN
                RAISE EXCEPTION 'Transfer identity or completed history is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER guard_transfer_history BEFORE UPDATE OR DELETE ON transfer_links FOR EACH ROW EXECUTE FUNCTION guard_transfer_history()"
    )
    op.execute("""
        CREATE FUNCTION guard_action_transaction_history() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            RAISE EXCEPTION 'Financial action elements are immutable' USING ERRCODE = '23514';
        END $$
    """)
    op.execute(
        "CREATE TRIGGER guard_action_transaction_history BEFORE UPDATE OR DELETE ON financial_action_transactions FOR EACH ROW EXECUTE FUNCTION guard_action_transaction_history()"
    )
    op.execute(
        "GRANT SELECT ON transactions, transfer_links, financial_action_transactions TO financeiq_runtime"
    )
    for table, grant_columns in {
        "transactions": "id,user_id,account_id,source_movement_id,occurred_on,amount,description,kind,category_id,category_origin,categorization_rule_id,refund_of_transaction_id,refund_action_id,last_action_id",
        "transfer_links": "id,user_id,outgoing_transaction_id,incoming_transaction_id,confirmation_action_id",
        "financial_action_transactions": "id,user_id,action_id,transaction_id,before_state,after_state",
    }.items():
        op.execute(f"GRANT INSERT ({grant_columns}) ON {table} TO financeiq_runtime")
    op.execute(
        "GRANT UPDATE (kind,category_id,category_origin,categorization_rule_id,refund_of_transaction_id,refund_action_id,last_action_id,revision,cancelled_by_id) ON transactions TO financeiq_runtime"
    )
    op.execute(
        "GRANT UPDATE (ended_by_action_id,cancelled_by_id) ON transfer_links TO financeiq_runtime"
    )
    op.execute(
        "GRANT INSERT (result_transaction_id), UPDATE (result_transaction_id) ON import_movements TO financeiq_runtime"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE financial_actions, import_movements, transactions, transfer_links, financial_action_transactions IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM transactions)
                OR EXISTS (SELECT 1 FROM transfer_links)
                OR EXISTS (SELECT 1 FROM financial_action_transactions)
                OR EXISTS (SELECT 1 FROM import_movements WHERE result_transaction_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM financial_actions WHERE action_type IN
                    ('set_kind','set_category','confirm_transfer','replace_transfer',
                     'unlink_transfer','link_refund','unlink_refund','confirm_unlinked_refund'))
            THEN
                RAISE EXCEPTION 'Cannot downgrade nonempty Transactions or transaction audit';
            END IF;
        END $$
    """)
    op.execute(
        "REVOKE INSERT (result_transaction_id), UPDATE (result_transaction_id) ON import_movements FROM financeiq_runtime"
    )
    for name in (
        "fk_import_movements_user_id_account_id_result_transaction_id_transactions",
        "fk_import_movements_user_id_result_transaction_id_transactions",
    ):
        op.drop_constraint(op.f(name), "import_movements", type_="foreignkey")
    op.drop_constraint(
        op.f("uq_import_movements_user_id_attempt_id_result_transaction_id"),
        "import_movements",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_import_movements_decision_result_valid"),
        "import_movements",
        type_="check",
    )
    op.drop_column("import_movements", "result_transaction_id")
    op.drop_table("financial_action_transactions")
    op.drop_table("transfer_links")
    op.drop_table("transactions")
    for function in (
        "guard_action_transaction_history",
        "guard_transfer_history",
        "guard_transaction_history",
    ):
        op.execute(f"DROP FUNCTION {function}()")
    op.drop_constraint(
        op.f("ck_financial_actions_action_type_valid"),
        "financial_actions",
        type_="check",
    )
    op.create_check_constraint(
        "action_type_valid",
        "financial_actions",
        "action_type IN ('confirm_balance','revoke_balance','confirm_history',"
        "'revoke_history','archive_account','restore_account','import_confirm','import_cancel')",
    )
