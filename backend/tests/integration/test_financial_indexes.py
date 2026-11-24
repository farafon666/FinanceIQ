"""PostgreSQL index contract and the scoped reads it supports."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateIndex

from financeiq.db.models import metadata
from tests.support.financial import book, cancel, classify, linked_book

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

ACTIVE = "cancelled_by_id IS NULL"
ACTIVE_INDEXES = {
    "ix_transactions_active_list": "user_id, occurred_on DESC, id DESC",
    "ix_transactions_active_account_list": "user_id, account_id, occurred_on DESC, id DESC",
    "ix_transactions_active_candidates": "user_id, account_id, occurred_on, amount",
    "ix_transactions_active_category": "user_id, category_id, occurred_on",
    "ix_transactions_active_refunds": "user_id, refund_of_transaction_id",
}
HISTORY_COLUMNS = {
    "transactions": ("cancelled_by_id", "refund_of_transaction_id", "refund_action_id"),
    "transfer_links": (
        "outgoing_transaction_id",
        "incoming_transaction_id",
        "confirmation_action_id",
        "ended_by_action_id",
    ),
    "import_movements": ("result_transaction_id",),
    "import_cancellations": ("action_id",),
    "financial_action_transactions": ("transaction_id",),
}


async def test_financial_index_contract(financial_migrator: AsyncEngine) -> None:
    expected: dict[str, tuple[str, str, str | None]] = {
        name: ("transactions", columns, ACTIVE)
        for name, columns in ACTIVE_INDEXES.items()
    }
    expected.update(
        {
            f"ix_{table}_user_id_{column}": (table, f"user_id, {column}", None)
            for table, columns in HISTORY_COLUMNS.items()
            for column in columns
        }
    )
    expected.update(
        {
            "ix_import_batches_history": (
                "import_batches",
                "user_id, created_at DESC, id",
                None,
            ),
            "ix_import_attempts_history": (
                "import_attempts",
                "user_id, batch_id, attempt_number DESC",
                None,
            ),
            "ix_import_movements_fingerprint": (
                "import_movements",
                "user_id, account_id, fingerprint_version, fingerprint",
                None,
            ),
        }
    )
    async with financial_migrator.connect() as connection:
        rows = await connection.execute(
            text(
                """
                SELECT c.relname, t.relname, am.amname, i.indisunique, i.indisvalid,
                    pg_get_indexdef(i.indexrelid), pg_get_expr(i.indpred, i.indrelid)
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                JOIN pg_class t ON t.oid = i.indrelid
                JOIN pg_am am ON am.oid = c.relam
                WHERE t.relnamespace = 'public'::regnamespace
                    AND c.relname = ANY(:names)
                """
            ),
            {"names": list(expected)},
        )
        actual = {row[0]: tuple(row[1:]) for row in rows}

    assert actual.keys() == expected.keys()
    for name, (table, columns, predicate) in expected.items():
        database_ddl = f"CREATE INDEX {name} ON public.{table} USING btree ({columns})"
        orm_ddl = f"CREATE INDEX {name} ON {table} ({columns})"
        if predicate is not None:
            database_ddl += f" WHERE ({predicate})"
            orm_ddl += f" WHERE {predicate}"
        assert actual[name] == (
            table,
            "btree",
            False,
            True,
            database_ddl,
            f"({predicate})" if predicate else None,
        )
        index = next(
            index for index in metadata.tables[table].indexes if index.name == name
        )
        assert (
            str(CreateIndex(index).compile(dialect=financial_migrator.dialect))
            == orm_ddl
        )


async def test_scoped_operation_lists_and_repeat_candidates(
    financial_engine: AsyncEngine, financial_migrator: AsyncEngine
) -> None:
    async with financial_engine.begin() as connection:
        original = await book(connection, ("-100.00", "-100.00", "-100.01", "100.00"))
        other_account = await book(connection, owner=original.owner)
        await book(connection)
        cancelled = await book(
            connection, owner=original.owner, account=original.account
        )
        later = await book(
            connection,
            owner=original.owner,
            account=original.account,
            occurred_on="2026-10-02",
        )
        await linked_book(connection, original)
        await cancel(connection, cancelled)
        category = uuid4()
        await connection.execute(
            text(
                "INSERT INTO categories(id,user_id,name,name_key,kind) "
                "VALUES (:category,:owner,'Synthetic','synthetic','expense')"
            ),
            {"category": category, "owner": original.owner},
        )
        await classify(
            connection,
            original,
            original.facts[0],
            changes={"category_id": category, "category_origin": "manual"},
            kind="set_category",
        )

    scoped = f"WHERE user_id=:owner AND {ACTIVE} AND occurred_on>=:day"
    order = "ORDER BY occurred_on DESC, id DESC"
    owner_list = f"SELECT id FROM transactions {scoped} {order}"
    account_list = (
        f"SELECT id FROM transactions {scoped} AND account_id=:account {order}"
    )
    candidate_filter = (
        "t.user_id=:owner AND t.account_id=:account AND t.occurred_on=:day "
        "AND t.amount=:amount"
    )
    parameters = {
        "owner": original.owner,
        "day": date(2026, 10, 1),
        "account": original.account,
        "category": category,
        "amount": Decimal("-100.00"),
    }
    async with financial_engine.begin() as connection:
        assert list(
            (await connection.execute(text(owner_list), parameters)).scalars()
        ) == [
            later.facts[0],
            *sorted(original.facts + other_account.facts, reverse=True),
        ]
        assert list(
            (await connection.execute(text(account_list), parameters)).scalars()
        ) == [later.facts[0], *sorted(original.facts, reverse=True)]
        assert list(
            (
                await connection.execute(
                    text(
                        f"SELECT id FROM transactions {scoped} AND category_id=:category"
                    ),
                    parameters,
                )
            ).scalars()
        ) == [original.facts[0]]
        matches = await connection.execute(
            text(
                "SELECT DISTINCT t.id FROM transactions t JOIN import_movements m "
                "ON m.user_id=t.user_id AND m.result_transaction_id=t.id "
                "JOIN import_attempts p ON p.user_id=m.user_id AND p.id=m.attempt_id "
                f"WHERE {candidate_filter} AND t.{ACTIVE} AND p.status='accepted'"
            ),
            parameters,
        )
        assert sorted(matches.scalars()) == sorted(original.facts[:2])
        history = await connection.execute(
            text(
                f"SELECT t.id FROM transactions t WHERE {candidate_filter} AND t.cancelled_by_id IS NOT NULL"
            ),
            parameters,
        )
        assert list(history.scalars()) == list(cancelled.facts)

    async with financial_engine.begin() as connection:
        # Match the owner's account and day so amount is the selective key.
        await book(
            connection,
            tuple(f"-{amount}.00" for amount in range(1000, 1128)),
            owner=original.owner,
            account=original.account,
        )
    async with financial_migrator.begin() as connection:
        await connection.execute(text("ANALYZE transactions"))

    async with financial_engine.begin() as connection:
        # Keep the check about index applicability for a small, synthetic table.
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        await connection.execute(text("SET LOCAL enable_bitmapscan = off"))
        for statement, index, bindings in (
            (owner_list, "ix_transactions_active_list", parameters),
            (
                account_list,
                "ix_transactions_active_account_list",
                parameters | {"account": other_account.account},
            ),
            (
                f"SELECT t.id FROM transactions t WHERE {candidate_filter} AND t.{ACTIVE}",
                "ix_transactions_active_candidates",
                parameters,
            ),
        ):
            plan = await connection.execute(
                text(f"EXPLAIN (COSTS OFF) {statement}"), bindings
            )
            assert index in "\n".join(plan.scalars())
