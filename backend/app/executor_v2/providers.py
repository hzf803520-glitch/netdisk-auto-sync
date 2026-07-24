from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from app.executor_v2.store import ExecutorStore, utc_now
from app.extensions.adapters.baidu_adapter import BaiduAdapter
from app.extensions.adapters.quark_adapter import QuarkAdapter
from app.extensions.adapters.uc_adapter import UCAdapter
from app.extensions.adapters.xunlei_adapter import XunleiAdapter


logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"baidu", "quark", "uc", "xunlei"}
ADAPTER_CLASSES = {
    "baidu": BaiduAdapter,
    "quark": QuarkAdapter,
    "uc": UCAdapter,
    "xunlei": XunleiAdapter,
}


@dataclass
class SourceContext:
    pwd_id: str
    passcode: str
    stoken: str
    root_id: str
    files: list[dict[str, Any]]
    fingerprint: str


class ProviderService:
    def __init__(self, store: ExecutorStore) -> None:
        self.store = store
        self._locks = {provider: threading.RLock() for provider in SUPPORTED_PROVIDERS}

    def validate_and_save_account(self, provider: str, secret: dict[str, Any]) -> str:
        if provider not in SUPPORTED_PROVIDERS:
            raise RuntimeError("不支持这个网盘")
        with self._locks[provider]:
            adapter = self._adapter_from_secret(provider, secret)
            account_info = adapter.init()
            if not account_info:
                raise RuntimeError("登录已完成，但网盘会话验证失败")
            display_name = self._display_name(provider, adapter, account_info)
            exported = self._export_secret(provider, adapter, secret)
            self.store.save_account(
                provider,
                exported,
                display_name=display_name,
                message="官方登录会话已验证并加密保存",
            )
            return display_name

    def verify_account(self, provider: str) -> bool:
        account = self.store.get_account(provider, include_secret=True)
        if not account or not account.get("secret"):
            self.store.update_account_state(provider, "login-required", "等待直接登录")
            return False
        try:
            with self._locks[provider]:
                adapter = self._adapter_from_secret(provider, account["secret"])
                info = adapter.init()
                if not info:
                    raise RuntimeError("账号会话失效")
                display_name = self._display_name(provider, adapter, info)
                secret = self._export_secret(provider, adapter, account["secret"])
                self.store.save_account(
                    provider,
                    secret,
                    display_name=display_name,
                    message="会话有效",
                )
            return True
        except Exception as exc:
            logger.warning("%s account verification failed: %s", provider, exc)
            self.store.update_account_state(
                provider,
                "expired",
                "会话已失效，请重新直接登录",
            )
            return False

    def sync_resource(
        self,
        resource: dict[str, Any],
        *,
        force_transfer: bool = False,
    ) -> dict[str, Any]:
        provider = str(resource["provider"])
        account = self.store.get_account(provider, include_secret=True)
        if not account or account.get("state") != "connected":
            raise RuntimeError(f"{provider}账号尚未登录")
        secret = account.get("secret") or {}

        with self._locks[provider]:
            adapter = self._adapter_from_secret(provider, secret)
            if not adapter.init():
                self.store.update_account_state(
                    provider,
                    "expired",
                    "会话已失效，请重新直接登录",
                )
                raise RuntimeError("网盘登录会话已失效")

            source = self._read_source(adapter, resource)
            old_files = _decode_file_list(resource.get("source_files_json"))
            old_by_path = {
                str(item.get("relPath") or ""): item
                for item in old_files
                if item.get("relPath")
            }
            new_items = [
                item for item in source.files if item["relPath"] not in old_by_path
            ]
            changed_items = [
                item
                for item in source.files
                if item["relPath"] in old_by_path
                and _file_signature(item)
                != _file_signature(old_by_path[item["relPath"]])
            ]
            first_transfer = not old_files
            to_transfer = (
                _minimal_transfer_roots(source.files)
                if first_transfer
                else _minimal_transfer_roots(new_items)
            )

            target_folder_id = self._ensure_folder(adapter, resource["target_folder"])
            transferred = 0
            for item in to_transfer:
                parent_path = str(item.get("parentPath") or "").strip("/")
                target_path = str(resource["target_folder"]).rstrip("/")
                if parent_path:
                    target_path = f"{target_path}/{parent_path}"
                parent_id = self._ensure_folder(adapter, target_path)
                self._save_source_item(
                    adapter,
                    source,
                    item,
                    parent_id,
                )
                self._wait_for_target_item(
                    adapter,
                    parent_id,
                    str(item["name"]),
                )
                transferred += 1

            settings = self.store.get_settings()
            share_url = str(resource.get("share_url") or "").strip()
            share_code = str(resource.get("share_code") or "").strip()
            if settings["autoShare"]:
                if not share_url or not self.validate_share(share_url):
                    share_url, share_code = self._create_share(
                        provider,
                        adapter,
                        target_folder_id,
                        str(resource["title"]),
                    )
                    if not self.validate_share(share_url):
                        raise RuntimeError("新分享链接已生成，但匿名访问校验失败")

            exported = self._export_secret(provider, adapter, secret)
            self.store.save_account(
                provider,
                exported,
                display_name=str(account.get("display_name") or ""),
                message="会话有效",
            )

            episode_info = _episode_info(source.files)
            if first_transfer:
                message = f"首次转存完成，共处理 {transferred} 个顶层项目"
            elif transferred:
                message = f"发现新增内容并补存 {transferred} 个项目"
            elif changed_items:
                message = "发现同名文件内容变化；为避免生成重复文件，已保留现有目标文件"
            else:
                message = "未发现新增文件，目标目录保持同步"
            return {
                "status": "已同步",
                "share_url": share_url,
                "share_code": share_code,
                "episode_info": episode_info,
                "source_fingerprint": source.fingerprint,
                "source_files_json": json.dumps(
                    source.files,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "target_folder_id": target_folder_id,
                "last_checked_at": utc_now(),
                "last_synced_at": utc_now()
                if first_transfer or transferred
                else resource.get("last_synced_at"),
                "message": message,
            }

    def validate_share(self, url: str) -> bool:
        if not url.startswith("https://"):
            return False
        try:
            response = requests.get(
                url,
                timeout=20,
                allow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    )
                },
            )
        except requests.RequestException:
            return False
        if response.status_code >= 400:
            return False
        text = response.text[:200_000]
        invalid_markers = (
            "分享的文件已经被删除",
            "分享链接已失效",
            "该分享已取消",
            "分享已过期",
            "share not found",
        )
        return not any(marker in text for marker in invalid_markers)

    def _adapter_from_secret(self, provider: str, secret: dict[str, Any]):
        adapter_class = ADAPTER_CLASSES.get(provider)
        if not adapter_class:
            raise RuntimeError("无法创建网盘适配器")
        if provider == "xunlei":
            refresh_token = str(secret.get("refresh_token") or "").strip()
            if not refresh_token:
                raise RuntimeError("迅雷登录会话缺少 refresh_token")
            adapter = adapter_class(
                cookie=refresh_token,
                config={"refresh_token": refresh_token},
                account_name="executor-v2",
            )
        else:
            cookie = str(secret.get("cookie") or "").strip()
            if not cookie:
                raise RuntimeError("网盘登录会话缺少 Cookie")
            adapter = adapter_class(
                cookie=cookie,
                config={"cookie": cookie},
                account_name="executor-v2",
            )
        return adapter

    @staticmethod
    def _display_name(provider: str, adapter: Any, account_info: Any) -> str:
        if isinstance(account_info, dict):
            for key in (
                "nickname",
                "nick_name",
                "username",
                "user_name",
                "display_name",
                "baidu_name",
            ):
                value = str(account_info.get(key) or "").strip()
                if value:
                    return value[:160]
        value = str(getattr(adapter, "nickname", "") or "").strip()
        return value[:160] or f"{provider}账号"

    @staticmethod
    def _export_secret(
        provider: str, adapter: Any, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        if provider == "xunlei":
            token = str(
                getattr(adapter, "_refresh_token", "")
                or fallback.get("refresh_token")
                or ""
            ).strip()
            return {"refresh_token": token}
        if provider == "baidu":
            session = getattr(adapter, "_session", None)
            session_cookies = (
                session.cookies.get_dict()
                if session is not None and hasattr(session, "cookies")
                else {}
            )
            if session_cookies:
                return {
                    "cookie": "; ".join(
                        f"{key}={value}" for key, value in session_cookies.items()
                    )
                }
        cookie = str(
            getattr(adapter, "cookie", "") or fallback.get("cookie") or ""
        ).strip()
        return {"cookie": cookie}

    def _read_source(self, adapter: Any, resource: dict[str, Any]) -> SourceContext:
        extracted = adapter.extract_url(str(resource["source_url"]))
        if not extracted or not extracted[0]:
            raise RuntimeError("无法解析源分享链接")
        pwd_id, detected_code, root_id, _paths = extracted
        passcode = str(resource.get("source_code") or detected_code or "")
        token_payload = adapter.get_stoken(str(pwd_id), passcode)
        stoken = _extract_stoken(token_payload)
        if stoken is None:
            message = _result_message(token_payload, "源分享链接验证失败")
            raise RuntimeError(message)

        files: list[dict[str, Any]] = []
        visited: set[str] = set()
        self._walk_source(
            adapter,
            str(pwd_id),
            stoken,
            str(root_id or "0"),
            "",
            files,
            visited,
            depth=0,
        )
        fingerprint_payload = [
            {
                "relPath": item["relPath"],
                "fid": item["fid"],
                "size": item["size"],
                "updatedAt": item["updatedAt"],
                "dir": item["dir"],
            }
            for item in files
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return SourceContext(
            pwd_id=str(pwd_id),
            passcode=passcode,
            stoken=stoken,
            root_id=str(root_id or "0"),
            files=files,
            fingerprint=fingerprint,
        )

    def _walk_source(
        self,
        adapter: Any,
        pwd_id: str,
        stoken: str,
        parent_id: str,
        parent_path: str,
        output: list[dict[str, Any]],
        visited: set[str],
        *,
        depth: int,
    ) -> None:
        if depth > 8 or len(output) >= 5000 or parent_id in visited:
            return
        visited.add(parent_id)
        detail = adapter.get_detail(
            pwd_id,
            stoken,
            parent_id,
            _fetch_share=1 if depth == 0 else 0,
            fetch_share_full_path=0,
        )
        items = _result_items(detail)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            fid = _first_text(raw, "fid", "file_id", "id", "fs_id", "fileId")
            name = _first_text(
                raw,
                "file_name",
                "name",
                "server_filename",
                "title",
            )
            if not fid or not name:
                continue
            is_dir = bool(
                raw.get("dir")
                or raw.get("is_dir")
                or raw.get("file_type") == 0
                or raw.get("kind") == "drive#folder"
            )
            rel_path = f"{parent_path}/{name}".strip("/")
            descriptor = {
                "fid": fid,
                "token": _first_text(
                    raw,
                    "share_fid_token",
                    "fid_token",
                    "file_token",
                )
                or fid,
                "name": name,
                "relPath": rel_path,
                "parentPath": parent_path,
                "dir": is_dir,
                "size": _first_int(raw, "size", "file_size"),
                "updatedAt": _first_int(
                    raw,
                    "updated_at",
                    "server_mtime",
                    "modified_time",
                    "mtime",
                ),
            }
            output.append(descriptor)
            if is_dir:
                self._walk_source(
                    adapter,
                    pwd_id,
                    stoken,
                    fid,
                    rel_path,
                    output,
                    visited,
                    depth=depth + 1,
                )
        output.sort(key=lambda item: item["relPath"])

    def _ensure_folder(self, adapter: Any, path: str) -> str:
        normalized = "/" + str(path or "").strip("/")
        if normalized == "/":
            return "0"
        existing = adapter.get_fids([normalized]) or []
        for item in existing:
            if not isinstance(item, dict):
                continue
            fid = _first_text(item, "fid", "file_id", "id", "fs_id")
            if fid:
                return fid

        result = adapter.mkdir(normalized)
        data = result.get("data") if isinstance(result, dict) else {}
        fid = _first_text(
            data if isinstance(data, dict) else {},
            "fid",
            "file_id",
            "id",
            "fs_id",
        )
        if not fid:
            existing = adapter.get_fids([normalized]) or []
            for item in existing:
                if isinstance(item, dict):
                    fid = _first_text(item, "fid", "file_id", "id", "fs_id")
                    if fid:
                        break
        if not fid:
            raise RuntimeError(
                _result_message(result, f"无法创建目标目录 {normalized}")
            )
        return fid

    def _save_source_item(
        self,
        adapter: Any,
        source: SourceContext,
        item: dict[str, Any],
        parent_id: str,
    ) -> None:
        result = adapter.save_file(
            [str(item["fid"])],
            [str(item.get("token") or item["fid"])],
            str(parent_id),
            source.pwd_id,
            source.stoken,
            [str(item["name"])],
        )
        if not isinstance(result, dict):
            raise RuntimeError("网盘转存接口返回格式错误")
        if _result_failed(result):
            raise RuntimeError(_result_message(result, "转存失败"))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        task_id = str(data.get("task_id") or result.get("task_id") or "").strip()
        if task_id and not data.get("_sync"):
            provider = str(getattr(adapter, "DRIVE_TYPE", "") or "")
            if provider in {"quark", "uc"}:
                task = self._poll_quark_uc_task(adapter, provider, task_id)
            else:
                task = adapter.query_task(task_id)
            if _result_failed(task):
                raise RuntimeError(_result_message(task, "转存任务失败"))

    @staticmethod
    def _wait_for_target_item(
        adapter: Any,
        parent_id: str,
        item_name: str,
    ) -> None:
        latest: Any = None
        for _ in range(20):
            latest = adapter.ls_dir(str(parent_id), max_items=1000)
            if not _result_failed(latest):
                names = {
                    _first_text(
                        item,
                        "file_name",
                        "name",
                        "server_filename",
                        "title",
                    )
                    for item in _result_items(latest)
                    if isinstance(item, dict)
                }
                if item_name in names:
                    return
            time.sleep(1)
        raise RuntimeError(_result_message(latest, f"目标目录未确认到 {item_name}"))

    @staticmethod
    def _poll_quark_uc_task(
        adapter: Any, provider: str, task_id: str
    ) -> dict[str, Any]:
        pr = "ucpro" if provider == "quark" else "UCBrowser"
        latest: dict[str, Any] = {}
        for retry_index in range(120):
            params = {
                "pr": pr,
                "fr": "pc",
                "task_id": task_id,
                "retry_index": retry_index,
                "__dt": int(random.uniform(1, 5) * 60 * 1000),
                "__t": time.time(),
            }
            response = adapter._send_request(
                "GET",
                f"{adapter.BASE_URL}/1/clouddrive/task",
                params=params,
            )
            latest = response.json()
            status = (latest.get("data") or {}).get("status")
            if status == 2:
                return latest
            if status == -1 or _result_failed(latest):
                return latest
            time.sleep(0.5)
        raise RuntimeError("网盘异步任务等待超时")

    def _create_share(
        self,
        provider: str,
        adapter: Any,
        folder_id: str,
        title: str,
    ) -> tuple[str, str]:
        if provider in {"quark", "uc"}:
            pr = "ucpro" if provider == "quark" else "UCBrowser"
            response = adapter._send_request(
                "POST",
                f"{adapter.BASE_URL}/1/clouddrive/share",
                params={"pr": pr, "fr": "pc"},
                json={
                    "fid_list": [folder_id],
                    "expired_type": 2,
                    "title": title[:120],
                    "url_type": 1,
                },
            ).json()
            if _result_failed(response):
                raise RuntimeError(_result_message(response, "创建分享失败"))
            task_id = str((response.get("data") or {}).get("task_id") or "")
            if not task_id:
                raise RuntimeError("创建分享任务未返回任务编号")
            task = self._poll_quark_uc_task(adapter, provider, task_id)
            share_id = str((task.get("data") or {}).get("share_id") or "")
            if not share_id:
                raise RuntimeError("分享任务完成但未返回分享编号")
            password = adapter._send_request(
                "POST",
                f"{adapter.BASE_URL}/1/clouddrive/share/password",
                params={"pr": pr, "fr": "pc"},
                json={"share_id": share_id},
            ).json()
            data = password.get("data") if isinstance(password, dict) else {}
            url = str((data or {}).get("share_url") or "").strip()
            code = str(
                (data or {}).get("passcode") or (data or {}).get("pwd") or ""
            ).strip()
            if not url:
                raise RuntimeError(_result_message(password, "未能读取新分享链接"))
            return url, code

        if provider == "baidu":
            bdstoken = str(adapter._get_bdstoken() or "")
            share_fid = self._baidu_numeric_fid(adapter, folder_id)
            response = adapter._request(
                "POST",
                "https://pan.baidu.com/share/set",
                params={
                    "channel": "chunlei",
                    "clienttype": "0",
                    "app_id": "250528",
                    "web": "1",
                    "bdstoken": bdstoken,
                },
                data={
                    "period": "0",
                    "pwd": "",
                    "eflag_disable": "true",
                    "channel_list": "[]",
                    "schannel": "0",
                    "fid_list": json.dumps([share_fid]),
                },
            ).json()
            url = str(
                response.get("link")
                or response.get("shorturl")
                or response.get("url")
                or ""
            ).strip()
            if int(response.get("errno") or 0) != 0 or not url:
                raise RuntimeError(_result_message(response, "百度分享创建失败"))
            return url, ""

        if provider == "xunlei":
            response = adapter._request(
                "POST",
                "https://api-pan.xunlei.com/drive/v1/share",
                body={
                    "file_ids": [folder_id],
                    "share_to": "copy",
                    "params": {
                        "subscribe_push": "false",
                        "WithPassCodeInLink": "true",
                    },
                    "title": title[:120],
                    "restore_limit": -1,
                    "expiration_days": -1,
                },
            )
            url = str(response.get("share_url") or "").strip()
            code = str(
                response.get("pass_code") or response.get("passcode") or ""
            ).strip()
            if not url:
                raise RuntimeError(_result_message(response, "迅雷分享创建失败"))
            return url, code

        raise RuntimeError("这个网盘暂不支持创建分享")

    @staticmethod
    def _baidu_numeric_fid(adapter: Any, folder_id: str) -> int:
        raw = str(folder_id or "").strip()
        if raw.isdigit():
            return int(raw)
        normalized = "/" + raw.strip("/")
        parent_path = normalized.rsplit("/", 1)[0] or "/"
        target_name = normalized.rsplit("/", 1)[-1]
        result = adapter._api_list(parent_path)
        for item in result.get("list", []) if isinstance(result, dict) else []:
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("server_filename")
                or str(item.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
            )
            fid = str(item.get("fs_id") or "")
            if name == target_name and fid.isdigit():
                return int(fid)
        raise RuntimeError("未能读取百度目标目录的分享编号")


def _extract_stoken(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    value = (
        data.get("stoken")
        or data.get("pass_code_token")
        or payload.get("stoken")
        or payload.get("pass_code_token")
    )
    if value is None:
        if payload.get("code") in (0, "0") or payload.get("status") in (
            200,
            "200",
        ):
            return ""
        return None
    return str(value)


def _result_items(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (
        data.get("list"),
        data.get("files"),
        payload.get("list"),
        payload.get("files"),
    ):
        if isinstance(value, list):
            return value
    return []


def _result_failed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    if "error" in payload and payload.get("error"):
        return True
    code = payload.get("code")
    status = payload.get("status")
    errno = payload.get("errno")
    if code not in (None, 0, "0", 200, "200"):
        return True
    if status not in (None, 0, "0", 200, "200"):
        return True
    if errno not in (None, 0, "0"):
        return True
    return False


def _result_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in (
            "message",
            "msg",
            "error_description",
            "error",
            "errmsg",
            "show_msg",
        ):
            value = str(payload.get(key) or "").strip()
            if value:
                return value[:800]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("message", "msg", "error", "error_description"):
                value = str(data.get(key) or "").strip()
                if value:
                    return value[:800]
    return fallback


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _first_int(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        try:
            if value is not None and str(value).strip():
                return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def _decode_file_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, dict)]


def _file_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(item.get("relPath") or ""),
        int(item.get("size") or 0),
        int(item.get("updatedAt") or 0),
        bool(item.get("dir")),
    )


def _minimal_transfer_roots(
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    paths = {str(item.get("relPath") or "") for item in files}
    roots: list[dict[str, Any]] = []
    for item in sorted(
        files,
        key=lambda value: str(value.get("relPath") or "").count("/"),
    ):
        parent = str(item.get("parentPath") or "")
        has_selected_ancestor = False
        while parent:
            if parent in paths and any(
                str(root.get("relPath") or "") == parent and bool(root.get("dir"))
                for root in roots
            ):
                has_selected_ancestor = True
                break
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        if not has_selected_ancestor:
            roots.append(item)
    return roots


def _episode_info(files: list[dict[str, Any]]) -> str:
    names = [str(item.get("name") or "") for item in files if not item.get("dir")]
    episodes: set[int] = set()
    patterns = (
        re.compile(r"第\s*(\d{1,4})\s*[集话]"),
        re.compile(r"(?:^|[\s._-])[Ee][Pp]?(\d{1,4})(?:[\s._-]|$)"),
        re.compile(r"(?:^|[\s._-])(\d{1,4})(?:[\s._-]|$)"),
    )
    for name in names:
        for pattern in patterns:
            matches = pattern.findall(name)
            if matches:
                episodes.update(
                    number
                    for number in (int(value) for value in matches)
                    if 0 < number < 5000
                )
                break
    if episodes:
        maximum = max(episodes)
        return f"1–{maximum} 集"
    if names:
        return f"{len(names)} 个文件"
    return "等待识别"
