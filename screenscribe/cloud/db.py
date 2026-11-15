"""SQLite (WAL) store for Screenscribe Cloud.

``CloudStore`` is the only module that speaks SQL. Every HTTP handler and the
worker go through its typed methods, so a future PostgreSQL backend replaces
this file without touching routers. Connections are per-thread, opened in
autocommit mode (``isolation_level=None``) so that explicit
``BEGIN IMMEDIATE`` transactions are the single source of write atomicity:
quota reservation, refresh rotation and device-code consumption each run as
one serialized write transaction.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .schemas import GateCode, inactive_subscription
from .security import isoformat_z, new_id, parse_iso

SCHEMA_VERSION = 1
ACTIVE_STATUSES = frozenset({"active", "trialing"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NULL,
    apple_sub TEXT UNIQUE NULL,
    display_name TEXT NOT NULL DEFAULT '',
    preferred_lang TEXT NOT NULL DEFAULT 'en',
    stripe_customer_id TEXT UNIQUE NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    transport TEXT NOT NULL,
    family_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_family ON sessions(family_id);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    refresh_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    family_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    rotated_to TEXT NULL,
    revoked_at TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_tokens(family_id);
CREATE TABLE IF NOT EXISTS device_codes (
    device_code_hash TEXT PRIMARY KEY,
    user_code TEXT UNIQUE NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL,
    user_id TEXT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    interval_seconds INTEGER NOT NULL DEFAULT 5,
    last_polled_at TEXT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    user_id TEXT UNIQUE NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 0,
    plan TEXT NOT NULL DEFAULT 'none',
    source TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'inactive',
    external_subscription_id TEXT NULL,
    original_transaction_id TEXT NULL,
    environment TEXT NOT NULL DEFAULT 'Production',
    period_start TEXT NULL,
    period_end TEXT NULL,
    minutes_limit REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_external ON subscriptions(external_subscription_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_original_tx ON subscriptions(original_transaction_id);
CREATE TABLE IF NOT EXISTS usage_ledger (
    entry_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    delta_minutes REAL NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, ref_type, ref_id, period_key)
);
CREATE INDEX IF NOT EXISTS idx_ledger_user_period ON usage_ledger(user_id, period_key);
CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress_pct INTEGER NOT NULL DEFAULT 0,
    video_filename TEXT NOT NULL,
    video_path TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    reserved_minutes REAL NOT NULL DEFAULT 0.0,
    language TEXT NOT NULL DEFAULT 'auto',
    preset TEXT NOT NULL DEFAULT 'programming',
    prompt_override TEXT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',
    share_id TEXT NULL,
    review_id TEXT NULL,
    error_message TEXT NULL,
    worker_id TEXT NULL,
    idempotency_key TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(user_id, idempotency_key);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    share_id TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    job_id TEXT NULL,
    visibility TEXT NOT NULL,
    title TEXT NOT NULL,
    video_name TEXT NOT NULL,
    video_sha256 TEXT NULL,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    language TEXT NOT NULL DEFAULT 'en',
    storage_dir TEXT NOT NULL,
    report_json_rel TEXT NOT NULL,
    report_html_rel TEXT NULL,
    report_md_rel TEXT NULL,
    video_rel TEXT NULL,
    has_video INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id, created_at);
"""


# -- Records -----------------------------------------------------------------


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    email: str
    password_hash: str | None
    apple_sub: str | None
    display_name: str
    preferred_lang: str
    stripe_customer_id: str | None
    created_at: str
    updated_at: str

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "preferred_lang": self.preferred_lang,
            "apple_linked": self.apple_sub is not None,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    transport: str
    family_id: str
    expires_at: str


@dataclass(frozen=True)
class SubscriptionRecord:
    subscription_id: str
    user_id: str
    active: bool
    plan: str
    source: str
    status: str
    external_subscription_id: str | None
    original_transaction_id: str | None
    environment: str
    period_start: str | None
    period_end: str | None
    minutes_limit: float
    updated_at: str

    @property
    def period_key(self) -> str:
        return self.period_start or "open"

    def is_active(self, now: datetime) -> bool:
        if not self.active or self.status not in ACTIVE_STATUSES:
            return False
        end = parse_iso(self.period_end)
        return end is None or end > now


