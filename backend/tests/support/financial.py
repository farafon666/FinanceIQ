"""Complete synthetic financial commands; no bypass of production guards."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from financeiq.shared.financial_evidence import ActionEvidence


@dataclass(frozen=True)
class Book:
    owner: UUID
    account: UUID
    batch: UUID
    attempt: UUID
    action: UUID
    facts: tuple[UUID, ...]
    movements: tuple[UUID, ...]


async def action(
    connection: AsyncConnection,
    owner: UUID,
    kind: str,
    *,
    identity: UUID | None = None,
    account: UUID | None = None,
    attempt: UUID | None = None,
    parameters: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
    consents: list[str] | None = None,
    reason: str | None = None,
) -> tuple[UUID, dict[str, object]]:
    identity = identity or uuid4()
    details = ActionEvidence.model_validate(
        {
            "accepted_plan": {
                "user_id": str(owner),
                "action_type": kind,
                "account_id": str(account) if account else None,
                "source_attempt_id": str(attempt) if attempt else None,
                "parameters": parameters or {},
                "consents": consents or [],
            },
            "result": result or {},
        }
    ).model_dump(mode="json")
    await connection.execute(
        text(
            "INSERT INTO financial_actions(id,user_id,request_id,action_type,account_id,"
            "source_attempt_id,plan_version,plan_digest,reason,details) VALUES "
            "(:id,:owner,:id,:kind,:account,:attempt,1,:digest,:reason,CAST(:details AS jsonb))"
        ),
        {
            "id": identity,
            "owner": owner,
            "kind": kind,
            "account": account,
            "attempt": attempt,
            "digest": b"e" * 32,
            "reason": reason,
            "details": json.dumps(details),
        },
    )
    return identity, details


async def snapshot(connection: AsyncConnection, fact: UUID) -> dict[str, object]:
    value = await connection.scalar(
        text(
            "SELECT to_jsonb(t) - ARRAY['id','user_id','created_at','account_id',"
            "'source_movement_id','occurred_on','amount','description'] FROM transactions t WHERE id=:id"
        ),
        {"id": fact},
    )
    assert isinstance(value, dict)
    return value


async def item(
    connection: AsyncConnection,
    owner: UUID,
    action_id: UUID,
    fact: UUID,
    before: dict[str, object],
) -> None:
    after = await snapshot(connection, fact)
    await connection.execute(
        text(
            "INSERT INTO financial_action_transactions(id,user_id,action_id,transaction_id,"
            "before_state,after_state) VALUES (:id,:owner,:action,:fact,CAST(:before AS jsonb),CAST(:after AS jsonb))"
        ),
        {
            "id": uuid4(),
            "owner": owner,
            "action": action_id,
            "fact": fact,
            "before": json.dumps(before),
            "after": json.dumps(after),
        },
    )


async def book(
    connection: AsyncConnection,
    amounts: tuple[str, ...] = ("-100.00",),
    *,
    owner: UUID | None = None,
    account: UUID | None = None,
    accept: bool = True,
    batch: UUID | None = None,
    number: int = 1,
    occurred_on: str = "2026-10-01",
    before_accept: str | None = None,
    audit: bool = True,
    result_overrides: dict[str, object] | None = None,
) -> Book:
    if owner is None:
        owner = uuid4()
        await connection.execute(
            text(
                "INSERT INTO users(id,email,password_hash,timezone) VALUES (:id,:email,'synthetic','UTC')"
            ),
            {"id": owner, "email": f"{owner}@example.test"},
        )
    if account is None:
        account = uuid4()
        await connection.execute(
            text(
                "INSERT INTO accounts(id,user_id,name,currency_code) VALUES (:id,:owner,'Synthetic','RUB')"
            ),
            {"id": account, "owner": owner},
        )
    if batch is None:
        batch = uuid4()
        await connection.execute(
            text(
                "INSERT INTO import_batches(id,user_id,source_code,format_code,file_sha256,byte_size) "
                "VALUES (:id,:owner,'money_manager','money_manager_xlsx_v1',:hash,100)"
            ),
            {"id": batch, "owner": owner, "hash": hashlib.sha256(batch.bytes).digest()},
        )
    attempt, action_id, mapping = uuid4(), uuid4(), uuid4()
    facts = tuple(uuid4() for _ in amounts)
    movements = tuple(uuid4() for _ in amounts)
    await connection.execute(
        text(
            "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) "
            "VALUES (:id,:owner,:batch,:number,'2026-10-01 00:00Z','2026-10-02 00:00Z','synthetic_v1')"
        ),
        {"id": attempt, "owner": owner, "batch": batch, "number": number},
    )
    await connection.execute(
        text(
            "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name,account_id) "
            "VALUES (:id,:owner,:attempt,'Synthetic',:account)"
        ),
        {"id": mapping, "owner": owner, "attempt": attempt, "account": account},
    )
    results: list[dict[str, object]] = []
    for index, (amount, fact, movement) in enumerate(
        zip(amounts, facts, movements, strict=True)
    ):
        row = uuid4()
        await connection.execute(
            text(
                "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number,source_values) "
                "VALUES (:id,:owner,:attempt,:sheet,:number,'{}')"
            ),
            {
                "id": row,
                "owner": owner,
                "attempt": attempt,
                "sheet": "Расходы" if Decimal(amount) < 0 else "Доходы",
                "number": index + 3,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id,"
                "occurred_on,amount,source_comment,fingerprint) VALUES "
                "(:id,:owner,:attempt,:row,:mapping,'single',:account,:day,:amount,'',:fingerprint)"
            ),
            {
                "id": movement,
                "owner": owner,
                "attempt": attempt,
                "row": row,
                "mapping": mapping,
                "account": account,
                "amount": Decimal(amount),
                "fingerprint": b"f" * 32,
                "day": date.fromisoformat(occurred_on),
            },
        )
        results.append(
            {
                "movement_id": str(movement),
                "transaction_id": str(fact),
                "decision": "insert",
            }
        )
    value = Book(owner, account, batch, attempt, action_id, facts, movements)
    if not accept:
        return value
    parameters: dict[str, object] = {
        "parse_complete": True,
        "errors": [],
        "pending": [],
        "movements": results,
    }
    result: dict[str, object] = {
        "movements": results,
        "source_row_count": len(amounts),
        "movement_count": len(amounts),
        "transaction_ids": [str(f) for f in facts],
        "classifications": [
            {
                "transaction_id": str(fact),
                "state": {
                    "kind": "expense" if Decimal(amount) < 0 else "income",
                    "category_id": None,
                    "category_origin": "none",
                    "categorization_rule_id": None,
                    "refund_of_transaction_id": None,
                    "refund_action_id": None,
                    "last_action_id": str(action_id),
                    "revision": 1,
                    "cancelled_by_id": None,
                },
            }
            for fact, amount in zip(facts, amounts, strict=True)
        ],
        "facts": [
            {
                "transaction_id": str(f),
                "account_id": str(account),
                "occurred_on": occurred_on,
            }
            for f in facts
        ],
    }
    result |= result_overrides or {}
    _, details = await action(
        connection,
        owner,
        "import_confirm",
        identity=action_id,
        attempt=attempt,
        parameters=parameters,
        result=result,
    )
    for amount, fact, movement in zip(amounts, facts, movements, strict=True):
        await connection.execute(
            text(
                "UPDATE import_movements SET decision='insert',result_transaction_id=:fact WHERE id=:movement"
            ),
            {"fact": fact, "movement": movement},
        )
        await connection.execute(
            text(
                "INSERT INTO transactions(id,user_id,account_id,source_movement_id,occurred_on,amount,description,kind,last_action_id) "
                "VALUES (:id,:owner,:account,:movement,:day,:amount,'',:kind,:action)"
            ),
            {
                "id": fact,
                "owner": owner,
                "account": account,
                "movement": movement,
                "amount": Decimal(amount),
                "kind": "expense" if Decimal(amount) < 0 else "income",
                "action": action_id,
                "day": date.fromisoformat(occurred_on),
            },
        )
        if audit:
            await item(connection, owner, action_id, fact, {})
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='preview',excel_date_system='1900',preview_digest=:digest,"
            "preview_plan=CAST(:plan AS jsonb),source_row_count=:count,movement_count=:count WHERE id=:id"
        ),
        {
            "id": attempt,
            "digest": b"e" * 32,
            "plan": json.dumps(details["accepted_plan"]),
            "count": len(amounts),
        },
    )
    if before_accept:
        await connection.execute(
            text(before_accept),
            {
                "attempt": attempt,
                "movement": movements[0],
                "fact": facts[0],
                "account": account,
                "action": action_id,
            },
        )
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='accepted',accepted_action_id=:action,preview_plan=NULL WHERE id=:id"
        ),
        {"id": attempt, "action": action_id},
    )
    return value


async def classify(
    connection: AsyncConnection,
    value: Book,
    fact: UUID,
    *,
    kind: str = "set_kind",
    changes: dict[str, object] | None = None,
    consents: list[str] | None = None,
) -> UUID:
    before = await snapshot(connection, fact)
    changes = changes or {"kind": "unclassified"}
    identity = uuid4()
    if changes.get("kind") == "refund":
        changes = changes | {"refund_action_id": identity}
    await action(
        connection,
        value.owner,
        kind,
        identity=identity,
        result={"transaction_ids": [str(fact)]},
        consents=consents,
    )
    await connection.execute(
        text(
            "UPDATE transactions SET "
            + ",".join(f"{key}=:{key}" for key in changes)
            + ",last_action_id=:action WHERE id=:id"
        ),
        changes | {"id": fact, "action": identity},
    )
    await item(connection, value.owner, identity, fact, before)
    return identity


async def transfer(
    connection: AsyncConnection,
    value: Book,
    outgoing: UUID | None,
    incoming: UUID | None = None,
    *,
    consents: list[str] | None = None,
) -> UUID:
    identity, action_id = uuid4(), uuid4()
    sides = [side for side in (outgoing, incoming) if side is not None]
    before = {side: await snapshot(connection, side) for side in sides}
    await action(
        connection,
        value.owner,
        "confirm_transfer",
        identity=action_id,
        parameters={
            "transfer_links": [
                {
                    "id": str(identity),
                    "outgoing_transaction_id": str(outgoing) if outgoing else None,
                    "incoming_transaction_id": str(incoming) if incoming else None,
                }
            ]
        },
        result={"transaction_ids": [str(side) for side in sides]},
        consents=["own_accounts"] if consents is None else consents,
    )
    await connection.execute(
        text(
            "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,incoming_transaction_id,confirmation_action_id) "
            "VALUES (:id,:owner,:outgoing,:incoming,:action)"
        ),
        {
            "id": identity,
            "owner": value.owner,
            "outgoing": outgoing,
            "incoming": incoming,
            "action": action_id,
        },
    )
    for side in sides:
        await connection.execute(
            text(
                "UPDATE transactions SET kind='transfer',category_id=NULL,category_origin='none',"
                "categorization_rule_id=NULL,last_action_id=:action WHERE id=:id"
            ),
            {"id": side, "action": action_id},
        )
        await item(connection, value.owner, action_id, side, before[side])
    return identity


async def confirmation(
    connection: AsyncConnection,
    value: Book,
    *,
    balance: bool = False,
    start: str = "2026-10-01",
    end: str = "2026-10-01",
) -> UUID:
    identity = uuid4()
    version = await connection.scalar(
        text("SELECT facts_version FROM accounts WHERE id=:id"), {"id": value.account}
    )
    parameters: dict[str, object] = {"confirmation_id": str(identity)}
    if balance:
        parameters |= {"balance_date": start, "balance": "0.00"}
    else:
        parameters |= {
            "start_date": start,
            "end_date": end,
            "observed_facts_version": version,
            "observed_facts_digest": (b"h" * 32).hex(),
        }
    action_id, _ = await action(
        connection,
        value.owner,
        "confirm_balance" if balance else "confirm_history",
        account=value.account,
        parameters=parameters,
        result={"confirmation_id": str(identity)},
        consents=["eod_balance" if balance else "history_complete"],
    )
    columns = (
        "balance_date,balance"
        if balance
        else "start_date,end_date,observed_facts_version,observed_facts_digest"
    )
    expressions = ":start,0" if balance else ":start,:end,:version,:digest"
    table = "balance_confirmations" if balance else "history_confirmations"
    await connection.execute(
        text(
            f"INSERT INTO {table}(id,user_id,account_id,confirmation_action_id,{columns}) "
            f"VALUES (:id,:owner,:account,:action,{expressions})"
        ),
        {
            "id": identity,
            "owner": value.owner,
            "account": value.account,
            "action": action_id,
            "start": date.fromisoformat(start),
            "end": date.fromisoformat(end),
            "version": version,
            "digest": b"h" * 32,
        },
    )
    return identity


async def cancel(
    connection: AsyncConnection,
    value: Book,
    *,
    reason: str | None = "Synthetic correction",
) -> UUID:
    excluded = list(
        (
            await connection.execute(
                text(
                    "SELECT result_transaction_id FROM import_movements WHERE attempt_id=:id AND decision='insert' ORDER BY id"
                ),
                {"id": value.attempt},
            )
        ).scalars()
    )
    preserved = list(
        (
            await connection.execute(
                text(
                    "SELECT result_transaction_id FROM import_movements WHERE attempt_id=:id AND decision='link' ORDER BY id"
                ),
                {"id": value.attempt},
            )
        ).scalars()
    )
    facts_json = await connection.scalar(
        text(
            "SELECT coalesce(jsonb_agg(jsonb_build_object('transaction_id',t.id,'account_id',t.account_id,"
            "'occurred_on',t.occurred_on)),'[]') FROM transactions t JOIN import_movements m "
            "ON m.id=t.source_movement_id WHERE m.attempt_id=:id"
        ),
        {"id": value.attempt},
    )
    before = {fact: await snapshot(connection, fact) for fact in excluded}
    identity = uuid4()
    action_id, _ = await action(
        connection,
        value.owner,
        "import_cancel",
        attempt=value.attempt,
        parameters={"excluded_ids": [str(fact) for fact in excluded]},
        result={
            "excluded_ids": [str(fact) for fact in excluded],
            "preserved_link_ids": [str(fact) for fact in preserved],
            "facts": facts_json,
            "transaction_ids": [str(fact) for fact in excluded],
        },
        reason=reason,
    )
    await connection.execute(
        text(
            "INSERT INTO import_cancellations(id,user_id,attempt_id,action_id) VALUES (:id,:owner,:attempt,:action)"
        ),
        {
            "id": identity,
            "owner": value.owner,
            "attempt": value.attempt,
            "action": action_id,
        },
    )
    await connection.execute(
        text(
            "UPDATE transfer_links SET cancelled_by_id=:id WHERE user_id=:owner AND ended_by_action_id IS NULL "
            "AND cancelled_by_id IS NULL AND (outgoing_transaction_id=ANY(CAST(:facts AS uuid[])) OR incoming_transaction_id=ANY(CAST(:facts AS uuid[]))) "
            "AND (outgoing_transaction_id IS NULL OR outgoing_transaction_id=ANY(CAST(:facts AS uuid[]))) "
            "AND (incoming_transaction_id IS NULL OR incoming_transaction_id=ANY(CAST(:facts AS uuid[])))"
        ),
        {"id": identity, "owner": value.owner, "facts": excluded},
    )
    for fact in excluded:
        await connection.execute(
            text(
                "UPDATE transactions SET cancelled_by_id=:cancel,last_action_id=:action WHERE id=:id"
            ),
            {"id": fact, "cancel": identity, "action": action_id},
        )
        await item(connection, value.owner, action_id, fact, before[fact])
    return identity


async def linked_book(
    connection: AsyncConnection,
    original: Book,
    *,
    transfer_pair: tuple[UUID, UUID] | None = None,
) -> Book:
    stage = await book(
        connection, owner=original.owner, account=original.account, accept=False
    )
    fact = original.facts[0]
    await connection.execute(
        text(
            "UPDATE import_movements SET decision='link',result_transaction_id=:fact,"
            "amount=t.amount,occurred_on=t.occurred_on,source_comment='different source comment' "
            "FROM transactions t WHERE import_movements.id=:id AND t.id=:fact"
        ),
        {"fact": fact, "id": stage.movements[0]},
    )
    movements = [
        {
            "movement_id": str(stage.movements[0]),
            "transaction_id": str(fact),
            "decision": "link",
        }
    ]
    parameters: dict[str, object] = {
        "parse_complete": True,
        "errors": [],
        "pending": [],
        "movements": movements,
    }
    classification = await snapshot(connection, fact)
    link = uuid4()
    if transfer_pair:
        revision = classification["revision"]
        assert isinstance(revision, int)
        classification |= {
            "kind": "transfer",
            "last_action_id": str(stage.action),
            "revision": revision + 1,
        }
        parameters["transfer_links"] = [
            {
                "id": str(link),
                "outgoing_transaction_id": str(transfer_pair[0]),
                "incoming_transaction_id": str(transfer_pair[1]),
            }
        ]
    _, details = await action(
        connection,
        stage.owner,
        "import_confirm",
        identity=stage.action,
        attempt=stage.attempt,
        parameters=parameters,
        consents=["own_accounts"] if transfer_pair else [],
        result={
            "movements": movements,
            "source_row_count": 1,
            "movement_count": 1,
            "facts": [],
            "classifications": [{"transaction_id": str(fact), "state": classification}],
            "transaction_ids": [str(side) for side in transfer_pair]
            if transfer_pair
            else [],
        },
    )
    if transfer_pair:
        for side in transfer_pair:
            before = await snapshot(connection, side)
            await connection.execute(
                text(
                    "UPDATE transactions SET kind='transfer',last_action_id=:action WHERE id=:id"
                ),
                {"id": side, "action": stage.action},
            )
            await item(connection, stage.owner, stage.action, side, before)
        await connection.execute(
            text(
                "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,incoming_transaction_id,confirmation_action_id) VALUES (:id,:owner,:outgoing,:incoming,:action)"
            ),
            {
                "id": link,
                "owner": stage.owner,
                "outgoing": transfer_pair[0],
                "incoming": transfer_pair[1],
                "action": stage.action,
            },
        )
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='preview',excel_date_system='1900',preview_digest=:digest,"
            "preview_plan=CAST(:plan AS jsonb),source_row_count=1,movement_count=1 WHERE id=:id"
        ),
        {
            "id": stage.attempt,
            "digest": b"e" * 32,
            "plan": json.dumps(details["accepted_plan"]),
        },
    )
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='accepted',accepted_action_id=:action,preview_plan=NULL WHERE id=:id"
        ),
        {"id": stage.attempt, "action": stage.action},
    )
    return Book(
        stage.owner,
        stage.account,
        stage.batch,
        stage.attempt,
        stage.action,
        (fact,),
        stage.movements,
    )


async def archive_account(
    connection: AsyncConnection, value: Book, *, restore: bool = False
) -> UUID:
    before = await connection.scalar(
        text("SELECT to_jsonb(archived_at) FROM accounts WHERE id=:id"),
        {"id": value.account},
    )
    after = (
        None
        if restore
        else await connection.scalar(text("SELECT to_jsonb(clock_timestamp())"))
    )
    identity, _ = await action(
        connection,
        value.owner,
        "restore_account" if restore else "archive_account",
        account=value.account,
        parameters={"before_archived_at": before, "after_archived_at": after},
        result={"account_id": str(value.account), "archived_at": after},
    )
    await connection.execute(
        text(
            "UPDATE accounts SET archived_at=CAST(:after AS timestamptz) WHERE id=:id"
        ),
        {
            "id": value.account,
            "after": datetime.fromisoformat(after) if after else None,
        },
    )
    return identity
