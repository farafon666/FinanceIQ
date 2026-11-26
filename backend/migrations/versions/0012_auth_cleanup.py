"""Provide bounded cleanup of expired Auth state to the runtime role.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_auth_sessions_expiry",
        "auth_sessions",
        ["expires_at", "id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_auth_sessions_revoked",
        "auth_sessions",
        ["id"],
        postgresql_where=sa.text("revoked_at IS NOT NULL"),
    )
    op.create_index(
        "ix_auth_rate_limits_expiry",
        "auth_rate_limits",
        ["window_start", "scope", "key_digest"],
    )
    op.execute(
        """
        CREATE FUNCTION public.cleanup_auth_batch() RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            batch_size constant integer := 500;
            cutoff constant timestamptz := clock_timestamp();
            deleted bigint;
            more boolean;
        BEGIN
            DELETE FROM public.auth_sessions WHERE id IN (
                SELECT id FROM public.auth_sessions
                WHERE revoked_at IS NULL AND expires_at <= cutoff
                ORDER BY expires_at, id LIMIT batch_size FOR UPDATE SKIP LOCKED
            );
            GET DIAGNOSTICS deleted = ROW_COUNT;
            more := deleted = batch_size;

            DELETE FROM public.auth_sessions WHERE id IN (
                SELECT id FROM public.auth_sessions WHERE revoked_at IS NOT NULL
                ORDER BY id LIMIT batch_size FOR UPDATE SKIP LOCKED
            );
            GET DIAGNOSTICS deleted = ROW_COUNT;
            more := more OR deleted = batch_size;

            DELETE FROM public.auth_rate_limits
            WHERE (scope, key_digest, window_start) IN (
                SELECT scope, key_digest, window_start FROM public.auth_rate_limits
                WHERE window_start <= cutoff - interval '15 minutes'
                ORDER BY window_start, scope, key_digest
                LIMIT batch_size FOR UPDATE SKIP LOCKED
            );
            GET DIAGNOSTICS deleted = ROW_COUNT;
            RETURN more OR deleted = batch_size;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.cleanup_auth_batch() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.cleanup_auth_batch() TO financeiq_runtime"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION public.cleanup_auth_batch()")
    op.drop_index("ix_auth_rate_limits_expiry", table_name="auth_rate_limits")
    op.drop_index("ix_auth_sessions_revoked", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expiry", table_name="auth_sessions")
