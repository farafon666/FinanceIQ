"""Import persistence and lifecycle on disposable migrated PostgreSQL."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financeiq.core.config import MigrationSettings, Settings
from financeiq.db.models import metadata
from financeiq.db.session import (
    create_engine,
    create_migration_engine,
    create_session_factory,
)
from financeiq.modules.imports.models import (
    ImportAccountMapping,
    ImportAttempt,
    ImportBatch,
    ImportCancellation,
    ImportMovement,
    ImportRow,
)
from tests.support.postgres import TestPostgres

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def imports_migrator(
    runtime_postgres: TestPostgres,
) -> AsyncIterator[AsyncEngine]:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=runtime_postgres.url(
                "financeiq_migrator", sqlalchemy=True
            )
        )
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def imports_engine(
    runtime_postgres: TestPostgres, imports_migrator: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    async def clear() -> None:
        # Only the disposable TestPostgres instance. Runtime cannot TRUNCATE;
        # accepted history deliberately cannot be removed through normal DML.
        async with imports_migrator.begin() as connection:
            await connection.execute(text("TRUNCATE " + ", ".join(metadata.tables)))

    engine = create_engine(
        Settings(
            app_env="test",
            database_url=runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
        )
    )
    try:
        await clear()
        yield engine
    finally:
        await engine.dispose()
        await clear()


@dataclass(frozen=True)
class Graph:
    owner: UUID
    account: UUID
    batch: UUID
    attempt: UUID
    mapping: UUID
    row: UUID
    movement: UUID
    action: UUID

    def parameters(self) -> dict[str, UUID]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


async def seed(connection: AsyncConnection) -> Graph:
    graph = Graph(*(uuid4() for _ in range(8)))
    parameters = graph.parameters()
    statements = (
        "INSERT INTO users(id,email,password_hash,timezone) "
        "VALUES (:owner,CAST(CAST(:owner AS uuid) AS text)||'@example.test','synthetic','UTC')",
        "INSERT INTO accounts(id,user_id,name,currency_code) "
        "VALUES (:account,:owner,'Synthetic','RUB')",
        "INSERT INTO import_batches(id,user_id,source_code,format_code,file_sha256,byte_size) "
        "VALUES (:batch,:owner,'money_manager','money_manager_xlsx_v1',decode(repeat('ab',32),'hex'),1024)",
        "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) "
        "VALUES (:attempt,:owner,:batch,1,'2026-11-01 00:00+00','2026-11-02 00:00+00','synthetic_v1')",
        "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name,account_id) "
        "VALUES (:mapping,:owner,:attempt,' Карта Ё ',:account)",
        "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number,source_values,raw_payload) "
        "VALUES (:row,:owner,:attempt,'Расходы',1048576,"
        '\'{"amount":"-12.30","comment":" Кафе Ё "}\', \'{}\')',
        "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id,occurred_on,amount,source_comment,fingerprint) "
        "VALUES (:movement,:owner,:attempt,:row,:mapping,'single',:account,'2026-10-01',-12.30,' Кафе Ё ',decode(repeat('cd',32),'hex'))",
    )
    for statement in statements:
        await connection.execute(text(statement), parameters)
    return graph


async def prepare_accept(connection: AsyncConnection, graph: Graph) -> None:
    for statement in (
        "UPDATE import_attempts SET status='preview',excel_date_system='1900',"
        "preview_digest=decode(repeat('ef',32),'hex'),preview_plan='{}',"
        "source_row_count=1,movement_count=1 WHERE id=:attempt",
        "INSERT INTO financial_actions(id,user_id,request_id,action_type,plan_version,plan_digest,source_attempt_id,details) "
        "VALUES (:action,:owner,:action,'import_confirm',1,decode(repeat('ef',32),'hex'),:attempt,'{}')",
        "UPDATE import_movements SET decision='insert' WHERE id=:movement",
        "UPDATE import_rows SET raw_payload=NULL WHERE id=:row",
    ):
        await connection.execute(text(statement), graph.parameters())


async def accept(connection: AsyncConnection, graph: Graph) -> None:
    await prepare_accept(connection, graph)
    await connection.execute(
        text(
            "UPDATE import_attempts SET status='accepted',accepted_action_id=:action,preview_plan=NULL WHERE id=:attempt"
        ),
        graph.parameters(),
    )


@pytest_asyncio.fixture
async def seeded(imports_engine: AsyncEngine) -> Graph:
    async with imports_engine.begin() as connection:
        return await seed(connection)


async def test_orm_roundtrip_defaults_and_accepted_history(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    factory = create_session_factory(imports_engine)
    async with factory.begin() as session:
        batch = await session.get(ImportBatch, seeded.batch)
        attempt = await session.get(ImportAttempt, seeded.attempt)
        mapping = await session.get(ImportAccountMapping, seeded.mapping)
        row = await session.get(ImportRow, seeded.row)
        movement = await session.get(ImportMovement, seeded.movement)
        assert batch is not None and attempt is not None and mapping is not None
        assert row is not None and movement is not None
        assert batch.created_at.tzinfo is not None
        assert batch.file_sha256 == bytes.fromhex("ab" * 32)
        assert (attempt.status, attempt.preview_version, attempt.content_present) == (
            "processing",
            1,
            True,
        )
        assert attempt.preview_plan is None and attempt.content_purged_at is None
        assert mapping.source_account_name == " Карта Ё "
        assert row.row_number == 1048576 and row.validation_errors == []
        assert row.source_values == {"amount": "-12.30", "comment": " Кафе Ё "}
        assert movement.amount == Decimal("-12.30")
        assert movement.source_comment == " Кафе Ё "
        assert movement.decision == "pending"
        assert movement.fingerprint_version == "money_manager_match_v1"
    async with imports_engine.begin() as connection:
        await accept(connection, seeded)
        cancellation = uuid4()
        action = uuid4()
        await connection.execute(
            text(
                "INSERT INTO financial_actions(id,user_id,request_id,action_type,plan_version,plan_digest,source_attempt_id,details) "
                "VALUES (:id,:owner,:id,'import_cancel',1,decode(repeat('ab',32),'hex'),:attempt,'{}')"
            ),
            {"id": action, "owner": seeded.owner, "attempt": seeded.attempt},
        )
        await connection.execute(
            text(
                "INSERT INTO import_cancellations(id,user_id,attempt_id,action_id) VALUES (:id,:owner,:attempt,:action)"
            ),
            {
                "id": cancellation,
                "owner": seeded.owner,
                "attempt": seeded.attempt,
                "action": action,
            },
        )
    async with factory.begin() as session:
        loaded = await session.get(ImportCancellation, cancellation)
        accepted = await session.get(ImportAttempt, seeded.attempt)
        assert loaded is not None and loaded.action_id == action
        assert accepted is not None and accepted.status == "accepted"
        assert accepted.content_present and accepted.preview_digest is not None


@pytest.mark.parametrize(
    ("table", "key", "assignment", "state"),
    [
        ("import_batches", "batch", "source_code='bank'", "23514"),
        ("import_batches", "batch", "format_code='other'", "23514"),
        ("import_batches", "batch", "file_sha256=decode('ab','hex')", "23514"),
        ("import_batches", "batch", "byte_size=0", "23514"),
        ("import_batches", "batch", "byte_size=10485761", "23514"),
        ("import_attempts", "attempt", "attempt_number=0", "23514"),
        ("import_attempts", "attempt", "status='unknown'", "23514"),
        ("import_attempts", "attempt", "status='rejected'", "23514"),
        ("import_attempts", "attempt", "status='accepted'", "23514"),
        ("import_attempts", "attempt", "received_at='infinity'", "23514"),
        (
            "import_attempts",
            "attempt",
            "expires_at=received_at+interval '86399 seconds'",
            "23514",
        ),
        ("import_attempts", "attempt", "preview_version=0", "23514"),
        ("import_attempts", "attempt", "preview_digest=decode('ab','hex')", "23514"),
        ("import_attempts", "attempt", "preview_plan='[]'", "23514"),
        ("import_attempts", "attempt", "preview_plan='null'", "23514"),
        ("import_attempts", "attempt", "excel_date_system='unix'", "23514"),
        ("import_attempts", "attempt", "source_row_count=-1", "23514"),
        ("import_attempts", "attempt", "source_row_count=10001", "23514"),
        ("import_attempts", "attempt", "movement_count=-1", "23514"),
        ("import_attempts", "attempt", "movement_count=20001", "23514"),
        ("import_attempts", "attempt", "content_present=false", "23514"),
        ("import_attempts", "attempt", "content_purged_at='infinity'", "23514"),
        ("import_account_mappings", "mapping", "source_account_name=''", "23514"),
        (
            "import_account_mappings",
            "mapping",
            "source_account_name=repeat('я',8193)",
            "23514",
        ),
        ("import_rows", "row", "sheet_name='расходы'", "23514"),
        ("import_rows", "row", "row_number=2", "23514"),
        ("import_rows", "row", "row_number=1048577", "23514"),
        ("import_rows", "row", "source_values='[]'", "23514"),
        ("import_rows", "row", "raw_payload='null'", "23514"),
        ("import_rows", "row", "validation_errors='{}'", "23514"),
        ("import_movements", "movement", "side='both'", "23514"),
        ("import_movements", "movement", "occurred_on='-infinity'", "23514"),
        ("import_movements", "movement", "amount=0", "23514"),
        ("import_movements", "movement", "amount='NaN'", "23514"),
        ("import_movements", "movement", "amount='Infinity'", "22003"),
        ("import_movements", "movement", "amount=10000000000000000", "22003"),
        ("import_movements", "movement", "source_comment=repeat('я',2001)", "23514"),
        ("import_movements", "movement", "fingerprint=decode('ab','hex')", "23514"),
        ("import_movements", "movement", "fingerprint_version='other'", "23514"),
        ("import_movements", "movement", "decision='skip'", "23514"),
        ("import_movements", "movement", "side=NULL", "23502"),
        ("import_rows", "row", "validation_errors=NULL", "23502"),
    ],
)
async def test_storage_constraints(
    imports_migrator: AsyncEngine,
    seeded: Graph,
    table: str,
    key: str,
    assignment: str,
    state: str,
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_migrator.begin() as connection:
            await connection.execute(
                text(f"UPDATE {table} SET {assignment} WHERE id=:id"),
                {"id": seeded.parameters()[key]},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == state


@pytest.mark.parametrize(
    "amount", ["-9999999999999999.99", "9999999999999999.99", "0.01", "-0.01"]
)
async def test_exact_money_boundaries(
    imports_engine: AsyncEngine, seeded: Graph, amount: str
) -> None:
    async with imports_engine.begin() as connection:
        await connection.execute(
            text("UPDATE import_movements SET amount=:amount WHERE id=:movement"),
            {"amount": Decimal(amount), "movement": seeded.movement},
        )
    async with imports_engine.begin() as connection:
        stored = await connection.scalar(
            text("SELECT amount FROM import_movements WHERE id=:movement"),
            seeded.parameters(),
        )
        assert stored == Decimal(amount)


async def test_nullable_staging_and_orm_json_null(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE import_movements SET account_id=NULL,amount=NULL,occurred_on=NULL,source_comment=NULL,fingerprint=NULL WHERE id=:movement"
            ),
            seeded.parameters(),
        )
        await connection.execute(
            text(
                "UPDATE import_account_mappings SET account_id=NULL WHERE id=:mapping"
            ),
            seeded.parameters(),
        )
    factory = create_session_factory(imports_engine)
    async with factory.begin() as session:
        row = await session.get(ImportRow, seeded.row)
        assert row is not None
        row.source_values = None
        row.raw_payload = None
    async with imports_engine.begin() as connection:
        assert await connection.scalar(
            text(
                "SELECT source_values IS NULL AND raw_payload IS NULL FROM import_rows WHERE id=:row"
            ),
            seeded.parameters(),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO import_batches(id,user_id,source_code,format_code,file_sha256,byte_size) SELECT :new_id,user_id,source_code,format_code,file_sha256,byte_size FROM import_batches WHERE id=:batch",
        "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) SELECT :new_id,user_id,batch_id,2,received_at,expires_at,parser_version FROM import_attempts WHERE id=:attempt",
        "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) SELECT :new_id,user_id,attempt_id,source_account_name FROM import_account_mappings WHERE id=:mapping",
        "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number) SELECT :new_id,user_id,attempt_id,sheet_name,row_number FROM import_rows WHERE id=:row",
        "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side) SELECT :new_id,user_id,attempt_id,row_id,mapping_id,side FROM import_movements WHERE id=:movement",
    ],
)
async def test_import_identity_uniqueness(
    imports_engine: AsyncEngine, seeded: Graph, statement: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(statement), seeded.parameters() | {"new_id": uuid4()}
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23505"


async def test_repeated_fingerprint_and_exact_mapping_names(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    async with imports_engine.begin() as connection:
        for name in ("карта ё", " Карта Е "):
            await connection.execute(
                text(
                    "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:id,:owner,:attempt,:name)"
                ),
                seeded.parameters() | {"id": uuid4(), "name": name},
            )
        await connection.execute(
            text(
                "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id,amount,occurred_on,fingerprint) SELECT :id,user_id,attempt_id,row_id,mapping_id,'outgoing',account_id,amount,occurred_on,fingerprint FROM import_movements WHERE id=:movement"
            ),
            seeded.parameters() | {"id": uuid4()},
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM import_movements WHERE fingerprint=decode(repeat('cd',32),'hex')"
                )
            )
            == 2
        )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) VALUES (:id,:owner,:other_batch,2,'2026-11-01+00','2026-11-02+00','v1')",
        "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name,account_id) VALUES (:id,:owner,:attempt,'Other',:other_account)",
        "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:id,:owner,:other_attempt,'Other')",
        "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number) VALUES (:id,:owner,:other_attempt,'Доходы',3)",
        "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side) VALUES (:id,:owner,:attempt,:other_row,:mapping,'incoming')",
        "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side) VALUES (:id,:owner,:attempt,:row,:other_mapping,'incoming')",
        "INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id) VALUES (:id,:owner,:attempt,:row,:mapping,'incoming',:other_account)",
        "INSERT INTO financial_actions(id,user_id,request_id,action_type,plan_version,plan_digest,source_attempt_id,details) VALUES (:id,:owner,:id,'import_confirm',1,decode(repeat('ab',32),'hex'),:other_attempt,'{}')",
    ],
)
async def test_cross_owner_links_rejected(
    imports_engine: AsyncEngine, seeded: Graph, statement: str
) -> None:
    async with imports_engine.begin() as connection:
        other = await seed(connection)
    parameters: dict[str, UUID] = seeded.parameters() | {"id": uuid4()}
    parameters.update(
        {f"other_{key}": value for key, value in other.parameters().items()}
    )
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE import_attempts SET preview_version=2 WHERE id=:attempt",
        "UPDATE import_attempts SET status='expired',accepted_action_id=NULL WHERE id=:attempt",
        "UPDATE import_rows SET raw_payload='{}' WHERE id=:row",
        "UPDATE import_movements SET amount=-1 WHERE id=:movement",
        "UPDATE import_account_mappings SET account_id=NULL WHERE id=:mapping",
        "DELETE FROM import_movements WHERE id=:movement",
        "DELETE FROM import_rows WHERE id=:row",
        "DELETE FROM import_account_mappings WHERE id=:mapping",
        "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number) VALUES (:id,:owner,:attempt,'Доходы',3)",
    ],
)
async def test_accepted_provenance_is_immutable(
    imports_engine: AsyncEngine, seeded: Graph, statement: str
) -> None:
    async with imports_engine.begin() as connection:
        await accept(connection, seeded)
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(statement), seeded.parameters() | {"id": uuid4()}
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


async def purge(connection: AsyncConnection, graph: Graph) -> None:
    for table in ("import_movements", "import_rows", "import_account_mappings"):
        await connection.execute(
            text(f"DELETE FROM {table} WHERE attempt_id=:attempt"), graph.parameters()
        )
    await connection.execute(
        text(
            "UPDATE import_attempts SET content_present=false,content_purged_at=clock_timestamp(),preview_plan=NULL,preview_digest=NULL,excel_date_system=NULL WHERE id=:attempt"
        ),
        graph.parameters(),
    )


@pytest.mark.parametrize("status", ["expired", "abandoned"])
async def test_atomic_cleanup_preserves_metadata_and_other_history(
    imports_engine: AsyncEngine, seeded: Graph, status: str
) -> None:
    async with imports_engine.begin() as connection:
        other = await seed(connection)
        await accept(connection, other)
        await connection.execute(
            text("UPDATE import_attempts SET status=:status WHERE id=:attempt"),
            seeded.parameters() | {"status": status},
        )
        await purge(connection, seeded)
    async with imports_engine.begin() as connection:
        assert await connection.scalar(
            text(
                "SELECT NOT content_present AND content_purged_at IS NOT NULL FROM import_attempts WHERE id=:attempt"
            ),
            seeded.parameters(),
        )
        assert await connection.scalar(text("SELECT count(*) FROM import_batches")) == 2
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM import_rows WHERE attempt_id=:attempt"),
                other.parameters(),
            )
            == 1
        )
        # New parse retains batch identity but has its own attempt and TTL.
        await connection.execute(
            text(
                "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) VALUES (:id,:owner,:batch,2,'2026-11-03 00:00+00','2026-11-04 00:00+00','v2')"
            ),
            seeded.parameters() | {"id": uuid4()},
        )


@pytest.mark.parametrize(
    "mode", ["partial_delete", "flag_only", "delete_without_flag", "exception"]
)
async def test_incomplete_cleanup_rolls_back(
    imports_engine: AsyncEngine, seeded: Graph, mode: str
) -> None:
    async with imports_engine.begin() as connection:
        await connection.execute(
            text("UPDATE import_attempts SET status='expired' WHERE id=:attempt"),
            seeded.parameters(),
        )
    with pytest.raises((DBAPIError, RuntimeError)):
        async with imports_engine.begin() as connection:
            if mode in ("partial_delete", "delete_without_flag", "exception"):
                await connection.execute(
                    text("DELETE FROM import_movements WHERE id=:movement"),
                    seeded.parameters(),
                )
            if mode == "delete_without_flag":
                for table in ("import_rows", "import_account_mappings"):
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE attempt_id=:attempt"),
                        seeded.parameters(),
                    )
            if mode in ("partial_delete", "flag_only"):
                await connection.execute(
                    text(
                        "UPDATE import_attempts SET content_present=false,content_purged_at=clock_timestamp() WHERE id=:attempt"
                    ),
                    seeded.parameters(),
                )
            if mode == "exception":
                raise RuntimeError("synthetic interrupted cleanup")
    async with imports_engine.begin() as connection:
        assert await connection.scalar(
            text("SELECT content_present FROM import_attempts WHERE id=:attempt"),
            seeded.parameters(),
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM import_movements WHERE id=:movement"),
                seeded.parameters(),
            )
            == 1
        )


@pytest.mark.parametrize("status", ["processing", "preview", "rejected"])
async def test_active_content_cannot_be_deleted(
    imports_engine: AsyncEngine, seeded: Graph, status: str
) -> None:
    async with imports_engine.begin() as connection:
        if status != "processing":
            await connection.execute(
                text(
                    "UPDATE import_attempts SET status=:status,error_code='synthetic' WHERE id=:attempt"
                ),
                seeded.parameters() | {"status": status},
            )
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM import_movements WHERE id=:movement"),
                seeded.parameters(),
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE import_batches SET byte_size=2048 WHERE id=:batch",
        "UPDATE import_attempts SET parser_version='other' WHERE id=:attempt",
        "UPDATE import_rows SET row_number=3 WHERE id=:row",
        "UPDATE import_account_mappings SET source_account_name='Other' WHERE id=:mapping",
        "UPDATE import_movements SET side='incoming' WHERE id=:movement",
        "UPDATE financial_actions SET source_attempt_id=NULL WHERE id=:action",
        "DELETE FROM import_batches WHERE id=:batch",
        "DELETE FROM import_attempts WHERE id=:attempt",
        "DELETE FROM import_cancellations",
        "TRUNCATE import_rows",
        "ALTER TABLE import_rows ADD COLUMN forbidden integer",
        "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version,status) VALUES (:id,:owner,:batch,2,'2026-11-01 00:00+00','2026-11-02 00:00+00','v1','preview')",
        "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number,created_at) VALUES (:id,:owner,:attempt,'Доходы',3,now())",
    ],
)
async def test_runtime_permissions(
    imports_engine: AsyncEngine, seeded: Graph, statement: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(statement), seeded.parameters() | {"id": uuid4()}
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "42501"


async def test_orm_can_create_staging_graph(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    factory = create_session_factory(imports_engine)
    async with factory.begin() as session:
        batch = ImportBatch(
            user_id=seeded.owner,
            source_code="money_manager",
            format_code="money_manager_xlsx_v1",
            file_sha256=b"x" * 32,
            byte_size=10485760,
        )
        session.add(batch)
        await session.flush()
        received = datetime(2026, 11, 1, tzinfo=UTC)
        attempt = ImportAttempt(
            user_id=seeded.owner,
            batch_id=batch.id,
            attempt_number=1,
            received_at=received,
            expires_at=received + timedelta(seconds=86400),
            parser_version="synthetic_v1",
            preview_plan=None,
        )
        session.add(attempt)
        await session.flush()
        mapping = ImportAccountMapping(
            user_id=seeded.owner,
            attempt_id=attempt.id,
            source_account_name="Без сопоставления",
        )
        row = ImportRow(
            user_id=seeded.owner,
            attempt_id=attempt.id,
            sheet_name="Доходы",
            row_number=3,
            source_values=None,
            raw_payload=None,
        )
        session.add_all([mapping, row])
        await session.flush()
        movement = ImportMovement(
            user_id=seeded.owner,
            attempt_id=attempt.id,
            mapping_id=mapping.id,
            row_id=row.id,
            side="single",
        )
        session.add(movement)
        await session.flush()
        assert all(
            isinstance(record.id, UUID)
            for record in (batch, attempt, mapping, row, movement)
        )
        identity = movement.id
    async with factory.begin() as session:
        stored = await session.get(ImportMovement, identity)
        assert (
            stored is not None and stored.account_id is None and stored.amount is None
        )
        assert stored.decision == "pending"


async def test_full_length_source_account_name_is_preserved(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    # Valid source text must not rely on TOAST compression to fit a unique index.
    name = "".join(uuid4().hex for _ in range(256))
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:id,:owner,:attempt,:name)"
            ),
            seeded.parameters() | {"id": uuid4(), "name": name},
        )
    async with imports_engine.begin() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT source_account_name FROM import_account_mappings WHERE source_account_name=:name"
                ),
                {"name": name},
            )
            == name
        )


@pytest.mark.parametrize(
    "assignment",
    [
        "source_row_count=NULL",
        "source_row_count=0",
        "movement_count=NULL",
        "movement_count=0",
        "preview_digest=NULL",
        "excel_date_system=NULL",
    ],
)
async def test_accept_requires_complete_local_metadata(
    imports_engine: AsyncEngine, seeded: Graph, assignment: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await prepare_accept(connection, seeded)
            await connection.execute(
                text(f"UPDATE import_attempts SET {assignment} WHERE id=:attempt"),
                seeded.parameters(),
            )
            await connection.execute(
                text(
                    "UPDATE import_attempts SET status='accepted',accepted_action_id=:action WHERE id=:attempt"
                ),
                seeded.parameters(),
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"
    async with imports_engine.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT status FROM import_attempts WHERE id=:attempt"),
                seeded.parameters(),
            )
            == "processing"
        )
        assert (
            await connection.scalar(text("SELECT count(*) FROM financial_actions")) == 0
        )


@pytest.mark.parametrize("status", ["preview", "rejected", "expired", "abandoned"])
async def test_cannot_return_to_processing(
    imports_engine: AsyncEngine, seeded: Graph, status: str
) -> None:
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE import_attempts SET status=:status,error_code='synthetic' WHERE id=:attempt"
            ),
            seeded.parameters() | {"status": status},
        )
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE import_attempts SET status='processing' WHERE id=:attempt"
                ),
                seeded.parameters(),
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23514"


async def test_preview_change_requires_new_version_and_digest(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE import_attempts SET status='preview',preview_plan='{}',preview_digest=decode(repeat('ab',32),'hex') WHERE id=:attempt"
            ),
            seeded.parameters(),
        )
    for assignment in (
        "preview_plan=jsonb_build_object('changed',true)",
        "preview_version=2,preview_plan=jsonb_build_object('changed',true)",
    ):
        with pytest.raises(DBAPIError):
            async with imports_engine.begin() as connection:
                await connection.execute(
                    text(f"UPDATE import_attempts SET {assignment} WHERE id=:attempt"),
                    seeded.parameters(),
                )
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE import_attempts SET preview_version=2,preview_digest=decode(repeat('cd',32),'hex'),preview_plan=jsonb_build_object('changed',true) WHERE id=:attempt"
            ),
            seeded.parameters(),
        )


@pytest.mark.parametrize(
    "mode",
    [
        "unaccepted",
        "duplicate",
        "update",
        "delete",
        "foreign_action",
        "foreign_attempt",
    ],
)
async def test_cancellation_history_constraints(
    imports_engine: AsyncEngine, imports_migrator: AsyncEngine, seeded: Graph, mode: str
) -> None:
    cancellation = uuid4()
    async with imports_engine.begin() as connection:
        if mode != "unaccepted":
            await accept(connection, seeded)
        else:
            await prepare_accept(connection, seeded)
        if mode in ("duplicate", "update", "delete"):
            await connection.execute(
                text(
                    "INSERT INTO import_cancellations(id,user_id,attempt_id,action_id) VALUES (:id,:owner,:attempt,:action)"
                ),
                seeded.parameters() | {"id": cancellation},
            )
        other = await seed(connection)
        await accept(connection, other)
    parameters = seeded.parameters() | {
        "id": cancellation,
        "other_action": other.action,
        "other_attempt": other.attempt,
    }
    if mode in ("update", "delete"):
        engine = imports_migrator  # Bypass column ACL to exercise the guard itself.
        statement = (
            "UPDATE import_cancellations SET action_id=:action WHERE id=:id"
            if mode == "update"
            else "DELETE FROM import_cancellations WHERE id=:id"
        )
    else:
        engine = imports_engine
        action = ":other_action" if mode == "foreign_action" else ":action"
        attempt = ":other_attempt" if mode == "foreign_attempt" else ":attempt"
        statement = f"INSERT INTO import_cancellations(id,user_id,attempt_id,action_id) VALUES (:new_id,:owner,{attempt},{action})"
    expected = (
        "23505"
        if mode == "duplicate"
        else "23503"
        if mode.startswith("foreign_")
        else "23514"
    )
    with pytest.raises(DBAPIError) as failure:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters | {"new_id": uuid4()})
    assert getattr(failure.value.orig, "sqlstate", None) == expected


async def test_child_edit_waits_for_accept_and_then_fails(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    async with imports_engine.begin() as connection:
        await prepare_accept(connection, seeded)
    async with imports_engine.connect() as first, imports_engine.connect() as second:
        first_tx = await first.begin()
        await second.begin()
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        await first.execute(
            text(
                "UPDATE import_attempts SET status='accepted',accepted_action_id=:action,preview_plan=NULL WHERE id=:attempt"
            ),
            seeded.parameters(),
        )
        contender = asyncio.create_task(
            second.execute(
                text("UPDATE import_rows SET source_values='{}' WHERE id=:row"),
                seeded.parameters(),
            )
        )
        try:
            async with asyncio.timeout(5):
                while not await first.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}
                ):
                    await asyncio.sleep(0.01)
            assert not contender.done()
            await first_tx.commit()
            with pytest.raises(DBAPIError) as failure:
                await asyncio.wait_for(contender, timeout=5)
            assert getattr(failure.value.orig, "sqlstate", None) == "23514"
        finally:
            if first_tx.is_active:
                await first_tx.rollback()
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
            await second.rollback()
    async with imports_engine.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT source_values->>'amount' FROM import_rows WHERE id=:row"),
                seeded.parameters(),
            )
            == "-12.30"
        )


@pytest.mark.parametrize("foreign_key", ["row_id", "mapping_id"])
async def test_same_owner_different_attempt_cannot_supply_movement_links(
    imports_engine: AsyncEngine, seeded: Graph, foreign_key: str
) -> None:
    new_attempt, new_mapping, new_row = uuid4(), uuid4(), uuid4()
    parameters = seeded.parameters() | {
        "new_attempt": new_attempt,
        "new_mapping": new_mapping,
        "new_row": new_row,
        "new_id": uuid4(),
    }
    async with imports_engine.begin() as connection:
        await connection.execute(
            text("UPDATE import_attempts SET status='abandoned' WHERE id=:attempt"),
            parameters,
        )
        await connection.execute(
            text(
                "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) SELECT :new_attempt,user_id,batch_id,2,received_at,expires_at,parser_version FROM import_attempts WHERE id=:attempt"
            ),
            parameters,
        )
        await connection.execute(
            text(
                "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:new_mapping,:owner,:new_attempt,'New')"
            ),
            parameters,
        )
        await connection.execute(
            text(
                "INSERT INTO import_rows(id,user_id,attempt_id,sheet_name,row_number) VALUES (:new_row,:owner,:new_attempt,'Доходы',3)"
            ),
            parameters,
        )
    row = ":row" if foreign_key == "row_id" else ":new_row"
    mapping = ":mapping" if foreign_key == "mapping_id" else ":new_mapping"
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO import_movements(id,user_id,attempt_id,row_id,mapping_id,side,account_id) VALUES (:new_id,:owner,:new_attempt,{row},{mapping},'single',NULL)"
                ),
                parameters,
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"


@pytest.mark.parametrize("status", ["preview", "rejected"])
async def test_current_attempt_uniqueness_covers_all_current_statuses(
    imports_engine: AsyncEngine, seeded: Graph, status: str
) -> None:
    async with imports_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE import_attempts SET status=:status,error_code='synthetic' WHERE id=:attempt"
            ),
            seeded.parameters() | {"status": status},
        )
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO import_attempts(id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version) SELECT :id,user_id,batch_id,2,received_at,expires_at,parser_version FROM import_attempts WHERE id=:attempt"
                ),
                seeded.parameters() | {"id": uuid4()},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23505"


@pytest.mark.parametrize("length", [20, 8192])
async def test_mapping_uniqueness_after_waiting_for_parent_lock(
    imports_engine: AsyncEngine, seeded: Graph, length: int
) -> None:
    name = "".join(uuid4().hex for _ in range(256))[:length]
    statement = text(
        "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:id,:owner,:attempt,:name)"
    )
    parameters = seeded.parameters() | {"id": uuid4(), "name": name}
    async with imports_engine.connect() as first, imports_engine.connect() as second:
        first_tx = await first.begin()
        await second.begin()
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        await first.execute(statement, parameters)
        contender = asyncio.create_task(
            second.execute(statement, parameters | {"id": uuid4()})
        )
        try:
            async with asyncio.timeout(5):
                while not await first.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}
                ):
                    await asyncio.sleep(0.01)
            assert not contender.done()
            await first_tx.commit()
            with pytest.raises(DBAPIError) as failure:
                await asyncio.wait_for(contender, timeout=5)
            assert getattr(failure.value.orig, "sqlstate", None) == "23505"
        finally:
            if first_tx.is_active:
                await first_tx.rollback()
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
            await second.rollback()
    async with imports_engine.begin() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM import_account_mappings WHERE source_account_name=:name"
                ),
                {"name": name},
            )
            == 1
        )


async def test_mapping_duplicates_in_one_statement_roll_back(
    imports_engine: AsyncEngine, seeded: Graph
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:first,:owner,:attempt,'Same'),(:second,:owner,:attempt,'Same')"
                ),
                seeded.parameters() | {"first": uuid4(), "second": uuid4()},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "23505"
    async with imports_engine.begin() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM import_account_mappings WHERE source_account_name='Same'"
                )
            )
            == 0
        )


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
async def test_mapping_writes_require_read_committed(
    imports_engine: AsyncEngine, seeded: Graph, isolation: str
) -> None:
    # A parent lock does not advance a fixed snapshot when its data is unchanged.
    # Do not let callers bypass exact-name uniqueness by changing write isolation.
    with pytest.raises(DBAPIError) as failure:
        async with imports_engine.begin() as connection:
            await connection.execute(
                text(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            )
            await connection.execute(
                text(
                    "INSERT INTO import_account_mappings(id,user_id,attempt_id,source_account_name) VALUES (:id,:owner,:attempt,'Isolation')"
                ),
                seeded.parameters() | {"id": uuid4()},
            )
    assert getattr(failure.value.orig, "sqlstate", None) == "0A000"
