from __future__ import annotations

import hmac
import logging
import os
import threading
from typing import Any

from app.executor_v2.browser_login import BrowserLoginManager
from app.executor_v2.providers import ProviderService, SUPPORTED_PROVIDERS
from app.executor_v2.scheduler import ResourceScheduler
from app.executor_v2.store import ExecutorStore


logger = logging.getLogger(__name__)


class ExecutorRuntime:
    def __init__(self) -> None:
        token = os.getenv("EXECUTOR_TOKEN", "").strip()
        if len(token) < 32:
            raise RuntimeError(
                "EXECUTOR_TOKEN is required and must contain at least 32 characters"
            )
        self.token = token
        self.store = ExecutorStore()
        self.providers = ProviderService(self.store)
        self.browsers = BrowserLoginManager(self.store.profile_dir)
        self.scheduler = ResourceScheduler(self.store, self.providers)
        self._stop = threading.Event()
        self._janitor: threading.Thread | None = None
        self._auth_lock = threading.RLock()
        self._validating: set[str] = set()

    def start(self) -> None:
        self.scheduler.start()
        self._stop.clear()
        self._janitor = threading.Thread(
            target=self._janitor_loop,
            name="executor-v2-login-janitor",
            daemon=True,
        )
        self._janitor.start()

    def stop(self) -> None:
        self._stop.set()
        if self._janitor:
            self._janitor.join(timeout=5)
        self.browsers.close_all()
        self.scheduler.shutdown()

    def valid_bearer(self, authorization: str) -> bool:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return False
        supplied = authorization[len(prefix) :].strip()
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def start_login(self, provider: str) -> dict[str, Any]:
        if provider not in SUPPORTED_PROVIDERS:
            raise RuntimeError("不支持这个网盘")
        with self._auth_lock:
            for old_session_id in self.store.expire_pending_logins():
                self.browsers.close(old_session_id)
            session = self.store.create_login_session(provider)
        session_id = str(session["session_id"])
        thread = threading.Thread(
            target=self._launch_login,
            args=(session_id, provider),
            name=f"executor-v2-login-{provider}",
            daemon=True,
        )
        thread.start()
        return session

    def poll_login(
        self,
        provider: str,
        session_id: str,
    ) -> dict[str, Any]:
        session = self.store.get_login_session(session_id=session_id)
        if not session or session.get("provider") != provider:
            raise RuntimeError("登录会话不存在")
        status = str(session.get("status") or "")
        if status != "pending":
            if status in {"expired", "error", "connected"}:
                self.browsers.close(session_id)
            return session
        if not self.browsers.has_session(session_id):
            return session

        try:
            secret = self.browsers.extract_login_secret(session_id, provider)
        except Exception as exc:
            logger.warning("login state read failed for %s: %s", provider, exc)
            return session
        if not secret:
            return session

        with self._auth_lock:
            if session_id in self._validating:
                return session
            self._validating.add(session_id)
        self.store.update_login_session(
            session_id,
            message="已识别官方登录，正在验证并加密保存会话",
        )
        thread = threading.Thread(
            target=self._complete_login,
            args=(session_id, provider, secret),
            name=f"executor-v2-validate-{provider}",
            daemon=True,
        )
        thread.start()
        return self.store.get_login_session(session_id=session_id) or session

    def session_from_public_token(self, public_token: str) -> dict[str, Any]:
        session = self.store.get_login_session(public_token=public_token)
        if not session:
            raise RuntimeError("登录窗口无效")
        if session.get("status") == "expired":
            self.browsers.close(str(session["session_id"]))
        return session

    def screenshot(self, public_token: str) -> bytes:
        session = self.session_from_public_token(public_token)
        if session.get("status") != "pending":
            raise RuntimeError(str(session.get("message") or "登录已结束"))
        return self.browsers.screenshot(str(session["session_id"]))

    def control(
        self,
        public_token: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        session = self.session_from_public_token(public_token)
        if session.get("status") != "pending":
            raise RuntimeError(str(session.get("message") or "登录已结束"))
        session_id = str(session["session_id"])
        if action == "click":
            self.browsers.click(
                session_id,
                float(payload.get("x") or 0),
                float(payload.get("y") or 0),
            )
        elif action == "text":
            self.browsers.insert_text(
                session_id,
                str(payload.get("text") or ""),
            )
        elif action == "key":
            self.browsers.keypress(
                session_id,
                str(payload.get("key") or ""),
            )
        elif action == "refresh":
            self.browsers.refresh(session_id)
        else:
            raise RuntimeError("不支持这个操作")

    def _launch_login(self, session_id: str, provider: str) -> None:
        try:
            self.browsers.start(session_id, provider)
            current = self.store.get_login_session(session_id=session_id)
            if not current or current.get("status") != "pending":
                self.browsers.close(session_id)
                return
            self.store.update_login_session(
                session_id,
                message="官方登录页面已打开，请扫码或在受保护窗口完成登录",
            )
        except Exception as exc:
            logger.exception("failed to open %s login browser", provider)
            self.store.update_login_session(
                session_id,
                status="error",
                message=f"官方登录页面打开失败：{exc}",
            )
            self.browsers.close(session_id)

    def _complete_login(
        self,
        session_id: str,
        provider: str,
        secret: dict[str, Any],
    ) -> None:
        try:
            with self._auth_lock:
                current = self.store.get_login_session(session_id=session_id)
                if not current or current.get("status") != "pending":
                    return
                display_name = self.providers.validate_and_save_account(
                    provider,
                    secret,
                )
                self.store.resume_provider_resources(provider)
            self.store.update_login_session(
                session_id,
                status="connected",
                message=f"{display_name} 已连接，会话已加密保存",
            )
            self.scheduler.notify()
        except Exception as exc:
            logger.warning("%s login validation failed: %s", provider, exc)
            self.store.update_login_session(
                session_id,
                status="error",
                message=str(exc),
            )
        finally:
            with self._auth_lock:
                self._validating.discard(session_id)
            self.browsers.close(session_id)

    def _janitor_loop(self) -> None:
        while not self._stop.wait(30):
            for session_id in self.store.expire_timed_out_logins():
                self.browsers.close(session_id)


_runtime: ExecutorRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> ExecutorRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = ExecutorRuntime()
        return _runtime