@dataclass(frozen=True)
class DeviceCodeRecord:
    device_code_hash: str
    user_code: str
    client_id: str
    status: str
    user_id: str | None
    interval_seconds: int
    last_polled_at: str | None
    expires_at: str
    created_at: str


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    user_id: str
    status: str
    stage: str
    progress_pct: int
    video_filename: str
    video_path: str
    duration_seconds: float
    reserved_minutes: float
    language: str
    preset: str
    prompt_override: str | None
    visibility: str
    share_id: str | None
    review_id: str | None
    error_message: str | None
    worker_id: str | None
    idempotency_key: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class NewJob:
    job_id: str
    user_id: str
    video_filename: str
    video_path: str
    duration_seconds: float
    language: str
    preset: str
    prompt_override: str | None
    visibility: str
    idempotency_key: str | None


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    share_id: str
    user_id: str
    source_kind: str
    job_id: str | None
    visibility: str
    title: str
    video_name: str
    video_sha256: str | None
    duration_seconds: float
    language: str
    storage_dir: str
    report_json_rel: str
    report_html_rel: str | None
    report_md_rel: str | None
    video_rel: str | None
    has_video: bool
    idempotency_key: str | None
    created_at: str
    updated_at: str

    @property
    def directory(self) -> Path:
        return Path(self.storage_dir)


@dataclass(frozen=True)
class ReservationOutcome:
    status: Literal["ok", "no_active_subscription", "quota_exceeded", "duplicate"]
    job: JobRecord | None = None


@dataclass(frozen=True)
class RefreshOutcome:
    status: Literal["ok", "invalid", "reused", "expired"]
    user_id: str | None = None


@dataclass(frozen=True)
class DevicePollOutcome:
    status: Literal[
        "ok",
        "authorization_pending",
        "slow_down",
        "expired_token",
        "access_denied",
        "invalid_grant",
    ]
    user_id: str | None = None


@dataclass(frozen=True)
class TokenRows:
    """Hashes and expiry instants for one access + refresh pair."""

    access_hash: str
    refresh_hash: str
    access_expires_at: str
    refresh_expires_at: str
    transport: str


