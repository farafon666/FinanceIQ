"""Create import staging, accepted provenance and cancellation history.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("financial_actions", sa.Column("source_attempt_id", sa.Uuid()))
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
    op.create_table(
        "import_batches",
        sa.Column("source_code", sa.Text(collation="C"), nullable=False),
        sa.Column("format_code", sa.Text(collation="C"), nullable=False),
        sa.Column("file_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "format_code = 'money_manager_xlsx_v1'",
            name=op.f("ck_import_batches_format_code_valid"),
        ),
        sa.CheckConstraint(
            "source_code = 'money_manager'",
            name=op.f("ck_import_batches_source_code_valid"),
        ),
        sa.CheckConstraint(
            "byte_size BETWEEN 1 AND 10485760",
            name=op.f("ck_import_batches_byte_size_valid"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)", name=op.f("ck_import_batches_created_at_finite")
        ),
        sa.CheckConstraint(
            "octet_length(file_sha256) = 32",
            name=op.f("ck_import_batches_file_sha256_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_batches_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
        sa.UniqueConstraint("user_id", "id", name=op.f("uq_import_batches_user_id_id")),
        sa.UniqueConstraint(
            "user_id",
            "source_code",
            "file_sha256",
            name=op.f("uq_import_batches_user_id_source_code_file_sha256"),
        ),
    )
    op.create_index(
        "ix_import_batches_history",
        "import_batches",
        ["user_id", sa.literal_column("created_at DESC"), "id"],
        unique=False,
    )
    op.create_table(
        "import_attempts",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Text(collation="C"),
            server_default=sa.text("'processing'"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column(
            "excel_date_system",
            sa.Text(collation="C"),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "preview_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "preview_digest",
            sa.LargeBinary(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "preview_plan",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_row_count",
            sa.Integer(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "movement_count",
            sa.Integer(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "content_present",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "content_purged_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.Column(
            "error_code", sa.Text(), server_default=sa.text("NULL"), nullable=True
        ),
        sa.Column(
            "accepted_action_id",
            sa.Uuid(),
            server_default=sa.text("NULL"),
            nullable=True,
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
            "(content_present AND content_purged_at IS NULL) OR (NOT content_present AND content_purged_at IS NOT NULL AND status IN ('expired','abandoned') AND preview_digest IS NULL AND preview_plan IS NULL AND excel_date_system IS NULL)",
            name=op.f("ck_import_attempts_content_state_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (accepted_action_id IS NOT NULL)",
            name=op.f("ck_import_attempts_accepted_action_valid"),
        ),
        sa.CheckConstraint(
            "excel_date_system IS NULL OR excel_date_system IN ('1900','1904')",
            name=op.f("ck_import_attempts_excel_date_system_valid"),
        ),
        sa.CheckConstraint(
            "isfinite(received_at) AND isfinite(expires_at) AND expires_at = received_at + interval '86400 seconds'",
            name=op.f("ck_import_attempts_lifetime_valid"),
        ),
        sa.CheckConstraint(
            "preview_plan IS NULL OR jsonb_typeof(preview_plan) = 'object'",
            name=op.f("ck_import_attempts_preview_plan_object"),
        ),
        sa.CheckConstraint(
            "status <> 'accepted' OR (source_row_count IS NOT NULL AND source_row_count > 0 AND movement_count IS NOT NULL AND movement_count > 0 AND preview_digest IS NOT NULL AND excel_date_system IS NOT NULL AND content_present)",
            name=op.f("ck_import_attempts_accepted_metadata_valid"),
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR error_code IS NOT NULL",
            name=op.f("ck_import_attempts_rejected_error_required"),
        ),
        sa.CheckConstraint(
            "status IN ('processing','preview','rejected','expired','abandoned','accepted')",
            name=op.f("ck_import_attempts_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name=op.f("ck_import_attempts_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "content_purged_at IS NULL OR isfinite(content_purged_at)",
            name=op.f("ck_import_attempts_content_purged_at_finite"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)", name=op.f("ck_import_attempts_created_at_finite")
        ),
        sa.CheckConstraint(
            "movement_count IS NULL OR movement_count BETWEEN 0 AND 20000",
            name=op.f("ck_import_attempts_movement_count_valid"),
        ),
        sa.CheckConstraint(
            "preview_digest IS NULL OR octet_length(preview_digest) = 32",
            name=op.f("ck_import_attempts_preview_digest_length"),
        ),
        sa.CheckConstraint(
            "preview_version > 0",
            name=op.f("ck_import_attempts_preview_version_positive"),
        ),
        sa.CheckConstraint(
            "source_row_count IS NULL OR source_row_count BETWEEN 0 AND 10000",
            name=op.f("ck_import_attempts_source_row_count_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "accepted_action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f(
                "fk_import_attempts_user_id_accepted_action_id_financial_actions"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "batch_id"],
            ["import_batches.user_id", "import_batches.id"],
            name=op.f("fk_import_attempts_user_id_batch_id_import_batches"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_attempts_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_attempts")),
        sa.UniqueConstraint(
            "user_id",
            "batch_id",
            "attempt_number",
            name=op.f("uq_import_attempts_user_id_batch_id_attempt_number"),
        ),
        sa.UniqueConstraint(
            "user_id", "id", name=op.f("uq_import_attempts_user_id_id")
        ),
    )
    op.create_index(
        "ix_import_attempts_cleanup",
        "import_attempts",
        ["expires_at", "id"],
        unique=False,
        postgresql_where=sa.text("content_present AND status <> 'accepted'"),
    )
    op.create_index(
        "ix_import_attempts_history",
        "import_attempts",
        ["user_id", "batch_id", sa.literal_column("attempt_number DESC")],
        unique=False,
    )
    op.create_index(
        "uq_import_attempts_current",
        "import_attempts",
        ["user_id", "batch_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('processing','preview','rejected')"),
    )
    op.create_table(
        "import_account_mappings",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_account_name", sa.Text(collation="C"), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "char_length(source_account_name) BETWEEN 1 AND 8192",
            name=op.f("ck_import_account_mappings_source_account_name_length"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)",
            name=op.f("ck_import_account_mappings_created_at_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            name=op.f("fk_import_account_mappings_user_id_account_id_accounts"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            name=op.f("fk_import_account_mappings_user_id_attempt_id_import_attempts"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_account_mappings_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_account_mappings")),
        sa.UniqueConstraint(
            "user_id",
            "attempt_id",
            "id",
            "account_id",
            name=op.f("uq_import_account_mappings_user_id_attempt_id_id_account_id"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "attempt_id",
            "id",
            name=op.f("uq_import_account_mappings_user_id_attempt_id_id"),
        ),
    )
    op.create_index(
        "ix_import_account_mappings_name_bucket",
        "import_account_mappings",
        ["user_id", "attempt_id", sa.text("md5(source_account_name)")],
        unique=False,
    )
    op.create_table(
        "import_cancellations",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "isfinite(created_at)",
            name=op.f("ck_import_cancellations_created_at_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "action_id"],
            ["financial_actions.user_id", "financial_actions.id"],
            name=op.f("fk_import_cancellations_user_id_action_id_financial_actions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            name=op.f("fk_import_cancellations_user_id_attempt_id_import_attempts"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_cancellations_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_cancellations")),
        sa.UniqueConstraint(
            "user_id",
            "attempt_id",
            name=op.f("uq_import_cancellations_user_id_attempt_id"),
        ),
        sa.UniqueConstraint(
            "user_id", "id", name=op.f("uq_import_cancellations_user_id_id")
        ),
    )
    op.create_table(
        "import_rows",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("sheet_name", sa.Text(collation="C"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column(
            "source_values",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
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
            "jsonb_typeof(validation_errors) = 'array'",
            name=op.f("ck_import_rows_validation_errors_array"),
        ),
        sa.CheckConstraint(
            "raw_payload IS NULL OR jsonb_typeof(raw_payload) = 'object'",
            name=op.f("ck_import_rows_raw_payload_object"),
        ),
        sa.CheckConstraint(
            "sheet_name IN ('Расходы','Доходы','Переводы')",
            name=op.f("ck_import_rows_sheet_name_valid"),
        ),
        sa.CheckConstraint(
            "source_values IS NULL OR jsonb_typeof(source_values) = 'object'",
            name=op.f("ck_import_rows_source_values_object"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)", name=op.f("ck_import_rows_created_at_finite")
        ),
        sa.CheckConstraint(
            "row_number BETWEEN 3 AND 1048576",
            name=op.f("ck_import_rows_row_number_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "attempt_id"],
            ["import_attempts.user_id", "import_attempts.id"],
            name=op.f("fk_import_rows_user_id_attempt_id_import_attempts"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_rows_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
        sa.UniqueConstraint(
            "user_id",
            "attempt_id",
            "id",
            name=op.f("uq_import_rows_user_id_attempt_id_id"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "attempt_id",
            "sheet_name",
            "row_number",
            name=op.f("uq_import_rows_user_id_attempt_id_sheet_name_row_number"),
        ),
    )
    op.create_table(
        "import_movements",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("row_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.Text(collation="C"), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("source_comment", sa.Text(), nullable=True),
        sa.Column(
            "fingerprint_version",
            sa.Text(collation="C"),
            server_default=sa.text("'money_manager_match_v1'"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column(
            "decision",
            sa.Text(collation="C"),
            server_default=sa.text("'pending'"),
            nullable=False,
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
            "decision IN ('pending','insert','link')",
            name=op.f("ck_import_movements_decision_valid"),
        ),
        sa.CheckConstraint(
            "fingerprint_version = 'money_manager_match_v1'",
            name=op.f("ck_import_movements_fingerprint_version_valid"),
        ),
        sa.CheckConstraint(
            "side IN ('single','outgoing','incoming')",
            name=op.f("ck_import_movements_side_valid"),
        ),
        sa.CheckConstraint(
            "amount IS NULL OR (amount BETWEEN -9999999999999999.99 AND 9999999999999999.99 AND amount <> 0)",
            name=op.f("ck_import_movements_amount_valid"),
        ),
        sa.CheckConstraint(
            "fingerprint IS NULL OR octet_length(fingerprint) = 32",
            name=op.f("ck_import_movements_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "isfinite(created_at)", name=op.f("ck_import_movements_created_at_finite")
        ),
        sa.CheckConstraint(
            "occurred_on IS NULL OR isfinite(occurred_on)",
            name=op.f("ck_import_movements_occurred_on_finite"),
        ),
        sa.CheckConstraint(
            "source_comment IS NULL OR char_length(source_comment) <= 2000",
            name=op.f("ck_import_movements_source_comment_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "attempt_id", "mapping_id", "account_id"],
            [
                "import_account_mappings.user_id",
                "import_account_mappings.attempt_id",
                "import_account_mappings.id",
                "import_account_mappings.account_id",
            ],
            name=op.f(
                "fk_import_movements_user_id_attempt_id_mapping_id_account_id_import_account_mappings"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "attempt_id", "mapping_id"],
            [
                "import_account_mappings.user_id",
                "import_account_mappings.attempt_id",
                "import_account_mappings.id",
            ],
            name=op.f(
                "fk_import_movements_user_id_attempt_id_mapping_id_import_account_mappings"
            ),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "attempt_id", "row_id"],
            ["import_rows.user_id", "import_rows.attempt_id", "import_rows.id"],
            name=op.f("fk_import_movements_user_id_attempt_id_row_id_import_rows"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_import_movements_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_movements")),
        sa.UniqueConstraint(
            "user_id",
            "account_id",
            "id",
            name=op.f("uq_import_movements_user_id_account_id_id"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "attempt_id",
            "row_id",
            "side",
            name=op.f("uq_import_movements_user_id_attempt_id_row_id_side"),
        ),
    )
    op.create_index(
        "ix_import_movements_fingerprint",
        "import_movements",
        ["user_id", "account_id", "fingerprint_version", "fingerprint"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_financial_actions_user_id_source_attempt_id_import_attempts",
        "financial_actions",
        "import_attempts",
        ["user_id", "source_attempt_id"],
        ["user_id", "id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    )
    # These guards protect storage lifecycle, not the full C-06--C-11 semantics.
    # The shared owner-root protocol arrives in 2.8. Local parent locks prevent
    # accepted provenance from racing with a child edit or cleanup already now.
    op.execute("""
        CREATE FUNCTION guard_import_attempt() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'processing' OR NOT NEW.content_present THEN
                    RAISE EXCEPTION 'An import attempt starts in processing'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF (to_jsonb(NEW) - ARRAY['status','excel_date_system','preview_version',
                    'preview_digest','preview_plan','source_row_count','movement_count',
                    'content_present','content_purged_at','error_code','accepted_action_id'])
                IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status','excel_date_system','preview_version',
                    'preview_digest','preview_plan','source_row_count','movement_count',
                    'content_present','content_purged_at','error_code','accepted_action_id'])
            THEN
                RAISE EXCEPTION 'Import attempt identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
            IF OLD.status = 'accepted' OR NOT OLD.content_present THEN
                RAISE EXCEPTION 'Accepted or purged attempt is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT (
                (OLD.status = 'processing' AND NEW.status IN ('processing','preview','rejected','expired','abandoned'))
                OR (OLD.status = 'preview' AND NEW.status IN ('preview','accepted','expired','abandoned'))
                OR (OLD.status = 'rejected' AND NEW.status IN ('expired','abandoned'))
                OR (OLD.status IN ('expired','abandoned') AND NEW.status = OLD.status)
            ) THEN
                RAISE EXCEPTION 'Invalid import attempt transition'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.status IN ('expired','abandoned') AND
                (to_jsonb(NEW) - ARRAY['content_present','content_purged_at',
                    'preview_digest','preview_plan','excel_date_system']) IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['content_present','content_purged_at',
                    'preview_digest','preview_plan','excel_date_system'])
            THEN
                RAISE EXCEPTION 'Terminal import attempt permits cleanup only'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.preview_version < OLD.preview_version THEN
                RAISE EXCEPTION 'Preview version cannot decrease'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.status = 'preview' AND NEW.status = 'preview'
                AND (NEW.preview_plan IS DISTINCT FROM OLD.preview_plan
                    OR NEW.preview_digest IS DISTINCT FROM OLD.preview_digest)
                AND (NEW.preview_version <= OLD.preview_version
                    OR NEW.preview_digest IS NULL
                    OR NEW.preview_digest IS NOT DISTINCT FROM OLD.preview_digest)
            THEN
                RAISE EXCEPTION 'Changed preview requires a new version and digest'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER guard_import_attempt BEFORE INSERT OR UPDATE ON import_attempts FOR EACH ROW EXECUTE FUNCTION guard_import_attempt()"
    )
    op.execute("""
        CREATE FUNCTION guard_import_content() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE
            target_user uuid;
            target_attempt uuid;
            parent_status text;
            parent_content boolean;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                target_user := OLD.user_id;
                target_attempt := OLD.attempt_id;
            ELSE
                target_user := NEW.user_id;
                target_attempt := NEW.attempt_id;
            END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.id, NEW.user_id, NEW.attempt_id, NEW.created_at) IS DISTINCT FROM
                (OLD.id, OLD.user_id, OLD.attempt_id, OLD.created_at)
            THEN
                RAISE EXCEPTION 'Import content identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            SELECT status, content_present INTO parent_status, parent_content
            FROM import_attempts WHERE user_id = target_user AND id = target_attempt
            FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Import attempt not found' USING ERRCODE = '23503';
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF parent_status NOT IN ('expired','abandoned') OR NOT parent_content THEN
                    RAISE EXCEPTION 'Only terminal unaccepted content can be deleted'
                        USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF parent_status NOT IN ('processing','preview') OR NOT parent_content THEN
                RAISE EXCEPTION 'Import content is immutable in this state'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_TABLE_NAME = 'import_account_mappings' THEN
                IF current_setting('transaction_isolation') <> 'read committed' THEN
                    RAISE EXCEPTION 'Import mapping writes require READ COMMITTED'
                        USING ERRCODE = '0A000';
                END IF;
                -- Hash collisions do not imply identity. The VOLATILE function
                -- reads after acquiring the parent lock, including after a wait.
                IF EXISTS (
                    SELECT 1 FROM import_account_mappings
                    WHERE user_id=NEW.user_id AND attempt_id=NEW.attempt_id
                        AND md5(source_account_name)=md5(NEW.source_account_name)
                        AND source_account_name=NEW.source_account_name COLLATE "C"
                        AND id<>NEW.id
                ) THEN
                    RAISE EXCEPTION 'Duplicate source account name'
                        USING ERRCODE = '23505';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION check_import_purge() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE
            target_user uuid;
            target_attempt uuid;
            present boolean;
        BEGIN
            IF TG_TABLE_NAME = 'import_attempts' THEN
                target_user := NEW.user_id;
                target_attempt := NEW.id;
            ELSE
                target_user := OLD.user_id;
                target_attempt := OLD.attempt_id;
            END IF;
            SELECT content_present INTO present FROM import_attempts
                WHERE user_id = target_user AND id = target_attempt;
            IF NOT FOUND OR (TG_OP = 'DELETE' AND present) THEN
                RAISE EXCEPTION 'Import cleanup must finish atomically'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT present AND (
                EXISTS (SELECT 1 FROM import_movements WHERE user_id=target_user AND attempt_id=target_attempt)
                OR EXISTS (SELECT 1 FROM import_rows WHERE user_id=target_user AND attempt_id=target_attempt)
                OR EXISTS (SELECT 1 FROM import_account_mappings WHERE user_id=target_user AND attempt_id=target_attempt)
            ) THEN
                RAISE EXCEPTION 'Purged import retains child content'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for table in ("import_movements", "import_rows", "import_account_mappings"):
        op.execute(
            f"CREATE TRIGGER guard_import_content BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_import_content()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER check_import_purge AFTER DELETE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_import_purge()"
        )
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_import_purge AFTER INSERT OR UPDATE ON import_attempts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_import_purge()"
    )
    op.execute("""
        CREATE FUNCTION guard_import_cancellation() RETURNS trigger
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE parent_status text;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Import cancellation is immutable'
                    USING ERRCODE = '23514';
            END IF;
            SELECT status INTO parent_status FROM import_attempts
                WHERE user_id=NEW.user_id AND id=NEW.attempt_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Import attempt not found' USING ERRCODE = '23503';
            END IF;
            IF parent_status <> 'accepted' THEN
                RAISE EXCEPTION 'Only accepted imports can be cancelled'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER guard_import_cancellation BEFORE INSERT OR UPDATE OR DELETE ON import_cancellations FOR EACH ROW EXECUTE FUNCTION guard_import_cancellation()"
    )
    op.execute(
        "GRANT SELECT ON import_batches, import_attempts, import_account_mappings, import_rows, import_movements, import_cancellations TO financeiq_runtime"
    )
    grants = {
        "import_batches": "id,user_id,source_code,format_code,file_sha256,byte_size",
        "import_attempts": "id,user_id,batch_id,attempt_number,received_at,expires_at,parser_version",
        "import_account_mappings": "id,user_id,attempt_id,source_account_name,account_id",
        "import_rows": "id,user_id,attempt_id,sheet_name,row_number,source_values,raw_payload,validation_errors",
        "import_movements": "id,user_id,attempt_id,row_id,mapping_id,side,account_id,occurred_on,amount,source_comment,fingerprint_version,fingerprint,decision",
        "import_cancellations": "id,user_id,attempt_id,action_id",
    }
    for table, columns in grants.items():
        op.execute(f"GRANT INSERT ({columns}) ON {table} TO financeiq_runtime")
    for table, columns in {
        "import_attempts": "status,excel_date_system,preview_version,preview_digest,preview_plan,source_row_count,movement_count,content_present,content_purged_at,error_code,accepted_action_id",
        "import_account_mappings": "account_id",
        "import_rows": "source_values,raw_payload,validation_errors",
        "import_movements": "account_id,occurred_on,amount,source_comment,fingerprint,decision",
    }.items():
        op.execute(f"GRANT UPDATE ({columns}) ON {table} TO financeiq_runtime")
    op.execute(
        "GRANT DELETE ON import_movements, import_rows, import_account_mappings TO financeiq_runtime"
    )
    op.execute(
        "GRANT INSERT (source_attempt_id) ON financial_actions TO financeiq_runtime"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE financial_actions, import_batches, import_attempts, import_account_mappings, import_rows, import_movements, import_cancellations IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM import_batches)
                OR EXISTS (SELECT 1 FROM import_attempts)
                OR EXISTS (SELECT 1 FROM import_account_mappings)
                OR EXISTS (SELECT 1 FROM import_rows)
                OR EXISTS (SELECT 1 FROM import_movements)
                OR EXISTS (SELECT 1 FROM import_cancellations)
                OR EXISTS (SELECT 1 FROM financial_actions WHERE source_attempt_id IS NOT NULL
                    OR action_type IN ('import_confirm','import_cancel'))
            THEN
                RAISE EXCEPTION 'Cannot downgrade nonempty Imports or import audit';
            END IF;
        END $$
    """)
    op.drop_constraint(
        "fk_financial_actions_user_id_source_attempt_id_import_attempts",
        "financial_actions",
        type_="foreignkey",
    )
    for table in (
        "import_cancellations",
        "import_movements",
        "import_rows",
        "import_account_mappings",
        "import_attempts",
        "import_batches",
    ):
        op.drop_table(table)
    for function in (
        "guard_import_cancellation",
        "check_import_purge",
        "guard_import_content",
        "guard_import_attempt",
    ):
        op.execute(f"DROP FUNCTION {function}()")
    op.drop_column("financial_actions", "source_attempt_id")
    op.drop_constraint(
        op.f("ck_financial_actions_action_type_valid"),
        "financial_actions",
        type_="check",
    )
    op.create_check_constraint(
        "action_type_valid",
        "financial_actions",
        "action_type IN ('confirm_balance','revoke_balance','confirm_history',"
        "'revoke_history','archive_account','restore_account')",
    )
