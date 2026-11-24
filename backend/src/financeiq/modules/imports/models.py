"""Import staging and permanent provenance, including financial results."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financeiq.db.base import Base


class _ImportRecord:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )


class ImportBatch(_ImportRecord, Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint("source_code = 'money_manager'", name="source_code_valid"),
        CheckConstraint(
            "format_code = 'money_manager_xlsx_v1'", name="format_code_valid"
        ),
        CheckConstraint("octet_length(file_sha256) = 32", name="file_sha256_length"),
        CheckConstraint("byte_size BETWEEN 1 AND 10485760", name="byte_size_valid"),
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "source_code", "file_sha256"),
        Index("ix_import_batches_history", "user_id", text("created_at DESC"), "id"),
    )

    source_code: Mapped[str] = mapped_column(Text(collation="C"))
    format_code: Mapped[str] = mapped_column(Text(collation="C"))
    file_sha256: Mapped[bytes] = mapped_column(LargeBinary)
    byte_size: Mapped[int] = mapped_column(BigInteger)


class ImportAttempt(_ImportRecord, Base):
    __tablename__ = "import_attempts"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        CheckConstraint(
            "status IN ('processing','preview','rejected','expired','abandoned','accepted')",
            name="status_valid",
        ),
        CheckConstraint(
            "isfinite(received_at) AND isfinite(expires_at) AND "
            "expires_at = received_at + interval '86400 seconds'",
            name="lifetime_valid",
        ),
        CheckConstraint(
            "excel_date_system IS NULL OR excel_date_system IN ('1900','1904')",
            name="excel_date_system_valid",
        ),
        CheckConstraint("preview_version > 0", name="preview_version_positive"),
        CheckConstraint(
            "preview_digest IS NULL OR octet_length(preview_digest) = 32",
            name="preview_digest_length",
        ),
        CheckConstraint(
            "preview_plan IS NULL OR jsonb_typeof(preview_plan) = 'object'",
            name="preview_plan_object",
        ),
        CheckConstraint(
            "source_row_count IS NULL OR source_row_count BETWEEN 0 AND 10000",
            name="source_row_count_valid",
        ),
        CheckConstraint(
            "movement_count IS NULL OR movement_count BETWEEN 0 AND 20000",
            name="movement_count_valid",
        ),
        CheckConstraint(
            "(status = 'accepted') = (accepted_action_id IS NOT NULL)",
            name="accepted_action_valid",
        ),
        CheckConstraint(
            "status <> 'accepted' OR (source_row_count IS NOT NULL AND "
            "source_row_count > 0 AND movement_count IS NOT NULL AND "
            "movement_count > 0 AND preview_digest IS NOT NULL AND "
            "excel_date_system IS NOT NULL AND content_present)",
            name="accepted_metadata_valid",
        ),
        CheckConstraint(
            "status <> 'rejected' OR error_code IS NOT NULL",
            name="rejected_error_required",
        ),
        CheckConstraint(
            "content_purged_at IS NULL OR isfinite(content_purged_at)",
            name="content_purged_at_finite",
        ),
        CheckConstraint(
            "(content_present AND content_purged_at IS NULL) OR "
            "(NOT content_present AND content_purged_at IS NOT NULL AND "
            "status IN ('expired','abandoned') AND preview_digest IS NULL AND "
            "preview_plan IS NULL AND excel_date_system IS NULL)",
            name="content_state_valid",
        ),
        ForeignKeyConstraint(
            ["user_id", "batch_id"],
            ["import_batches.user_id", "import_batches.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "accepted_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "batch_id", "attempt_number"),
        Index(
            "uq_import_attempts_current",
            "user_id",
            "batch_id",
            unique=True,
            postgresql_where=text("status IN ('processing','preview','rejected')"),
        ),
        Index(
            "ix_import_attempts_history",
            "user_id",
            "batch_id",
            text("attempt_number DESC"),
        ),
        Index(
            "ix_import_attempts_cleanup",
            "expires_at",
            "id",
            postgresql_where=text("content_present AND status <> 'accepted'"),
        ),
    )

    batch_id: Mapped[UUID] = mapped_column()
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        Text(collation="C"), server_default=text("'processing'")
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parser_version: Mapped[str] = mapped_column(Text)
    excel_date_system: Mapped[str | None] = mapped_column(
        Text(collation="C"), server_default=text("NULL")
    )
    preview_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    preview_digest: Mapped[bytes | None] = mapped_column(
        LargeBinary, server_default=text("NULL")
    )
    preview_plan: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), server_default=text("NULL")
    )
    source_row_count: Mapped[int | None] = mapped_column(
        Integer, server_default=text("NULL")
    )
    movement_count: Mapped[int | None] = mapped_column(
        Integer, server_default=text("NULL")
    )
    content_present: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    content_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("NULL")
    )
    error_code: Mapped[str | None] = mapped_column(Text, server_default=text("NULL"))
    accepted_action_id: Mapped[UUID | None] = mapped_column(server_default=text("NULL"))


class ImportAccountMapping(_ImportRecord, Base):
    __tablename__ = "import_account_mappings"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "char_length(source_account_name) BETWEEN 1 AND 8192",
            name="source_account_name_length",
        ),
        ForeignKeyConstraint(
            ["user_id", "attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        # Full 8192-character names cannot be B-tree keys. The lifecycle guard
        # compares exact names under the parent lock; hash buckets only aid lookup.
        Index(
            "ix_import_account_mappings_name_bucket",
            "user_id",
            "attempt_id",
            text("md5(source_account_name)"),
        ),
        UniqueConstraint("user_id", "attempt_id", "id"),
        UniqueConstraint("user_id", "attempt_id", "id", "account_id"),
    )

    attempt_id: Mapped[UUID] = mapped_column()
    source_account_name: Mapped[str] = mapped_column(Text(collation="C"))
    account_id: Mapped[UUID | None] = mapped_column()


class ImportRow(_ImportRecord, Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "sheet_name IN ('Расходы','Доходы','Переводы')", name="sheet_name_valid"
        ),
        CheckConstraint("row_number BETWEEN 3 AND 1048576", name="row_number_valid"),
        CheckConstraint(
            "source_values IS NULL OR jsonb_typeof(source_values) = 'object'",
            name="source_values_object",
        ),
        CheckConstraint(
            "raw_payload IS NULL OR jsonb_typeof(raw_payload) = 'object'",
            name="raw_payload_object",
        ),
        CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'", name="validation_errors_array"
        ),
        ForeignKeyConstraint(
            ["user_id", "attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("user_id", "attempt_id", "id"),
        UniqueConstraint("user_id", "attempt_id", "sheet_name", "row_number"),
    )

    attempt_id: Mapped[UUID] = mapped_column()
    sheet_name: Mapped[str] = mapped_column(Text(collation="C"))
    row_number: Mapped[int] = mapped_column(Integer)
    source_values: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    validation_errors: Mapped[list[object]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )


class ImportMovement(_ImportRecord, Base):
    __tablename__ = "import_movements"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint("side IN ('single','outgoing','incoming')", name="side_valid"),
        CheckConstraint(
            "occurred_on IS NULL OR isfinite(occurred_on)", name="occurred_on_finite"
        ),
        CheckConstraint(
            "amount IS NULL OR (amount BETWEEN -9999999999999999.99 AND "
            "9999999999999999.99 AND amount <> 0)",
            name="amount_valid",
        ),
        CheckConstraint(
            "source_comment IS NULL OR char_length(source_comment) <= 2000",
            name="source_comment_length",
        ),
        CheckConstraint(
            "fingerprint_version = 'money_manager_match_v1'",
            name="fingerprint_version_valid",
        ),
        CheckConstraint(
            "fingerprint IS NULL OR octet_length(fingerprint) = 32",
            name="fingerprint_length",
        ),
        CheckConstraint(
            "decision IN ('pending','insert','link')", name="decision_valid"
        ),
        CheckConstraint(
            "(decision <> 'pending' OR result_transaction_id IS NULL) "
            "AND (decision <> 'link' OR result_transaction_id IS NOT NULL)",
            name="decision_result_valid",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "result_transaction_id"],
            ["transactions.user_id", "transactions.account_id", "transactions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["user_id", "result_transaction_id"],
            ["transactions.user_id", "transactions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["user_id", "attempt_id", "row_id"],
            ["import_rows.user_id", "import_rows.attempt_id", "import_rows.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "attempt_id", "mapping_id"],
            [
                "import_account_mappings.user_id",
                "import_account_mappings.attempt_id",
                "import_account_mappings.id",
            ],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "attempt_id", "mapping_id", "account_id"],
            [
                "import_account_mappings.user_id",
                "import_account_mappings.attempt_id",
                "import_account_mappings.id",
                "import_account_mappings.account_id",
            ],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("user_id", "account_id", "id"),
        UniqueConstraint("user_id", "attempt_id", "row_id", "side"),
        UniqueConstraint("user_id", "attempt_id", "result_transaction_id"),
        Index(
            "ix_import_movements_user_id_result_transaction_id",
            "user_id",
            "result_transaction_id",
        ),
        Index(
            "ix_import_movements_fingerprint",
            "user_id",
            "account_id",
            "fingerprint_version",
            "fingerprint",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column()
    row_id: Mapped[UUID] = mapped_column()
    mapping_id: Mapped[UUID] = mapped_column()
    side: Mapped[str] = mapped_column(Text(collation="C"))
    account_id: Mapped[UUID | None] = mapped_column()
    occurred_on: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    source_comment: Mapped[str | None] = mapped_column(Text)
    fingerprint_version: Mapped[str] = mapped_column(
        Text(collation="C"), server_default=text("'money_manager_match_v1'")
    )
    fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    decision: Mapped[str] = mapped_column(
        Text(collation="C"), server_default=text("'pending'")
    )
    result_transaction_id: Mapped[UUID | None] = mapped_column()


class ImportCancellation(_ImportRecord, Base):
    __tablename__ = "import_cancellations"
    __table_args__ = (
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        ForeignKeyConstraint(
            ["user_id", "attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        UniqueConstraint("user_id", "attempt_id"),
        UniqueConstraint("user_id", "id"),
        Index("ix_import_cancellations_user_id_action_id", "user_id", "action_id"),
    )

    attempt_id: Mapped[UUID] = mapped_column()
    action_id: Mapped[UUID] = mapped_column()
