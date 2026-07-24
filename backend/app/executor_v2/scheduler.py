from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from app.executor_v2.providers import ProviderService
from app.executor_v2.store import (
    ExecutorStore,
    epoch_now,
    settings_interval_seconds,
    utc_now,
)


logger = logging.getLogger(__name__)


class ResourceScheduler:
    """Small persistent scheduler backed by the executor database."""

    def __init__(
        self,
        store: ExecutorStore,
        providers: ProviderService,
        *,
        max_workers: int = 2,
    ) -> None:
        self.store = store
        self.providers = providers
        self._max_workers = max(1, min(int(max_workers), 4))
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="executor-v2-job",
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._inflight: set[str] = set()
        self._lock = threading.RLock()
        self._maintenance_lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="executor-v2-scheduler",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread:
            thread.join(timeout=10)
        self._executor.shutdown(wait=False, cancel_futures=False)

    def enqueue(self, resource_key: str, action: str) -> bool:
        queued = self.store.enqueue_resource(resource_key, action)
        if queued:
            self._wake.set()
        return queued

    def notify(self) -> None:
        self._wake.set()

    def run_due_and_wait(self, timeout_seconds: int = 240) -> dict[str, Any]:
        """Keep one HTTP request open while due background work is running."""
        if not self._maintenance_lock.acquire(blocking=False):
            with self._lock:
                active = len(self._inflight)
            return {"busy": True, "inFlight": active, "remaining": 0}
        started = time.monotonic()
        try:
            deadline = started + max(10, min(int(timeout_seconds), 840))
            while not self._stop.is_set():
                self._dispatch_due()
                with self._lock:
                    active = len(self._inflight)
                due = self.store.due_resources(limit=1)
                if not due and active == 0:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wake.wait(timeout=min(2, remaining))
                self._wake.clear()
            with self._lock:
                active = len(self._inflight)
            due_count = 1 if self.store.due_resources(limit=1) else 0
            return {
                "busy": bool(active or due_count),
                "inFlight": active,
                "remaining": due_count,
                "elapsedSeconds": round(time.monotonic() - started, 2),
            }
        finally:
            self._maintenance_lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch_due()
            except Exception:
                logger.exception("executor scheduler dispatch failed")
            # Neon Free suspends after five idle minutes. Polling PostgreSQL
            # more often would keep compute running and exhaust its CU-hours.
            # Explicit queue writes still wake this loop immediately.
            self._wake.wait(timeout=600)
            self._wake.clear()

    def _dispatch_due(self) -> None:
        with self._lock:
            available = self._max_workers - len(self._inflight)
        if available <= 0:
            return

        for resource in self.store.due_resources(limit=available):
            key = str(resource["resource_key"])
            action = str(resource.get("pending_action") or "check")
            with self._lock:
                if key in self._inflight:
                    continue
                self._inflight.add(key)
            status = "转存中" if action == "transfer" else "检查中"
            if not self.store.claim_resource(key, status):
                with self._lock:
                    self._inflight.discard(key)
                continue
            future = self._executor.submit(self._run_resource, resource, action)
            future.add_done_callback(
                lambda completed, resource_key=key: self._job_finished(
                    resource_key, completed
                )
            )

    def _job_finished(self, resource_key: str, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("unhandled executor job failure: %s", resource_key)
        finally:
            with self._lock:
                self._inflight.discard(resource_key)
            self._wake.set()

    def _run_resource(
        self,
        resource: dict[str, Any],
        action: str,
    ) -> None:
        key = str(resource["resource_key"])
        settings = self.store.get_settings()
        try:
            result = self.providers.sync_resource(
                resource,
                force_transfer=action == "transfer",
            )
        except Exception as exc:
            error_text = str(exc)
            if _looks_like_account_expiry(error_text):
                self.store.update_account_state(
                    str(resource.get("provider") or ""),
                    "expired",
                    "会话已失效，请重新直接登录",
                )
            retry_count = int(resource.get("retry_count") or 0) + 1
            retry_enabled = bool(settings["retryEnabled"])
            if retry_enabled:
                base_delay = min(300 * (2 ** min(retry_count - 1, 7)), 21600)
                next_run = epoch_now() + base_delay * random.uniform(0.9, 1.1)
                pending_action = action
                retry_message = f"{error_text}；第 {retry_count} 次重试将在稍后自动进行"
            else:
                next_run = self._next_regular_run(settings_interval_seconds(settings))
                pending_action = ""
                retry_message = error_text
            self.store.update_resource(
                key,
                status="失败",
                last_checked_at=utc_now(),
                next_run_at=next_run,
                retry_count=retry_count,
                pending_action=pending_action,
                message=retry_message,
            )
            logger.warning("resource %s sync failed: %s", key, exc)
            return

        self.store.update_resource(
            key,
            **result,
            next_run_at=self._next_regular_run(settings_interval_seconds(settings)),
            retry_count=0,
            pending_action="",
        )

    @staticmethod
    def _next_regular_run(interval_seconds: int) -> float:
        interval = max(300, int(interval_seconds))
        return epoch_now() + interval * random.uniform(0.95, 1.08)


def _looks_like_account_expiry(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "unauthenticated",
            "refresh_token",
            "token 无效",
            "认证失败",
            "登录会话已失效",
            "cookie无效",
            "cookie 无效",
            "http 401",
        )
    )
