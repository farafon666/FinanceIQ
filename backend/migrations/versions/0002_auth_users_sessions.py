"""Create users and server-side session verifiers.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("email", sa.Text(collation="C"), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.CheckConstraint(
            "email = lower(email) AND email ~ '^[!-~]+$'", name="email_canonical"
        ),
        sa.CheckConstraint("password_hash <> ''", name="password_hash_nonempty"),
        sa.CheckConstraint("timezone <> ''", name="timezone_nonempty"),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
        sa.CheckConstraint(
            "octet_length(csrf_token_hash) = 32", name="csrf_token_hash_length"
        ),
        sa.CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        sa.CheckConstraint(
            "isfinite(expires_at) AND expires_at > created_at", name="expires_at_valid"
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR (isfinite(revoked_at) AND revoked_at >= created_at)",
            name="revoked_at_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.execute("GRANT SELECT, INSERT ON users, auth_sessions TO financeiq_runtime")
    op.execute(
        "GRANT UPDATE (email, password_hash, timezone, is_active) "
        "ON users TO financeiq_runtime"
    )
    op.execute(
        "GRANT UPDATE (expires_at, revoked_at, csrf_token_hash) "
        "ON auth_sessions TO financeiq_runtime"
    )


def downgrade() -> None:
    # Keep writers out between the emptiness check and DROP. Never erase Auth data.
    op.execute("LOCK TABLE users, auth_sessions IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM auth_sessions) THEN
                RAISE EXCEPTION 'Cannot downgrade nonempty Auth tables';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
