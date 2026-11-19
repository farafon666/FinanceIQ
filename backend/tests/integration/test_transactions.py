"""Transaction storage, provenance cycles and immutable history on PostgreSQL."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financeiq.db.financial_actions import FinancialAction, FinancialActionTransaction
from financeiq.db.session import create_session_factory
from financeiq.modules.transactions.models import Transaction, TransferLink
from tests.integration.test_imports import Graph, seed
from tests.integration.test_imports import imports_engine as imports_engine
from tests.integration.test_imports import imports_migrator as imports_migrator

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def create_fact(
    connection: AsyncConnection,
    graph: Graph | None = None,
    *,
    overrides: dict[str, str] | None = None,
) -> tuple[Graph, UUID]:
    if graph is None:
        graph = await seed(connection)
    else:
        graph = replace(graph, row=uuid4(), movement=uuid4(), action=uuid4())
        parameters = graph.parameters()
        await connection.execute(
            text(
                "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number,source_values,raw_payload) "
                "SELECT :row,:owner,:attempt,'Расходы',count(*)+3,'{}','{}' FROM import_rows WHERE attempt_id=:attempt"
            ),
            parameters,
        )
        await connection.execute(
            text(
                "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id,occurred_on,amount,source_comment) "
                "VALUES (:movement,:owner,:attempt,:row,:mapping,'single',:account,'2026-10-01',-12.30,' Кафе Ё ')"
            ),
            parameters,
        )
    transaction = uuid4()
    parameters = graph.parameters() | {"transaction": transaction}
    await connection.execute(
        text(
            "INSERT INTO financial_actions(id,user_id,request_id,action_type,"
            "plan_version,plan_digest,source_attempt_id,details) VALUES "
            "(:action,:owner,:action,'import_confirm',1,"
            "decode(repeat('ef',32),'hex'),:attempt,'{}')"
        ),
        parameters,
    )
    # Both result FKs must defer: the fact does not exist at this point.
    await connection.execute(
        text(
            "UPDATE import_movements SET decision='insert',result_transaction_id=:transaction WHERE id=:movement"
        ),
        parameters,
    )
    values = {
        "id": ":transaction",
        "user_id": ":owner",
        "account_id": ":account",
        "source_movement_id": ":movement",
        "occurred_on": "'2026-10-01'",
        "amount": "-12.30",
        "description": "' Кафе Ё '",
        "last_action_id": ":action",
    } | (overrides or {})
    await connection.execute(
        text(
            f"INSERT INTO transactions({','.join(values)}) VALUES ({','.join(values.values())})"
        ),
        parameters,
    )
    return graph, transaction


async def test_orm_roundtrip_and_provenance_cycle(imports_engine: AsyncEngine) -> None:
    async with imports_engine.begin() as connection:
        graph, transaction = await create_fact(connection)
    factory = create_session_factory(imports_engine)
    async with factory.begin() as session:
        fact = await session.get(Transaction, transaction)
        assert fact is not None
        assert fact.amount == Decimal("-12.30")
        assert fact.occurred_on == date(2026, 10, 1)
        assert fact.description == " Кафе Ё "
        assert fact.kind == "unclassified" and fact.category_origin == "none"
        assert fact.revision == 1 and fact.cancelled_by_id is None
        assert fact.category_id is None and fact.refund_of_transaction_id is None
        assert fact.source_movement_id == graph.movement
        assert fact.created_at.tzinfo is not None
        item = FinancialActionTransaction(
            user_id=graph.owner,
            action_id=graph.action,
            transaction_id=transaction,
            before_state={},
            after_state={"revision": 1, "kind": "unclassified"},
        )
        link = TransferLink(
            user_id=graph.owner,
            outgoing_transaction_id=transaction,
            confirmation_action_id=graph.action,
        )
        session.add_all([item, link])
        await session.flush()
        item_id, link_id = item.id, link.id
    async with factory.begin() as session:
        loaded_item = await session.get(FinancialActionTransaction, item_id)
        loaded_link = await session.get(TransferLink, link_id)
        assert loaded_item is not None and loaded_item.before_state == {}
        assert loaded_item.after_state == {"revision": 1, "kind": "unclassified"}
        assert loaded_link is not None and loaded_link.incoming_transaction_id is None
        assert loaded_link.ended_by_action_id is None
        assert loaded_link.cancelled_by_id is None


async def test_orm_can_insert_fact_with_server_defaults(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph = await seed(connection)
    factory = create_session_factory(imports_engine)
    async with factory.begin() as session:
        action = FinancialAction(
            id=graph.action,
            user_id=graph.owner,
            request_id=uuid4(),
            action_type="import_confirm",
            plan_version=1,
            plan_digest=bytes.fromhex("ab" * 32),
            source_attempt_id=graph.attempt,
            details={},
        )
        session.add(action)
        await session.flush()
        fact = Transaction(
            user_id=graph.owner,
            account_id=graph.account,
            source_movement_id=graph.movement,
            occurred_on=date(2026, 10, 1),
            amount=Decimal("-12.30"),
            description=" Кафе Ё ",
            last_action_id=graph.action,
        )
        session.add(fact)
        await session.flush()
        assert fact.revision == 1 and fact.cancelled_by_id is None
        assert fact.kind == "unclassified" and fact.category_origin == "none"
        await session.execute(
            text(
                "UPDATE import_movements SET decision='insert',result_transaction_id=:fact WHERE id=:movement"
            ),
            {"fact": fact.id, "movement": graph.movement},
        )


async def test_accepted_result_cannot_be_rewritten(imports_engine: AsyncEngine) -> None:
    async with imports_engine.begin() as connection:
        graph, _ = await create_fact(connection)
        await add_cancellation(connection, graph)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE import_movements SET result_transaction_id=NULL WHERE id=:movement"
                ),
                graph.parameters(),
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


async def test_full_transfer_stores_real_sides_on_different_dates(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph, outgoing = await create_fact(
            connection, overrides={"amount": "-1000", "kind": "'transfer'"}
        )
        account, mapping = uuid4(), uuid4()
        await connection.execute(
            text(
                "INSERT INTO accounts(id,user_id,name,currency_code) VALUES (:id,:owner,'Other own','RUB')"
            ),
            {"id": account, "owner": graph.owner},
        )
        await connection.execute(
            text(
                "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name,account_id) VALUES (:id,:owner,:attempt,'Other own',:account)"
            ),
            graph.parameters() | {"id": mapping, "account": account},
        )
        _, incoming = await create_fact(
            connection,
            replace(graph, account=account, mapping=mapping),
            overrides={
                "amount": "1000",
                "kind": "'transfer'",
                "occurred_on": "'2026-10-02'",
            },
        )
        await connection.execute(
            text(
                "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,incoming_transaction_id,confirmation_action_id) VALUES (:id,:owner,:outgoing,:incoming,:action)"
            ),
            graph.parameters()
            | {"id": uuid4(), "outgoing": outgoing, "incoming": incoming},
        )
    async with imports_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == 2
        assert await connection.scalar(
            text("SELECT sum(amount) FROM transactions")
        ) == Decimal("0")


@pytest.mark.parametrize("origin", ["none", "manual", "rule"])
async def test_valid_direct_category_origins(
    imports_engine: AsyncEngine, origin: str
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        category, rule = await add_category(connection, graph.owner)
        await connection.execute(
            text(
                "UPDATE transactions SET kind='expense',category_origin=:origin,category_id=:category,categorization_rule_id=:rule,revision=2 WHERE id=:id"
            ),
            {
                "id": fact,
                "origin": origin,
                "category": category if origin != "none" else None,
                "rule": rule if origin == "rule" else None,
            },
        )
    async with imports_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT category_origin FROM transactions WHERE id=:id"),
                {"id": fact},
            )
            == origin
        )


@pytest.mark.parametrize(
    ("overrides", "state"),
    [
        ({"amount": "0"}, "23514"),
        ({"amount": "-0.00"}, "23514"),
        ({"amount": "'NaN'"}, "23514"),
        ({"amount": "'Infinity'"}, "22003"),
        ({"amount": "'-Infinity'"}, "22003"),
        ({"amount": "10000000000000000"}, "22003"),
        ({"amount": "NULL"}, "23502"),
        ({"occurred_on": "'infinity'"}, "23514"),
        ({"occurred_on": "'-infinity'"}, "23514"),
        ({"occurred_on": "NULL"}, "23502"),
        ({"created_at": "'infinity'"}, "23514"),
        ({"description": "repeat('я',2001)"}, "23514"),
        ({"description": "NULL"}, "23502"),
        ({"kind": "'other'"}, "23514"),
        ({"kind": "'Income'"}, "23514"),
        ({"kind": "'income'"}, "23514"),
        ({"kind": "'expense'", "amount": "1"}, "23514"),
        ({"revision": "0"}, "23514"),
        ({"category_origin": "'other'"}, "23514"),
        ({"category_id": ":transaction"}, "23514"),
        ({"categorization_rule_id": ":transaction"}, "23514"),
        ({"category_origin": "'rule'"}, "23514"),
        ({"category_origin": "'rule'", "category_id": ":transaction"}, "23514"),
        (
            {"category_origin": "'rule'", "categorization_rule_id": ":transaction"},
            "23514",
        ),
        (
            {"category_origin": "'manual'", "categorization_rule_id": ":transaction"},
            "23514",
        ),
        ({"category_origin": "'manual'"}, "23514"),
        ({"kind": "'transfer'", "category_origin": "'manual'"}, "23514"),
        ({"kind": "'refund'"}, "23514"),
        ({"refund_action_id": ":action"}, "23514"),
        ({"refund_of_transaction_id": ":transaction"}, "23514"),
        ({"kind": "'refund'", "refund_action_id": ":action"}, "23514"),
        (
            {
                "kind": "'refund'",
                "refund_action_id": ":action",
                "refund_of_transaction_id": ":transaction",
            },
            "23514",
        ),
        (
            {
                "kind": "'refund'",
                "refund_action_id": ":action",
                "category_origin": "'rule'",
                "category_id": ":transaction",
                "categorization_rule_id": ":transaction",
            },
            "23514",
        ),
    ],
)
async def test_fact_local_constraints(
    imports_migrator: AsyncEngine,
    imports_engine: AsyncEngine,
    overrides: dict[str, str],
    state: str,
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_migrator.begin() as connection:
            await create_fact(connection, overrides=overrides)
    assert getattr(failure.value.orig, "sqlstate", None) == state
    async with imports_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == 0


@pytest.mark.parametrize(
    "amount", ["-9999999999999999.99", "9999999999999999.99", "0.01", "-0.01"]
)
async def test_money_boundaries(imports_engine: AsyncEngine, amount: str) -> None:
    async with imports_engine.begin() as connection:
        _, fact = await create_fact(
            connection, overrides={"amount": amount, "description": "repeat('я',2000)"}
        )
    async with imports_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT amount,char_length(description) FROM transactions WHERE id=:id"
                ),
                {"id": fact},
            )
        ).one()
        assert row[0] == Decimal(amount) and row[1] == 2000


async def test_decimal_aggregates_and_repeated_purchases(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph, _ = await create_fact(connection, overrides={"amount": "0.10"})
        await create_fact(connection, graph, overrides={"amount": "0.20"})
        assert await connection.scalar(
            text("SELECT sum(amount) FROM transactions")
        ) == Decimal("0.30")
        graph2, _ = await create_fact(
            connection, overrides={"amount": "9999999999999999.99"}
        )
        await create_fact(
            connection, graph2, overrides={"amount": "9999999999999999.99"}
        )
        assert await connection.scalar(
            text("SELECT sum(amount) FROM transactions WHERE user_id=:owner"),
            graph2.parameters(),
        ) == Decimal("19999999999999999.98")


async def test_missing_result_fails_at_commit_and_rolls_back(
    imports_engine: AsyncEngine,
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            graph = await seed(connection)
            await connection.execute(
                text(
                    "UPDATE import_movements SET decision='link',result_transaction_id=:missing WHERE id=:movement"
                ),
                graph.parameters() | {"missing": uuid4()},
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM import_movements"))
                == 1
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"
    async with imports_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM users")) == 0


@pytest.mark.parametrize("decision", ["pending", "link"])
async def test_result_decision_check(
    imports_engine: AsyncEngine, decision: str
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE import_movements SET decision=:decision,result_transaction_id=:result WHERE id=:movement"
                ),
                graph.parameters()
                | {
                    "decision": decision,
                    "result": fact if decision == "pending" else None,
                },
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


async def test_source_and_attempt_result_uniqueness(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        other_graph, _ = await create_fact(connection, graph)
    for statement, parameters in (
        (
            "INSERT INTO transactions(id,user_id,account_id,source_movement_id,occurred_on,amount,description,last_action_id) SELECT :id,user_id,account_id,source_movement_id,occurred_on,amount,description,last_action_id FROM transactions WHERE id=:fact",
            {"id": uuid4(), "fact": fact},
        ),
        (
            "UPDATE import_movements SET result_transaction_id=:fact WHERE id=:movement",
            {"fact": fact, "movement": other_graph.movement},
        ),
    ):
        with pytest.raises(DBAPIError) as failure:
            async with imports_engine.begin() as connection:
                await connection.execute(text(statement), parameters)
        assert getattr(failure.value.orig, "sqlstate", None) == "23505"


async def add_cancellation(connection: AsyncConnection, graph: Graph) -> UUID:
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='preview',excel_date_system='1900',preview_digest=decode(repeat('ef',32),'hex'),source_row_count=1,movement_count=1 WHERE id=:attempt"
        ),
        graph.parameters(),
    )
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='accepted',accepted_action_id=:action WHERE id=:attempt"
        ),
        graph.parameters(),
    )
    cancellation = uuid4()
    await connection.execute(
        text(
            "INSERT INTO import_cancellations(id,user_id,attempt_id,action_id) VALUES (:id,:owner,:attempt,:action)"
        ),
        graph.parameters() | {"id": cancellation},
    )
    return cancellation


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transactions SET amount=-20 WHERE id=:fact",
        "UPDATE transactions SET occurred_on='2026-10-02' WHERE id=:fact",
        "UPDATE transactions SET description='Changed' WHERE id=:fact",
        "UPDATE transactions SET user_id=:id WHERE id=:fact",
        "UPDATE transactions SET account_id=:id WHERE id=:fact",
        "UPDATE transactions SET source_movement_id=:id WHERE id=:fact",
        "UPDATE transactions SET id=:id WHERE id=:fact",
        "UPDATE transactions SET created_at=created_at+interval '1 second' WHERE id=:fact",
        "DELETE FROM transactions WHERE id=:fact",
    ],
)
async def test_fact_source_guard_even_for_owner(
    imports_engine: AsyncEngine,
    imports_migrator: AsyncEngine,
    statement: str,
) -> None:
    async with imports_engine.begin() as connection:
        _, fact = await create_fact(connection)
    with pytest.raises(DBAPIError) as failure:
        async with imports_migrator.begin() as connection:
            await connection.execute(text(statement), {"fact": fact, "id": uuid4()})
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


async def test_cancellation_is_one_way_and_preserves_fact(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        cancellation = await add_cancellation(connection, graph)
    with pytest.raises(DBAPIError):
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE transactions SET cancelled_by_id=:cancel,kind='expense' WHERE id=:id"
                ),
                {"id": fact, "cancel": cancellation},
            )
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE transactions SET cancelled_by_id=:cancel,revision=2 WHERE id=:id"
            ),
            {"id": fact, "cancel": cancellation},
        )
    for assignment in (
        "cancelled_by_id=NULL",
        "cancelled_by_id=id",
        "kind='expense'",
        "revision=3",
    ):
        with pytest.raises(DBAPIError) as failure:
            async with imports_engine.begin() as connection:
                await connection.execute(
                    text(f"UPDATE transactions SET {assignment} WHERE id=:id"),
                    {"id": fact},
                )
        assert getattr(failure.value.orig, "sqlstate", None) == "23514"
    async with imports_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT amount,kind,cancelled_by_id FROM transactions WHERE id=:id"
                ),
                {"id": fact},
            )
        ).one()
        assert tuple(row) == (Decimal("-12.30"), "unclassified", cancellation)


async def test_refund_representation_and_different_own_account(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph, original = await create_fact(connection, overrides={"kind": "'expense'"})
        account = uuid4()
        mapping = uuid4()
        await connection.execute(
            text(
                "INSERT INTO accounts(id,user_id,name,currency_code) VALUES (:id,:owner,'Other own account','RUB')"
            ),
            {"id": account, "owner": graph.owner},
        )
        await connection.execute(
            text(
                "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name,account_id) VALUES (:id,:owner,:attempt,'Other',:account)"
            ),
            graph.parameters() | {"id": mapping, "account": account},
        )
        _, linked = await create_fact(
            connection,
            replace(graph, account=account, mapping=mapping),
            overrides={
                "amount": "3.00",
                "kind": "'refund'",
                "refund_of_transaction_id": f"'{original}'::uuid",
                "refund_action_id": ":action",
            },
        )
        _, unlinked = await create_fact(
            connection,
            graph,
            overrides={
                "amount": "3.00",
                "kind": "'refund'",
                "category_origin": "'manual'",
                "refund_action_id": ":action",
            },
        )
    factory = create_session_factory(imports_engine)
    async with factory.begin() as session:
        refund = await session.get(Transaction, linked)
        separate = await session.get(Transaction, unlinked)
        assert refund is not None and refund.account_id == account
        assert (
            refund.refund_of_transaction_id == original and refund.category_id is None
        )
        assert separate is not None and separate.category_origin == "manual"
        assert (
            separate.refund_of_transaction_id is None
            and separate.refund_action_id is not None
        )


async def add_category(connection: AsyncConnection, owner: UUID) -> tuple[UUID, UUID]:
    category, rule = uuid4(), uuid4()
    await connection.execute(
        text(
            "INSERT INTO categories(id,user_id,name,name_key,kind) VALUES (:id,:owner,'Synthetic','synthetic','expense')"
        ),
        {"id": category, "owner": owner},
    )
    await connection.execute(
        text(
            "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) VALUES (:id,:owner,:category,'Synthetic',10)"
        ),
        {"id": rule, "owner": owner, "category": category},
    )
    return category, rule


@pytest.mark.parametrize(
    "reference",
    [
        "account",
        "source",
        "last_action",
        "category",
        "rule",
        "refund_original",
        "refund_action",
        "cancellation",
        "result",
        "nullable_result",
        "transfer_outgoing",
        "transfer_incoming",
        "transfer_confirmation",
        "transfer_end",
        "transfer_cancellation",
        "audit_action",
        "audit_fact",
    ],
)
async def test_foreign_owner_references_rejected(
    imports_engine: AsyncEngine, reference: str
) -> None:
    async with imports_engine.begin() as connection:
        own, fact = await create_fact(connection)
        foreign, foreign_fact = await create_fact(connection)
        own_category, _ = await add_category(connection, own.owner)
        category, rule = await add_category(connection, foreign.owner)
        cancellation = await add_cancellation(connection, foreign)
    parameters = own.parameters() | {
        "fact": fact,
        "foreign_fact": foreign_fact,
        "foreign_action": foreign.action,
        "foreign_cancel": cancellation,
        "id": uuid4(),
    }
    assignments = {
        "last_action": f"last_action_id='{foreign.action}'",
        "category": f"kind='expense',category_origin='manual',category_id='{category}'",
        "rule": f"kind='expense',category_origin='rule',category_id='{own_category}',categorization_rule_id='{rule}'",
        "refund_original": f"kind='refund',refund_action_id=last_action_id,refund_of_transaction_id='{foreign_fact}'",
        "refund_action": f"kind='refund',category_origin='manual',refund_action_id='{foreign.action}'",
        "cancellation": f"cancelled_by_id='{cancellation}'",
    }
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            if reference in ("account", "source"):
                field = "account_id" if reference == "account" else "source_movement_id"
                foreign_id = (
                    foreign.account if reference == "account" else foreign.movement
                )
                await create_fact(
                    connection, overrides={field: f"'{foreign_id}'::uuid"}
                )
            elif reference in assignments:
                await connection.execute(
                    text(
                        f"UPDATE transactions SET {assignments[reference]} WHERE id=:fact"
                    ),
                    parameters,
                )
            elif reference in ("result", "nullable_result"):
                if reference == "nullable_result":
                    staging = await seed(connection)
                    parameters = parameters | {"movement": staging.movement}
                extra = ",account_id=NULL" if reference == "nullable_result" else ""
                await connection.execute(
                    text(
                        f"UPDATE import_movements SET decision='link',result_transaction_id=:foreign_fact{extra} WHERE id=:movement"
                    ),
                    parameters,
                )
            elif reference.startswith("transfer_"):
                columns = {
                    "id": ":id",
                    "user_id": ":owner",
                    "outgoing_transaction_id": ":fact",
                    "confirmation_action_id": ":action",
                }
                field, expression = {
                    "transfer_outgoing": ("outgoing_transaction_id", ":foreign_fact"),
                    "transfer_incoming": ("incoming_transaction_id", ":foreign_fact"),
                    "transfer_confirmation": (
                        "confirmation_action_id",
                        ":foreign_action",
                    ),
                    "transfer_end": ("ended_by_action_id", ":foreign_action"),
                    "transfer_cancellation": ("cancelled_by_id", ":foreign_cancel"),
                }[reference]
                if reference in ("transfer_end", "transfer_cancellation"):
                    await connection.execute(
                        text(
                            "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,confirmation_action_id) VALUES (:id,:owner,:fact,:action)"
                        ),
                        parameters,
                    )
                    await connection.execute(
                        text(
                            f"UPDATE transfer_links SET {field}={expression} WHERE id=:id"
                        ),
                        parameters,
                    )
                else:
                    columns[field] = expression
                    await connection.execute(
                        text(
                            f"INSERT INTO transfer_links({','.join(columns)}) VALUES ({','.join(columns.values())})"
                        ),
                        parameters,
                    )
            else:
                action = ":foreign_action" if reference == "audit_action" else ":action"
                target = ":foreign_fact" if reference == "audit_fact" else ":fact"
                await connection.execute(
                    text(
                        f"INSERT INTO financial_action_transactions(id,user_id,action_id,transaction_id,before_state,after_state) VALUES (:id,:owner,{action},{target},'{{}}','{{}}')"
                    ),
                    parameters,
                )
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"


async def test_source_and_result_account_scope(imports_engine: AsyncEngine) -> None:
    async with imports_engine.begin() as connection:
        graph, first = await create_fact(connection)
        account, mapping = uuid4(), uuid4()
        await connection.execute(
            text(
                "INSERT INTO accounts(id,user_id,name,currency_code) VALUES (:account,:owner,'Other','RUB')"
            ),
            graph.parameters() | {"account": account},
        )
        await connection.execute(
            text(
                "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name,account_id) VALUES (:mapping,:owner,:attempt,'Other',:account)"
            ),
            graph.parameters() | {"mapping": mapping, "account": account},
        )
        other_graph, _ = await create_fact(
            connection, replace(graph, account=account, mapping=mapping)
        )
        await connection.execute(
            text(
                "UPDATE import_movements SET result_transaction_id=NULL WHERE id=:movement"
            ),
            graph.parameters(),
        )
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await create_fact(
                connection, graph, overrides={"account_id": f"'{account}'::uuid"}
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE import_movements SET result_transaction_id=:first WHERE id=:movement"
                ),
                {"first": first, "movement": other_graph.movement},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"


async def with_other_account(connection: AsyncConnection, graph: Graph) -> Graph:
    account, mapping = uuid4(), uuid4()
    await connection.execute(
        text(
            "INSERT INTO accounts(id,user_id,name,currency_code) "
            "VALUES (:id,:owner,'Other own account','RUB')"
        ),
        graph.parameters() | {"id": account},
    )
    await connection.execute(
        text(
            "INSERT INTO import_account_mappings(id,user_id,attempt_id,"
            "source_account_name,account_id) VALUES "
            "(:id,:owner,:attempt,'Other own account',:account)"
        ),
        graph.parameters() | {"id": mapping, "account": account},
    )
    return replace(graph, account=account, mapping=mapping)


async def add_observation(connection: AsyncConnection, graph: Graph) -> Graph:
    """A different book can link an existing fact without replacing its source."""
    observation = replace(
        graph,
        batch=uuid4(),
        attempt=uuid4(),
        mapping=uuid4(),
        row=uuid4(),
        movement=uuid4(),
        action=uuid4(),
    )
    for statement in (
        "INSERT INTO import_batches(id,user_id,source_code,format_code,file_sha256,byte_size) "
        "VALUES (:batch,:owner,'money_manager','money_manager_xlsx_v1',"
        "decode(repeat('de',32),'hex'),1024)",
        "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,"
        "received_at,expires_at,parser_version) VALUES "
        "(:attempt,:owner,:batch,1,'2026-11-03+00','2026-11-04+00','synthetic_v1')",
        "INSERT INTO import_account_mappings(id,user_id,attempt_id,"
        "source_account_name,account_id) VALUES "
        "(:mapping,:owner,:attempt,'Observation',:account)",
        "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number) "
        "VALUES (:row,:owner,:attempt,'Расходы',3)",
        "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id) "
        "VALUES (:movement,:owner,:attempt,:row,:mapping,'single',:account)",
    ):
        await connection.execute(text(statement), observation.parameters())
    return observation


@pytest.mark.parametrize(
    "target", ["foreign", "foreign_null_account", "other_own_account"]
)
async def test_invalid_result_fails_only_on_commit_and_rolls_back(
    imports_engine: AsyncEngine, target: str
) -> None:
    async with imports_engine.begin() as connection:
        own, own_fact = await create_fact(connection)
        other, other_fact = await create_fact(connection)
        result = other_fact
        if target == "other_own_account":
            _, result = await create_fact(
                connection, await with_other_account(connection, own)
            )
        observation = await add_observation(connection, own)
    audit = uuid4()
    parameters = observation.parameters() | {
        "action": own.action,
        "result": result,
        "audit": audit,
        "fact": own_fact,
    }
    executed = False
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text("UPDATE accounts SET name='Uncommitted change' WHERE id=:account"),
                parameters,
            )
            await connection.execute(
                text(
                    "INSERT INTO financial_action_transactions(id,user_id,action_id,"
                    "transaction_id,before_state,after_state) "
                    "VALUES (:audit,:owner,:action,:fact,'{}','{}')"
                ),
                parameters,
            )
            nullable = ",account_id=NULL" if target == "foreign_null_account" else ""
            await connection.execute(
                text(
                    "UPDATE import_movements SET decision='link',"
                    f"result_transaction_id=:result{nullable} WHERE id=:movement"
                ),
                parameters,
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT result_transaction_id FROM import_movements WHERE id=:movement"
                    ),
                    parameters,
                )
                == result
            )
            executed = True
    assert executed, "The invalid reference must reach commit before failing"
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"

    # A separate checkout observes durable state, not the failed transaction's
    # snapshot. Both owners' facts and the second owner's graph remain intact.
    async with imports_engine.connect() as connection:
        for graph, fact in ((own, own_fact), (other, other_fact)):
            row = (
                await connection.execute(
                    text(
                        "SELECT account_id,decision,result_transaction_id "
                        "FROM import_movements WHERE id=:movement"
                    ),
                    graph.parameters(),
                )
            ).one()
            assert tuple(row) == (graph.account, "insert", fact)
            assert (
                await connection.scalar(
                    text("SELECT name FROM accounts WHERE id=:account"),
                    graph.parameters(),
                )
                == "Synthetic"
            )
            assert (
                await connection.scalar(
                    text("SELECT source_movement_id FROM transactions WHERE id=:fact"),
                    {"fact": fact},
                )
                == graph.movement
            )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM financial_action_transactions WHERE id=:audit"
                ),
                parameters,
            )
            == 0
        )
        row = (
            await connection.execute(
                text(
                    "SELECT account_id,decision,result_transaction_id "
                    "FROM import_movements WHERE id=:movement"
                ),
                parameters,
            )
        ).one()
        assert tuple(row) == (own.account, "pending", None)
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE import_movements SET decision='link',result_transaction_id=:fact "
                "WHERE id=:movement"
            ),
            parameters,
        )
    async with imports_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT decision FROM import_movements WHERE id=:movement"),
                parameters,
            )
            == "link"
        )


async def test_nullable_account_keeps_valid_own_result(
    imports_engine: AsyncEngine,
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        observation = await add_observation(connection, graph)
        await connection.execute(
            text(
                "UPDATE import_movements SET account_id=NULL,decision='link',"
                "result_transaction_id=:fact WHERE id=:movement"
            ),
            observation.parameters() | {"fact": fact},
        )
    async with imports_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT m.account_id,m.result_transaction_id,a.account_id "
                    "FROM import_movements m JOIN import_account_mappings a "
                    "ON a.user_id=m.user_id AND a.attempt_id=m.attempt_id "
                    "AND a.id=m.mapping_id WHERE m.id=:movement"
                ),
                observation.parameters(),
            )
        ).one()
        assert tuple(row) == (None, fact, graph.account)


@pytest.mark.parametrize("sides", ["both", "outgoing", "incoming"])
async def test_transfer_can_reference_different_own_accounts(
    imports_engine: AsyncEngine, sides: str
) -> None:
    async with imports_engine.begin() as connection:
        graph, first = await create_fact(
            connection,
            overrides={
                "kind": "'transfer'",
                "amount": "12.30" if sides == "incoming" else "-12.30",
            },
        )
        outgoing = first if sides != "incoming" else None
        incoming = first if sides == "incoming" else None
        if sides == "both":
            incoming_graph, incoming = await create_fact(
                connection,
                await with_other_account(connection, graph),
                overrides={"kind": "'transfer'", "amount": "12.30"},
            )
            assert graph.account != incoming_graph.account
        link = uuid4()
        await connection.execute(
            text(
                "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,"
                "incoming_transaction_id,confirmation_action_id) "
                "VALUES (:id,:owner,:outgoing,:incoming,:action)"
            ),
            graph.parameters()
            | {
                "id": link,
                "outgoing": outgoing,
                "incoming": incoming,
            },
        )
    async with imports_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT user_id,outgoing_transaction_id,incoming_transaction_id "
                    "FROM transfer_links WHERE id=:id"
                ),
                {"id": link},
            )
        ).one()
        assert tuple(row) == (graph.owner, outgoing, incoming)
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == (
            2 if sides == "both" else 1
        )


@pytest.mark.parametrize("role", ["outgoing", "incoming"])
@pytest.mark.parametrize("ending", ["ended_by_action_id", "cancelled_by_id"])
async def test_transfer_history_releases_active_role(
    imports_engine: AsyncEngine, imports_migrator: AsyncEngine, role: str, ending: str
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        cancellation = await add_cancellation(connection, graph)
        old = uuid4()
        parameters = graph.parameters() | {"fact": fact, "id": old}
        insert = f"INSERT INTO transfer_links(id,user_id,{role}_transaction_id,confirmation_action_id) VALUES (:id,:owner,:fact,:action)"
        await connection.execute(text(insert), parameters)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(text(insert), parameters | {"id": uuid4()})
    assert getattr(failure.value.orig, "sqlstate", None) == "23505"
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(f"UPDATE transfer_links SET {ending}=:ending WHERE id=:id"),
            {
                "ending": graph.action
                if ending == "ended_by_action_id"
                else cancellation,
                "id": old,
            },
        )
        await connection.execute(text(insert), parameters | {"id": uuid4()})
        assert await connection.scalar(text("SELECT count(*) FROM transfer_links")) == 2
    for statement in (
        f"UPDATE transfer_links SET {ending}=NULL WHERE id=:id",
        "UPDATE transfer_links SET outgoing_transaction_id=:id WHERE id=:id",
        "UPDATE transfer_links SET confirmation_action_id=:id WHERE id=:id",
        "DELETE FROM transfer_links WHERE id=:id",
    ):
        with pytest.raises(DBAPIError) as failure:
            async with imports_migrator.begin() as connection:
                await connection.execute(text(statement), parameters)
        assert getattr(failure.value.orig, "sqlstate", None) == "23514"


@pytest.mark.parametrize(
    "values",
    [
        "NULL,NULL,:action,NULL,NULL",
        ":fact,:fact,:action,NULL,NULL",
        ":fact,NULL,:action,:action,:cancel",
    ],
)
async def test_transfer_local_checks(
    imports_engine: AsyncEngine, imports_migrator: AsyncEngine, values: str
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        cancellation = await add_cancellation(connection, graph)
    with pytest.raises(DBAPIError) as failure:
        async with imports_migrator.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,incoming_transaction_id,confirmation_action_id,ended_by_action_id,cancelled_by_id) VALUES (:id,:owner,{values})"
                ),
                graph.parameters()
                | {"id": uuid4(), "fact": fact, "cancel": cancellation},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


@pytest.mark.parametrize("field", ["before_state", "after_state"])
@pytest.mark.parametrize(
    ("value", "state"), [("'[]'", "23514"), ("'null'", "23514"), ("NULL", "23502")]
)
async def test_audit_json_objects(
    imports_engine: AsyncEngine, field: str, value: str, state: str
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
    before, after = (value, "'{}'") if field == "before_state" else ("'{}'", value)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO financial_action_transactions(id,user_id,action_id,transaction_id,before_state,after_state) VALUES (:id,:owner,:action,:fact,{before},{after})"
                ),
                graph.parameters() | {"id": uuid4(), "fact": fact},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == state


async def test_audit_uniqueness_and_immutability(
    imports_engine: AsyncEngine, imports_migrator: AsyncEngine
) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
        parameters = graph.parameters() | {"id": uuid4(), "fact": fact}
        insert = "INSERT INTO financial_action_transactions(id,user_id,action_id,transaction_id,before_state,after_state) VALUES (:id,:owner,:action,:fact,'{}','{}')"
        await connection.execute(text(insert), parameters)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(text(insert), parameters | {"id": uuid4()})
    assert getattr(failure.value.orig, "sqlstate", None) == "23505"
    for statement in (
        "UPDATE financial_action_transactions SET after_state='{}' WHERE id=:id",
        "DELETE FROM financial_action_transactions WHERE id=:id",
    ):
        with pytest.raises(DBAPIError) as failure:
            async with imports_migrator.begin() as connection:
                await connection.execute(text(statement), parameters)
        assert getattr(failure.value.orig, "sqlstate", None) == "23514"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transactions SET amount=1",
        "UPDATE transactions SET id=:id",
        "UPDATE transactions SET user_id=:id",
        "UPDATE transactions SET created_at=now()",
        "UPDATE transactions SET account_id=:id",
        "UPDATE transactions SET source_movement_id=:id",
        "DELETE FROM transactions",
        "DELETE FROM transfer_links",
        "DELETE FROM financial_action_transactions",
        "UPDATE financial_action_transactions SET before_state='{}'",
        "UPDATE transfer_links SET incoming_transaction_id=:id",
        "TRUNCATE transactions",
        "ALTER TABLE transactions DISABLE TRIGGER guard_transaction_history",
        "INSERT INTO transactions(id,user_id,account_id,source_movement_id,occurred_on,amount,description,last_action_id,revision) VALUES (:id,:owner,:account,:movement,'2026-10-01',1,'',:action,2)",
        "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,confirmation_action_id,ended_by_action_id) VALUES (:id,:owner,:fact,:action,:action)",
    ],
)
async def test_runtime_acl(imports_engine: AsyncEngine, statement: str) -> None:
    async with imports_engine.begin() as connection:
        graph, fact = await create_fact(connection)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(statement), graph.parameters() | {"id": uuid4(), "fact": fact}
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "42501"


@pytest.mark.parametrize(
    "action_type",
    [
        "set_kind",
        "set_category",
        "confirm_transfer",
        "replace_transfer",
        "unlink_transfer",
        "link_refund",
        "unlink_refund",
        "confirm_unlinked_refund",
    ],
)
async def test_new_action_types(imports_engine: AsyncEngine, action_type: str) -> None:
    async with imports_engine.begin() as connection:
        graph, _ = await create_fact(connection)
        await connection.execute(
            text(
                "INSERT INTO financial_actions(id,user_id,request_id,action_type,plan_version,plan_digest,details) VALUES (:id,:owner,:id,:type,1,decode(repeat('ab',32),'hex'),'{}')"
            ),
            {"id": uuid4(), "owner": graph.owner, "type": action_type},
        )
