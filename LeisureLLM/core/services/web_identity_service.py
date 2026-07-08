"""Durable web identity and session management for the admin console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.actors import ActorContext, normalize_role
from core.services.operational_record_service import OperationalRecordService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _normalize_username(username: str) -> str:
    normalized = str(username or "").strip().lower()
    if not normalized:
        raise ValueError("username is required")
    if len(normalized) < 3:
        raise ValueError("username must be at least 3 characters")
    if any(ch.isspace() for ch in normalized):
        raise ValueError("username cannot contain spaces")
    return normalized


def _validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8:
        raise ValueError("password must be at least 8 characters")
    return value


def _hash_password(password: str) -> str:
    raw_password = _validate_password(password).encode("utf-8")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(raw_password, salt=salt, n=16384, r=8, p=1, dklen=64)
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def _verify_password(password: str, encoded_hash: str) -> bool:
    try:
        scheme, n_value, r_value, p_value, salt_b64, digest_b64 = encoded_hash.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        candidate = hashlib.scrypt(
            _validate_password(password).encode("utf-8"),
            salt=salt,
            n=int(n_value),
            r=int(r_value),
            p=int(p_value),
            dklen=len(expected),
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def _hash_session_token(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


class IdentityError(Exception):
    """Base exception for web identity failures."""


class AuthenticationError(IdentityError):
    """Raised when credentials or session state are invalid."""


class AuthorizationError(IdentityError):
    """Raised when a role or bootstrap precondition is not met."""


class WebIdentityService:
    session_cookie_name = "mka_session"
    session_ttl = timedelta(days=7)

    def __init__(self, db: Any):
        self.db = db
        self.operational_records = OperationalRecordService(db)

    async def ensure_schema(self) -> None:
        async with self.db.acquire() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stable_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'member')),
                    password_hash TEXT NOT NULL,
                    actor_id INTEGER NOT NULL REFERENCES operational_actors(id) ON DELETE RESTRICT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    bootstrap_source TEXT,
                    created_by_account_id INTEGER REFERENCES web_accounts(id) ON DELETE SET NULL,
                    last_login_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_web_accounts_role ON web_accounts(role, is_active);

                CREATE TABLE IF NOT EXISTS web_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stable_id TEXT NOT NULL UNIQUE,
                    account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
                    session_token_hash TEXT NOT NULL UNIQUE,
                    ip_address TEXT,
                    user_agent TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_web_sessions_account ON web_sessions(account_id, expires_at DESC);
                CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at, revoked_at);

                CREATE TABLE IF NOT EXISTS operational_record_legacy_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES operational_records(id) ON DELETE CASCADE,
                    legacy_table TEXT NOT NULL,
                    legacy_id INTEGER NOT NULL,
                    linked_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(legacy_table, legacy_id),
                    UNIQUE(record_id, legacy_table, legacy_id)
                );
                CREATE INDEX IF NOT EXISTS idx_operational_record_legacy_links_record
                    ON operational_record_legacy_links(record_id, linked_at DESC);
                """
            )
            await conn.commit()

    async def has_any_accounts(self) -> bool:
        await self.ensure_schema()
        row = await self.db.fetchone("SELECT COUNT(*) FROM web_accounts")
        return bool(row and row[0])

    async def get_account_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        await self.ensure_schema()
        row = await self.db.fetchone("""
SELECT wa.*, oa.stable_id AS actor_stable_id, oa.actor_kind, oa.external_ref,
oa.display_name AS actor_display_name
FROM web_accounts wa
JOIN operational_actors oa ON oa.id = wa.actor_id
WHERE wa.username_normalized = ?
""",
(_normalize_username(username),),)
        return dict(row) if row else None

    async def list_accounts(self) -> list[Dict[str, Any]]:
        await self.ensure_schema()
        async with self.db.acquire() as conn, conn.execute(
            """
                SELECT wa.id, wa.stable_id, wa.username, wa.display_name, wa.role, wa.is_active,
                       wa.bootstrap_source, wa.last_login_at, wa.created_at, wa.updated_at,
                       oa.stable_id AS actor_stable_id
                FROM web_accounts wa
                JOIN operational_actors oa ON oa.id = wa.actor_id
                ORDER BY wa.role DESC, wa.username_normalized ASC
                """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def create_account(
        self,
        *,
        username: str,
        password: str,
        display_name: Optional[str],
        role: str,
        created_by_account_id: Optional[int] = None,
        bootstrap_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.ensure_schema()

        normalized_username = _normalize_username(username)
        normalized_role = normalize_role(role)
        password_hash = _hash_password(password)
        account_stable_id = f"webacct_{uuid.uuid4().hex}"
        actor = await self.operational_records.ensure_actor(
            actor_kind="web_account",
            external_ref=account_stable_id,
            display_name=(display_name or username).strip()[:200],
        )
        now = _utc_now_iso()

        async with self.db.acquire() as conn:
            async with conn.execute(
                "SELECT id FROM web_accounts WHERE username_normalized = ?",
                (normalized_username,),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                raise IdentityError(f"username '{normalized_username}' already exists")

            async with conn.execute(
                """
                INSERT INTO web_accounts
                    (stable_id, username, username_normalized, display_name, role,
                     password_hash, actor_id, bootstrap_source, created_by_account_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_stable_id,
                    username.strip(),
                    normalized_username,
                    (display_name or username).strip()[:200],
                    normalized_role,
                    password_hash,
                    actor["id"],
                    bootstrap_source,
                    created_by_account_id,
                    now,
                    now,
                ),
            ) as cur:
                cur.lastrowid
            await conn.commit()

        account = await self.get_account_by_username(normalized_username)
        if not account:
            raise IdentityError("failed to create account")
        return account

    async def bootstrap_admin(
        self,
        *,
        bootstrap_token: str,
        expected_bootstrap_token: str,
        username: str,
        password: str,
        display_name: Optional[str],
    ) -> Dict[str, Any]:
        await self.ensure_schema()
        if await self.has_any_accounts():
            raise AuthorizationError("bootstrap admin already exists")
        if not hmac.compare_digest(str(bootstrap_token or "").strip(), str(expected_bootstrap_token or "").strip()):
            raise AuthenticationError("invalid bootstrap token")
        return await self.create_account(
            username=username,
            password=password,
            display_name=display_name,
            role="admin",
            bootstrap_source="legacy_admin_token",
        )

    async def authenticate(
        self,
        *,
        username: str,
        password: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[Dict[str, Any], str]:
        await self.ensure_schema()
        account = await self.get_account_by_username(username)
        if not account or not account.get("is_active"):
            raise AuthenticationError("invalid username or password")
        if not _verify_password(password, str(account["password_hash"])):
            raise AuthenticationError("invalid username or password")

        session_token = secrets.token_urlsafe(48)
        session_hash = _hash_session_token(session_token)
        now = _utc_now_iso()
        expires_at = (_utc_now() + self.session_ttl).isoformat()

        async with self.db.acquire() as conn:
            await conn.execute(
                "UPDATE web_accounts SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, account["id"]),
            )
            await conn.execute(
                """
                INSERT INTO web_sessions
                    (stable_id, account_id, session_token_hash, ip_address, user_agent,
                     created_at, last_seen_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"websess_{uuid.uuid4().hex}",
                    account["id"],
                    session_hash,
                    ip_address,
                    user_agent,
                    now,
                    now,
                    expires_at,
                ),
            )
            await conn.commit()
        return account, session_token

    async def revoke_session(self, session_token: Optional[str]) -> None:
        await self.ensure_schema()
        if not session_token:
            return
        session_hash = _hash_session_token(session_token)
        await self.db.execute(
            "UPDATE web_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE session_token_hash = ?",
            (_utc_now_iso(), session_hash),
            )
    async def get_session_actor(self, session_token: Optional[str]) -> Optional[ActorContext]:
        await self.ensure_schema()
        if not session_token:
            return None
        session_hash = _hash_session_token(session_token)
        now = _utc_now_iso()
        async with self.db.acquire() as conn:
            async with conn.execute(
                """
                SELECT ws.id AS session_id,
                       wa.id AS account_id,
                       wa.username,
                       wa.display_name,
                       wa.role,
                       oa.id AS actor_id,
                       oa.stable_id AS actor_stable_id,
                       oa.actor_kind,
                       oa.external_ref,
                       oa.display_name AS actor_display_name
                FROM web_sessions ws
                JOIN web_accounts wa ON wa.id = ws.account_id
                JOIN operational_actors oa ON oa.id = wa.actor_id
                WHERE ws.session_token_hash = ?
                  AND ws.revoked_at IS NULL
                  AND ws.expires_at > ?
                  AND wa.is_active = 1
                """,
                (session_hash, now),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            await conn.execute(
                "UPDATE web_sessions SET last_seen_at = ? WHERE id = ?",
                (now, row["session_id"]),
            )
            await conn.commit()

        return ActorContext(
            actor_id=int(row["actor_id"]),
            stable_id=str(row["actor_stable_id"]),
            actor_kind=str(row["actor_kind"]),
            external_ref=str(row["external_ref"]),
            display_name=row["actor_display_name"] or row["display_name"],
            role=str(row["role"]),
            account_id=int(row["account_id"]),
            username=row["username"],
            session_id=int(row["session_id"]),
            auth_source="web_session",
        )