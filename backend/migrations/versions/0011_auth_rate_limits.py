"""Store shared Auth rate limits.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limits",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key_digest", sa.LargeBinary(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "key_digest", "window_start"),
        sa.CheckConstraint(
            "scope IN ('login_ip', 'login_email', 'register_ip')", name="known_scope"
        ),
        sa.CheckConstraint("octet_length(key_digest) = 32", name="key_digest_length"),
        sa.CheckConstraint("isfinite(window_start)", name="window_start_finite"),
        sa.CheckConstraint("attempt_count > 0", name="attempt_count_positive"),
    )
    op.execute("GRANT SELECT, INSERT ON auth_rate_limits TO financeiq_runtime")
    op.execute("GRANT UPDATE (attempt_count) ON auth_rate_limits TO financeiq_runtime")


def downgrade() -> None:
    op.execute("LOCK TABLE auth_rate_limits IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM auth_rate_limits) THEN
                RAISE EXCEPTION 'Cannot downgrade nonempty Auth rate limits';
            END IF;
        END $$
        """
    )
    op.drop_table("auth_rate_limits")
