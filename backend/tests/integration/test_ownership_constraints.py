"""The approved FK contract, independently checked against PostgreSQL and ORM."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financeiq.db.models import metadata
from tests.integration.test_imports import imports_migrator as imports_migrator

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

ForeignKey = tuple[str, tuple[str, ...], str, tuple[str, ...]]

# Deliberately independent of metadata: deleting a key in both ORM and migrations
# must still fail this acceptance test. These are DATABASE_SCHEMA §§2--7 keys.
OWNED_TABLES = (
    "accounts",
    "auth_sessions",
    "balance_confirmations",
    "categories",
    "categorization_rules",
    "financial_action_transactions",
    "financial_actions",
    "history_confirmations",
    "import_account_mappings",
    "import_attempts",
    "import_batches",
    "import_cancellations",
    "import_movements",
    "import_rows",
    "transactions",
    "transfer_links",
)
SCOPED_REFERENCES = {
    "balance_confirmations": (
        ("user_id,account_id", "accounts", "user_id,id"),
        ("user_id,confirmation_action_id", "financial_actions", "user_id,id"),
        ("user_id,revocation_action_id", "financial_actions", "user_id,id"),
    ),
    "history_confirmations": (
        ("user_id,account_id", "accounts", "user_id,id"),
        ("user_id,confirmation_action_id", "financial_actions", "user_id,id"),
        ("user_id,revocation_action_id", "financial_actions", "user_id,id"),
    ),
    "categorization_rules": (("user_id,category_id", "categories", "user_id,id"),),
    "financial_actions": (
        ("user_id,account_id", "accounts", "user_id,id"),
        ("user_id,source_attempt_id", "import_attempts", "user_id,id"),
    ),
    "financial_action_transactions": (
        ("user_id,action_id", "financial_actions", "user_id,id"),
        ("user_id,transaction_id", "transactions", "user_id,id"),
    ),
    "import_attempts": (
        ("user_id,batch_id", "import_batches", "user_id,id"),
        ("user_id,accepted_action_id", "financial_actions", "user_id,id"),
    ),
    "import_account_mappings": (
        ("user_id,attempt_id", "import_attempts", "user_id,id"),
        ("user_id,account_id", "accounts", "user_id,id"),
    ),
    "import_rows": (("user_id,attempt_id", "import_attempts", "user_id,id"),),
    "import_movements": (
        ("user_id,attempt_id,row_id", "import_rows", "user_id,attempt_id,id"),
        (
            "user_id,attempt_id,mapping_id",
            "import_account_mappings",
            "user_id,attempt_id,id",
        ),
        (
            "user_id,attempt_id,mapping_id,account_id",
            "import_account_mappings",
            "user_id,attempt_id,id,account_id",
        ),
        (
            "user_id,account_id,result_transaction_id",
            "transactions",
            "user_id,account_id,id",
        ),
        ("user_id,result_transaction_id", "transactions", "user_id,id"),
    ),
    "import_cancellations": (
        ("user_id,attempt_id", "import_attempts", "user_id,id"),
        ("user_id,action_id", "financial_actions", "user_id,id"),
    ),
    "transactions": (
        ("user_id,account_id", "accounts", "user_id,id"),
        (
            "user_id,account_id,source_movement_id",
            "import_movements",
            "user_id,account_id,id",
        ),
        ("user_id,category_id", "categories", "user_id,id"),
        ("user_id,categorization_rule_id", "categorization_rules", "user_id,id"),
        ("user_id,refund_of_transaction_id", "transactions", "user_id,id"),
        ("user_id,refund_action_id", "financial_actions", "user_id,id"),
        ("user_id,last_action_id", "financial_actions", "user_id,id"),
        ("user_id,cancelled_by_id", "import_cancellations", "user_id,id"),
    ),
    "transfer_links": (
        ("user_id,outgoing_transaction_id", "transactions", "user_id,id"),
        ("user_id,incoming_transaction_id", "transactions", "user_id,id"),
        ("user_id,confirmation_action_id", "financial_actions", "user_id,id"),
        ("user_id,ended_by_action_id", "financial_actions", "user_id,id"),
        ("user_id,cancelled_by_id", "import_cancellations", "user_id,id"),
    ),
}
EXPECTED_KEYS: set[ForeignKey] = {
    (table, ("user_id",), "users", ("id",)) for table in OWNED_TABLES
} | {
    (table, tuple(columns.split(",")), target, tuple(references.split(",")))
    for table, keys in SCOPED_REFERENCES.items()
    for columns, target, references in keys
}
DEFERRED_KEYS: set[ForeignKey] = {
    (
        "import_movements",
        ("user_id", "account_id", "result_transaction_id"),
        "transactions",
        ("user_id", "account_id", "id"),
    ),
    (
        "import_movements",
        ("user_id", "result_transaction_id"),
        "transactions",
        ("user_id", "id"),
    ),
}


async def test_complete_foreign_key_contract(imports_migrator: AsyncEngine) -> None:
    async with imports_migrator.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text("""
                    SELECT source.relname AS source, target.relname AS target,
                        ARRAY(SELECT a.attname
                              FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum,n)
                              JOIN pg_attribute a ON a.attrelid=c.conrelid
                                  AND a.attnum=k.attnum ORDER BY k.n) AS columns,
                        ARRAY(SELECT a.attname
                              FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum,n)
                              JOIN pg_attribute a ON a.attrelid=c.confrelid
                                  AND a.attnum=k.attnum ORDER BY k.n) AS refs,
                        c.confupdtype::text, c.confdeltype::text,
                        c.confmatchtype::text,
                        c.condeferrable, c.condeferred, c.convalidated,
                        EXISTS(SELECT 1 FROM pg_attribute a
                               WHERE a.attrelid=c.conrelid
                                   AND a.attname='user_id'
                                   AND a.attnotnull) AS owner_required,
                        EXISTS(SELECT 1 FROM pg_constraint u
                               WHERE u.conrelid=c.confrelid
                                   AND u.contype IN ('p','u')
                                   AND NOT u.condeferrable
                                   AND u.conkey=c.confkey) AS target_key
                    FROM pg_constraint c
                    JOIN pg_class source ON source.oid=c.conrelid
                    JOIN pg_class target ON target.oid=c.confrelid
                    JOIN pg_namespace ns ON ns.oid=source.relnamespace
                    WHERE c.contype='f' AND ns.nspname='public'
                """)
                )
            )
            .mappings()
            .all()
        )
    actual: set[ForeignKey] = set()
    for row in rows:
        key = (row["source"], tuple(row["columns"]), row["target"], tuple(row["refs"]))
        actual.add(key)
        assert row["confupdtype"] == row["confdeltype"] == "r", key
        assert row["confmatchtype"] == "s", key
        assert row["owner_required"], key
        assert row["convalidated"] and row["target_key"], key
        assert row["condeferrable"] == row["condeferred"] == (key in DEFERRED_KEYS), key
    assert actual == EXPECTED_KEYS
    assert len(rows) == len(EXPECTED_KEYS)

    for table_name in OWNED_TABLES:
        assert not metadata.tables[table_name].c.user_id.nullable, table_name
    orm_keys: set[ForeignKey] = set()
    for table in metadata.tables.values():
        for fk in table.foreign_key_constraints:
            key = (
                table.name,
                tuple(fk.column_keys),
                fk.referred_table.name,
                tuple(element.column.name for element in fk.elements),
            )
            orm_keys.add(key)
            assert fk.ondelete == fk.onupdate == "RESTRICT", key
            assert fk.match in (None, "SIMPLE"), key
            assert bool(fk.deferrable) == (key in DEFERRED_KEYS), key
            assert (fk.initially == "DEFERRED") == (key in DEFERRED_KEYS), key
    assert orm_keys == EXPECTED_KEYS
