"""C-01--C-11 at the actual migration head, including real commit failures."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financeiq.db.session import create_session_factory
from financeiq.modules.transactions.models import Transaction
from tests.support.financial import (
    action,
    archive_account,
    book,
    cancel,
    classify,
    confirmation,
    item,
    linked_book,
    snapshot,
    transfer,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@asynccontextmanager
async def rejected(
    engine: AsyncEngine, state: str = "23514"
) -> AsyncIterator[AsyncConnection]:
    with pytest.raises(DBAPIError) as failure:
        async with engine.begin() as connection:
            yield connection
    assert getattr(failure.value.orig, "sqlstate", None) == state


async def test_complete_book_has_provenance_audit_and_server_versions(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT facts_version FROM accounts WHERE id=:id"),
                {"id": value.account},
            )
            == 1
        )


async def test_transfer_without_confirmed_link_rolls_back(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    with pytest.raises(DBAPIError) as failure:
        async with financial_engine.begin() as connection:
            await classify(
                connection, value, value.facts[0], changes={"kind": "transfer"}
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


@pytest.mark.parametrize("incoming", [False, True])
async def test_one_real_transfer_side_is_valid(
    financial_engine: AsyncEngine, incoming: bool
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("100.00" if incoming else "-100.00",))
        await transfer(
            connection,
            value,
            None if incoming else value.facts[0],
            value.facts[0] if incoming else None,
        )
        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    async with financial_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == 1


@pytest.mark.parametrize("bad", [None, "amount", "account", "role", "consent"])
async def test_transfer_pairs_validate_both_roles_accounts_amounts_and_evidence(
    financial_engine: AsyncEngine, bad: str | None
) -> None:
    async with financial_engine.begin() as connection:
        outgoing = await book(connection)
        incoming = await book(
            connection,
            ("99.99" if bad == "amount" else "100.00",),
            owner=outgoing.owner,
            account=outgoing.account if bad == "account" else None,
            occurred_on="2026-10-02",
        )

    async def command(connection: AsyncConnection) -> None:
        await transfer(
            connection,
            outgoing,
            incoming.facts[0] if bad == "role" else outgoing.facts[0],
            outgoing.facts[0] if bad == "role" else incoming.facts[0],
            consents=[] if bad == "consent" else None,
        )

    if bad:
        async with rejected(financial_engine) as connection:
            await command(connection)
    else:
        async with financial_engine.begin() as connection:
            await command(connection)
        async with financial_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT sum(amount) FROM transactions"))
                == 0
            )


@pytest.mark.parametrize(
    "amounts", [("-100.00", "30.00", "70.00"), ("100.00", "-30.00", "-70.00")]
)
async def test_partial_refunds_at_exact_limit_are_valid(
    financial_engine: AsyncEngine, amounts: tuple[str, ...]
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, amounts)
        for fact in value.facts[1:]:
            await classify(
                connection,
                value,
                fact,
                kind="link_refund",
                changes={"kind": "refund", "refund_of_transaction_id": value.facts[0]},
            )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM transactions WHERE kind='refund'")
            )
            == 2
        )


async def test_refund_on_other_own_account_and_later_date(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        original = await book(connection)
        refund = await book(
            connection, ("30.00",), owner=original.owner, occurred_on="2026-10-02"
        )
        await classify(
            connection,
            refund,
            refund.facts[0],
            kind="link_refund",
            changes={"kind": "refund", "refund_of_transaction_id": original.facts[0]},
        )


@pytest.mark.parametrize(
    "bad", ["early", "sign", "original_kind", "original_change", "chain"]
)
async def test_refund_checks_original_and_both_dependency_directions(
    financial_engine: AsyncEngine, bad: str
) -> None:
    async with financial_engine.begin() as connection:
        original = await book(
            connection, occurred_on="2026-10-02" if bad == "early" else "2026-10-01"
        )
        refund = await book(
            connection, ("-30.00" if bad == "sign" else "30.00",), owner=original.owner
        )
        if bad == "original_kind":
            await classify(connection, original, original.facts[0])
        if bad in ("original_change", "chain"):
            await classify(
                connection,
                refund,
                refund.facts[0],
                kind="link_refund",
                changes={
                    "kind": "refund",
                    "refund_of_transaction_id": original.facts[0],
                },
            )
    async with rejected(financial_engine) as connection:
        if bad == "original_change":
            await classify(connection, original, original.facts[0])
        elif bad == "chain":
            next_refund = await book(connection, ("-1.00",), owner=original.owner)
            await classify(
                connection,
                next_refund,
                next_refund.facts[0],
                kind="link_refund",
                changes={"kind": "refund", "refund_of_transaction_id": refund.facts[0]},
            )
        else:
            await classify(
                connection,
                refund,
                refund.facts[0],
                kind="link_refund",
                changes={
                    "kind": "refund",
                    "refund_of_transaction_id": original.facts[0],
                },
            )


@pytest.mark.parametrize(
    "consents", [[], ["unlinked_refund"], ["unlinked_refund", "unverified_limit"]]
)
async def test_unlinked_refund_needs_both_acknowledgements(
    financial_engine: AsyncEngine, consents: list[str]
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("30.00",))

    async def command(connection: AsyncConnection) -> None:
        await classify(
            connection,
            value,
            value.facts[0],
            kind="confirm_unlinked_refund",
            changes={"kind": "refund", "category_origin": "manual"},
            consents=consents,
        )

    if len(consents) < 2:
        async with rejected(financial_engine) as connection:
            await command(connection)
    else:
        async with financial_engine.begin() as connection:
            await command(connection)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE import_attempts SET source_row_count=2 WHERE id=:attempt",
        "UPDATE import_attempts SET movement_count=2 WHERE id=:attempt",
        "UPDATE import_rows SET validation_errors='[\"bad\"]' WHERE attempt_id=:attempt",
        "UPDATE import_rows SET raw_payload='{}' WHERE attempt_id=:attempt",
        "UPDATE import_rows SET source_values=NULL WHERE attempt_id=:attempt",
        "UPDATE import_movements SET decision='pending',result_transaction_id=NULL WHERE id=:movement",
        "UPDATE import_movements SET fingerprint=NULL WHERE id=:movement",
        "UPDATE import_movements SET source_comment='changed' WHERE id=:movement",
        "UPDATE import_movements SET occurred_on='2026-10-02' WHERE id=:movement",
    ],
)
async def test_incomplete_acceptance_rolls_back_whole_book(
    financial_engine: AsyncEngine, damage: str
) -> None:
    async with rejected(financial_engine) as connection:
        await book(connection, before_accept=damage)
    async with financial_engine.connect() as connection:
        for table in (
            "transactions",
            "import_attempts",
            "financial_actions",
            "accounts",
        ):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_deferred_checks_allow_repair_of_intermediate_refund_total(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("-100.00", "60.00", "60.00"))
    async with financial_engine.begin() as connection:
        for fact in value.facts[1:]:
            await classify(
                connection,
                value,
                fact,
                kind="link_refund",
                changes={"kind": "refund", "refund_of_transaction_id": value.facts[0]},
            )
        await classify(
            connection,
            value,
            value.facts[2],
            kind="unlink_refund",
            changes={
                "kind": "unclassified",
                "refund_of_transaction_id": None,
                "refund_action_id": None,
            },
        )
        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_new_fact_revokes_every_overlap_but_not_balance_or_other_dates(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        first = await confirmation(connection, value)
        second = await confirmation(
            connection, value, start="2026-09-30", end="2026-10-02"
        )
        outside = await confirmation(
            connection, value, start="2026-10-03", end="2026-10-04"
        )
        balance = await confirmation(connection, value, balance=True)
    async with financial_engine.begin() as connection:
        added = await book(connection, owner=value.owner, account=value.account)
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM history_confirmations WHERE revocation_action_id=:id"
                ),
                {"id": added.action},
            )
            == 2
        )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT facts_version FROM accounts WHERE id=:id"),
                {"id": value.account},
            )
            == 2
        )
        for table, identity in (
            ("history_confirmations", outside),
            ("balance_confirmations", balance),
        ):
            assert (
                await connection.scalar(
                    text(f"SELECT revocation_action_id FROM {table} WHERE id=:id"),
                    {"id": identity},
                )
                is None
            )
        assert first != second


async def test_link_and_classification_preserve_completeness_and_fact_version(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        history = await confirmation(connection, value)
    async with financial_engine.begin() as connection:
        linked = await linked_book(connection, value)
        await classify(connection, value, value.facts[0])
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT facts_version FROM accounts WHERE id=:id"),
                {"id": value.account},
            )
            == 1
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT revocation_action_id FROM history_confirmations WHERE id=:id"
                ),
                {"id": history},
            )
            is None
        )
        assert (
            await connection.scalar(
                text("SELECT description FROM transactions WHERE id=:id"),
                {"id": value.facts[0]},
            )
            == ""
        )
    async with financial_engine.begin() as connection:
        await cancel(connection, linked)
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT cancelled_by_id FROM transactions WHERE id=:id"),
                {"id": value.facts[0]},
            )
            is None
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM financial_action_transactions")
            )
            == 2
        )


async def test_cancellation_revokes_completeness_and_preserves_source_history(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("-100.00", "30.00"))
        await classify(
            connection,
            value,
            value.facts[1],
            kind="link_refund",
            changes={"kind": "refund", "refund_of_transaction_id": value.facts[0]},
        )
        history = await confirmation(connection, value)
        balance = await confirmation(connection, value, balance=True)
    async with financial_engine.begin() as connection:
        cancellation = await cancel(connection, value)
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM transactions WHERE cancelled_by_id=:id"),
                {"id": cancellation},
            )
            == 2
        )
        assert await connection.scalar(text("SELECT facts_version FROM accounts")) == 4
        assert (
            await connection.scalar(
                text(
                    "SELECT revocation_action_id FROM history_confirmations WHERE id=:id"
                ),
                {"id": history},
            )
            is not None
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT revocation_action_id FROM balance_confirmations WHERE id=:id"
                ),
                {"id": balance},
            )
            is None
        )
        assert (
            await connection.scalar(text("SELECT count(*) FROM import_movements")) == 2
        )


@pytest.mark.parametrize("dependency", ["link", "refund_out", "refund_in", "transfer"])
async def test_external_relationships_block_cancellation(
    financial_engine: AsyncEngine, dependency: str
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        if dependency == "link":
            await linked_book(connection, value)
        else:
            other = await book(connection, ("100.00",), owner=value.owner)
            if dependency == "transfer":
                await transfer(connection, value, value.facts[0], other.facts[0])
            else:
                original, refund = (
                    (value, other) if dependency == "refund_out" else (other, value)
                )
                await classify(
                    connection,
                    refund,
                    refund.facts[0],
                    kind="link_refund",
                    changes={
                        "kind": "refund",
                        "refund_of_transaction_id": original.facts[0],
                    },
                )
    async with rejected(financial_engine) as connection:
        await cancel(connection, value)
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT count(*) FROM import_cancellations"))
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM transactions WHERE cancelled_by_id IS NOT NULL"
                )
            )
            == 0
        )


async def test_cancelled_external_observation_no_longer_blocks_original(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        other = await linked_book(connection, value)
    async with financial_engine.begin() as connection:
        await cancel(connection, other)
    async with financial_engine.begin() as connection:
        await cancel(connection, value)


async def test_new_attempt_only_after_cancellation_and_old_result_stays_immutable(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    async with rejected(financial_engine) as connection:
        await book(
            connection,
            owner=value.owner,
            account=value.account,
            batch=value.batch,
            number=2,
            accept=False,
        )
    async with financial_engine.begin() as connection:
        await cancel(connection, value)
        replacement = await book(
            connection,
            owner=value.owner,
            account=value.account,
            batch=value.batch,
            number=2,
        )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM import_attempts WHERE status='accepted'")
            )
            == 2
        )
        assert replacement.facts != value.facts
    async with rejected(financial_engine) as connection:
        await connection.execute(
            text("UPDATE transactions SET cancelled_by_id=NULL WHERE id=:id"),
            {"id": value.facts[0]},
        )


async def test_missing_or_forged_audit_states_roll_back(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    for mode in ("missing", "forged"):
        async with rejected(financial_engine) as connection:
            identity, _ = await action(
                connection,
                value.owner,
                "set_kind",
                result={"transaction_ids": [str(value.facts[0])]},
            )
            await connection.execute(
                text(
                    "UPDATE transactions SET kind='unclassified',last_action_id=:action WHERE id=:id"
                ),
                {"action": identity, "id": value.facts[0]},
            )
            if mode == "forged":
                await item(connection, value.owner, identity, value.facts[0], {})


@pytest.mark.parametrize("column", ["revision", "facts_version"])
async def test_runtime_cannot_supply_server_versions(
    financial_engine: AsyncEngine, column: str
) -> None:
    async with financial_engine.begin() as connection:
        await book(connection)
    async with rejected(financial_engine, "42501") as connection:
        table = "transactions" if column == "revision" else "accounts"
        await connection.execute(text(f"UPDATE {table} SET {column}={column}+1"))


@pytest.mark.parametrize("kind", ["income", "expense"])
async def test_category_direction_archive_and_rule_history(
    financial_engine: AsyncEngine, kind: str
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("100.00" if kind == "income" else "-100.00",))
        category, rule = uuid4(), uuid4()
        await connection.execute(
            text(
                "INSERT INTO categories(id,user_id,name,name_key,kind) VALUES (:id,:owner,'Synthetic','synthetic',:kind)"
            ),
            {"id": category, "owner": value.owner, "kind": kind},
        )
        await connection.execute(
            text(
                "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) VALUES (:id,:owner,:category,'x',10)"
            ),
            {"id": rule, "owner": value.owner, "category": category},
        )
        await classify(
            connection,
            value,
            value.facts[0],
            kind="set_category",
            changes={
                "category_id": category,
                "category_origin": "rule",
                "categorization_rule_id": rule,
            },
        )
    async with financial_engine.begin() as connection:
        await connection.execute(
            text("UPDATE categories SET archived_at=clock_timestamp() WHERE id=:id"),
            {"id": category},
        )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT is_active FROM categorization_rules WHERE id=:id"),
                {"id": rule},
            )
            is False
        )
        assert (
            await connection.scalar(
                text("SELECT category_id FROM transactions WHERE id=:id"),
                {"id": value.facts[0]},
            )
            == category
        )
    async with rejected(financial_engine) as connection:
        await connection.execute(
            text("UPDATE categorization_rules SET is_active=true WHERE id=:id"),
            {"id": rule},
        )
    async with rejected(financial_engine) as connection:
        added = await book(
            connection,
            ("100.00" if kind == "income" else "-100.00",),
            owner=value.owner,
        )
        await classify(
            connection,
            added,
            added.facts[0],
            kind="set_category",
            changes={"category_id": category, "category_origin": "manual"},
        )
    async with financial_engine.begin() as connection:
        await connection.execute(
            text("UPDATE categories SET archived_at=NULL WHERE id=:id"),
            {"id": category},
        )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT is_active FROM categorization_rules WHERE id=:id"),
                {"id": rule},
            )
            is False
        )


async def test_foreign_references_and_nullable_owner_paths_are_rejected_at_head(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        first = await book(connection)
        second = await book(connection)
    async with rejected(financial_engine, "23503") as connection:
        stage = await book(
            connection, owner=first.owner, account=first.account, accept=False
        )
        await connection.execute(
            text(
                "UPDATE import_movements SET account_id=NULL,decision='link',result_transaction_id=:foreign WHERE id=:id"
            ),
            {"id": stage.movements[0], "foreign": second.facts[0]},
        )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT amount FROM transactions WHERE id=:id"),
                {"id": second.facts[0]},
            )
            == -100
        )


async def test_unlinking_full_pair_requires_both_unclassified_with_new_audit(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        first = await book(connection)
        second = await book(connection, ("100.00",), owner=first.owner)
        link = await transfer(connection, first, first.facts[0], second.facts[0])

    async def unlink(connection: AsyncConnection, both: bool) -> None:
        sides = (first.facts[0], second.facts[0]) if both else (first.facts[0],)
        identity, _ = await action(
            connection,
            first.owner,
            "unlink_transfer",
            result={"transaction_ids": [str(side) for side in sides]},
        )
        await connection.execute(
            text("UPDATE transfer_links SET ended_by_action_id=:action WHERE id=:id"),
            {"id": link, "action": identity},
        )
        for side in sides:
            before = await snapshot(connection, side)
            await connection.execute(
                text(
                    "UPDATE transactions SET kind='unclassified',last_action_id=:action WHERE id=:id"
                ),
                {"id": side, "action": identity},
            )
            await item(connection, first.owner, identity, side, before)

    async with rejected(financial_engine) as connection:
        await unlink(connection, False)
    async with financial_engine.begin() as connection:
        await unlink(connection, True)
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM transactions WHERE kind='unclassified'")
            )
            == 2
        )
        assert (
            await connection.scalar(
                text("SELECT ended_by_action_id FROM transfer_links WHERE id=:id"),
                {"id": link},
            )
            is not None
        )


async def test_all_link_confirm_with_existing_transfer_blocks_cancellation(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        first = await book(connection)
        second = await book(connection, ("100.00",), owner=first.owner)
        linked = await linked_book(
            connection, first, transfer_pair=(first.facts[0], second.facts[0])
        )
    async with rejected(financial_engine) as connection:
        await cancel(connection, linked)
    async with financial_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == 2


async def test_archive_requires_fresh_targeted_evidence_and_preserves_old_commands(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    async with rejected(financial_engine) as connection:
        await connection.execute(
            text("UPDATE accounts SET archived_at=clock_timestamp() WHERE id=:id"),
            {"id": value.account},
        )
    async with financial_engine.begin() as connection:
        await archive_account(connection, value)
    async with rejected(financial_engine) as connection:
        await book(connection, owner=value.owner, account=value.account)
    async with financial_engine.begin() as connection:
        await classify(connection, value, value.facts[0])
        await confirmation(connection, value, balance=True)
        await cancel(connection, value)
    async with financial_engine.begin() as connection:
        await archive_account(connection, value, restore=True)
    async with rejected(financial_engine) as connection:
        await connection.execute(
            text(
                "UPDATE accounts SET archived_at=(SELECT CAST(details->'result'->>'archived_at' AS timestamptz) FROM financial_actions WHERE action_type='archive_account') WHERE id=:id"
            ),
            {"id": value.account},
        )


async def test_refund_inherits_archived_category_without_new_assignment(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("-100.00", "30.00"))
        category = uuid4()
        await connection.execute(
            text(
                "INSERT INTO categories(id,user_id,name,name_key,kind) VALUES (:id,:owner,'Expense','expense','expense')"
            ),
            {"id": category, "owner": value.owner},
        )
        await classify(
            connection,
            value,
            value.facts[0],
            kind="set_category",
            changes={"category_id": category, "category_origin": "manual"},
        )
    async with financial_engine.begin() as connection:
        await connection.execute(
            text("UPDATE categories SET archived_at=clock_timestamp() WHERE id=:id"),
            {"id": category},
        )
        await classify(
            connection,
            value,
            value.facts[1],
            kind="link_refund",
            changes={"kind": "refund", "refund_of_transaction_id": value.facts[0]},
        )
    async with financial_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT r.category_id,o.category_id AS inherited FROM transactions r JOIN transactions o ON o.id=r.refund_of_transaction_id WHERE r.id=:id"
                ),
                {"id": value.facts[1]},
            )
        ).one()
        assert row.category_id is None and row.inherited == category


async def test_wrong_category_direction_is_rejected(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        category = uuid4()
        await connection.execute(
            text(
                "INSERT INTO categories(id,user_id,name,name_key,kind) VALUES (:id,:owner,'Income','income','income')"
            ),
            {"id": category, "owner": value.owner},
        )
    async with rejected(financial_engine) as connection:
        await classify(
            connection,
            value,
            value.facts[0],
            kind="set_category",
            changes={"category_id": category, "category_origin": "manual"},
        )


async def test_failed_late_acceptance_restores_existing_history_and_version(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        history = await confirmation(connection, value)
    async with rejected(financial_engine) as connection:
        await book(
            connection,
            owner=value.owner,
            account=value.account,
            before_accept="UPDATE import_rows SET raw_payload='{}' WHERE attempt_id=:attempt",
        )
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT facts_version FROM accounts WHERE id=:id"),
                {"id": value.account},
            )
            == 1
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT revocation_action_id FROM history_confirmations WHERE id=:id"
                ),
                {"id": history},
            )
            is None
        )
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == 1


async def test_initial_audit_cannot_be_omitted(financial_engine: AsyncEngine) -> None:
    async with rejected(financial_engine) as connection:
        await book(connection, audit=False)


async def test_new_attempt_cannot_hide_behind_immediate_abandonment(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    async with rejected(financial_engine) as connection:
        stage = await book(
            connection,
            owner=value.owner,
            account=value.account,
            batch=value.batch,
            number=2,
            accept=False,
        )
        await connection.execute(
            text("UPDATE import_attempts SET status='abandoned' WHERE id=:id"),
            {"id": stage.attempt},
        )


async def test_saved_classification_survives_later_changes(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        linked = await linked_book(connection, value)
    async with financial_engine.begin() as connection:
        await classify(connection, value, value.facts[0])
    async with financial_engine.connect() as connection:
        for identity in (value.action, linked.action):
            result = await connection.scalar(
                text("SELECT details->'result' FROM financial_actions WHERE id=:id"),
                {"id": identity},
            )
            assert result["classifications"][0]["state"]["kind"] == "expense"
            assert result["classifications"][0]["state"]["revision"] == 1
        assert (
            await connection.scalar(
                text("SELECT kind FROM transactions WHERE id=:id"),
                {"id": value.facts[0]},
            )
            == "unclassified"
        )


async def test_orm_expires_server_revision_after_update(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    factory = create_session_factory(financial_engine)
    async with factory.begin() as session:
        fact = await session.get(Transaction, value.facts[0])
        assert fact is not None
        connection = await session.connection()
        before = await snapshot(connection, fact.id)
        identity, _ = await action(
            connection,
            value.owner,
            "set_kind",
            result={"transaction_ids": [str(fact.id)]},
        )
        fact.kind = "unclassified"
        fact.last_action_id = identity
        await session.flush()
        assert "revision" in sa_inspect(fact).expired_attributes
        await session.refresh(fact)
        assert fact.revision == 2
        await item(connection, value.owner, identity, fact.id, before)


@pytest.mark.parametrize(
    "classifications", [None, [], [{"transaction_id": str(uuid4()), "state": {}}]]
)
async def test_incomplete_saved_classifications_are_rejected(
    financial_engine: AsyncEngine, classifications: object
) -> None:
    async with rejected(financial_engine) as connection:
        await book(connection, result_overrides={"classifications": classifications})


async def test_replacing_single_transfer_with_pair_preserves_both_versions(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        first = await book(connection)
        second = await book(connection, ("100.00",), owner=first.owner)
        old_link = await transfer(connection, first, first.facts[0])
    async with financial_engine.begin() as connection:
        identity, new_link = uuid4(), uuid4()
        sides = (first.facts[0], second.facts[0])
        await action(
            connection,
            first.owner,
            "replace_transfer",
            identity=identity,
            parameters={
                "transfer_links": [
                    {
                        "id": str(new_link),
                        "outgoing_transaction_id": str(sides[0]),
                        "incoming_transaction_id": str(sides[1]),
                    }
                ]
            },
            result={"transaction_ids": [str(side) for side in sides]},
            consents=["own_accounts"],
        )
        await connection.execute(
            text("UPDATE transfer_links SET ended_by_action_id=:action WHERE id=:id"),
            {"action": identity, "id": old_link},
        )
        for side in sides:
            before = await snapshot(connection, side)
            await connection.execute(
                text(
                    "UPDATE transactions SET kind='transfer',last_action_id=:action WHERE id=:id"
                ),
                {"id": side, "action": identity},
            )
            await item(connection, first.owner, identity, side, before)
        await connection.execute(
            text(
                "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,incoming_transaction_id,confirmation_action_id) VALUES (:id,:owner,:outgoing,:incoming,:action)"
            ),
            {
                "id": new_link,
                "owner": first.owner,
                "outgoing": sides[0],
                "incoming": sides[1],
                "action": identity,
            },
        )
    async with financial_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM transfer_links")) == 2
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM transfer_links WHERE ended_by_action_id IS NULL"
                )
            )
            == 1
        )


async def test_cancelled_result_cannot_hide_new_link_to_already_cancelled_fact(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        await cancel(connection, value)
    async with rejected(financial_engine) as connection:
        linked = await linked_book(connection, value)
        await cancel(connection, linked)


async def test_valid_link_history_can_be_accepted_then_cancelled_in_one_transaction(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        linked = await linked_book(connection, value)
        await cancel(connection, linked)
        await cancel(connection, value)
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT count(*) FROM import_cancellations"))
            == 2
        )


async def test_refunds_cannot_exceed_original_in_one_command(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("-100.00", "60.00", "60.00"))
    with pytest.raises(DBAPIError) as failure:
        async with financial_engine.begin() as connection:
            for fact in value.facts[1:]:
                await classify(
                    connection,
                    value,
                    fact,
                    kind="link_refund",
                    changes={
                        "kind": "refund",
                        "refund_of_transaction_id": value.facts[0],
                    },
                    consents=[],
                )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"
