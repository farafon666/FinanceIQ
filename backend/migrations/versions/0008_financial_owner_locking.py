"""Serialize financial writes on the owner's users row."""

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

FINANCIAL_TABLES = (
    "accounts",
    "balance_confirmations",
    "history_confirmations",
    "categories",
    "categorization_rules",
    "financial_actions",
    "import_batches",
    "import_attempts",
    "import_account_mappings",
    "import_rows",
    "import_movements",
    "import_cancellations",
    "transactions",
    "transfer_links",
    "financial_action_transactions",
)
TRIGGER_NAME = "a_financial_owner_lock"


def _lock_tables() -> None:
    tables = ", ".join(f"public.{table}" for table in FINANCIAL_TABLES)
    op.execute(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE")


def _validate_existing_history() -> None:
    # The 0007 guards checked individual commands. Earlier concurrent commits
    # can still have left invalid history, which new triggers cannot repair.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.categorization_rules rule
                JOIN public.categories c ON c.user_id=rule.user_id AND c.id=rule.category_id
                WHERE rule.is_active AND c.archived_at IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM public.accounts a WHERE a.facts_version<>(
                    SELECT count(*)+count(t.cancelled_by_id) FROM public.transactions t
                    WHERE t.user_id=a.user_id AND t.account_id=a.id
                )
            ) OR EXISTS (
                SELECT 1 FROM public.accounts a WHERE a.archived_at IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM public.financial_actions action
                        WHERE action.user_id=a.user_id AND action.account_id=a.id
                            AND action.action_type='archive_account'
                            AND action.details->'result' @> jsonb_build_object(
                                'account_id',a.id,'archived_at',a.archived_at
                            )
                    )
            ) THEN
                RAISE EXCEPTION 'Legacy financial history is incompatible with approved invariants'
                    USING ERRCODE='23514';
            END IF;

            PERFORM public.financial_check_transaction(user_id,id) FROM public.transactions;
            PERFORM public.financial_check_transfer(user_id,id) FROM public.transfer_links;
            PERFORM public.financial_check_attempt(user_id,id) FROM public.import_attempts;
            PERFORM public.financial_check_cancellation(user_id,id) FROM public.import_cancellations;
            PERFORM public.financial_check_action(user_id,id) FROM public.financial_actions;
            PERFORM public.financial_check_confirmation(user_id,id,true) FROM public.balance_confirmations;
            PERFORM public.financial_check_confirmation(user_id,id,false) FROM public.history_confirmations;
        END
        $$
    """)


def upgrade() -> None:
    _lock_tables()
    _validate_existing_history()
    op.execute("""
        CREATE FUNCTION public.lock_financial_owner(owner_id uuid) RETURNS void
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'Financial writes require READ COMMITTED'
                    USING ERRCODE='0A000';
            END IF;
            PERFORM id FROM public.users WHERE id=owner_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Financial owner does not exist' USING ERRCODE='23503';
            END IF;
        END
        $$
    """)
    # Row locking requires UPDATE privilege on at least one column. Batch
    # identity remains insert-only for runtime, so expose just this scoped lock.
    op.execute("""
        CREATE FUNCTION public.lock_financial_import_batches(owner_id uuid, batch_ids uuid[])
        RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE locked_count bigint;
        BEGIN
            PERFORM public.lock_financial_owner(owner_id);
            SELECT count(*) INTO locked_count FROM (
                SELECT id FROM public.import_batches
                WHERE user_id=owner_id AND id=ANY(batch_ids)
                ORDER BY id FOR NO KEY UPDATE
            ) locked;
            RETURN locked_count;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.guard_financial_owner_lock() RETURNS trigger
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                -- Preserve the table's NOT NULL diagnostic for invalid inserts.
                IF NEW.user_id IS NULL THEN RETURN NEW; END IF;
                PERFORM public.lock_financial_owner(NEW.user_id);
                IF TG_TABLE_NAME='financial_actions' THEN
                    -- Column defaults are evaluated before a BEFORE INSERT trigger.
                    NEW.created_at:=clock_timestamp();
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP='UPDATE' AND NEW.user_id IS DISTINCT FROM OLD.user_id THEN
                RAISE EXCEPTION 'Financial owner is immutable' USING ERRCODE='23514';
            END IF;
            PERFORM public.lock_financial_owner(OLD.user_id);
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.guard_financial_owner_lock() "
        "FROM PUBLIC, financeiq_runtime"
    )
    for signature in (
        "lock_financial_owner(uuid)",
        "lock_financial_import_batches(uuid,uuid[])",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO financeiq_runtime")

    # PostgreSQL runs same-kind triggers in name order. Take the root before
    # guards that read dependencies or take the local import-attempt lock.
    for table in FINANCIAL_TABLES:
        op.execute(
            f"CREATE TRIGGER {TRIGGER_NAME} BEFORE INSERT OR UPDATE OR DELETE "
            f"ON public.{table} FOR EACH ROW "
            "EXECUTE FUNCTION public.guard_financial_owner_lock()"
        )


def downgrade() -> None:
    _lock_tables()
    for table in reversed(FINANCIAL_TABLES):
        op.execute(f"DROP TRIGGER {TRIGGER_NAME} ON public.{table}")
    op.execute("DROP FUNCTION public.guard_financial_owner_lock()")
    op.execute("DROP FUNCTION public.lock_financial_import_batches(uuid,uuid[])")
    op.execute("DROP FUNCTION public.lock_financial_owner(uuid)")
