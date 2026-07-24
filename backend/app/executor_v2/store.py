from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - SQLite-only local development
    psycopg = None
    dict_row = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch_now() -> float:
    return datetime.now(timezone.utc).timestamp()


class SecretBox:
    def __init__(self) -> None:
        raw_key = os.getenv("EXECUTOR_MASTER_KEY", "").strip().encode("utf-8")
        if len(raw_key) < 32:
            raise RuntimeError(
                "EXECUTOR_MASTER_KEY is required and must contain "
                "at least 32 characters"
            )
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
        self._fernet = Fernet(derived_key)

    def encrypt(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return self._fernet.encrypt(raw.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        if not token:
            return {}
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
            value = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored account session could not be decrypted") from exc
        return value if isinstance(value, dict) else {}


class DatabaseConnection:
    """Small DB-API compatibility wrapper for SQLite and PostgreSQL."""

    def __init__(self, raw: Any, backend: str) -> None:
        self.raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Iterable[Any] = ()):
        if self.backend == "postgresql":
            sql = sql.replace(
                "MIN(resources.next_run_at, excluded.next_run_at)",
                "LEAST(resources.next_run_at, excluded.next_run_at)",
            )
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, tuple(params))


class ExecutorStore:
    def __init__(self) -> None:
        data_dir = Path(os.getenv("EXECUTOR_DATA_DIR", "/tmp/netdisk-executor"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "executor-v2.sqlite3"
        self.profile_dir = data_dir / "browser-profiles"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.backend = "postgresql" if self.database_url else "sqlite"
        require_postgres = os.getenv("EXECUTOR_REQUIRE_POSTGRES", "").strip().lower()
        if require_postgres in {"1", "true", "yes"} and not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is required for this deployment; "
                "connect a Neon PostgreSQL database"
            )
        if self.backend == "postgresql" and psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed")
        self._lock = threading.RLock()
        self.secrets = SecretBox()
        self._initialize()

    @contextmanager
    def connection(self):
        if self.backend == "postgresql":
            connection = psycopg.connect(
                self.database_url,
                autocommit=True,
                connect_timeout=30,
                row_factory=dict_row,
            )
        else:
            connection = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield DatabaseConnection(connection, self.backend)
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self.connection() as db:
            if self.backend == "sqlite":
                db.execute("PRAGMA journal_mode = WAL")
            statements = (
                """
                CREATE TABLE IF NOT EXISTS executor_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    provider TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'login-required',
                    display_name TEXT NOT NULL DEFAULT '',
                    encrypted_secret TEXT NOT NULL DEFAULT '',
                    last_verified_at TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS login_sessions (
                    session_id TEXT PRIMARY KEY,
                    public_token TEXT UNIQUE NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS resources (
                    resource_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_code TEXT NOT NULL DEFAULT '',
                    target_folder TEXT NOT NULL,
                    current_share_url TEXT NOT NULL DEFAULT '',
                    monitor_enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT '待处理',
                    share_url TEXT NOT NULL DEFAULT '',
                    share_code TEXT NOT NULL DEFAULT '',
                    episode_info TEXT NOT NULL DEFAULT '等待识别',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    source_files_json TEXT NOT NULL DEFAULT '[]',
                    target_folder_id TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT,
                    last_synced_at TEXT,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    pending_action TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_resources_due
                ON resources(monitor_enabled, next_run_at)
                """,
            )
            for statement in statements:
                db.execute(statement)
            if self.backend == "postgresql":
                db.execute(
                    "ALTER TABLE resources "
                    "ADD COLUMN IF NOT EXISTS "
                    "pending_action TEXT NOT NULL DEFAULT ''"
                )
            else:
                resource_columns = {
                    str(row["name"])
                    for row in db.execute("PRAGMA table_info(resources)").fetchall()
                }
                if "pending_action" not in resource_columns:
                    db.execute(
                        "ALTER TABLE resources "
                        "ADD COLUMN pending_action TEXT NOT NULL DEFAULT ''"
                    )
            instance = db.execute(
                "SELECT value FROM executor_meta WHERE key = 'instance_id'"
            ).fetchone()
            if not instance:
                db.execute(
                    "INSERT INTO executor_meta(key, value) VALUES('instance_id', ?)",
                    (f"render-{secrets.token_hex(8)}",),
                )
            db.execute(
                """
                INSERT INTO executor_meta(key, value)
                VALUES('settings', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (
                    json.dumps(
                        {
                            "checkIntervalHours": 3,
                            "autoShare": True,
                            "folderByTitle": True,
                            "retryEnabled": True,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            for provider in ("baidu", "quark", "uc", "xunlei"):
                db.execute(
                    """
                    INSERT INTO accounts(provider, state, updated_at)
                    VALUES(?, 'login-required', ?)
                    ON CONFLICT(provider) DO NOTHING
                    """,
                    (provider, utc_now()),
                )
            db.execute(
                """
                UPDATE resources
                SET status = '待处理', next_run_at = 0,
                    pending_action = CASE
                        WHEN pending_action = '' THEN 'check'
                        ELSE pending_action
                    END,
                    message = '服务重启后任务已恢复', updated_at = ?
                WHERE status IN ('转存中', '检查中')
                """,
                (utc_now(),),
            )
            db.execute(
                """
                UPDATE login_sessions
                SET status = 'expired',
                    message = '执行器重启，请重新开始登录',
                    updated_at = ?
                WHERE status = 'pending'
                """,
                (utc_now(),),
            )

    def instance_id(self) -> str:
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM executor_meta WHERE key = 'instance_id'"
            ).fetchone()
        return str(row["value"]) if row else "render-unknown"

    def get_settings(self) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM executor_meta WHERE key = 'settings'"
            ).fetchone()
        try:
            payload = json.loads(str(row["value"])) if row else {}
        except json.JSONDecodeError:
            payload = {}
        return normalize_settings(payload)

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = normalize_settings(payload)
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO executor_meta(key, value) VALUES('settings', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (json.dumps(settings, separators=(",", ":")),),
            )
        return settings

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT provider, state, display_name, last_verified_at, message
                FROM accounts ORDER BY
                CASE provider
                    WHEN 'baidu' THEN 1
                    WHEN 'quark' THEN 2
                    WHEN 'uc' THEN 3
                    WHEN 'xunlei' THEN 4
                    ELSE 9
                END
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_account(self, provider: str, *, include_secret: bool = False):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE provider = ?",
                (provider,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        if include_secret:
            result["secret"] = self.secrets.decrypt(
                str(result.pop("encrypted_secret", ""))
            )
        else:
            result.pop("encrypted_secret", None)
        return result

    def save_account(
        self,
        provider: str,
        secret: dict[str, Any],
        *,
        display_name: str = "",
        state: str = "connected",
        message: str = "会话有效",
    ) -> None:
        now = utc_now()
        encrypted = self.secrets.encrypt(secret)
        with self._lock, self.connection() as db:
            db.execute(
                """
                UPDATE accounts
                SET state = ?, display_name = ?, encrypted_secret = ?,
                    last_verified_at = ?, message = ?, updated_at = ?
                WHERE provider = ?
                """,
                (
                    state,
                    display_name[:160],
                    encrypted,
                    now,
                    message[:500],
                    now,
                    provider,
                ),
            )

    def update_account_state(
        self,
        provider: str,
        state: str,
        message: str,
        *,
        verified: bool = False,
    ) -> None:
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute(
                """
                UPDATE accounts
                SET state = ?, message = ?,
                    last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END,
                    updated_at = ?
                WHERE provider = ?
                """,
                (state, message[:500], bool(verified), now, now, provider),
            )

    def create_login_session(
        self,
        provider: str,
        metadata: dict[str, Any] | None = None,
        *,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        now = utc_now()
        session = {
            "session_id": secrets.token_urlsafe(24),
            "public_token": secrets.token_urlsafe(32),
            "provider": provider,
            "status": "pending",
            "message": "等待你在官方页面完成登录",
            "expires_at": epoch_now() + ttl_seconds,
            "metadata_json": json.dumps(
                metadata or {}, ensure_ascii=False, separators=(",", ":")
            ),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO login_sessions(
                    session_id, public_token, provider, status, message,
                    expires_at, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(session.values()),
            )
        return session

    def expire_pending_logins(
        self,
        provider: str | None = None,
    ) -> list[str]:
        with self._lock, self.connection() as db:
            if provider:
                rows = db.execute(
                    """
                    SELECT session_id FROM login_sessions
                    WHERE provider = ? AND status = 'pending'
                    """,
                    (provider,),
                ).fetchall()
                db.execute(
                    """
                    UPDATE login_sessions
                    SET status = 'expired', message = '已开始新的登录会话',
                        updated_at = ?
                    WHERE provider = ? AND status = 'pending'
                    """,
                    (utc_now(), provider),
                )
            else:
                rows = db.execute(
                    """
                    SELECT session_id FROM login_sessions
                    WHERE status = 'pending'
                    """
                ).fetchall()
                db.execute(
                    """
                    UPDATE login_sessions
                    SET status = 'expired', message = '已开始新的登录会话',
                        updated_at = ?
                    WHERE status = 'pending'
                    """,
                    (utc_now(),),
                )
        return [str(row["session_id"]) for row in rows]

    def expire_timed_out_logins(self) -> list[str]:
        with self._lock, self.connection() as db:
            rows = db.execute(
                """
                SELECT session_id FROM login_sessions
                WHERE status = 'pending' AND expires_at < ?
                """,
                (epoch_now(),),
            ).fetchall()
            if rows:
                db.execute(
                    """
                    UPDATE login_sessions
                    SET status = 'expired',
                        message = '登录会话已过期，请重新开始',
                        updated_at = ?
                    WHERE status = 'pending' AND expires_at < ?
                    """,
                    (utc_now(), epoch_now()),
                )
        return [str(row["session_id"]) for row in rows]

    def get_login_session(
        self,
        *,
        session_id: str | None = None,
        public_token: str | None = None,
    ):
        field = "session_id" if session_id else "public_token"
        value = session_id or public_token or ""
        with self.connection() as db:
            row = db.execute(
                f"SELECT * FROM login_sessions WHERE {field} = ?",
                (value,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.pop("metadata_json"))
        except json.JSONDecodeError:
            result["metadata"] = {}
        if result["status"] == "pending" and result["expires_at"] < epoch_now():
            self.update_login_session(
                result["session_id"],
                status="expired",
                message="登录会话已过期，请重新开始",
            )
            result["status"] = "expired"
            result["message"] = "登录会话已过期，请重新开始"
        return result

    def update_login_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        columns: list[str] = []
        values: list[Any] = []
        if status is not None:
            columns.append("status = ?")
            values.append(status)
        if message is not None:
            columns.append("message = ?")
            values.append(message[:500])
        if metadata is not None:
            columns.append("metadata_json = ?")
            values.append(
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            )
        columns.append("updated_at = ?")
        values.append(utc_now())
        values.append(session_id)
        with self._lock, self.connection() as db:
            db.execute(
                f"UPDATE login_sessions SET {', '.join(columns)} WHERE session_id = ?",
                values,
            )

    def register_resources(
        self,
        resources: Iterable[dict[str, Any]],
        *,
        interval_seconds: int,
    ) -> tuple[int, int]:
        accepted = 0
        rejected = 0
        now = utc_now()
        with self._lock, self.connection() as db:
            for item in resources:
                try:
                    resource_key = str(item["resourceKey"]).strip()
                    provider = str(item["provider"]).strip()
                    title = str(item["title"]).strip()
                    source_url = str(item["sourceUrl"]).strip()
                    target_folder = str(item["targetFolder"]).strip()
                except (KeyError, TypeError, AttributeError):
                    rejected += 1
                    continue
                if (
                    not resource_key
                    or provider not in {"baidu", "quark", "uc", "xunlei"}
                    or not title
                    or not source_url
                    or not target_folder
                ):
                    rejected += 1
                    continue

                existing = db.execute(
                    "SELECT share_url FROM resources WHERE resource_key = ?",
                    (resource_key,),
                ).fetchone()
                current_share = str(item.get("currentShareUrl") or "").strip()
                share_url = str(existing["share_url"]) if existing else current_share
                immediate = (
                    0 if not share_url else epoch_now() + max(300, interval_seconds)
                )
                db.execute(
                    """
                    INSERT INTO resources(
                        resource_key, provider, title, source_url, source_code,
                        target_folder, current_share_url, monitor_enabled,
                        next_run_at, pending_action, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(resource_key) DO UPDATE SET
                        provider = excluded.provider,
                        title = excluded.title,
                        source_url = excluded.source_url,
                        source_code = excluded.source_code,
                        target_folder = excluded.target_folder,
                        current_share_url = excluded.current_share_url,
                        monitor_enabled = excluded.monitor_enabled,
                        next_run_at = CASE
                            WHEN resources.share_url = '' THEN 0
                            ELSE MIN(resources.next_run_at, excluded.next_run_at)
                        END,
                        pending_action = CASE
                            WHEN resources.share_url = '' THEN 'transfer'
                            ELSE resources.pending_action
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        resource_key,
                        provider,
                        title[:300],
                        source_url[:2000],
                        str(item.get("sourceCode") or "")[:100],
                        target_folder[:800],
                        current_share[:2000],
                        0 if item.get("monitorEnabled") is False else 1,
                        immediate,
                        "transfer" if not share_url else "",
                        now,
                        now,
                    ),
                )
                accepted += 1
        return accepted, rejected

    def get_resources(self, resource_keys: Iterable[str]) -> list[dict[str, Any]]:
        keys = [str(key) for key in resource_keys if str(key)]
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        with self.connection() as db:
            rows = db.execute(
                f"""
                SELECT * FROM resources
                WHERE resource_key IN ({placeholders})
                """,
                keys,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_resource(self, resource_key: str):
        rows = self.get_resources([resource_key])
        return rows[0] if rows else None

    def due_resources(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT * FROM resources
                WHERE (monitor_enabled = 1 OR pending_action != '')
                  AND next_run_at <= ?
                  AND status NOT IN ('转存中', '检查中')
                ORDER BY next_run_at ASC
                LIMIT ?
                """,
                (epoch_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_resource(self, resource_key: str, status: str) -> bool:
        with self._lock, self.connection() as db:
            cursor = db.execute(
                """
                UPDATE resources
                SET status = ?, message = '', updated_at = ?
                WHERE resource_key = ?
                  AND status NOT IN ('转存中', '检查中')
                """,
                (status, utc_now(), resource_key),
            )
        return cursor.rowcount == 1

    def enqueue_resource(self, resource_key: str, action: str) -> bool:
        normalized = "check" if action == "check" else "transfer"
        with self._lock, self.connection() as db:
            cursor = db.execute(
                """
                UPDATE resources
                SET pending_action = ?, next_run_at = 0, retry_count = 0,
                    status = '待处理', message = '任务已进入持久化队列',
                    updated_at = ?
                WHERE resource_key = ?
                """,
                (normalized, utc_now(), resource_key),
            )
        return cursor.rowcount == 1

    def resume_provider_resources(self, provider: str) -> int:
        with self._lock, self.connection() as db:
            cursor = db.execute(
                """
                UPDATE resources
                SET status = '待处理', next_run_at = 0,
                    pending_action = CASE
                        WHEN share_url = '' THEN 'transfer'
                        WHEN pending_action = '' THEN 'check'
                        ELSE pending_action
                    END,
                    message = '账号已重新连接，任务自动恢复',
                    updated_at = ?
                WHERE provider = ? AND status = '失败'
                  AND (monitor_enabled = 1 OR pending_action != '')
                """,
                (utc_now(), provider),
            )
        return cursor.rowcount

    def update_resource(self, resource_key: str, **fields: Any) -> None:
        allowed = {
            "status",
            "share_url",
            "share_code",
            "episode_info",
            "source_fingerprint",
            "source_files_json",
            "target_folder_id",
            "last_checked_at",
            "last_synced_at",
            "next_run_at",
            "retry_count",
            "pending_action",
            "message",
        }
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if not pairs:
            return
        columns = [f"{key} = ?" for key, _ in pairs]
        values = [value for _, value in pairs]
        columns.append("updated_at = ?")
        values.extend([utc_now(), resource_key])
        with self._lock, self.connection() as db:
            db.execute(
                f"UPDATE resources SET {', '.join(columns)} WHERE resource_key = ?",
                values,
            )


def normalize_settings(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        interval = int(payload.get("checkIntervalHours", 3))
    except (TypeError, ValueError):
        interval = 3
    if interval not in {1, 3, 6, 12, 24}:
        interval = 3
    configured_minutes = os.getenv("EXECUTOR_CHECK_INTERVAL_MINUTES", "").strip()
    try:
        interval_minutes = int(
            configured_minutes or payload.get("checkIntervalMinutes") or interval * 60
        )
    except (TypeError, ValueError):
        interval_minutes = interval * 60
    interval_minutes = max(5, min(interval_minutes, 1440))
    return {
        "checkIntervalHours": interval,
        "checkIntervalMinutes": interval_minutes,
        "autoShare": bool(payload.get("autoShare", True)),
        "folderByTitle": bool(payload.get("folderByTitle", True)),
        "retryEnabled": bool(payload.get("retryEnabled", True)),
    }


def settings_interval_seconds(settings: dict[str, Any]) -> int:
    try:
        minutes = int(settings.get("checkIntervalMinutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes:
        return max(5, min(minutes, 1440)) * 60
    try:
        hours = int(settings.get("checkIntervalHours") or 3)
    except (TypeError, ValueError):
        hours = 3
    return max(1, min(hours, 24)) * 3600
