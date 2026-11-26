"""Auth persistence only: no token issuance, password hashing or HTTP behavior."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from financeiq.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "email = lower(email) AND email ~ '^[!-~]+$'", name="email_canonical"
        ),
        CheckConstraint("password_hash <> ''", name="password_hash_nonempty"),
        CheckConstraint("timezone <> ''", name="timezone_nonempty"),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    email: Mapped[str] = mapped_column(Text(collation="C"), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
        CheckConstraint(
            "octet_length(csrf_token_hash) = 32", name="csrf_token_hash_length"
        ),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "isfinite(expires_at) AND expires_at > created_at", name="expires_at_valid"
        ),
        CheckConstraint(
            "revoked_at IS NULL OR (isfinite(revoked_at) AND revoked_at >= created_at)",
            name="revoked_at_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="RESTRICT"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    csrf_token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('login_ip', 'login_email', 'register_ip')", name="known_scope"
        ),
        CheckConstraint("octet_length(key_digest) = 32", name="key_digest_length"),
        CheckConstraint("isfinite(window_start)", name="window_start_finite"),
        CheckConstraint("attempt_count > 0", name="attempt_count_positive"),
    )

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key_digest: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer)
