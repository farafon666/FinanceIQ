"""Create personal categories and categorization rules.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(collation="C"), nullable=False),
        sa.Column("kind", sa.Text(collation="C"), nullable=False),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("kind IN ('income', 'expense')", name="kind_valid"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        sa.CheckConstraint(
            "name_key <> '' AND name_key <> 'без категории'", name="name_key_valid"
        ),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.CheckConstraint(
            "archived_at IS NULL OR (isfinite(archived_at) AND archived_at >= created_at)",
            name="archived_at_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id"),
    )
    op.create_index(
        "uq_categories_active_name",
        "categories",
        ["user_id", "kind", "name_key"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "categorization_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "active_priority",
            sa.Integer(),
            sa.Computed(
                "CASE WHEN is_active THEN priority ELSE NULL END", persisted=True
            ),
            nullable=True,
        ),
        sa.CheckConstraint("priority > 0", name="priority_positive"),
        sa.CheckConstraint(
            "char_length(pattern) BETWEEN 1 AND 200", name="pattern_length"
        ),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id"),
        sa.UniqueConstraint(
            "user_id", "active_priority", deferrable=True, initially="DEFERRED"
        ),
    )
    op.create_index(
        "ix_categorization_rules_user_id_category_id",
        "categorization_rules",
        ["user_id", "category_id"],
    )
    # Row-local immutability only. C-05 and owner-root locking come in 2.7/2.8.
    op.execute("""
        CREATE FUNCTION guard_category_kind_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind IS DISTINCT FROM OLD.kind THEN
                RAISE EXCEPTION 'Category direction is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER guard_category_kind_update BEFORE UPDATE ON categories FOR EACH ROW EXECUTE FUNCTION guard_category_kind_update()"
    )
    op.execute("GRANT SELECT ON categories, categorization_rules TO financeiq_runtime")
    op.execute(
        "GRANT INSERT (id, user_id, name, name_key, kind) ON categories TO financeiq_runtime"
    )
    op.execute(
        "GRANT UPDATE (name, name_key, archived_at) ON categories TO financeiq_runtime"
    )
    op.execute(
        "GRANT INSERT (id, user_id, category_id, pattern, priority, is_active) ON categorization_rules TO financeiq_runtime"
    )
    op.execute(
        "GRANT UPDATE (category_id, pattern, priority, is_active) ON categorization_rules TO financeiq_runtime"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE categories, categorization_rules IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM categories)
                OR EXISTS (SELECT 1 FROM categorization_rules)
            THEN
                RAISE EXCEPTION 'Cannot downgrade nonempty Categories tables';
            END IF;
        END $$
    """)
    op.drop_table("categorization_rules")
    op.drop_table("categories")
    op.execute("DROP FUNCTION guard_category_kind_update()")