def _user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        email=row["email"],
        password_hash=row["password_hash"],
        apple_sub=row["apple_sub"],
        display_name=row["display_name"],
        preferred_lang=row["preferred_lang"],
        stripe_customer_id=row["stripe_customer_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _subscription(row: sqlite3.Row) -> SubscriptionRecord:
    return SubscriptionRecord(
        subscription_id=row["subscription_id"],
        user_id=row["user_id"],
        active=bool(row["active"]),
        plan=row["plan"],
        source=row["source"],
        status=row["status"],
        external_subscription_id=row["external_subscription_id"],
        original_transaction_id=row["original_transaction_id"],
        environment=row["environment"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        minutes_limit=float(row["minutes_limit"]),
        updated_at=row["updated_at"],
    )


def _device(row: sqlite3.Row) -> DeviceCodeRecord:
    return DeviceCodeRecord(
        device_code_hash=row["device_code_hash"],
        user_code=row["user_code"],
        client_id=row["client_id"],
        status=row["status"],
        user_id=row["user_id"],
        interval_seconds=int(row["interval_seconds"]),
        last_polled_at=row["last_polled_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def _job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        user_id=row["user_id"],
        status=row["status"],
        stage=row["stage"],
        progress_pct=int(row["progress_pct"]),
        video_filename=row["video_filename"],
        video_path=row["video_path"],
        duration_seconds=float(row["duration_seconds"]),
        reserved_minutes=float(row["reserved_minutes"]),
        language=row["language"],
        preset=row["preset"],
        prompt_override=row["prompt_override"],
        visibility=row["visibility"],
        share_id=row["share_id"],
        review_id=row["review_id"],
        error_message=row["error_message"],
        worker_id=row["worker_id"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _review(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        review_id=row["review_id"],
        share_id=row["share_id"],
        user_id=row["user_id"],
        source_kind=row["source_kind"],
        job_id=row["job_id"],
        visibility=row["visibility"],
        title=row["title"],
        video_name=row["video_name"],
        video_sha256=row["video_sha256"],
        duration_seconds=float(row["duration_seconds"]),
        language=row["language"],
        storage_dir=row["storage_dir"],
        report_json_rel=row["report_json_rel"],
        report_html_rel=row["report_html_rel"],
        report_md_rel=row["report_md_rel"],
        video_rel=row["video_rel"],
        has_video=bool(row["has_video"]),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 2)


def _expired(expires_at: str, now: datetime) -> bool:
    parsed = parse_iso(expires_at)
    return parsed is None or parsed <= now


# -- Store -------------------------------------------------------------------


class CloudStore:
    """Typed repository facade over one SQLite database file."""

    def __init__(self, path: Path | str) -> None:
        raw = str(path)
        self._memory = raw == ":memory:"
        self._path = Path(raw)
        self._uri = (
            f"file:screenscribe-cloud-{secrets.token_hex(6)}?mode=memory&cache=shared"
            if self._memory
            else ""
        )
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._keeper: sqlite3.Connection | None = None
        if self._memory:
            self._keeper = self._open()

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> sqlite3.Connection:
        if self._memory:
            conn = sqlite3.connect(self._uri, uri=True, timeout=5.0, isolation_level=None)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if not self._memory:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._connections.append(conn)
        return conn

    def connection(self) -> sqlite3.Connection:
        """Connection owned by the calling thread (opened lazily)."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                # Owned by another thread; SQLite releases it with that thread.
                continue
        self._local = threading.local()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Explicit write transaction (``BEGIN IMMEDIATE`` takes the write lock)."""
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.connection().execute(sql, params).fetchone()
        return row

    def _all(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = self.connection().execute(sql, params).fetchall()
        return rows

    # -- Schema & health --------------------------------------------------

    def init_schema(self, now: datetime) -> None:
        conn = self.connection()
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, isoformat_z(now)),
        )

    def ping(self) -> bool:
        try:
            row = self._one("SELECT COUNT(*) AS n FROM schema_migrations", ())
        except sqlite3.Error:
            return False
        return row is not None

    # -- Users ------------------------------------------------------------

    def create_user(
        self,
        *,
        email: str,
        password_hash: str | None,
        display_name: str,
        preferred_lang: str,
        now: datetime,
        apple_sub: str | None = None,
    ) -> UserRecord | None:
        """Insert a user; ``None`` when the email (or Apple subject) is taken."""
        user_id = new_id("usr_")
        stamp = isoformat_z(now)
        try:
            self.connection().execute(
                "INSERT INTO users (user_id, email, password_hash, apple_sub, display_name,"
                " preferred_lang, stripe_customer_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    user_id,
                    email,
                    password_hash,
                    apple_sub,
                    display_name,
                    preferred_lang,
                    stamp,
                    stamp,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> UserRecord | None:
        row = self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return _user(row) if row is not None else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        row = self._one("SELECT * FROM users WHERE email = ?", (email,))
        return _user(row) if row is not None else None

    def get_user_by_apple_sub(self, apple_sub: str) -> UserRecord | None:
        row = self._one("SELECT * FROM users WHERE apple_sub = ?", (apple_sub,))
        return _user(row) if row is not None else None

    def get_user_by_stripe_customer(self, customer_id: str) -> UserRecord | None:
        row = self._one("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,))
        return _user(row) if row is not None else None

    def link_apple_sub(self, user_id: str, apple_sub: str, now: datetime) -> bool:
        try:
            cursor = self.connection().execute(
                "UPDATE users SET apple_sub = ?, updated_at = ? WHERE user_id = ? AND apple_sub IS NULL",
                (apple_sub, isoformat_z(now), user_id),
            )
        except sqlite3.IntegrityError:
            return False
        return cursor.rowcount > 0

    def set_stripe_customer(self, user_id: str, customer_id: str, now: datetime) -> None:
        self.connection().execute(
            "UPDATE users SET stripe_customer_id = ?, updated_at = ? WHERE user_id = ?",
            (customer_id, isoformat_z(now), user_id),
        )

    def delete_user(self, user_id: str) -> tuple[list[str], list[str]] | None:
        """Delete an account and cascade all owned sessions, jobs, reviews and usage rows.

        Returns ``(share_ids, job_ids)`` for post-commit filesystem cleanup, or
        ``None`` when the user does not exist.
        """
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing is None:
                return None
            share_rows = conn.execute(
                "SELECT share_id FROM reviews WHERE user_id = ?", (user_id,)
            ).fetchall()
            job_rows = conn.execute(
                "SELECT job_id FROM jobs WHERE user_id = ?", (user_id,)
            ).fetchall()
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return (
            [str(row["share_id"]) for row in share_rows],
            [str(row["job_id"]) for row in job_rows],
        )

    # -- Sessions & refresh tokens ----------------------------------------

    @staticmethod
    def _insert_token_rows(
        conn: sqlite3.Connection,
        *,
        user_id: str,
        family_id: str,
        rows: TokenRows,
        now: datetime,
    ) -> str:
        stamp = isoformat_z(now)
        conn.execute(
            "INSERT INTO sessions (session_id, token_hash, user_id, transport, family_id,"
            " expires_at, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                new_id("ses_"),
                rows.access_hash,
                user_id,
                rows.transport,
                family_id,
                rows.access_expires_at,
                stamp,
            ),
        )
        refresh_id = new_id("rft_")
        conn.execute(
            "INSERT INTO refresh_tokens (refresh_id, token_hash, user_id, family_id, expires_at,"
            " rotated_to, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
            (refresh_id, rows.refresh_hash, user_id, family_id, rows.refresh_expires_at, stamp),
        )
        return refresh_id

    @staticmethod
    def _revoke_family(conn: sqlite3.Connection, family_id: str, now: datetime) -> None:
        stamp = isoformat_z(now)
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
            (stamp, family_id),
        )
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
            (stamp, family_id),
        )

    def issue_token_pair(self, user_id: str, rows: TokenRows, now: datetime) -> str:
        """Persist a fresh access + refresh pair in a new family; returns family id."""
        family_id = new_id("fam_")
        with self.transaction() as conn:
            self._insert_token_rows(conn, user_id=user_id, family_id=family_id, rows=rows, now=now)
        return family_id

    def create_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        transport: str,
        expires_at: str,
        now: datetime,
        family_id: str | None = None,
    ) -> str:
        """Standalone session (web cookie) without a refresh token."""
        session_id = new_id("ses_")
        self.connection().execute(
            "INSERT INTO sessions (session_id, token_hash, user_id, transport, family_id,"
            " expires_at, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                session_id,
                token_hash,
                user_id,
                transport,
                family_id or new_id("fam_"),
                expires_at,
                isoformat_z(now),
            ),
        )
        return session_id

    def get_active_session(self, token_hash: str, now: datetime) -> SessionRecord | None:
        row = self._one(
            "SELECT session_id, user_id, transport, family_id, expires_at FROM sessions"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        )
        if row is None or _expired(row["expires_at"], now):
            return None
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            transport=row["transport"],
            family_id=row["family_id"],
            expires_at=row["expires_at"],
        )

    def revoke_session(self, token_hash: str, now: datetime) -> SessionRecord | None:
        """Revoke one access session; bearer families also lose their refresh token."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT session_id, user_id, transport, family_id, expires_at FROM sessions"
                " WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            self._revoke_family(conn, row["family_id"], now)
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            transport=row["transport"],
            family_id=row["family_id"],
            expires_at=row["expires_at"],
        )

    def rotate_refresh_token(
        self, refresh_hash: str, rows: TokenRows, now: datetime
    ) -> RefreshOutcome:
        """Atomically rotate a refresh token (RFC 9700 reuse detection).

        Presenting a refresh token that was already rotated (or revoked)
        revokes the whole token family: a stolen-and-replayed token kills the
        legitimate client's chain too, forcing a fresh sign-in.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT refresh_id, user_id, family_id, expires_at, rotated_to, revoked_at"
                " FROM refresh_tokens WHERE token_hash = ?",
                (refresh_hash,),
            ).fetchone()
            if row is None:
                return RefreshOutcome("invalid")
            if row["rotated_to"] is not None or row["revoked_at"] is not None:
                self._revoke_family(conn, row["family_id"], now)
                return RefreshOutcome("reused", row["user_id"])
            if _expired(row["expires_at"], now):
                conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = ? WHERE refresh_id = ?",
                    (isoformat_z(now), row["refresh_id"]),
                )
                return RefreshOutcome("expired", row["user_id"])
            new_refresh_id = self._insert_token_rows(
                conn, user_id=row["user_id"], family_id=row["family_id"], rows=rows, now=now
            )
            conn.execute(
                "UPDATE refresh_tokens SET rotated_to = ? WHERE refresh_id = ?",
                (new_refresh_id, row["refresh_id"]),
            )
            return RefreshOutcome("ok", row["user_id"])

    # -- Device authorization (RFC 8628) ----------------------------------

    def create_device_code(
        self,
        *,
        device_code_hash: str,
        user_code: str,
        client_id: str,
        interval_seconds: int,
        expires_at: str,
        now: datetime,
    ) -> bool:
        try:
            self.connection().execute(
                "INSERT INTO device_codes (device_code_hash, user_code, client_id, status, user_id,"
                " interval_seconds, last_polled_at, expires_at, created_at)"
                " VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)",
                (
                    device_code_hash,
                    user_code,
                    client_id,
                    interval_seconds,
                    expires_at,
                    isoformat_z(now),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_device_by_user_code(self, user_code: str) -> DeviceCodeRecord | None:
        row = self._one("SELECT * FROM device_codes WHERE user_code = ?", (user_code,))
        return _device(row) if row is not None else None

    def decide_device_code(
        self, user_code: str, *, user_id: str, approve: bool, now: datetime
    ) -> bool:
        """Approve/deny a pending, unexpired code; ``False`` when not decidable."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, expires_at FROM device_codes WHERE user_code = ?", (user_code,)
            ).fetchone()
            if row is None or row["status"] != "pending" or _expired(row["expires_at"], now):
                return False
            conn.execute(
                "UPDATE device_codes SET status = ?, user_id = ? WHERE user_code = ?",
                ("approved" if approve else "denied", user_id, user_code),
            )
        return True

    def poll_device_code(
        self, device_code_hash: str, rows: TokenRows, now: datetime
    ) -> DevicePollOutcome:
        """One RFC 8628 token poll; mints tokens exactly once on approval."""
        stamp = isoformat_z(now)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM device_codes WHERE device_code_hash = ?", (device_code_hash,)
            ).fetchone()
            if row is None:
                return DevicePollOutcome("invalid_grant")
            device = _device(row)
            if device.status == "consumed":
                return DevicePollOutcome("invalid_grant")
            if _expired(device.expires_at, now):
                return DevicePollOutcome("expired_token")
            if device.status == "denied":
                conn.execute(
                    "UPDATE device_codes SET status = 'consumed', last_polled_at = ?"
                    " WHERE device_code_hash = ?",
                    (stamp, device_code_hash),
                )
                return DevicePollOutcome("access_denied")
            if device.status == "approved" and device.user_id:
                conn.execute(
                    "UPDATE device_codes SET status = 'consumed', last_polled_at = ?"
                    " WHERE device_code_hash = ?",
                    (stamp, device_code_hash),
                )
                self._insert_token_rows(
                    conn, user_id=device.user_id, family_id=new_id("fam_"), rows=rows, now=now
                )
                return DevicePollOutcome("ok", device.user_id)
            last = parse_iso(device.last_polled_at)
            if (
                device.interval_seconds > 0
                and last is not None
                and (now - last).total_seconds() < device.interval_seconds
            ):
                conn.execute(
                    "UPDATE device_codes SET interval_seconds = interval_seconds + 5,"
                    " last_polled_at = ? WHERE device_code_hash = ?",
                    (stamp, device_code_hash),
                )
                return DevicePollOutcome("slow_down")
            conn.execute(
                "UPDATE device_codes SET last_polled_at = ? WHERE device_code_hash = ?",
                (stamp, device_code_hash),
            )
            return DevicePollOutcome("authorization_pending")

    # -- Subscriptions & ledger -------------------------------------------

    def get_subscription(self, user_id: str) -> SubscriptionRecord | None:
        row = self._one("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
        return _subscription(row) if row is not None else None

    def find_subscription_by_external_id(self, external_id: str) -> SubscriptionRecord | None:
        row = self._one(
            "SELECT * FROM subscriptions WHERE external_subscription_id = ?", (external_id,)
        )
        return _subscription(row) if row is not None else None

    def find_subscription_by_original_transaction(
        self, original_transaction_id: str
    ) -> SubscriptionRecord | None:
        row = self._one(
            "SELECT * FROM subscriptions WHERE original_transaction_id = ?",
            (original_transaction_id,),
        )
        return _subscription(row) if row is not None else None

    def upsert_subscription(
        self,
        user_id: str,
        *,
        active: bool,
        plan: str,
        source: str,
        status: str,
        minutes_limit: float,
        now: datetime,
        external_subscription_id: str | None = None,
        original_transaction_id: str | None = None,
        environment: str = "Production",
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> SubscriptionRecord:
        """Insert or replace the single subscription row of ``user_id``.

        A ``grant`` audit row is written for every distinct active period so
        the ledger shows when quota was credited (it does not affect usage).
        """
        stamp = isoformat_z(now)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO subscriptions (subscription_id, user_id, active, plan, source, status,"
                " external_subscription_id, original_transaction_id, environment, period_start,"
                " period_end, minutes_limit, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET active = excluded.active,"
                " plan = excluded.plan, source = excluded.source, status = excluded.status,"
                " external_subscription_id = COALESCE(excluded.external_subscription_id,"
                " subscriptions.external_subscription_id),"
                " original_transaction_id = COALESCE(excluded.original_transaction_id,"
                " subscriptions.original_transaction_id),"
                " environment = excluded.environment,"
                " period_start = COALESCE(excluded.period_start, subscriptions.period_start),"
                " period_end = COALESCE(excluded.period_end, subscriptions.period_end),"
                " minutes_limit = excluded.minutes_limit, updated_at = excluded.updated_at",
                (
                    new_id("sub_"),
                    user_id,
                    1 if active else 0,
                    plan,
                    source,
                    status,
                    external_subscription_id,
                    original_transaction_id,
                    environment,
                    period_start,
                    period_end,
                    float(minutes_limit),
                    stamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
            ).fetchone()
            record = _subscription(row)
            if record.active and record.minutes_limit > 0:
                conn.execute(
                    "INSERT OR IGNORE INTO usage_ledger (entry_id, user_id, kind, delta_minutes,"
                    " ref_type, ref_id, period_key, created_at) VALUES (?, ?, 'grant', ?, ?, ?, ?, ?)",
                    (
                        new_id("led_"),
                        user_id,
                        record.minutes_limit,
                        "stripe_period" if source == "stripe" else "appstore_tx",
                        record.external_subscription_id
                        or record.original_transaction_id
                        or record.subscription_id,
                        record.period_key,
                        stamp,
                    ),
                )
        return record

    def set_subscription_state(
        self, user_id: str, *, active: bool, status: str, now: datetime
    ) -> None:
        self.connection().execute(
            "UPDATE subscriptions SET active = ?, status = ?, updated_at = ? WHERE user_id = ?",
            (1 if active else 0, status, isoformat_z(now), user_id),
        )

    @staticmethod
    def _minutes_used(conn: sqlite3.Connection, user_id: str, period_key: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta_minutes), 0.0) AS total FROM usage_ledger"
            " WHERE user_id = ? AND period_key = ?"
            " AND kind IN ('reserve', 'refund', 'settle_delta')",
            (user_id, period_key),
        ).fetchone()
        total = float(row["total"]) if row is not None else 0.0
        return max(0.0, -total)

    def subscription_snapshot(self, user_id: str, now: datetime) -> dict[str, Any]:
        """``subscription`` block of ``GET /api/v1/auth/me``."""
        record = self.get_subscription(user_id)
        if record is None or not record.is_active(now):
            return inactive_subscription()
        used = self._minutes_used(self.connection(), user_id, record.period_key)
        remaining = max(0.0, record.minutes_limit - used)
        return {
            "active": True,
            "plan": record.plan,
            "source": record.source,
            "period_end": record.period_end,
            "minutes_limit": _number(record.minutes_limit),
            "minutes_used": round(used, 2),
            "minutes_remaining": round(remaining, 2),
        }

    def _gate(
        self, conn: sqlite3.Connection, user_id: str, required: float, now: datetime
    ) -> tuple[GateCode | None, SubscriptionRecord | None]:
        row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
        record = _subscription(row) if row is not None else None
        if record is None or not record.is_active(now):
            return "no_active_subscription", record
        remaining = record.minutes_limit - self._minutes_used(conn, user_id, record.period_key)
        if remaining <= 0 or remaining < required:
            return "quota_exceeded", record
        return None, record

    def check_gate(
        self, user_id: str, now: datetime, required_minutes: float = 0.0
    ) -> GateCode | None:
        """Read-only gate check (publish): active subscription and quota left."""
        with self.transaction(immediate=False) as conn:
            code, _record = self._gate(conn, user_id, required_minutes, now)
        return code

    def reserve_job(
        self, job: NewJob, required_minutes: float, now: datetime
    ) -> ReservationOutcome:
        """Gate + reserve minutes + enqueue the job in ONE write transaction.

        ``BEGIN IMMEDIATE`` serializes concurrent uploads of the same user, so
        two parallel jobs can never both spend the last remaining minutes.
        """
        stamp = isoformat_z(now)
        try:
            with self.transaction() as conn:
                code, record = self._gate(conn, job.user_id, required_minutes, now)
                if code is not None or record is None:
                    return ReservationOutcome(code or "no_active_subscription")
                conn.execute(
                    "INSERT INTO usage_ledger (entry_id, user_id, kind, delta_minutes, ref_type,"
                    " ref_id, period_key, created_at) VALUES (?, ?, 'reserve', ?, 'job', ?, ?, ?)",
                    (
                        new_id("led_"),
                        job.user_id,
                        -float(required_minutes),
                        job.job_id,
                        record.period_key,
                        stamp,
                    ),
                )
                conn.execute(
                    "INSERT INTO jobs (job_id, user_id, status, stage, progress_pct, video_filename,"
                    " video_path, duration_seconds, reserved_minutes, language, preset,"
                    " prompt_override, visibility, share_id, review_id, error_message, worker_id,"
                    " idempotency_key, created_at, updated_at)"
                    " VALUES (?, ?, 'queued', 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,"
                    " NULL, NULL, ?, ?, ?)",
                    (
                        job.job_id,
                        job.user_id,
                        job.video_filename,
                        job.video_path,
                        float(job.duration_seconds),
                        float(required_minutes),
                        job.language,
                        job.preset,
                        job.prompt_override,
                        job.visibility,
                        job.idempotency_key,
                        stamp,
                        stamp,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = (
                self.get_job_by_idempotency(job.user_id, job.idempotency_key)
                if job.idempotency_key
                else None
            )
            return ReservationOutcome("duplicate", existing)
        return ReservationOutcome("ok", self.get_job(job.job_id))

    def refund_job(self, job_id: str, now: datetime) -> bool:
        """Return a job's reserved minutes (idempotent; only before paid AI ran)."""
        cursor = self.connection().execute(
            "INSERT OR IGNORE INTO usage_ledger (entry_id, user_id, kind, delta_minutes, ref_type,"
            " ref_id, period_key, created_at)"
            " SELECT ?, user_id, 'refund', -delta_minutes, ref_type, ref_id, period_key, ?"
            " FROM usage_ledger WHERE kind = 'reserve' AND ref_type = 'job' AND ref_id = ?",
            (new_id("led_"), isoformat_z(now), job_id),
        )
        return cursor.rowcount > 0

    # -- Stripe webhook idempotency ---------------------------------------

    def stripe_event_seen(self, event_id: str) -> bool:
        row = self._one(
            "SELECT 1 AS seen FROM stripe_webhook_events WHERE event_id = ?", (event_id,)
        )
        return row is not None

    def mark_stripe_event(self, event_id: str, event_type: str, now: datetime) -> bool:
        cursor = self.connection().execute(
            "INSERT OR IGNORE INTO stripe_webhook_events (event_id, event_type, processed_at)"
            " VALUES (?, ?, ?)",
            (event_id, event_type, isoformat_z(now)),
        )
        return cursor.rowcount > 0

    # -- Jobs -------------------------------------------------------------

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self._one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return _job(row) if row is not None else None

    def get_job_for_user(self, job_id: str, user_id: str) -> JobRecord | None:
        row = self._one("SELECT * FROM jobs WHERE job_id = ? AND user_id = ?", (job_id, user_id))
        return _job(row) if row is not None else None

    def get_job_by_idempotency(self, user_id: str, key: str | None) -> JobRecord | None:
        if not key:
            return None
        row = self._one(
            "SELECT * FROM jobs WHERE user_id = ? AND idempotency_key = ?", (user_id, key)
        )
        return _job(row) if row is not None else None

    def list_jobs(self, user_id: str, limit: int = 50) -> list[JobRecord]:
        rows = self._all(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 200))),
        )
        return [_job(row) for row in rows]

    def claim_next_job(self, worker_id: str, now: datetime) -> JobRecord | None:
        stamp = isoformat_z(now)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'processing', stage = 'audio', progress_pct = 5,"
                " worker_id = ?, updated_at = ? WHERE job_id = ? AND status = 'queued'",
                (worker_id, stamp, row["job_id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return _job(claimed) if claimed is not None else None

    def update_job_stage(self, job_id: str, stage: str, progress_pct: int, now: datetime) -> None:
        self.connection().execute(
            "UPDATE jobs SET stage = ?, progress_pct = ?, updated_at = ?"
            " WHERE job_id = ? AND status = 'processing'",
            (stage, max(0, min(100, progress_pct)), isoformat_z(now), job_id),
        )

    def complete_job(self, job_id: str, *, share_id: str, review_id: str, now: datetime) -> None:
        self.connection().execute(
            "UPDATE jobs SET status = 'completed', stage = 'done', progress_pct = 100,"
            " share_id = ?, review_id = ?, error_message = NULL, updated_at = ? WHERE job_id = ?",
            (share_id, review_id, isoformat_z(now), job_id),
        )

    def fail_job(self, job_id: str, message: str, now: datetime) -> None:
        self.connection().execute(
            "UPDATE jobs SET status = 'failed', stage = 'error', error_message = ?, updated_at = ?"
            " WHERE job_id = ?",
            (message[:500], isoformat_z(now), job_id),
        )

    def fail_stale_jobs(self, older_than: datetime, now: datetime) -> list[str]:
        """Fail jobs stuck in ``processing`` since before ``older_than`` (crashed worker)."""
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE status = 'processing' AND updated_at < ?",
                (isoformat_z(older_than),),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            for job_id in job_ids:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', stage = 'error',"
                    " error_message = 'Processing was interrupted. Please upload again.',"
                    " updated_at = ? WHERE job_id = ?",
                    (isoformat_z(now), job_id),
                )
        return job_ids

    # -- Reviews ----------------------------------------------------------

    def create_review(self, review: ReviewRecord) -> tuple[ReviewRecord, bool]:
        """Insert a review; returns ``(record, created)``.

        A duplicate ``(user_id, idempotency_key)`` returns the existing review
        with ``created=False`` so publish retries are side-effect free.
        """
        try:
            self.connection().execute(
                "INSERT INTO reviews (review_id, share_id, user_id, source_kind, job_id, visibility,"
                " title, video_name, video_sha256, duration_seconds, language, storage_dir,"
                " report_json_rel, report_html_rel, report_md_rel, video_rel, has_video,"
                " idempotency_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.share_id,
                    review.user_id,
                    review.source_kind,
                    review.job_id,
                    review.visibility,
                    review.title,
                    review.video_name,
                    review.video_sha256,
                    float(review.duration_seconds),
                    review.language,
                    review.storage_dir,
                    review.report_json_rel,
                    review.report_html_rel,
                    review.report_md_rel,
                    review.video_rel,
                    1 if review.has_video else 0,
                    review.idempotency_key,
                    review.created_at,
                    review.updated_at,
                ),
            )
        except sqlite3.IntegrityError:
            existing = (
                self.get_review_by_idempotency(review.user_id, review.idempotency_key)
                if review.idempotency_key
                else None
            )
            if existing is None:
                raise
            return existing, False
        stored = self.get_review_by_share(review.share_id)
        if stored is None:
            raise RuntimeError("Review row vanished right after insert.")
        return stored, True

    def get_review_by_share(self, share_id: str) -> ReviewRecord | None:
        row = self._one("SELECT * FROM reviews WHERE share_id = ?", (share_id,))
        return _review(row) if row is not None else None

    def get_review_by_idempotency(self, user_id: str, key: str | None) -> ReviewRecord | None:
        if not key:
            return None
        row = self._one(
            "SELECT * FROM reviews WHERE user_id = ? AND idempotency_key = ?", (user_id, key)
        )
        return _review(row) if row is not None else None

    def list_reviews(self, user_id: str, limit: int = 100) -> list[ReviewRecord]:
        rows = self._all(
            "SELECT * FROM reviews WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 500))),
        )
        return [_review(row) for row in rows]

    def delete_review(self, share_id: str, user_id: str) -> ReviewRecord | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM reviews WHERE share_id = ? AND user_id = ?", (share_id, user_id)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM reviews WHERE share_id = ?", (share_id,))
            conn.execute(
                "UPDATE jobs SET share_id = NULL WHERE share_id = ? AND user_id = ?",
                (share_id, user_id),
            )
        return _review(row)


__all__ = [
    "ACTIVE_STATUSES",
    "SCHEMA_VERSION",
    "CloudStore",
    "DeviceCodeRecord",
    "DevicePollOutcome",
    "JobRecord",
    "NewJob",
    "RefreshOutcome",
    "ReservationOutcome",
    "ReviewRecord",
    "SessionRecord",
    "SubscriptionRecord",
    "TokenRows",
    "UserRecord",
]
