from __future__ import annotations

import hashlib
import html as html_lib
import http.client
import json
import base64
import hmac
import os
import queue
import re
import sqlite3
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from database import DATABASE_URL, DB_PATH, IntegrityError, Row, add_column, connect_db
from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).parent / "static"
BAIDU_BIN = Path(os.getenv("BAIDU_BIN", str(ROOT / "bin" / "BaiduPCS-Go")))
BAIDU_CONFIG_DIR = Path(os.getenv("BAIDU_CONFIG_DIR", str(ROOT / "data" / "baidu")))
LOCK = threading.RLock()
BAIDU_EXEC_LOCK = threading.BoundedSemaphore(2)  # 最多并行2个百度写入，兼顾速度与风控
RUNNING_TASKS: set[int] = set()
BATCH_STATE = {"scan": False, "update": False, "message": ""}
IMPORT_QUEUE: queue.Queue[int] = queue.Queue()
IMPORT_WORKERS = 3
IMPORT_STATE = {"running": False, "queued": 0, "active": 0, "completed": 0, "failed": 0, "message": ""}
QUARK_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def normalize_rel(path: str) -> str:
    return "/".join(x for x in str(path or "").replace("\\", "/").split("/") if x)


def parent_rel(path: str) -> str:
    path = normalize_rel(path)
    return path.rsplit("/", 1)[0] if "/" in path else ""


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def clean_folder_name(name: str) -> str:
    # 网盘目录不允许斜杠；同时去掉网页标题中常见的后缀。
    name = re.sub(r"[_\-]?(百度网盘|夸克网盘).*$", "", str(name or "").strip(), flags=re.I)
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip(" .")
    return name[:180]


def replace_path_basename(path: str, name: str) -> str:
    parent = "/" + parent_rel(path) if parent_rel(path) else ""
    return (parent.rstrip("/") + "/" + clean_folder_name(name)).replace("//", "/")


def init_db() -> None:
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                name TEXT NOT NULL,
                share_url TEXT NOT NULL,
                passcode TEXT DEFAULT '',
                save_path TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL DEFAULT 360,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT '待扫描',
                last_run TEXT,
                next_run TEXT,
                last_message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(platform, share_url, save_path)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_snapshots (
                task_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                source_id TEXT DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                modified_at TEXT DEFAULT '',
                fingerprint TEXT NOT NULL,
                is_dir INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL,
                PRIMARY KEY(task_id, file_path)
            );
            CREATE TABLE IF NOT EXISTS change_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                folder_path TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                change_type TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                modified_at TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                detected_at TEXT NOT NULL,
                applied_at TEXT,
                details TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_changes_task_status ON change_items(task_id,status);
            CREATE INDEX IF NOT EXISTS idx_changes_detected ON change_items(detected_at DESC);
            """
        )
        add_column(conn, "tasks", "auto_update INTEGER NOT NULL DEFAULT 1")
        add_column(conn, "tasks", "pending_files INTEGER NOT NULL DEFAULT 0")
        add_column(conn, "tasks", "pending_folders INTEGER NOT NULL DEFAULT 0")
        add_column(conn, "tasks", "last_scan TEXT")
        add_column(conn, "tasks", "last_update TEXT")
        add_column(conn, "tasks", "last_change TEXT DEFAULT ''")
        add_column(conn, "tasks", "monitor_mode TEXT DEFAULT 'precise'")
        add_column(conn, "tasks", "stored_files INTEGER NOT NULL DEFAULT 0")
        add_column(conn, "tasks", "stored_folders INTEGER NOT NULL DEFAULT 0")
        defaults = {
            "quark_cookie": "",
            "baidu_cookies": "",
            "baidu_transfer_dir": "/资源数据",
            "quark_cookie_verified_at": "",
            "baidu_cookie_verified_at": "",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        conn.execute("UPDATE tasks SET monitor_mode='task' WHERE platform='baidu'")
        conn.execute("UPDATE tasks SET monitor_mode='precise' WHERE platform='quark'")
        conn.execute("UPDATE tasks SET status='待扫描' WHERE status IN ('待运行','已加入夸克追更','待配置夸克')")
        conn.commit()


SENSITIVE_SETTINGS = {"quark_cookie", "baidu_cookies"}


def _cookie_cipher() -> Fernet | None:
    secret = os.getenv("COOKIE_ENCRYPTION_KEY", "").strip()
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _decode_setting(key: str, value: str) -> str:
    if key not in SENSITIVE_SETTINGS or not value.startswith("enc:v1:"):
        return value
    cipher = _cookie_cipher()
    if cipher is None:
        return ""
    try:
        return cipher.decrypt(value[7:].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def _encode_setting(key: str, value: str) -> str:
    if key not in SENSITIVE_SETTINGS or not value:
        return value
    cipher = _cookie_cipher()
    if cipher is None:
        raise RuntimeError("云端部署缺少 COOKIE_ENCRYPTION_KEY，无法安全保存 Cookie")
    return "enc:v1:" + cipher.encrypt(value.encode("utf-8")).decode("ascii")


def get_setting(key: str, default: str = "") -> str:
    with connect_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return _decode_setting(key, row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    stored = _encode_setting(key, value)
    with connect_db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, stored))
        conn.commit()



def cookie_preview(raw: str) -> str:
    """仅返回脱敏摘要，绝不把完整 Cookie 发送到页面。"""
    parts = []
    for item in str(raw or "").split(";"):
        if "=" not in item:
            continue
        key, value = item.strip().split("=", 1)
        if not key or not value:
            continue
        tail = value[-4:] if len(value) >= 4 else "****"
        parts.append(f"{key}=••••{tail}")
        if len(parts) >= 4:
            break
    return "; ".join(parts)


def add_log(task_id: int | None, level: str, message: str) -> None:
    message = str(message)[-16000:]
    with connect_db() as conn:
        conn.execute("INSERT INTO logs(task_id,level,message,created_at) VALUES(?,?,?,?)", (task_id, level, message, now_iso()))
        conn.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1500)")
        conn.commit()


def extract_links(text: str) -> list[dict[str, str]]:
    url_re = re.compile(r"https?://[^\s，,；;]+", re.I)
    code_re = re.compile(r"(?:提取码|密码|pwd)\s*[:：=]?\s*([A-Za-z0-9]{4,8})", re.I)
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        code_match = code_re.search(line)
        line_code = code_match.group(1) if code_match else ""
        for raw in url_re.findall(line):
            url = raw.rstrip(".)]}】>。")
            platform = "quark" if "pan.quark.cn" in url else "baidu" if "pan.baidu.com" in url else ""
            if not platform:
                continue
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            code = line_code or (qs.get("pwd") or [""])[0]
            sid = url.split("/s/")[-1].split("?")[0].split("#")[0][:12]
            out.append({"platform": platform, "url": url, "passcode": code, "name": f"{'夸克' if platform == 'quark' else '百度'}任务-{sid}"})
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in out:
        unique[(item["platform"], item["url"])] = item
    return list(unique.values())


def friendly_error_message(exc: Exception) -> str:
    """Convert low-level HTTP/API errors into concise Chinese task messages."""
    raw = str(exc or "").strip()
    low = raw.lower()
    # Quark share APIs commonly return HTTP 404 with code 41011 when the share
    # resource cannot be found (deleted, expired, cancelled, or malformed URL).
    if ("http 404" in low or "status\":404" in low or "status': 404" in low) and "41011" in low:
        return "夸克分享链接不存在或已失效，请在浏览器中打开原链接确认（错误码：404/41011）"
    if "http 404" in low:
        return "请求的分享链接或接口不存在，可能是链接失效、被取消或复制不完整（HTTP 404）"
    if "41011" in low:
        return "夸克分享资源不存在或已失效，请重新获取有效分享链接（错误码：41011）"
    if "提取码错误" in raw or "passcode" in low or "-9" in raw:
        return "分享链接的提取码错误，请检查后重新填写"
    if "cookie" in low and any(x in low for x in ("invalid", "expired", "无效", "过期", "失效")):
        return "网盘登录 Cookie 已失效，请到账号设置重新保存 Cookie"
    if "403" in low or "forbidden" in low:
        return "访问被网盘拒绝，可能是 Cookie 失效、账号风控或权限不足（HTTP 403）"
    if "429" in low:
        return "请求过于频繁，已触发网盘限流，请稍后再试（HTTP 429）"
    if "timeout" in low or "timed out" in low or "超时" in raw:
        return "连接网盘超时，请检查网络后重试"
    if "ssl" in low or "unexpected_eof" in low:
        return "与网盘建立安全连接时中断，程序稍后会自动重试；持续出现请检查网络"
    if "connection refused" in low:
        return "本地服务连接失败，请重启平台后再试"
    if "文件重复" in raw or "同名文件" in raw:
        return "目标目录已有同名内容；程序将按增量更新处理，无新增内容时不会重复保存"
    if "分享链接已失效" in raw or "链接无效" in raw or "无法识别" in raw:
        return "分享链接无效、已失效或复制不完整，请打开原链接确认"
    if raw.startswith("RuntimeError:"):
        raw = raw.split(":", 1)[1].strip()
    return raw or "发生未知错误，请查看运行日志"


def _decode_json_bytes(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"服务器返回的不是有效 JSON：{text[-800:]}") from exc


def _curl_json(method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    """Use macOS bundled curl as a resilient TLS fallback."""
    cmd = [
        "/usr/bin/curl", "--silent", "--show-error", "--location",
        "--connect-timeout", "15", "--max-time", str(max(timeout, 30)),
        "--retry", "3", "--retry-delay", "2", "--retry-all-errors",
        "--request", method,
    ]
    for key, value in headers.items():
        cmd += ["--header", f"{key}: {value}"]
    if body is not None:
        cmd += ["--data-binary", "@-"]
    cmd.append(url)
    proc = subprocess.run(cmd, input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(timeout + 20, 60))
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        raise RuntimeError(f"网络请求失败（curl {proc.returncode}）：{detail}")
    return _decode_json_bytes(proc.stdout)


def request_json(method: str, url: str, *, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 45) -> dict[str, Any]:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = None
    final_headers = {"Accept": "application/json", "User-Agent": QUARK_UA, "Connection": "close"}
    if headers:
        final_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        final_headers["Content-Type"] = "application/json"

    # Some CDN nodes close TLS without a proper close_notify. This option keeps
    # certificate verification enabled while tolerating that non-standard EOF.
    context = ssl.create_default_context()
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

    retryable_http = {408, 425, 429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method=method, headers=final_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                return _decode_json_bytes(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-1600:]
            if exc.code not in retryable_http:
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionResetError,
                BrokenPipeError, http.client.RemoteDisconnected) as exc:
            last_error = exc
        if attempt < 3:
            time.sleep(1.5 * (2 ** attempt))

    # urllib still failed: use the system curl TLS stack and its own retries.
    try:
        return _curl_json(method, url, body, final_headers, timeout)
    except Exception as curl_exc:
        reason = str(last_error or "未知网络错误")
        raise RuntimeError(f"SSL/网络连接连续重试失败：{reason}；curl 兜底也失败：{curl_exc}") from curl_exc


class QuarkClient:
    BASE = "https://drive-pc.quark.cn"

    def __init__(self, cookie: str):
        self.cookie = cookie.strip()
        if not self.cookie:
            raise RuntimeError("尚未配置夸克 Cookie")

    @property
    def headers(self) -> dict[str, str]:
        return {"Cookie": self.cookie, "Referer": "https://pan.quark.cn/", "Origin": "https://pan.quark.cn"}

    def call(self, method: str, path: str, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
        return request_json(method, self.BASE + path, params=params, payload=payload, headers=self.headers, timeout=timeout)

    def account(self) -> dict[str, Any]:
        data = request_json("GET", "https://pan.quark.cn/account/info", params={"fr": "pc", "platform": "pc"}, headers=self.headers)
        if not data.get("data"):
            raise RuntimeError(data.get("message") or "夸克 Cookie 无效或已过期")
        return data["data"]

    def get_stoken(self, pwd_id: str, passcode: str) -> str:
        r = self.call("POST", "/1/clouddrive/share/sharepage/token", {"pr": "ucpro", "fr": "pc"}, {"pwd_id": pwd_id, "passcode": passcode})
        if r.get("code") != 0 or not r.get("data", {}).get("stoken"):
            raise RuntimeError(r.get("message") or "夸克分享链接无效或提取码错误")
        return r["data"]["stoken"]

    def share_list(self, pwd_id: str, stoken: str, pdir_fid: str = "0") -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        page = 1
        while True:
            params = {"pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir_fid, "force": "0", "_page": page, "_size": "100", "_fetch_banner": "0", "_fetch_share": "0", "_fetch_total": "1", "_sort": "file_type:asc,updated_at:desc", "ver": "2"}
            r = self.call("GET", "/1/clouddrive/share/sharepage/detail", params)
            if r.get("code") != 0:
                raise RuntimeError(r.get("message") or "读取夸克分享目录失败")
            items = r.get("data", {}).get("list", [])
            merged.extend(items)
            total = int(r.get("metadata", {}).get("_total", len(merged)))
            if not items or len(merged) >= total:
                return merged
            page += 1

    def path_info(self, path: str) -> dict[str, Any] | None:
        r = self.call("POST", "/1/clouddrive/file/info/path_list", {"pr": "ucpro", "fr": "pc"}, {"file_path": [path], "namespace": "0"})
        if r.get("code") != 0:
            return None
        arr = r.get("data") or []
        return arr[0] if arr else None

    def mkdir(self, path: str) -> str:
        path = "/" + path.strip("/") if path.strip("/") else "/"
        existing = self.path_info(path)
        if existing:
            return str(existing["fid"])
        r = self.call("POST", "/1/clouddrive/file", {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}, {"pdir_fid": "0", "file_name": "", "dir_path": path, "dir_init_lock": False})
        if r.get("code") != 0:
            raise RuntimeError(r.get("message") or f"创建夸克目录失败：{path}")
        return str(r["data"]["fid"])

    def list_dir(self, fid: str) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        page = 1
        while True:
            params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "pdir_fid": fid, "_page": page, "_size": "100", "_fetch_total": "1", "_fetch_sub_dirs": "0", "_sort": "file_type:asc,updated_at:desc", "fetch_all_file": 1, "fetch_risk_file_name": 1}
            r = self.call("GET", "/1/clouddrive/file/sort", params)
            if r.get("code") != 0:
                raise RuntimeError(r.get("message") or "读取夸克目标目录失败")
            items = r.get("data", {}).get("list", [])
            merged.extend(items)
            total = int(r.get("metadata", {}).get("_total", len(merged)))
            if not items or len(merged) >= total:
                return merged
            page += 1

    def poll(self, task_id: str, timeout: int = 180) -> dict[str, Any]:
        deadline = time.time() + timeout
        retry = 0
        while time.time() < deadline:
            r = self.call("GET", "/1/clouddrive/task", {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "task_id": task_id, "retry_index": retry})
            status = r.get("data", {}).get("status")
            if status == 2:
                return r
            if status == 3:
                raise RuntimeError(r.get("message") or "夸克转存任务失败")
            retry += 1
            time.sleep(0.7)
        raise RuntimeError("夸克转存等待超时")

    def save_items(self, items: list[dict[str, Any]], to_fid: str, pwd_id: str, stoken: str) -> int:
        if not items:
            return 0
        r = self.call("POST", "/1/clouddrive/share/sharepage/save", {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "app": "clouddrive", "__t": int(time.time() * 1000)}, {"fid_list": [x["fid"] for x in items], "fid_token_list": [x["share_fid_token"] for x in items], "to_pdir_fid": to_fid, "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "scene": "link"}, timeout=90)
        if r.get("code") != 0:
            raise RuntimeError(r.get("message") or "夸克转存失败")
        self.poll(str(r["data"]["task_id"]))
        return len(items)

    def delete_items(self, fids: list[str]) -> None:
        if not fids:
            return
        r = self.call("POST", "/1/clouddrive/file/delete", {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}, {"action_type": 2, "filelist": fids, "exclude_fids": []})
        if r.get("code") != 0:
            raise RuntimeError(r.get("message") or "删除夸克旧文件失败")
        if r.get("data", {}).get("task_id"):
            self.poll(str(r["data"]["task_id"]))

    def share_root_name(self, share_url: str, passcode: str) -> str:
        m = re.search(r"/s/([A-Za-z0-9_-]+)", share_url)
        if not m:
            raise RuntimeError("无法识别夸克分享链接")
        pwd_id = m.group(1)
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(share_url).query)
        code = passcode or (qs.get("pwd") or [""])[0]
        stoken = self.get_stoken(pwd_id, code)
        root = self.share_list(pwd_id, stoken, "0")
        # 用户的链接均为一个顶层文件夹；保存时沿用该文件夹原名。
        if len(root) == 1 and root[0].get("dir"):
            return clean_folder_name(str(root[0].get("file_name") or ""))
        return ""

    def prepare_share(self, share_url: str, passcode: str) -> tuple[str, str, list[dict[str, Any]]]:
        m = re.search(r"/s/([A-Za-z0-9_-]+)", share_url)
        if not m:
            raise RuntimeError("无法识别夸克分享链接")
        pwd_id = m.group(1)
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(share_url).query)
        code = passcode or (qs.get("pwd") or [""])[0]
        stoken = self.get_stoken(pwd_id, code)
        root = self.share_list(pwd_id, stoken, "0")
        if len(root) == 1 and root[0].get("dir"):
            root = self.share_list(pwd_id, stoken, str(root[0]["fid"]))
        return pwd_id, stoken, root

    @staticmethod
    def fingerprint(item: dict[str, Any]) -> str:
        keys = {
            "fid": str(item.get("fid") or ""),
            "size": int(item.get("size") or 0),
            "updated_at": str(item.get("updated_at") or item.get("modify_time") or item.get("mtime") or ""),
            "md5": str(item.get("md5") or item.get("file_md5") or ""),
            "sha1": str(item.get("sha1") or ""),
            "dir": bool(item.get("dir")),
        }
        return hashlib.sha256(json.dumps(keys, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def source_map(self, pwd_id: str, stoken: str, root: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        def walk(items: list[dict[str, Any]], rel_dir: str) -> None:
            for item in items:
                name = str(item.get("file_name") or "").strip()
                if not name:
                    continue
                rel = normalize_rel(f"{rel_dir}/{name}")
                obj = dict(item)
                obj["_rel_path"] = rel
                obj["_folder_path"] = rel_dir
                obj["_fingerprint"] = self.fingerprint(item)
                result[rel] = obj
                if item.get("dir"):
                    walk(self.share_list(pwd_id, stoken, str(item["fid"])), rel)

        walk(root, "")
        return result

    def target_map(self, save_path: str) -> dict[str, dict[str, Any]]:
        info = self.path_info("/" + save_path.strip("/"))
        if not info:
            return {}
        result: dict[str, dict[str, Any]] = {}

        def walk(fid: str, rel_dir: str) -> None:
            for item in self.list_dir(fid):
                name = str(item.get("file_name") or "").strip()
                if not name:
                    continue
                rel = normalize_rel(f"{rel_dir}/{name}")
                obj = dict(item)
                obj["_rel_path"] = rel
                result[rel] = obj
                if item.get("dir"):
                    walk(str(item["fid"]), rel)

        walk(str(info["fid"]), "")
        return result

    def scan(self, share_url: str, passcode: str, save_path: str, previous: dict[str, dict[str, Any]]) -> dict[str, Any]:
        pwd_id, stoken, root = self.prepare_share(share_url, passcode)
        source = self.source_map(pwd_id, stoken, root)
        target = self.target_map(save_path)
        changes: list[dict[str, Any]] = []
        for path, src in source.items():
            is_dir = bool(src.get("dir"))
            old = target.get(path)
            prev = previous.get(path)
            change_type = ""
            details = ""
            if is_dir:
                if old is None:
                    # 非空目录会由内部文件自动创建，只单独记录空目录。
                    prefix = path + "/"
                    has_child = any(x.startswith(prefix) for x in source)
                    if not has_child:
                        change_type = "new_folder"
                        details = "新增空目录"
                elif not old.get("dir"):
                    change_type = "updated"
                    details = "目标同名项目不是文件夹"
            else:
                src_size = int(src.get("size") or 0)
                target_size = int((old or {}).get("size") or 0)
                if old is None:
                    change_type = "new"
                    details = "目标网盘中不存在"
                elif old.get("dir"):
                    change_type = "updated"
                    details = "目标同名项目是文件夹"
                elif src_size != target_size:
                    change_type = "updated"
                    details = f"文件大小变化：{human_size(target_size)} → {human_size(src_size)}"
                elif prev and prev.get("fingerprint") != src.get("_fingerprint"):
                    change_type = "updated"
                    details = "源文件标识或修改时间变化（即使大小相同也会更新）"
            if change_type:
                changes.append({
                    "folder_path": src.get("_folder_path", ""),
                    "file_path": path,
                    "file_name": path.rsplit("/", 1)[-1],
                    "change_type": change_type,
                    "size": int(src.get("size") or 0),
                    "modified_at": str(src.get("updated_at") or src.get("modify_time") or src.get("mtime") or ""),
                    "details": details,
                })
        removed = []
        for path, prev in previous.items():
            if path not in source:
                removed.append({
                    "folder_path": parent_rel(path),
                    "file_path": path,
                    "file_name": path.rsplit("/", 1)[-1],
                    "change_type": "source_removed",
                    "size": int(prev.get("size") or 0),
                    "modified_at": str(prev.get("modified_at") or ""),
                    "details": "源分享中已删除；平台不会删除你网盘里的副本",
                })
        return {"source": source, "target": target, "changes": changes, "removed": removed}

    def sync_selected(self, share_url: str, passcode: str, save_path: str, selected_paths: set[str], selected_dirs: set[str]) -> dict[str, int]:
        pwd_id, stoken, root = self.prepare_share(share_url, passcode)
        source = self.source_map(pwd_id, stoken, root)
        save_path = "/" + save_path.strip("/")
        self.mkdir(save_path)
        stats = {"new": 0, "updated": 0, "dirs": 0, "skipped": 0}
        for rel_dir in sorted(selected_dirs, key=lambda x: (x.count("/"), x)):
            self.mkdir(save_path.rstrip("/") + "/" + normalize_rel(rel_dir))
            stats["dirs"] += 1
        by_folder: dict[str, list[dict[str, Any]]] = {}
        for path in selected_paths:
            src = source.get(normalize_rel(path))
            if not src or src.get("dir"):
                continue
            by_folder.setdefault(parent_rel(path), []).append(src)
        for folder, src_items in sorted(by_folder.items()):
            target_path = save_path.rstrip("/") + ("/" + folder if folder else "")
            target_fid = self.mkdir(target_path)
            current = {str(x.get("file_name")): x for x in self.list_dir(target_fid)}
            pending: list[dict[str, Any]] = []
            to_delete: list[str] = []
            for src in src_items:
                name = str(src.get("file_name") or "")
                old = current.get(name)
                if old:
                    to_delete.append(str(old["fid"]))
                    stats["updated"] += 1
                else:
                    stats["new"] += 1
                pending.append(src)
            if to_delete:
                self.delete_items(to_delete)
            for i in range(0, len(pending), 50):
                self.save_items(pending[i:i + 50], target_fid, pwd_id, stoken)
        return stats


def baidu_cookie_parts(cookies: str) -> tuple[str, str]:
    values: dict[str, str] = {}
    for part in cookies.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            values[k] = v
    bduss, stoken = values.get("BDUSS", ""), values.get("STOKEN", "")
    if not bduss or not stoken:
        raise RuntimeError("百度 Cookie 必须同时包含 BDUSS 和 STOKEN")
    return bduss, stoken


def run_cmd_result(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    """运行 BaiduPCS-Go，同时保留非 0 退出码的完整输出供上层判断。"""
    env = os.environ.copy()
    BAIDU_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env["BAIDUPCS_GO_CONFIG_DIR"] = str(BAIDU_CONFIG_DIR)
    p = subprocess.run(args, cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout or ""


def run_cmd(args: list[str], timeout: int = 1800) -> str:
    code, output = run_cmd_result(args, timeout)
    if code != 0:
        raise RuntimeError(output[-4000:] or f"命令执行失败：{code}")
    return output


def ensure_baidu_ready(cookies: str, transfer_dir: str) -> str:
    if not BAIDU_BIN.exists():
        raise RuntimeError("百度执行器尚未安装。请关闭平台后重新双击“①启动平台.command”，它会自动下载。")
    try:
        BAIDU_BIN.chmod(0o755)
    except OSError:
        pass
    bduss, stoken = baidu_cookie_parts(cookies)
    login = run_cmd([str(BAIDU_BIN), "login", "--bduss", bduss, "--stoken", stoken], timeout=120)
    run_cmd([str(BAIDU_BIN), "mkdir", transfer_dir], timeout=120)
    return login[-1000:] or "百度账号验证完成"


def task_row(task_id: int) -> Row | None:
    with connect_db() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def previous_snapshots(task_id: int) -> dict[str, dict[str, Any]]:
    with connect_db() as conn:
        return {r["file_path"]: dict(r) for r in conn.execute("SELECT * FROM source_snapshots WHERE task_id=?", (task_id,))}


def persist_quark_scan(task_id: int, result: dict[str, Any]) -> tuple[int, int]:
    detected = now_iso()
    changes = result["changes"]
    removed = result["removed"]
    source = result["source"]
    with connect_db() as conn:
        conn.execute("DELETE FROM change_items WHERE task_id=? AND status='pending'", (task_id,))
        for item in changes:
            conn.execute(
                "INSERT INTO change_items(task_id,folder_path,file_path,file_name,change_type,size,modified_at,status,detected_at,details) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, item["folder_path"], item["file_path"], item["file_name"], item["change_type"], item["size"], item["modified_at"], "pending", detected, item["details"]),
            )
        # 删除提醒只记录一次，且不作为待更新项目。
        for item in removed:
            exists = conn.execute("SELECT 1 FROM change_items WHERE task_id=? AND file_path=? AND change_type='source_removed' ORDER BY id DESC LIMIT 1", (task_id, item["file_path"])).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO change_items(task_id,folder_path,file_path,file_name,change_type,size,modified_at,status,detected_at,details) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (task_id, item["folder_path"], item["file_path"], item["file_name"], item["change_type"], item["size"], item["modified_at"], "notice", detected, item["details"]),
                )
        conn.execute("DELETE FROM source_snapshots WHERE task_id=?", (task_id,))
        for path, src in source.items():
            conn.execute(
                "INSERT INTO source_snapshots(task_id,file_path,source_id,size,modified_at,fingerprint,is_dir,last_seen) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, path, str(src.get("fid") or ""), int(src.get("size") or 0), str(src.get("updated_at") or src.get("modify_time") or src.get("mtime") or ""), src["_fingerprint"], 1 if src.get("dir") else 0, detected),
            )
        pending_files = sum(1 for x in changes if x["change_type"] != "new_folder")
        folders = {x["folder_path"] for x in changes}
        summary = f"发现 {pending_files} 个文件变化，涉及 {len(folders)} 个目录" if changes else "未发现更新"
        conn.execute("UPDATE tasks SET pending_files=?,pending_folders=?,last_scan=?,last_change=?,status=?,last_message=? WHERE id=?", (pending_files, len(folders), detected, summary, "发现更新" if changes else "无更新", summary, task_id))
        conn.commit()
    return pending_files, len(folders)


def resolve_quark_task_name(task: Row) -> Row:
    client = QuarkClient(get_setting("quark_cookie"))
    real_name = client.share_root_name(task["share_url"], task["passcode"])
    if real_name and (task["name"] != real_name or task["save_path"].rstrip("/").rsplit("/", 1)[-1] != real_name):
        new_path = replace_path_basename(task["save_path"], real_name)
        with connect_db() as conn:
            conn.execute("UPDATE tasks SET name=?,save_path=?,last_message=? WHERE id=?", (real_name, new_path, f"已按分享源文件夹原名修正为：{real_name}", task["id"]))
            conn.commit()
        add_log(task["id"], "INFO", f"目标目录已自动修正：{task['save_path']} → {new_path}")
        return task_row(task["id"])
    return task


def scan_quark_task(task: Row) -> str:
    task = resolve_quark_task_name(task)
    client = QuarkClient(get_setting("quark_cookie"))
    result = client.scan(task["share_url"], task["passcode"], task["save_path"], previous_snapshots(task["id"]))
    files, folders = persist_quark_scan(task["id"], result)
    return f"夸克逐文件扫描完成：源目录共 {len(result['source'])} 项；待更新文件 {files} 个，涉及目录 {folders} 个。"


def pending_for_task(task_id: int, folder_path: str | None = None) -> list[Row]:
    with connect_db() as conn:
        if folder_path is None:
            return conn.execute("SELECT * FROM change_items WHERE task_id=? AND status='pending' ORDER BY folder_path,file_path", (task_id,)).fetchall()
        folder_path = normalize_rel(folder_path)
        if folder_path:
            return conn.execute("SELECT * FROM change_items WHERE task_id=? AND status='pending' AND (folder_path=? OR folder_path LIKE ?) ORDER BY folder_path,file_path", (task_id, folder_path, folder_path + "/%")).fetchall()
        return conn.execute("SELECT * FROM change_items WHERE task_id=? AND status='pending' ORDER BY folder_path,file_path", (task_id,)).fetchall()


def apply_quark_task(task: Row, folder_path: str | None = None) -> str:
    task = resolve_quark_task_name(task)
    rows = pending_for_task(task["id"], folder_path)
    if not rows:
        scan_quark_task(task)
        rows = pending_for_task(task["id"], folder_path)
    if not rows:
        return "夸克已是最新状态，没有需要转存的文件。"
    selected_paths = {normalize_rel(r["file_path"]) for r in rows if r["change_type"] in ("new", "updated")}
    selected_dirs = {normalize_rel(r["file_path"]) for r in rows if r["change_type"] == "new_folder"}
    client = QuarkClient(get_setting("quark_cookie"))
    stats = client.sync_selected(task["share_url"], task["passcode"], task["save_path"], selected_paths, selected_dirs)
    applied_at = now_iso()
    ids = [r["id"] for r in rows]
    with connect_db() as conn:
        conn.executemany("UPDATE change_items SET status='applied',applied_at=? WHERE id=?", [(applied_at, i) for i in ids])
        conn.execute("UPDATE tasks SET last_update=? WHERE id=?", (applied_at, task["id"]))
        conn.commit()
    # 再次扫描，用于确认更新成功并清除仍未解决的项目。
    scan_quark_task(task_row(task["id"]))
    scope = f"目录“/{normalize_rel(folder_path)}”" if folder_path else "全部变化"
    return f"夸克{scope}更新完成：新增 {stats['new']}，替换更新 {stats['updated']}，新建空目录 {stats['dirs']}。"


def mark_baidu_manual_pending(task_id: int) -> str:
    """百度扫描：已识别任务只刷新已保存数量；未识别任务执行一次可靠转存以取得原名。"""
    task = task_row(task_id)
    if not task:
        return "任务不存在"
    if _baidu_is_placeholder_name(task["name"]) or normalize_rel(task["save_path"]) == normalize_rel(get_setting("baidu_transfer_dir", "/资源数据")):
        return apply_baidu_task(task)
    try:
        files, folders = baidu_refresh_saved_inventory(task)
        msg = f"扫描完成：{task['save_path']} 当前有 {files} 个文件、{folders} 个子目录"
        with connect_db() as conn:
            conn.execute("UPDATE tasks SET pending_files=0,pending_folders=0,last_scan=?,status='无更新',last_message=? WHERE id=?", (now_iso(), msg, task_id))
            conn.commit()
        return msg
    except Exception as exc:
        raise RuntimeError(f"读取百度最终目录失败：{exc}")

def _decode_baidu_name(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        value = bytes(value, "utf-8").decode("unicode_escape") if "\\u" in value else value
    except Exception:
        pass
    value = html_lib.unescape(urllib.parse.unquote(value))
    value = re.sub(r"<[^>]+>", "", value).strip()
    value = re.sub(r"\s*[-_|｜]\s*(百度网盘|百度云|网盘分享).*$", "", value, flags=re.I)
    value = re.sub(r"^(分享文件|文件分享)[:：\s-]*", "", value)
    value = clean_folder_name(value)
    generic = {"", "百度网盘", "百度云", "分享的文件", "文件分享", "页面不存在", "百度网盘-分享无限制"}
    return "" if value in generic or value.startswith("百度网盘-") else value


def _baidu_http_text(url: str, headers: dict[str, str], method: str = "GET", data: bytes | None = None) -> tuple[str, dict[str, str]]:
    """百度分享页请求，返回正文和响应 Set-Cookie。urllib 失败时使用 macOS curl。"""
    ctx = ssl.create_default_context()
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers, data=data, method=method)
            with urllib.request.urlopen(req, timeout=35, context=ctx) as resp:
                cookies: dict[str, str] = {}
                for value in resp.headers.get_all("Set-Cookie") or []:
                    first = value.split(";", 1)[0]
                    if "=" in first:
                        k, v = first.split("=", 1)
                        cookies[k.strip()] = v.strip()
                return resp.read().decode("utf-8", errors="replace"), cookies
        except Exception:
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    try:
        cmd = ["/usr/bin/curl", "-L", "--silent", "--show-error", "--max-time", "45", "-X", method,
               "-A", QUARK_UA, "-D", "-"]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if data is not None:
            cmd += ["--data-binary", data.decode("utf-8", errors="replace")]
        cmd.append(url)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=55)
        raw = proc.stdout.decode("utf-8", errors="replace")
        parts = re.split(r"\r?\n\r?\n", raw)
        body = parts[-1] if parts else raw
        cookie_map: dict[str, str] = {}
        for line in raw.splitlines():
            if line.lower().startswith("set-cookie:"):
                first = line.split(":", 1)[1].strip().split(";", 1)[0]
                if "=" in first:
                    k, v = first.split("=", 1)
                    cookie_map[k.strip()] = v.strip()
        return body, cookie_map
    except Exception:
        return "", {}


def _baidu_name_candidates(body: str) -> list[str]:
    patterns = [
        r'"server_filename"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"filename"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"file_name"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"title"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)',
        r'<title[^>]*>(.*?)</title>',
    ]
    out: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, body or "", re.I | re.S):
            raw = m.group(1)
            try:
                if "\\u" in raw or "\\/" in raw or '\\"' in raw:
                    raw = json.loads('"' + raw.replace('"', '\\"') + '"')
            except Exception:
                pass
            name = _decode_baidu_name(raw)
            if name and name not in out:
                out.append(name)
    return out


def fetch_baidu_share_name(share_url: str, passcode: str = "") -> str:
    """读取百度分享真实顶层名称。支持带提取码链接的 share/verify 解锁。"""
    cookie = get_setting("baidu_cookies").strip()
    headers = {
        "User-Agent": QUARK_UA,
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Referer": "https://pan.baidu.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    bodies: list[str] = []
    body, set_cookies = _baidu_http_text(share_url, headers)
    if body:
        bodies.append(body)
    # 密码分享必须先调用 verify 获取 BDCLND，否则页面里通常拿不到 file_list。
    if passcode:
        sid = share_url.split("/s/")[-1].split("?")[0].split("#")[0]
        surl_values = [sid]
        if sid.startswith("1") and len(sid) > 1:
            surl_values.append(sid[1:])
        for surl in surl_values:
            verify_url = "https://pan.baidu.com/share/verify?" + urllib.parse.urlencode({
                "surl": surl, "t": str(int(time.time() * 1000)), "channel": "chunlei",
                "web": "1", "app_id": "250528", "clienttype": "0"
            })
            verify_headers = dict(headers)
            verify_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            verify_headers["X-Requested-With"] = "XMLHttpRequest"
            verify_body, verify_cookies = _baidu_http_text(
                verify_url, verify_headers, "POST", urllib.parse.urlencode({"pwd": passcode, "vcode": "", "vcode_str": ""}).encode()
            )
            randsk = ""
            try:
                obj = json.loads(verify_body or "{}")
                randsk = str(obj.get("randsk") or obj.get("data", {}).get("randsk") or "")
            except Exception:
                pass
            bdclnd = verify_cookies.get("BDCLND") or set_cookies.get("BDCLND") or randsk
            if bdclnd:
                unlocked_headers = dict(headers)
                unlocked_headers["Cookie"] = "; ".join(x for x in [cookie, "BDCLND=" + bdclnd] if x)
                unlocked, _ = _baidu_http_text(share_url, unlocked_headers)
                if unlocked:
                    bodies.insert(0, unlocked)
                    break
    for page in bodies:
        candidates = _baidu_name_candidates(page)
        if candidates:
            return candidates[0]
    return ""


def migrate_baidu_existing_task_path(task: Row, new_path: str) -> None:
    """任务由临时名改成真实名时，把旧目录内容一起迁移，避免页面改名后显示 0 文件。"""
    if not task or task["platform"] != "baidu":
        return
    old_path = "/" + normalize_rel(task["save_path"])
    new_path = "/" + normalize_rel(new_path)
    if old_path == new_path or not BAIDU_BIN.exists():
        return
    try:
        if not baidu_path_exists(old_path):
            return
        new_parent = "/" + parent_rel(new_path) if parent_rel(new_path) else "/"
        run_cmd([str(BAIDU_BIN), "mkdir", new_parent], timeout=120)
        if not baidu_path_exists(new_path):
            code, out = run_cmd_result([str(BAIDU_BIN), "mv", old_path, new_path], timeout=900)
            if code != 0:
                raise RuntimeError(_baidu_clean_output(out)[-2500:] or f"目录改名失败：{old_path} → {new_path}")
            add_log(task["id"], "INFO", f"已将旧临时目录改名为真实目录：{old_path} → {new_path}")
        else:
            stats = baidu_merge_contents(old_path, new_path, task_id=task["id"])
            baidu_remove_quiet(old_path)
            add_log(task["id"], "INFO", f"已把旧临时目录合并到真实目录：{old_path} → {new_path}；{stats}")
    except Exception as exc:
        # 不阻断名称修正，但保留明确日志，避免静默丢失旧目录。
        add_log(task["id"], "WARN", f"名称已识别，但旧目录自动迁移失败：{old_path} → {new_path}；{exc}")


def set_task_source_name(task_id: int, source_name: str, message: str = "") -> Row:
    source_name = clean_folder_name(source_name)
    if not source_name:
        raise RuntimeError("文件夹名称不能为空")
    task = task_row(task_id)
    new_path = replace_path_basename(task["save_path"], source_name)
    if task["platform"] == "baidu":
        with BAIDU_EXEC_LOCK:
            migrate_baidu_existing_task_path(task, new_path)
    with connect_db() as conn:
        conn.execute("UPDATE tasks SET name=?,save_path=?,status=?,last_message=? WHERE id=?",
                     (source_name, new_path, "待扫描", message or f"已按分享源文件夹名称修正为：{source_name}", task_id))
        conn.commit()
    add_log(task_id, "INFO", f"任务名称和保存目录已修正：{source_name} → {new_path}")
    refreshed = task_row(task_id)
    if refreshed and refreshed["platform"] == "baidu":
        try:
            baidu_refresh_saved_inventory(refreshed)
        except Exception as exc:
            add_log(task_id, "WARN", f"修正名称后刷新文件数量失败：{exc}")
    return task_row(task_id)


def resolve_baidu_share_name(task: Row) -> Row:
    real_name = fetch_baidu_share_name(task["share_url"], task["passcode"])
    if real_name:
        current_base = task["save_path"].rstrip("/").rsplit("/", 1)[-1]
        if task["name"] != real_name or current_base != real_name:
            return set_task_source_name(task["id"], real_name, f"已从百度分享链接识别原名：{real_name}")
        return task
    add_log(task["id"], "WARN", "暂未自动读取到百度分享文件夹原名。可在任务管理点击‘重新识别原名’，或点‘修正名称’手动填写。")
    return task

def _baidu_clean_output(output: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", str(output or ""))


def _baidu_is_duplicate(output: str) -> bool:
    text = _baidu_clean_output(output)
    return "文件重复" in text or "同名文件/文件夹" in text or "文件已存在" in text


def _baidu_is_failed(output: str, code: int = 0) -> bool:
    text = _baidu_clean_output(output)
    return code != 0 or "转存失败" in text or "保存到网盘失败" in text


def _baidu_join(parent: str, name: str) -> str:
    parent = "/" + normalize_rel(parent)
    if parent == "/":
        return "/" + str(name).strip("/")
    return parent.rstrip("/") + "/" + str(name).strip("/")


def baidu_path_exists(remote_path: str) -> bool:
    """通过 meta 核验真实远端路径；不再根据任务名称猜测。"""
    code, out = run_cmd_result([str(BAIDU_BIN), "meta", remote_path], timeout=120)
    text = _baidu_clean_output(out)
    low = text.lower()
    bad = ("不存在", "无法找到", "获取元信息失败", "file or directory does not exist", "param error", "错误码: 31066")
    return code == 0 and bool(text.strip()) and not any(x in low for x in bad)


def baidu_top_level_names(remote_dir: str) -> list[str]:
    """读取远端目录第一层名称，用于识别百度分享源的真实文件夹名。"""
    code, out = run_cmd_result([str(BAIDU_BIN), "tree", "--depth", "1", remote_dir], timeout=180)
    text = _baidu_clean_output(out)
    names: list[str] = []
    if code == 0:
        for raw in text.splitlines():
            line = raw.rstrip()
            m = re.search(r"(?:├──|└──)\s*(.+)$", line)
            if not m:
                continue
            name = m.group(1).strip()
            # --fsid 或部分版本可能在名称前显示 [数字]
            name = re.sub(r"^\[[^\]]+\]\s*", "", name).rstrip("/").strip()
            if name and name not in names:
                names.append(name)
    return names


def update_baidu_task_identity(task_id: int, name: str, save_path: str, message: str) -> Row:
    name = clean_folder_name(name) or name
    task = task_row(task_id)
    migrate_baidu_existing_task_path(task, save_path)
    with connect_db() as conn:
        conn.execute(
            "UPDATE tasks SET name=?,save_path=?,last_message=? WHERE id=?",
            (name, save_path, message[-2500:], task_id),
        )
        conn.commit()
    return task_row(task_id)


def baidu_transfer_args(task: Row) -> list[str]:
    link = task["share_url"]
    args = [str(BAIDU_BIN), "transfer", link]
    if task["passcode"] and "pwd=" not in link:
        args.append(task["passcode"])
    return args


def baidu_transfer_once(task: Row, target_dir: str) -> tuple[int, str]:
    """把分享内容转存到明确的空目录，并核验 BaiduPCS-Go 的工作目录。

    transfer 命令不能直接指定目标目录，只能保存到当前工作目录，因此所有百度
    操作都必须串行，避免两个任务互相改变 cd 工作目录。
    """
    run_cmd([str(BAIDU_BIN), "mkdir", target_dir], timeout=120)
    run_cmd([str(BAIDU_BIN), "cd", target_dir], timeout=120)
    code, pwd_out = run_cmd_result([str(BAIDU_BIN), "pwd"], timeout=60)
    pwd_text = _baidu_clean_output(pwd_out).strip().splitlines()
    current = pwd_text[-1].strip() if pwd_text else ""
    expected = "/" + normalize_rel(target_dir)
    if expected == "":
        expected = "/"
    if code != 0 or (current and normalize_rel(current) != normalize_rel(expected)):
        raise RuntimeError(f"百度执行器未切换到指定临时目录。期望：{expected}，实际：{current or '未知'}")
    return run_cmd_result(baidu_transfer_args(task), timeout=1800)


def baidu_remove_quiet(remote_path: str) -> None:
    try:
        run_cmd([str(BAIDU_BIN), "rm", remote_path], timeout=300)
    except Exception:
        pass


def baidu_meta_text(remote_path: str) -> tuple[int, str]:
    code, out = run_cmd_result([str(BAIDU_BIN), "meta", remote_path], timeout=120)
    return code, _baidu_clean_output(out)


def baidu_is_dir(remote_path: str) -> bool:
    """尽量可靠地区分远端文件与目录。"""
    code, text = baidu_meta_text(remote_path)
    low = text.lower()
    if code == 0:
        dir_patterns = (
            r'"isdir"\s*:\s*1', r'\bisdir\s*[:=]\s*(?:1|true)',
            r'(?:文件类型|类型|类别|属性)\s*[:：]\s*(?:目录|文件夹)',
            r'\[(?:目录|文件夹)\]', r'\b(directory|folder)\b',
        )
        file_patterns = (
            r'"isdir"\s*:\s*0', r'\bisdir\s*[:=]\s*(?:0|false)',
            r'(?:文件类型|类型|类别|属性)\s*[:：]\s*文件',
        )
        if any(re.search(p, text, re.I) for p in dir_patterns):
            return True
        if any(re.search(p, text, re.I) for p in file_patterns):
            return False
    # tree 若能列出直接子项，则该路径一定是目录。
    code, out = run_cmd_result([str(BAIDU_BIN), "tree", "--depth", "1", remote_path], timeout=180)
    clean = _baidu_clean_output(out)
    if code == 0 and re.search(r'(?:├──|└──)\s*.+', clean):
        return True
    # 空目录在部分版本的 meta 输出里只显示“大小 0B”，此时 ls 目录通常成功。
    code, out = run_cmd_result([str(BAIDU_BIN), "ls", remote_path], timeout=120)
    clean = _baidu_clean_output(out).lower()
    bad = ("不是目录", "not a directory", "文件不存在", "无法找到", "param error")
    return code == 0 and not any(x in clean for x in bad)


def _baidu_parse_tree_names(text: str) -> list[str]:
    names: list[str] = []
    for raw in _baidu_clean_output(text).splitlines():
        line = raw.rstrip()
        m = re.search(r"(?:├──|└──|\+--|`--|\|--|├─|└─)\s*(.+)$", line)
        if not m:
            continue
        name = re.sub(r"^\[[^\]]+\]\s*", "", m.group(1).strip()).rstrip("/").strip()
        if name and name not in names:
            names.append(name)
    return names


def _baidu_parse_ls_names(text: str) -> list[str]:
    names: list[str] = []
    skip_words = ("文件大小", "修改日期", "创建日期", "文件(目录)", "总计", "当前目录", "共 ", "序号")
    for raw in _baidu_clean_output(text).splitlines():
        line = raw.strip()
        if not line or set(line) <= set("-+=|─┌┐└┘├┤┬┴┼ ") or any(w in line for w in skip_words):
            continue
        # tablewriter 用竖线分栏；普通输出通常用两个以上空格分栏。
        if "|" in line:
            cols = [c.strip() for c in line.strip("|").split("|") if c.strip()]
        else:
            cols = [c.strip() for c in re.split(r"\s{2,}", line) if c.strip()]
        if not cols:
            continue
        candidate = cols[-1]
        candidate = re.sub(r"^\[[^\]]+\]\s*", "", candidate)
        candidate = re.sub(r"^[0-9]+\s+", "", candidate).rstrip("/").strip()
        if not candidate or candidate in (".", ".."):
            continue
        # 过滤错误、提示和统计行。
        if any(x in candidate.lower() for x in ("param error", "not found", "错误码", "获取目录列表失败")):
            continue
        if candidate not in names:
            names.append(candidate)
    return names


def baidu_top_level_names(remote_dir: str) -> list[str]:
    """兼容 BaiduPCS-Go 3.x/4.x 的 ls/tree 输出，读取目录第一层真实名称。"""
    candidates: list[str] = []
    commands = (
        [str(BAIDU_BIN), "ls", "-l", remote_dir],
        [str(BAIDU_BIN), "ls", remote_dir],
        [str(BAIDU_BIN), "tree", "--depth", "1", remote_dir],
    )
    raw_outputs: list[str] = []
    for cmd in commands:
        code, out = run_cmd_result(cmd, timeout=180)
        raw_outputs.append(_baidu_clean_output(out))
        parsed = _baidu_parse_tree_names(out) if "tree" in cmd else _baidu_parse_ls_names(out)
        for name in parsed:
            if name not in candidates:
                candidates.append(name)
    # 只保留远端真实存在的条目，避免把表头或统计数字当文件名。
    valid: list[str] = []
    for name in candidates:
        if baidu_path_exists(_baidu_join(remote_dir, name)):
            valid.append(name)
    if not valid:
        add_log(None, "WARN", "百度目录列表解析为空。目录：%s\n%s" % (remote_dir, "\n---\n".join(x[-2500:] for x in raw_outputs)))
    return valid


def baidu_file_signature(remote_path: str) -> dict[str, str]:
    """提取可用于判断同名文件是否变化的稳定字段。"""
    _, text = baidu_meta_text(remote_path)
    result: dict[str, str] = {}
    patterns = {
        "md5": [r'\bmd5\b\s*[:：=]\s*([a-fA-F0-9]{32})', r'"md5"\s*:\s*"([a-fA-F0-9]{32})"'],
        "size": [r'(?:文件大小|大小|size)\s*[:：=]\s*([0-9]+)', r'"size"\s*:\s*([0-9]+)'],
        "mtime": [r'(?:修改时间|server_mtime|mtime)\s*[:：=]\s*([^\n\r,}]+)', r'"server_mtime"\s*:\s*([0-9]+)'],
    }
    for key, pats in patterns.items():
        for pat in pats:
            m = re.search(pat, text, re.I)
            if m:
                result[key] = m.group(1).strip().strip('"')
                break
    return result


def baidu_files_differ(source_path: str, target_path: str) -> bool:
    """判断同名文件是否发生变化。

    优先比较 MD5，其次比较大小和修改时间。只有字段确实可用时才覆盖，
    避免把无法判断的同名文件误删。
    """
    src = baidu_file_signature(source_path)
    dst = baidu_file_signature(target_path)
    if src.get("md5") and dst.get("md5"):
        return src["md5"].lower() != dst["md5"].lower()
    if src.get("size") and dst.get("size") and src["size"] != dst["size"]:
        return True
    if src.get("mtime") and dst.get("mtime") and src["mtime"] != dst["mtime"]:
        return True
    return False


def baidu_move(source_path: str, target_dir: str) -> None:
    code, out = run_cmd_result([str(BAIDU_BIN), "mv", source_path, target_dir], timeout=900)
    if code != 0:
        raise RuntimeError(_baidu_clean_output(out)[-3000:] or f"移动失败：{source_path} → {target_dir}")


def baidu_merge_contents(source_dir: str, target_dir: str, *, task_id: int, relative: str = "") -> dict[str, int]:
    """把 source_dir 的内容合并到 target_dir，绝不移动 source_dir 外壳。

    这正是防止 `/文件夹1/文件夹1` 的核心：更新时只移动新增子项；已存在的
    同名目录递归合并，已存在且未确认变化的文件保留不动。
    """
    run_cmd([str(BAIDU_BIN), "mkdir", target_dir], timeout=120)
    stats = {"new_files": 0, "new_dirs": 0, "updated_files": 0, "skipped": 0}
    for name in baidu_top_level_names(source_dir):
        src = _baidu_join(source_dir, name)
        dst = _baidu_join(target_dir, name)
        src_is_dir = baidu_is_dir(src)
        dst_exists = baidu_path_exists(dst)
        if not dst_exists:
            baidu_move(src, target_dir)
            stats["new_dirs" if src_is_dir else "new_files"] += 1
            continue
        dst_is_dir = baidu_is_dir(dst)
        if src_is_dir and dst_is_dir:
            child = baidu_merge_contents(src, dst, task_id=task_id, relative=_baidu_join(relative or "/", name))
            for k, v in child.items():
                stats[k] += v
            continue
        if (not src_is_dir) and (not dst_is_dir) and baidu_files_differ(src, dst):
            # 旧文件进入百度回收站，新文件随后移动到原位置；不会形成重名副本。
            run_cmd([str(BAIDU_BIN), "rm", dst], timeout=300)
            baidu_move(src, target_dir)
            stats["updated_files"] += 1
            continue
        stats["skipped"] += 1
    return stats


def baidu_flatten_accidental_nested(target_dir: str, actual_name: str, task_id: int) -> dict[str, int]:
    """修复旧版本产生的 `/123/123`，把内层内容合并回外层后移除内层。"""
    nested = _baidu_join(target_dir, actual_name)
    if not baidu_path_exists(nested) or not baidu_is_dir(nested):
        return {"new_files": 0, "new_dirs": 0, "updated_files": 0, "skipped": 0}
    add_log(task_id, "WARN", f"检测到旧版重复嵌套目录，正在自动扁平化：{nested} → {target_dir}")
    stats = baidu_merge_contents(nested, target_dir, task_id=task_id)
    baidu_remove_quiet(nested)
    return stats


def _sum_stats(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in {**a, **b}}


def baidu_name_from_transfer_output(output: str) -> str:
    text = _baidu_clean_output(output)
    patterns = [
        r'(?:文件夹|目录)[“"\[]([^”"\]\n]+)[”"\]]',
        r'(?:转存|保存)(?:文件|目录|文件夹)?\s*[：:]\s*([^\n\r]+)',
        r'(?:转存成功|保存成功).*?[“"\[]([^”"\]\n]+)[”"\]]',
        r'server_filename["\s:=]+([^"\n,}]+)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            name = _decode_baidu_name(m.group(1).strip())
            if name and not name.startswith(("http", "/")):
                return name
    return ""


def baidu_refresh_saved_inventory(task: Row) -> tuple[int, int]:
    """扫描百度目标目录，显示已保存文件/目录数量和文件明细。"""
    root = task["save_path"]
    if not baidu_path_exists(root):
        with connect_db() as conn:
            conn.execute("UPDATE tasks SET stored_files=0,stored_folders=0 WHERE id=?", (task["id"],))
            conn.commit()
        return 0, 0
    files = 0
    folders = 0
    stack = [(root, "")]
    rows: list[tuple] = []
    seen: set[str] = set()
    while stack and len(seen) < 5000:
        remote, rel = stack.pop()
        if remote in seen:
            continue
        seen.add(remote)
        for name in baidu_top_level_names(remote):
            child = _baidu_join(remote, name)
            child_rel = normalize_rel((rel + "/" + name).strip("/"))
            if baidu_is_dir(child):
                folders += 1
                stack.append((child, child_rel))
                rows.append((task["id"], child_rel, "", 0, "", "dir:" + child_rel, 1, now_iso()))
            else:
                files += 1
                sig = baidu_file_signature(child)
                fp = "|".join([sig.get("md5", ""), sig.get("size", ""), sig.get("mtime", "")]) or child_rel
                rows.append((task["id"], child_rel, "", int(sig.get("size") or 0), sig.get("mtime", ""), fp, 0, now_iso()))
    with connect_db() as conn:
        conn.execute("DELETE FROM source_snapshots WHERE task_id=?", (task["id"],))
        if rows:
            conn.executemany("INSERT INTO source_snapshots(task_id,file_path,source_id,size,modified_at,fingerprint,is_dir,last_seen) VALUES(?,?,?,?,?,?,?,?)", rows)
        conn.execute("UPDATE tasks SET stored_files=?,stored_folders=? WHERE id=?", (files, folders, task["id"]))
        conn.commit()
    return files, folders


def _baidu_is_placeholder_name(name: str) -> bool:
    value = str(name or "").strip()
    return (not value) or bool(re.match(r"^(?:百度任务|百度链接|未识别)[-_]", value, re.I))


def _baidu_direct_root() -> str:
    return "/" + normalize_rel(get_setting("baidu_transfer_dir", "/资源数据"))


def _baidu_folder_snapshot(root: str) -> dict[str, str]:
    snap: dict[str, str] = {}
    for name in baidu_top_level_names(root):
        path = _baidu_join(root, name)
        if not baidu_is_dir(path):
            continue
        _, meta = baidu_meta_text(path)
        snap[name] = meta[-2000:]
    return snap


def _baidu_cleanup_one_wrapper(wrapper: str, final_root: str, task_id: int | None = None) -> bool:
    if not baidu_path_exists(wrapper) or not baidu_is_dir(wrapper):
        return False
    children = baidu_top_level_names(wrapper)
    if not children:
        baidu_remove_quiet(wrapper)
        return True
    for child in children:
        src = _baidu_join(wrapper, child)
        dst = _baidu_join(final_root, child)
        if not baidu_path_exists(dst):
            baidu_move(src, final_root)
        elif baidu_is_dir(src) and baidu_is_dir(dst):
            baidu_merge_contents(src, dst, task_id=task_id or 0)
            baidu_remove_quiet(src)
        # 同名文件保留最终目录中的版本，删除临时副本，避免重复。
        elif not baidu_is_dir(src):
            baidu_remove_quiet(src)
    baidu_remove_quiet(wrapper)
    return True


def baidu_cleanup_legacy_temp_dirs(final_root: str, task_id: int | None = None) -> int:
    """清理旧版本和异常中断遗留的中转目录。

    中转目录只允许短暂存在于网盘根目录，最终资源目录中不会保留任何临时层级。
    """
    fixed = 0
    run_cmd([str(BAIDU_BIN), "mkdir", final_root], timeout=120)

    # 旧版本曾把临时目录建在 /资源数据 里面，先把其中真实内容并回最终根目录。
    for name in list(baidu_top_level_names(final_root)):
        if name.startswith(("__网盘追更临时_", "__网盘追更内部临时区", "_网盘追更临时_", "__网盘追更中转_")):
            if _baidu_cleanup_one_wrapper(_baidu_join(final_root, name), final_root, task_id):
                fixed += 1

    # 兼容旧版内部恢复区。
    root_internal = "/__网盘追更内部临时区"
    if baidu_path_exists(root_internal):
        for name in list(baidu_top_level_names(root_internal)):
            wrapper = _baidu_join(root_internal, name)
            if _baidu_cleanup_one_wrapper(wrapper, final_root, task_id):
                fixed += 1
        baidu_remove_quiet(root_internal)

    # 本版的中转目录位于网盘根目录。若上次因断网/强退留下，启动下一次任务时强制清除。
    for name in list(baidu_top_level_names("/")):
        if name.startswith("__网盘追更中转_"):
            baidu_remove_quiet(_baidu_join("/", name))
            fixed += 1
    return fixed


def _baidu_pick_actual_name(task: Row, before: dict[str, str], after: dict[str, str], output: str) -> str:
    # 第一优先：本次转存后新增的真实顶层文件夹。
    new_names = [n for n in after if n not in before]
    if len(new_names) == 1:
        return clean_folder_name(new_names[0]) or new_names[0]
    # 第二优先：本次发生变化的唯一顶层文件夹。
    changed = [n for n in after if n not in before or after.get(n) != before.get(n)]
    if len(changed) == 1:
        return clean_folder_name(changed[0]) or changed[0]
    # 第三优先：执行器输出、分享页面。
    out_name = baidu_name_from_transfer_output(output)
    if out_name and out_name in after:
        return clean_folder_name(out_name) or out_name
    try:
        page_name = fetch_baidu_share_name(task["share_url"], task["passcode"])
    except Exception:
        page_name = ""
    if page_name and page_name in after:
        return clean_folder_name(page_name) or page_name
    # 已经识别过的任务沿用真实名称。
    if not _baidu_is_placeholder_name(task["name"]) and task["name"] in after:
        return clean_folder_name(task["name"]) or task["name"]
    # 保存路径本身已经指向真实目录时沿用。
    base = normalize_rel(task["save_path"]).rsplit("/", 1)[-1] if normalize_rel(task["save_path"]) else ""
    if base and not _baidu_is_placeholder_name(base) and base in after:
        return clean_folder_name(base) or base
    return ""


def _baidu_wait_top_level(remote_dir: str, timeout: int = 30) -> list[str]:
    """等待百度网盘目录列表最终一致，避免转存成功后立刻 ls 仍为空。"""
    deadline = time.time() + max(3, timeout)
    last: list[str] = []
    while time.time() < deadline:
        last = baidu_top_level_names(remote_dir)
        if last:
            return last
        time.sleep(1.2)
    return last


def _baidu_stage_name(task_id: int) -> str:
    token = uuid.uuid4().hex[:8]
    return f"/__网盘追更中转_{task_id}_{int(time.time())}_{token}"


def _baidu_pick_stage_folder(task: Row, stage_dir: str, output: str) -> tuple[str, str]:
    """从空白中转目录中的实际转存结果取得分享文件夹原名。"""
    names = _baidu_wait_top_level(stage_dir, 35)
    if not names:
        clean = _baidu_clean_output(output)[-2500:]
        raise RuntimeError("百度转存后中转目录为空。请确认分享链接有效、提取码正确且 Cookie 未失效。原始结果：" + clean)

    dirs = [name for name in names if baidu_is_dir(_baidu_join(stage_dir, name))]
    known = "" if _baidu_is_placeholder_name(task["name"]) else clean_folder_name(task["name"])
    page_name = ""
    try:
        page_name = fetch_baidu_share_name(task["share_url"], task["passcode"])
    except Exception:
        pass
    output_name = baidu_name_from_transfer_output(output)

    # 用户的链接是单个文件夹：优先使用中转目录里唯一的真实文件夹。
    for candidate in (page_name, output_name, known):
        if candidate and candidate in dirs:
            return candidate, _baidu_join(stage_dir, candidate)
    if len(dirs) == 1:
        return dirs[0], _baidu_join(stage_dir, dirs[0])

    # 极少数分享页把文件夹外壳拆开成多个顶层项目。此时只有已知名称可安全处理。
    if known and known in names:
        return known, _baidu_join(stage_dir, known)
    raise RuntimeError("分享链接没有识别为单个文件夹。中转目录检测到：" + "、".join(names[:20]))


def baidu_direct_transfer(task: Row) -> tuple[Row, str, bool]:
    """可靠的百度增量转存。

    BaiduPCS-Go 对已存在的同名顶层文件夹会直接返回“文件重复”，无法把新增子文件
    合并进去。因此每次先转存到一个唯一的空白中转目录，再只把中转目录里的新增或
    变化内容合并到 `/资源数据/<分享原名>`。中转目录位于网盘根目录，且无论成功、
    失败都会在 finally 中删除；最终网盘只保留用户需要的真实文件夹。
    """
    root = _baidu_direct_root()
    run_cmd([str(BAIDU_BIN), "mkdir", root], timeout=120)
    fixed = baidu_cleanup_legacy_temp_dirs(root, task["id"])
    stage_dir = _baidu_stage_name(task["id"])
    output = ""
    actual_name = ""
    final_path = ""
    stats = {"new_files": 0, "new_dirs": 0, "updated_files": 0, "skipped": 0}

    try:
        code, output = baidu_transfer_once(task, stage_dir)
        stage_names = _baidu_wait_top_level(stage_dir, 35)
        if not stage_names:
            clean = _baidu_clean_output(output)[-2500:]
            if _baidu_is_duplicate(clean):
                raise RuntimeError("百度在空白中转目录仍返回文件重复，通常表示分享链接或账号状态异常。原始结果：" + clean)
            raise RuntimeError(clean or f"百度转存失败，退出码 {code}")

        actual_name, staged_folder = _baidu_pick_stage_folder(task, stage_dir, output)
        actual_name = clean_folder_name(actual_name) or actual_name
        final_path = _baidu_join(root, actual_name)

        if not baidu_path_exists(final_path):
            # 首次转存：直接把真实文件夹从中转目录移动到 /资源数据，不移动中转外壳。
            baidu_move(staged_folder, root)
            stats["new_dirs"] += 1
        else:
            # 后续更新：仅把内部新增/变化项合并进去，绝不形成 /文件夹/文件夹。
            merged = baidu_merge_contents(staged_folder, final_path, task_id=task["id"])
            stats = _sum_stats(stats, merged)
            baidu_remove_quiet(staged_folder)

        # 修复旧版曾产生的 /真实名/真实名 嵌套。
        stats = _sum_stats(stats, baidu_flatten_accidental_nested(final_path, actual_name, task["id"]))

        with connect_db() as conn:
            conn.execute("UPDATE tasks SET name=?,save_path=? WHERE id=?", (actual_name, final_path, task["id"]))
            conn.commit()
        task = task_row(task["id"])
        files, folders = baidu_refresh_saved_inventory(task)
        changed = any(stats.get(k, 0) > 0 for k in ("new_files", "new_dirs", "updated_files"))
        status_text = "更新完成" if changed else "没有新增内容"
        extra = f"；已清理 {fixed} 个旧临时目录" if fixed else ""
        msg = (
            f"{status_text}：{final_path}；新增文件 {stats['new_files']}，新增目录 {stats['new_dirs']}，"
            f"替换更新 {stats['updated_files']}，跳过相同文件 {stats['skipped']}；"
            f"当前共 {files} 个文件、{folders} 个子目录{extra}。"
        )
        return task, msg, changed
    finally:
        # 先离开中转目录，再强制清理。清理失败会记录日志，并在下次任务开始前再次清理。
        try:
            run_cmd_result([str(BAIDU_BIN), "cd", "/"], timeout=60)
        except Exception:
            pass
        if stage_dir:
            try:
                code, clean_out = run_cmd_result([str(BAIDU_BIN), "rm", stage_dir], timeout=600)
                if code != 0 and baidu_path_exists(stage_dir):
                    add_log(task["id"], "WARN", f"中转目录首次清理失败，将在下次自动重试：{stage_dir}；{_baidu_clean_output(clean_out)[-1200:]}")
            except Exception as exc:
                add_log(task["id"], "WARN", f"中转目录清理异常，将在下次自动重试：{stage_dir}；{exc}")

def apply_baidu_task(task: Row) -> str:
    cookies = get_setting("baidu_cookies").strip()
    if not cookies:
        raise RuntimeError("尚未配置百度 Cookie")
    root = _baidu_direct_root()
    with BAIDU_EXEC_LOCK:
        ensure_baidu_ready(cookies, root)
        task, output, actually_saved = baidu_direct_transfer(task)
    stamp = now_iso()
    status = "已更新" if actually_saved else "无更新"
    with connect_db() as conn:
        conn.execute("UPDATE change_items SET status='applied',applied_at=? WHERE task_id=? AND status='pending'", (stamp, task["id"]))
        conn.execute("UPDATE tasks SET pending_files=0,pending_folders=0,last_update=?,last_scan=?,last_change=?,status=?,last_message=? WHERE id=?", (stamp, stamp, output[:200], status, output[-2500:], task["id"]))
        conn.commit()
    return output


# ============================================================================
# 百度网盘内置直连引擎（不依赖 BaiduPCS-Go、不下载 GitHub 文件）
# ============================================================================
# 仅使用用户本机保存的百度 Cookie，通过百度网页端接口完成：
# 1) 分享链接原名识别；2) 首次直接保存到 /资源数据/真实文件夹名；
# 3) 逐文件扫描；4) 后续仅转存新增/变化文件。
# 全流程不会创建任何“百度任务-xxx”或“网盘追更临时”云端目录。

BAIDU_APP_ID = "250528"
BAIDU_UA = QUARK_UA


def _bd_cookie_dict(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for piece in str(raw or "").split(";"):
        if "=" not in piece:
            continue
        key, value = piece.strip().split("=", 1)
        if key:
            result[key] = value
    return result


def _bd_norm_remote(path: str) -> str:
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts) if parts else "/"


def _bd_join(parent: str, name: str) -> str:
    parent = _bd_norm_remote(parent)
    return (parent.rstrip("/") + "/" + str(name).strip("/")).replace("//", "/")


def _bd_parent(path: str) -> str:
    path = _bd_norm_remote(path)
    if path == "/":
        return "/"
    value = path.rsplit("/", 1)[0]
    return value or "/"


def _bd_basename(path: str) -> str:
    return _bd_norm_remote(path).rstrip("/").rsplit("/", 1)[-1]


class BaiduDirectClient:
    BASE = "https://pan.baidu.com"

    def __init__(self, cookies: str):
        self.cookies = _bd_cookie_dict(cookies)
        if not self.cookies.get("BDUSS") or not self.cookies.get("STOKEN"):
            raise RuntimeError("百度 Cookie 必须同时包含 BDUSS 和 STOKEN")
        self.bdstoken = ""
        self._ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
            self._ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _save_set_cookies(self, headers: Any) -> None:
        try:
            values = headers.get_all("Set-Cookie") or []
        except Exception:
            values = []
        for value in values:
            first = str(value).split(";", 1)[0]
            if "=" in first:
                key, val = first.split("=", 1)
                self.cookies[key.strip()] = val.strip()

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                form: dict[str, Any] | None = None, referer: str | None = None,
                want_json: bool = True, timeout: int = 45) -> Any:
        if params:
            encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url += ("&" if "?" in url else "?") + encoded
        body = None
        headers = {
            "User-Agent": BAIDU_UA,
            "Accept": "application/json,text/plain,*/*" if want_json else "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": self.cookie_header(),
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Origin"] = "https://pan.baidu.com"

        last: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
                    self._save_set_cookies(resp.headers)
                    raw = resp.read()
                    if not want_json:
                        return raw.decode("utf-8", errors="replace")
                    txt = raw.decode("utf-8", errors="replace").strip()
                    if not txt:
                        return {}
                    try:
                        return json.loads(txt)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("百度接口返回非 JSON：" + txt[-1000:]) from exc
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[-1200:]
                if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                    raise RuntimeError(f"百度接口 HTTP {exc.code}：{detail}") from exc
                last = RuntimeError(f"HTTP {exc.code}: {detail}")
            except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionResetError,
                    BrokenPipeError, http.client.RemoteDisconnected) as exc:
                last = exc
            if attempt < 3:
                time.sleep(1.2 * (2 ** attempt))
        raise RuntimeError(f"百度网络连接连续重试失败：{last}")

    @staticmethod
    def feature_from_url(share_url: str) -> str:
        parsed = urllib.parse.urlsplit(share_url)
        if parsed.path.rstrip("/").endswith("/share/init"):
            surl = (urllib.parse.parse_qs(parsed.query).get("surl") or [""])[0]
            return "1" + surl.lstrip("1") if surl else ""
        m = re.search(r"/s/([^/?#]+)", parsed.path)
        return m.group(1) if m else ""

    @staticmethod
    def _tokens_from_page(page: str) -> dict[str, str]:
        candidates: list[str] = []
        patterns = [
            r"(\{.+?\"loginstate\".+?\})\);",
            r"yunData\.setData\((\{.*?\})\);",
            r"locals\.mset\((\{.*?\})\);",
        ]
        for pat in patterns:
            candidates.extend(m.group(1) for m in re.finditer(pat, page or "", re.S))
        for raw in candidates:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            token = str(data.get("bdstoken") or obj.get("bdstoken") or "")
            shareid = str(data.get("shareid") or obj.get("shareid") or "")
            share_uk = str(data.get("share_uk") or obj.get("share_uk") or data.get("uk") or obj.get("uk") or "")
            if token and shareid and share_uk:
                return {"bdstoken": token, "shareid": shareid, "share_uk": share_uk}
        # 页面格式变化时，再从局部 JSON 字段兜底。
        def field(name: str) -> str:
            m = re.search(rf'"{re.escape(name)}"\s*:\s*"?([^",}}]+)', page or "")
            return m.group(1).strip() if m else ""
        result = {"bdstoken": field("bdstoken"), "shareid": field("shareid"), "share_uk": field("share_uk") or field("uk")}
        return result if all(result.values()) else {}

    def get_bdstoken(self, force: bool = False) -> str:
        if self.bdstoken and not force:
            return self.bdstoken
        data = self.request("GET", self.BASE + "/api/gettemplatevariable", params={
            "clienttype": "0", "app_id": BAIDU_APP_ID, "web": "1", "fields": '["bdstoken"]'
        }, referer=self.BASE + "/disk/main")
        token = str((data.get("result") or {}).get("bdstoken") or data.get("bdstoken") or "")
        if not token or int(data.get("errno", 0) or 0) != 0:
            raise RuntimeError("百度 Cookie 已失效，无法取得账号令牌，请重新复制 Cookie")
        self.bdstoken = token
        return token

    def validate(self) -> str:
        data = self.request("GET", self.BASE + "/api/user/getinfo", params={"need_selfinfo": "1"}, referer=self.BASE + "/disk/main")
        errno = int(data.get("errno", 0) or 0)
        if errno != 0:
            raise RuntimeError(f"百度 Cookie 验证失败，错误码 {errno}")
        self.get_bdstoken()
        name = str((data.get("records") or [{}])[0].get("baidu_name") or data.get("baidu_name") or data.get("username") or "已登录")
        return name

    def share_context(self, share_url: str, passcode: str = "") -> dict[str, Any]:
        feature = self.feature_from_url(share_url)
        if not feature:
            raise RuntimeError("无法识别百度分享链接")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(share_url).query)
        code = str(passcode or (qs.get("pwd") or [""])[0]).strip()
        canonical = f"https://pan.baidu.com/s/{feature}"
        page = self.request("GET", canonical, referer=self.BASE + "/disk/main", want_json=False)
        tokens = self._tokens_from_page(page)
        if not tokens:
            raise RuntimeError("未能读取百度分享信息；请确认 Cookie 中包含有效 STOKEN，且分享链接未失效")

        if code:
            verify = self.request("POST", self.BASE + "/share/verify", params={
                "shareid": tokens["shareid"], "time": str(int(time.time() * 1000)),
                "clienttype": "1", "uk": tokens["share_uk"]
            }, form={"pwd": code, "vcode": "null", "vcode_str": "null", "bdstoken": tokens["bdstoken"]}, referer=canonical)
            errno = int(verify.get("errno", 0) or 0)
            if errno != 0:
                raise RuntimeError("百度提取码错误" if errno == -9 else f"百度分享验证失败，错误码 {errno}")
            randsk = str(verify.get("randsk") or "")
            if randsk and not self.cookies.get("BDCLND"):
                self.cookies["BDCLND"] = randsk
            page = self.request("GET", canonical, referer=f"https://pan.baidu.com/share/init?surl={feature[1:]}", want_json=False)
            tokens = self._tokens_from_page(page) or tokens

        root = self.share_list(tokens, feature, root=True)
        if len(root) != 1 or not bool(root[0].get("isdir")):
            names = "、".join(str(x.get("server_filename") or "") for x in root[:10])
            raise RuntimeError("当前版本要求分享链接顶层是一个文件夹；检测到：" + (names or "空目录"))
        item = root[0]
        name = clean_folder_name(str(item.get("server_filename") or ""))
        if not name:
            raise RuntimeError("百度分享文件夹名称为空")
        return {"feature": feature, "canonical": canonical, "tokens": tokens, "root": item, "name": name}

    def share_list(self, tokens: dict[str, str], feature: str, *, root: bool = False, dir_path: str = "") -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "bdstoken": tokens["bdstoken"], "web": "1", "app_id": BAIDU_APP_ID,
                "channel": "chunlei", "clienttype": "0", "page": page, "num": 100,
                "order": "name", "desc": "0", "showempty": "0", "shareid": tokens["shareid"],
                "uk": tokens["share_uk"], "shorturl": feature[1:] if feature.startswith("1") else feature,
            }
            if root:
                params["root"] = "1"
            else:
                params["root"] = "0"
                params["dir"] = dir_path
            data = self.request("GET", self.BASE + "/share/list", params=params, referer=f"https://pan.baidu.com/s/{feature}")
            errno = int(data.get("errno", 0) or 0)
            if errno != 0:
                raise RuntimeError(f"读取百度分享目录失败，错误码 {errno}")
            items = data.get("list") or []
            merged.extend(items)
            if len(items) < 100:
                return merged
            page += 1

    def source_tree(self, ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
        source: dict[str, dict[str, Any]] = {}
        tokens, feature, root = ctx["tokens"], ctx["feature"], ctx["root"]

        def walk(remote_path: str, relative_parent: str = "") -> None:
            for item in self.share_list(tokens, feature, root=False, dir_path=remote_path):
                name = str(item.get("server_filename") or "").strip()
                if not name:
                    continue
                rel = normalize_rel(relative_parent + "/" + name)
                is_dir = bool(item.get("isdir"))
                source[rel] = {
                    "path": rel, "name": name, "dir": is_dir,
                    "fs_id": str(item.get("fs_id") or ""),
                    "size": int(item.get("size") or 0),
                    "mtime": str(item.get("server_mtime") or item.get("local_mtime") or ""),
                    "md5": str(item.get("md5") or item.get("content_md5") or ""),
                    "remote_path": str(item.get("path") or _bd_join(remote_path, name)),
                }
                if is_dir:
                    walk(source[rel]["remote_path"], rel)
        walk(str(root.get("path") or "/" + ctx["name"]))
        return source

    def list_dir(self, remote_dir: str) -> list[dict[str, Any]]:
        remote_dir = _bd_norm_remote(remote_dir)
        merged: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.request("GET", self.BASE + "/api/list", params={
                "dir": remote_dir, "order": "name", "desc": "0", "showempty": "0",
                "web": "1", "page": page, "num": 1000, "channel": "chunlei",
                "app_id": BAIDU_APP_ID, "clienttype": "0", "bdstoken": self.get_bdstoken(),
            }, referer=self.BASE + "/disk/main")
            errno = int(data.get("errno", 0) or 0)
            if errno in (-9, -7, -6):
                return []
            if errno != 0:
                raise RuntimeError(f"读取百度目录失败：{remote_dir}，错误码 {errno}")
            items = data.get("list") or []
            merged.extend(items)
            if len(items) < 1000:
                return merged
            page += 1

    def path_item(self, remote_path: str) -> dict[str, Any] | None:
        remote_path = _bd_norm_remote(remote_path)
        if remote_path == "/":
            return {"path": "/", "isdir": 1, "server_filename": "/"}
        parent = _bd_parent(remote_path)
        name = _bd_basename(remote_path)
        for item in self.list_dir(parent):
            if str(item.get("server_filename") or "") == name:
                return item
        return None

    def path_exists(self, remote_path: str) -> bool:
        return self.path_item(remote_path) is not None

    def mkdir(self, remote_path: str) -> None:
        remote_path = _bd_norm_remote(remote_path)
        if remote_path == "/":
            return
        current = ""
        for part in remote_path.strip("/").split("/"):
            current = _bd_join(current or "/", part)
            old = self.path_item(current)
            if old:
                if not bool(old.get("isdir")):
                    raise RuntimeError(f"目标路径存在同名文件，无法创建目录：{current}")
                continue
            data = self.request("POST", self.BASE + "/api/create", params={
                "a": "commit", "bdstoken": self.get_bdstoken(), "clienttype": "0",
                "channel": "chunlei", "web": "1", "app_id": BAIDU_APP_ID,
            }, form={"path": current, "isdir": "1", "block_list": "[]", "method": "post"}, referer=self.BASE + "/disk/main")
            errno = int(data.get("errno", 0) or 0)
            if errno not in (0, -8):
                raise RuntimeError(f"创建百度目录失败：{current}，错误码 {errno}")

    def destination_tree(self, root_path: str) -> dict[str, dict[str, Any]]:
        root_path = _bd_norm_remote(root_path)
        if not self.path_exists(root_path):
            return {}
        result: dict[str, dict[str, Any]] = {}
        def walk(remote_dir: str, rel_parent: str = "") -> None:
            for item in self.list_dir(remote_dir):
                name = str(item.get("server_filename") or "").strip()
                if not name:
                    continue
                rel = normalize_rel(rel_parent + "/" + name)
                is_dir = bool(item.get("isdir"))
                result[rel] = {
                    "path": rel, "name": name, "dir": is_dir,
                    "fs_id": str(item.get("fs_id") or ""), "size": int(item.get("size") or 0),
                    "mtime": str(item.get("server_mtime") or item.get("local_mtime") or ""),
                    "md5": str(item.get("md5") or ""), "remote_path": str(item.get("path") or _bd_join(remote_dir, name)),
                }
                if is_dir:
                    walk(result[rel]["remote_path"], rel)
        walk(root_path)
        return result

    @staticmethod
    def same_file(src: dict[str, Any], dst: dict[str, Any]) -> bool:
        if bool(src.get("dir")) != bool(dst.get("dir")):
            return False
        if src.get("dir"):
            return True
        smd5, dmd5 = str(src.get("md5") or ""), str(dst.get("md5") or "")
        if smd5 and dmd5:
            return smd5 == dmd5
        if int(src.get("size") or 0) != int(dst.get("size") or 0):
            return False
        # 无 MD5 时结合大小和修改时间判断；同大小替换通常会改变 mtime。
        smt, dmt = str(src.get("mtime") or ""), str(dst.get("mtime") or "")
        return not smt or not dmt or smt == dmt

    def transfer(self, ctx: dict[str, Any], fs_ids: list[str], target_dir: str) -> dict[str, Any]:
        if not fs_ids:
            return {"saved": 0, "duplicate": False}
        self.mkdir(target_dir)
        tokens = ctx["tokens"]
        data = self.request("POST", self.BASE + "/share/transfer", params={
            "app_id": BAIDU_APP_ID, "channel": "chunlei", "clienttype": "0", "web": "1",
            "shareid": tokens["shareid"], "from": tokens["share_uk"], "bdstoken": tokens["bdstoken"],
        }, form={"fsidlist": "[" + ",".join(fs_ids) + "]", "path": _bd_norm_remote(target_dir)}, referer=ctx["canonical"], timeout=120)
        errno = int(data.get("errno", 0) or 0)
        if errno == 0:
            return {"saved": len(fs_ids), "duplicate": False, "data": data}
        info = data.get("info") or []
        inner = int((info[0] if info else {}).get("errno", 0) or 0)
        if errno == 4 or inner == -30:
            return {"saved": 0, "duplicate": True, "data": data}
        if errno == 12 and int(data.get("target_file_nums", 0) or 0) > int(data.get("target_file_nums_limit", 0) or 0):
            raise RuntimeError("百度单次转存文件数量超过账号限制")
        raise RuntimeError(f"百度分享转存失败，错误码 {errno}" + (f"，子错误 {inner}" if inner else ""))

    def delete_path(self, remote_path: str) -> None:
        remote_path = _bd_norm_remote(remote_path)
        if not self.path_exists(remote_path):
            return
        data = self.request("POST", self.BASE + "/api/filemanager", params={
            "opera": "delete", "async": "2", "onnest": "fail", "newVerify": "1",
            "bdstoken": self.get_bdstoken(), "clienttype": "0", "channel": "chunlei",
            "web": "1", "app_id": BAIDU_APP_ID,
        }, form={"filelist": json.dumps([remote_path], ensure_ascii=False)}, referer=self.BASE + "/disk/main", timeout=120)
        errno = int(data.get("errno", 0) or 0)
        if errno != 0:
            raise RuntimeError(f"删除百度旧文件失败：{remote_path}，错误码 {errno}")
        deadline = time.time() + 20
        while time.time() < deadline and self.path_exists(remote_path):
            time.sleep(0.8)
        if self.path_exists(remote_path):
            raise RuntimeError(f"等待百度删除完成超时：{remote_path}")


def _bd_task_identity(task: Row, real_name: str) -> Row:
    root = _bd_norm_remote(get_setting("baidu_transfer_dir", "/资源数据"))
    save_path = _bd_join(root, real_name)
    if task["name"] != real_name or _bd_norm_remote(task["save_path"]) != save_path:
        with connect_db() as conn:
            conn.execute("UPDATE tasks SET name=?,save_path=?,last_message=? WHERE id=?",
                         (real_name, save_path, f"已识别分享文件夹原名：{real_name}", task["id"]))
            conn.commit()
    return task_row(task["id"])


def _bd_compare(source: dict[str, dict[str, Any]], dest: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for rel, src in sorted(source.items(), key=lambda kv: (kv[0].count("/"), kv[0])):
        dst = dest.get(rel)
        if src.get("dir"):
            if not dst or not dst.get("dir"):
                changes.append({"file_path": rel, "file_name": src["name"], "folder_path": parent_rel(rel),
                                "change_type": "new_folder", "size": 0, "modified_at": src.get("mtime", ""),
                                "details": "百度分享源新增目录"})
        elif not dst:
            changes.append({"file_path": rel, "file_name": src["name"], "folder_path": parent_rel(rel),
                            "change_type": "new", "size": src.get("size", 0), "modified_at": src.get("mtime", ""),
                            "details": "百度分享源新增文件"})
        elif dst.get("dir") or not BaiduDirectClient.same_file(src, dst):
            changes.append({"file_path": rel, "file_name": src["name"], "folder_path": parent_rel(rel),
                            "change_type": "updated", "size": src.get("size", 0), "modified_at": src.get("mtime", ""),
                            "details": "百度分享源文件已替换或发生变化"})
    return changes


def _bd_persist_scan(task: Row, source: dict[str, dict[str, Any]], dest: dict[str, dict[str, Any]], changes: list[dict[str, Any]]) -> str:
    stamp = now_iso()
    old = previous_snapshots(task["id"])
    removed = [path for path in old if path not in source]
    with connect_db() as conn:
        conn.execute("DELETE FROM change_items WHERE task_id=? AND status='pending'", (task["id"],))
        for item in changes:
            conn.execute("INSERT INTO change_items(task_id,folder_path,file_path,file_name,change_type,size,modified_at,status,detected_at,details) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (task["id"], item["folder_path"], item["file_path"], item["file_name"], item["change_type"],
                          int(item["size"] or 0), str(item["modified_at"] or ""), "pending", stamp, item["details"]))
        for path in removed:
            exists = conn.execute("SELECT 1 FROM change_items WHERE task_id=? AND file_path=? AND change_type='source_removed' ORDER BY id DESC LIMIT 1", (task["id"], path)).fetchone()
            if not exists:
                conn.execute("INSERT INTO change_items(task_id,folder_path,file_path,file_name,change_type,size,modified_at,status,detected_at,details) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (task["id"], parent_rel(path), path, path.rsplit("/", 1)[-1], "source_removed", 0, "", "notice", stamp, "源分享中已删除；不会删除你网盘内副本"))
        conn.execute("DELETE FROM source_snapshots WHERE task_id=?", (task["id"],))
        for path, item in source.items():
            fingerprint = hashlib.sha256(json.dumps({"id": item.get("fs_id"), "size": item.get("size"), "mtime": item.get("mtime"), "md5": item.get("md5"), "dir": item.get("dir")}, sort_keys=True).encode()).hexdigest()
            conn.execute("INSERT INTO source_snapshots(task_id,file_path,source_id,size,modified_at,fingerprint,is_dir,last_seen) VALUES(?,?,?,?,?,?,?,?)",
                         (task["id"], path, str(item.get("fs_id") or ""), int(item.get("size") or 0), str(item.get("mtime") or ""), fingerprint, 1 if item.get("dir") else 0, stamp))
        pending_files = sum(1 for x in changes if x["change_type"] != "new_folder")
        folders = {x["folder_path"] for x in changes}
        stored_files = sum(1 for x in dest.values() if not x.get("dir"))
        stored_folders = sum(1 for x in dest.values() if x.get("dir"))
        summary = f"发现 {pending_files} 个文件变化，涉及 {len(folders)} 个目录" if changes else "未发现更新"
        conn.execute("UPDATE tasks SET pending_files=?,pending_folders=?,stored_files=?,stored_folders=?,last_scan=?,last_change=?,status=?,last_message=? WHERE id=?",
                     (pending_files, len(folders), stored_files, stored_folders, stamp, summary, "发现更新" if changes else "无更新", summary, task["id"]))
        conn.commit()
    return summary


def fetch_baidu_share_name(share_url: str, passcode: str = "") -> str:
    cookies = get_setting("baidu_cookies").strip()
    if not cookies:
        return ""
    return BaiduDirectClient(cookies).share_context(share_url, passcode)["name"]


def ensure_baidu_ready(cookies: str, transfer_dir: str) -> str:
    client = BaiduDirectClient(cookies)
    account = client.validate()
    client.mkdir(_bd_norm_remote(transfer_dir or "/资源数据"))
    return f"账号：{account}；保存根目录：{_bd_norm_remote(transfer_dir or '/资源数据')}"


def scan_baidu_task(task: Row) -> str:
    cookies = get_setting("baidu_cookies").strip()
    if not cookies:
        raise RuntimeError("尚未配置百度 Cookie")
    client = BaiduDirectClient(cookies)
    ctx = client.share_context(task["share_url"], task["passcode"])
    task = _bd_task_identity(task, ctx["name"])
    source = client.source_tree(ctx)
    dest = client.destination_tree(task["save_path"])
    changes = _bd_compare(source, dest)
    summary = _bd_persist_scan(task, source, dest, changes)
    return f"百度逐文件扫描完成：源目录 {len(source)} 项；{summary}；已保存 {sum(1 for x in dest.values() if not x.get('dir'))} 个文件。"


def mark_baidu_manual_pending(task_id: int) -> str:
    task = task_row(task_id)
    if not task:
        return "任务不存在"
    return scan_baidu_task(task)


def apply_baidu_task(task: Row) -> str:
    cookies = get_setting("baidu_cookies").strip()
    if not cookies:
        raise RuntimeError("尚未配置百度 Cookie")
    with BAIDU_EXEC_LOCK:
        client = BaiduDirectClient(cookies)
        ctx = client.share_context(task["share_url"], task["passcode"])
        task = _bd_task_identity(task, ctx["name"])
        root = _bd_norm_remote(get_setting("baidu_transfer_dir", "/资源数据"))
        final_path = _bd_join(root, ctx["name"])
        client.mkdir(root)

        # 首次导入：直接把分享顶层文件夹保存进 /资源数据，不创建任何中转目录。
        if not client.path_exists(final_path):
            result = client.transfer(ctx, [str(ctx["root"].get("fs_id") or "")], root)
            deadline = time.time() + 35
            while time.time() < deadline and not client.path_exists(final_path):
                time.sleep(1.0)
            if not client.path_exists(final_path):
                raise RuntimeError("百度接口返回成功，但目标文件夹尚未出现，请稍后点击扫描；未创建任何临时目录")
            source = client.source_tree(ctx)
            dest = client.destination_tree(final_path)
            _bd_persist_scan(task, source, dest, _bd_compare(source, dest))
            stamp = now_iso()
            files = sum(1 for x in dest.values() if not x.get("dir"))
            folders = sum(1 for x in dest.values() if x.get("dir"))
            msg = f"首次转存完成：{final_path}；当前 {files} 个文件、{folders} 个子目录。"
            with connect_db() as conn:
                conn.execute("UPDATE tasks SET pending_files=0,pending_folders=0,last_update=?,last_scan=?,status='已更新',last_message=? WHERE id=?", (stamp, stamp, msg, task["id"]))
                conn.commit()
            return msg

        source = client.source_tree(ctx)
        dest = client.destination_tree(final_path)
        changes = _bd_compare(source, dest)
        if not changes:
            _bd_persist_scan(task, source, dest, [])
            stamp = now_iso()
            msg = f"没有新增内容：{final_path} 已是最新状态。"
            with connect_db() as conn:
                conn.execute("UPDATE tasks SET pending_files=0,pending_folders=0,last_update=?,status='无更新',last_message=? WHERE id=?", (stamp, msg, task["id"]))
                conn.commit()
            return msg

        new_dirs = updated = new_files = 0
        # 先建立全部新增子目录，保证文件直接进入正确父目录。
        for item in sorted((x for x in changes if x["change_type"] == "new_folder"), key=lambda x: x["file_path"].count("/")):
            client.mkdir(_bd_join(final_path, item["file_path"]))
            new_dirs += 1

        # 文件按父目录批量转存；变化文件先删除旧版本再保存新版本。
        by_parent: dict[str, list[dict[str, Any]]] = {}
        change_map = {x["file_path"]: x for x in changes}
        for rel, src in source.items():
            change = change_map.get(rel)
            if not change or src.get("dir"):
                continue
            by_parent.setdefault(parent_rel(rel), []).append(src)
            if change["change_type"] == "updated":
                old_path = _bd_join(final_path, rel)
                client.delete_path(old_path)
                updated += 1
            else:
                new_files += 1

        for rel_parent, items in sorted(by_parent.items()):
            target = _bd_join(final_path, rel_parent) if rel_parent else final_path
            client.mkdir(target)
            for i in range(0, len(items), 50):
                ids = [str(x.get("fs_id") or "") for x in items[i:i+50] if x.get("fs_id")]
                client.transfer(ctx, ids, target)

        # 等待目录列表一致，然后重新扫描确认。
        time.sleep(1.5)
        source2 = client.source_tree(ctx)
        dest2 = client.destination_tree(final_path)
        remaining = _bd_compare(source2, dest2)
        _bd_persist_scan(task, source2, dest2, remaining)
        stamp = now_iso()
        msg = f"更新完成：{final_path}；新增文件 {new_files}，替换更新 {updated}，新增目录 {new_dirs}。"
        if remaining:
            msg += f" 仍有 {len(remaining)} 项等待百度目录刷新，可稍后再次扫描。"
        with connect_db() as conn:
            conn.execute("UPDATE tasks SET last_update=?,status=?,last_message=? WHERE id=?", (stamp, "已更新" if not remaining else "发现更新", msg, task["id"]))
            conn.commit()
        return msg

def update_schedule(task_id: int) -> None:
    task = task_row(task_id)
    if not task:
        return
    next_time = datetime.now() + timedelta(minutes=max(30, int(task["interval_minutes"])))
    with connect_db() as conn:
        conn.execute("UPDATE tasks SET last_run=?,next_run=? WHERE id=?", (now_iso(), next_time.replace(microsecond=0).isoformat(sep=" "), task_id))
        conn.commit()


def set_task_runtime(task_id: int, status: str, message: str | None = None) -> None:
    with connect_db() as conn:
        if message is None:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        else:
            conn.execute("UPDATE tasks SET status=?,last_message=? WHERE id=?", (status, message[-3000:], task_id))
        conn.commit()


def run_operation(task_id: int, mode: str, folder_path: str | None = None, scheduled: bool = False) -> None:
    try:
        task = task_row(task_id)
        if not task:
            return
        if mode == "scan":
            set_task_runtime(task_id, "扫描中")
            add_log(task_id, "INFO", "开始扫描每个文件是否发生变化")
            if task["platform"] == "quark":
                message = scan_quark_task(task)
            else:
                message = mark_baidu_manual_pending(task_id)
            add_log(task_id, "INFO", message)
            # 定时模式下，自动更新开关开启时继续应用变化。
            refreshed = task_row(task_id)
            if scheduled and refreshed and refreshed["auto_update"]:
                if refreshed["platform"] == "quark" and (refreshed["pending_files"] > 0 or refreshed["pending_folders"] > 0):
                    set_task_runtime(task_id, "更新中")
                    applied = apply_quark_task(refreshed)
                    add_log(task_id, "INFO", applied)
                    set_task_runtime(task_id, "已更新", applied)
                elif refreshed["platform"] == "baidu":
                    set_task_runtime(task_id, "更新中")
                    applied = apply_baidu_task(refreshed)
                    add_log(task_id, "INFO", applied)
                    set_task_runtime(task_id, "已更新", applied)
        elif mode == "update":
            set_task_runtime(task_id, "更新中")
            add_log(task_id, "INFO", f"开始更新{'指定目录：' + normalize_rel(folder_path) if folder_path is not None else '全部变化'}")
            if task["platform"] == "quark":
                message = apply_quark_task(task, folder_path)
            else:
                message = apply_baidu_task(task)
            set_task_runtime(task_id, "已更新", message)
            add_log(task_id, "INFO", message)
        update_schedule(task_id)
    except Exception as exc:
        technical = f"{type(exc).__name__}: {exc}"
        message = friendly_error_message(exc)
        set_task_runtime(task_id, "失败", message)
        update_schedule(task_id)
        add_log(task_id, "ERROR", f"{message}｜技术详情：{technical}")
    finally:
        with LOCK:
            RUNNING_TASKS.discard(task_id)


def start_task(task_id: int, mode: str, folder_path: str | None = None, scheduled: bool = False) -> bool:
    with LOCK:
        if task_id in RUNNING_TASKS:
            return False
        RUNNING_TASKS.add(task_id)
    threading.Thread(target=run_operation, args=(task_id, mode, folder_path, scheduled), daemon=True).start()
    return True


def run_batch(mode: str) -> None:
    key = "scan" if mode == "scan" else "update"
    with LOCK:
        if BATCH_STATE[key]:
            return
        BATCH_STATE[key] = True
        BATCH_STATE["message"] = "正在扫描全部任务" if mode == "scan" else "正在更新全部有变化任务"
    try:
        with connect_db() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE enabled=1 ORDER BY id").fetchall()
        total = len(rows)
        for index, row in enumerate(rows, 1):
            BATCH_STATE["message"] = f"{index}/{total}：处理任务 {row['id']}"
            while not start_task(row["id"], mode):
                time.sleep(1)
            while row["id"] in RUNNING_TASKS:
                time.sleep(1)
        BATCH_STATE["message"] = f"全部任务处理完成，共 {total} 个"
        add_log(None, "INFO", BATCH_STATE["message"])
    except Exception:
        BATCH_STATE["message"] = "批量任务失败"
        add_log(None, "ERROR", traceback.format_exc())
    finally:
        BATCH_STATE[key] = False



def enqueue_import_tasks(task_ids: list[int]) -> None:
    if not task_ids:
        return
    with LOCK:
        if not IMPORT_STATE["running"] and IMPORT_QUEUE.empty() and IMPORT_STATE["active"] == 0:
            IMPORT_STATE.update({"completed": 0, "failed": 0})
        IMPORT_STATE["running"] = True
        IMPORT_STATE["queued"] += len(task_ids)
        IMPORT_STATE["message"] = f"首次转存队列：等待 {IMPORT_STATE['queued']}，处理中 {IMPORT_STATE['active']}"
    for task_id in task_ids:
        IMPORT_QUEUE.put(task_id)


def import_worker_loop(worker_no: int) -> None:
    while True:
        task_id = IMPORT_QUEUE.get()
        with LOCK:
            IMPORT_STATE["queued"] = max(0, IMPORT_STATE["queued"] - 1)
            IMPORT_STATE["active"] += 1
            IMPORT_STATE["running"] = True
            IMPORT_STATE["message"] = f"首次转存队列：等待 {IMPORT_STATE['queued']}，处理中 {IMPORT_STATE['active']}，已完成 {IMPORT_STATE['completed']}"
            RUNNING_TASKS.add(task_id)
        try:
            set_task_runtime(task_id, "首次转存中", f"已进入并发队列，由工作线程 {worker_no} 处理")
            run_operation(task_id, "update")
            row = task_row(task_id)
            failed = bool(row and row["status"] == "失败")
            with LOCK:
                IMPORT_STATE["failed" if failed else "completed"] += 1
        except Exception as exc:
            message = friendly_error_message(exc)
            set_task_runtime(task_id, "失败", message)
            add_log(task_id, "ERROR", f"{message}｜技术详情：{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            with LOCK:
                IMPORT_STATE["failed"] += 1
        finally:
            with LOCK:
                IMPORT_STATE["active"] = max(0, IMPORT_STATE["active"] - 1)
                IMPORT_STATE["running"] = IMPORT_STATE["queued"] > 0 or IMPORT_STATE["active"] > 0
                if IMPORT_STATE["running"]:
                    IMPORT_STATE["message"] = f"首次转存队列：等待 {IMPORT_STATE['queued']}，处理中 {IMPORT_STATE['active']}，已完成 {IMPORT_STATE['completed']}，失败 {IMPORT_STATE['failed']}"
                else:
                    IMPORT_STATE["message"] = f"首次转存完成：成功 {IMPORT_STATE['completed']}，失败 {IMPORT_STATE['failed']}"
            IMPORT_QUEUE.task_done()


def trigger_due_tasks() -> list[int]:
    now = datetime.now()
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE enabled=1").fetchall()
    started: list[int] = []
    for row in rows:
        due = not row["next_run"]
        if row["next_run"]:
            try:
                due = datetime.fromisoformat(row["next_run"]) <= now
            except ValueError:
                due = True
        if due and start_task(row["id"], "scan", scheduled=True):
            started.append(int(row["id"]))
    return started


def cron_secret_ok(provided: str) -> bool:
    expected = os.getenv("CRON_SECRET", "").strip()
    return bool(expected and provided and hmac.compare_digest(provided, expected))


def scheduler_loop() -> None:
    while True:
        try:
            trigger_due_tasks()
        except Exception:
            add_log(None, "ERROR", traceback.format_exc())
        time.sleep(30)


def seed_settings_from_env() -> None:
    """首次部署时可从 Render Secret 注入 Cookie；之后以持久化数据库中的设置为准。"""
    mapping = {
        "QUARK_COOKIE": "quark_cookie",
        "BAIDU_COOKIES": "baidu_cookies",
        "BAIDU_TRANSFER_DIR": "baidu_transfer_dir",
    }
    for env_key, setting_key in mapping.items():
        value = os.getenv(env_key, "").strip()
        if value and not get_setting(setting_key).strip():
            set_setting(setting_key, value)


class Handler(BaseHTTPRequestHandler):
    server_version = "NetdiskSync/3.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def auth_required(self) -> bool:
        user = os.getenv("ADMIN_USER", "").strip()
        password = os.getenv("ADMIN_PASSWORD", "")
        # Render 公网部署必须配置管理员账号密码；未配置时拒绝访问，避免 Cookie 与任务被他人操作。
        if not user or not password:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            body = "服务尚未配置 ADMIN_USER 和 ADMIN_PASSWORD，请在 Render 环境变量中设置。".encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        header = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        if hmac.compare_digest(header, expected):
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Netdisk Sync"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = "需要管理员账号密码。".encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def send_json(self, obj: Any, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))

    def serve_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/healthz":
            return self.send_json({"ok": True, "service": "netdisk-sync", "database": "neon" if DATABASE_URL else "sqlite"})
        if path in ("/api/cron/hourly", "/api/cron/status"):
            supplied = (qs.get("secret") or [""])[0]
            if not cron_secret_ok(supplied):
                return self.send_json({"success": False, "message": "定时任务密钥错误"}, 403)
            if path == "/api/cron/hourly":
                started = trigger_due_tasks()
                add_log(None, "INFO", f"GitHub Actions 每小时触发：启动 {len(started)} 个到期任务")
                return self.send_json({"success": True, "started": started, "count": len(started)})
            with LOCK:
                running = sorted(RUNNING_TASKS)
                import_active = int(IMPORT_STATE.get("active", 0))
                import_queued = int(IMPORT_STATE.get("queued", 0))
            return self.send_json({"success": True, "running": running, "active": len(running) + import_active + import_queued})
        if self.auth_required():
            return
        if path in ("/", "/index.html"):
            return self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/status":
            with connect_db() as conn:
                counts = {r["platform"]: r["c"] for r in conn.execute("SELECT platform,COUNT(*) c FROM tasks GROUP BY platform")}
                failed = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='失败'").fetchone()[0]
                pending_files = conn.execute("SELECT COALESCE(SUM(pending_files),0) FROM tasks").fetchone()[0]
                pending_folders = conn.execute("SELECT COUNT(DISTINCT task_id || ':' || folder_path) FROM change_items WHERE status='pending'").fetchone()[0]
            qconf = bool(get_setting("quark_cookie"))
            bconf = bool(get_setting("baidu_cookies"))
            return self.send_json({"success": True, "data": {"quark": {"ok": qconf, "detail": "逐文件精确监控" if qconf else "请配置 Cookie"}, "baidu": {"ok": bconf, "detail": "内置直连增量引擎" if bconf else "请配置 Cookie"}, "docker": {"ok": True, "detail": "完全免 Docker"}, "counts": {"quark": counts.get("quark", 0), "baidu": counts.get("baidu", 0), "failed": failed, "pending_files": pending_files, "pending_folders": pending_folders}, "batch": dict(BATCH_STATE), "import_queue": dict(IMPORT_STATE)}})
        if path == "/api/tasks":
            with connect_db() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id DESC")]
            for row in rows:
                row["running"] = row["id"] in RUNNING_TASKS
            return self.send_json({"success": True, "data": rows})
        if path == "/api/change-groups":
            with connect_db() as conn:
                rows = [dict(r) for r in conn.execute(
                    """SELECT c.task_id,t.name,t.platform,c.folder_path,COUNT(*) item_count,
                    SUM(CASE WHEN c.change_type='new' THEN 1 ELSE 0 END) new_count,
                    SUM(CASE WHEN c.change_type='updated' THEN 1 ELSE 0 END) updated_count,
                    MIN(c.detected_at) detected_at
                    FROM change_items c JOIN tasks t ON t.id=c.task_id
                    WHERE c.status='pending' GROUP BY c.task_id,c.folder_path ORDER BY detected_at DESC"""
                )]
            return self.send_json({"success": True, "data": rows})
        if path == "/api/changes":
            task_id = int((qs.get("task_id") or ["0"])[0] or 0)
            status = (qs.get("status") or [""])[0]
            sql = "SELECT c.*,t.name task_name,t.platform FROM change_items c JOIN tasks t ON t.id=c.task_id WHERE 1=1"
            args: list[Any] = []
            if task_id:
                sql += " AND c.task_id=?"
                args.append(task_id)
            if status:
                sql += " AND c.status=?"
                args.append(status)
            sql += " ORDER BY c.id DESC LIMIT 300"
            with connect_db() as conn:
                rows = [dict(r) for r in conn.execute(sql, args)]
            return self.send_json({"success": True, "data": rows})
        if path == "/api/logs":
            with connect_db() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 150")]
            return self.send_json({"success": True, "data": rows})
        if path == "/api/settings":
            return self.send_json({"success": True, "data": {
                "quark_cookie_configured": bool(get_setting("quark_cookie")),
                "baidu_cookie_configured": bool(get_setting("baidu_cookies")),
                "quark_cookie_preview": cookie_preview(get_setting("quark_cookie")),
                "baidu_cookie_preview": cookie_preview(get_setting("baidu_cookies")),
                "quark_cookie_verified_at": get_setting("quark_cookie_verified_at"),
                "baidu_cookie_verified_at": get_setting("baidu_cookie_verified_at"),
                "baidu_transfer_dir": get_setting("baidu_transfer_dir", "/资源数据"),
                "cookie_storage": "Neon PostgreSQL（加密保存）" if DATABASE_URL else str(DB_PATH),
                "baidu_binary_installed": True
            }})
        return self.send_json({"success": False, "message": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if self.auth_required():
            return
        try:
            if path == "/api/import":
                p = self.read_json()
                items = extract_links(str(p.get("text", "")))
                root = str(p.get("save_root", "/资源数据")).strip() or "/资源数据"
                interval = max(60, int(p.get("interval_minutes", 360)))
                auto_update = 1 if p.get("auto_update", True) else 0
                added = duplicates = 0
                added_ids: list[int] = []
                # 快速入库：这里不访问百度或夸克网络，通常 1 秒内返回。
                # 真实名称识别与首次转存由后台 3 路队列完成，避免一条链接阻塞整批导入。
                with connect_db() as conn:
                    for item in items:
                        save_path = root.rstrip("/") if item["platform"] == "baidu" else root.rstrip("/") + "/" + item["name"]
                        try:
                            cur = conn.execute(
                                "INSERT INTO tasks(platform,name,share_url,passcode,save_path,interval_minutes,enabled,auto_update,status,next_run,created_at,monitor_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (item["platform"], item["name"], item["url"], item["passcode"], save_path, interval, 1, auto_update, "排队中", (datetime.now() + timedelta(minutes=interval)).replace(microsecond=0).isoformat(sep=" "), now_iso(), "precise" if item["platform"] == "quark" else "task"),
                            )
                            added_ids.append(int(cur.lastrowid))
                            added += 1
                        except IntegrityError:
                            duplicates += 1
                    conn.commit()
                enqueue_import_tasks(added_ids)
                return self.send_json({
                    "success": True,
                    "data": {"recognized": len(items), "added": added, "duplicates": duplicates, "queued": len(added_ids), "workers": IMPORT_WORKERS},
                    "message": "链接已快速加入，首次转存正在后台并发执行"
                })
            if path == "/api/tasks/scan-all":
                if BATCH_STATE["scan"]:
                    return self.send_json({"success": False, "message": "全部扫描正在运行"}, 409)
                threading.Thread(target=run_batch, args=("scan",), daemon=True).start()
                return self.send_json({"success": True, "message": "已开始扫描全部任务"})
            if path == "/api/tasks/update-all":
                if BATCH_STATE["update"]:
                    return self.send_json({"success": False, "message": "全部更新正在运行"}, 409)
                threading.Thread(target=run_batch, args=("update",), daemon=True).start()
                return self.send_json({"success": True, "message": "已开始更新全部有变化任务"})
            m = re.fullmatch(r"/api/tasks/(\d+)/(scan|update|run)", path)
            if m:
                task_id = int(m.group(1))
                mode = "scan" if m.group(2) == "scan" else "update"
                if not start_task(task_id, mode):
                    return self.send_json({"success": False, "message": "该任务正在运行"}, 409)
                return self.send_json({"success": True, "message": "任务已开始"})
            m = re.fullmatch(r"/api/tasks/(\d+)/folder-update", path)
            if m:
                p = self.read_json()
                folder = normalize_rel(str(p.get("folder_path", "")))
                if not start_task(int(m.group(1)), "update", folder):
                    return self.send_json({"success": False, "message": "该任务正在运行"}, 409)
                return self.send_json({"success": True, "message": "该目录更新已开始"})
            m = re.fullmatch(r"/api/tasks/(\d+)/resolve-name", path)
            if m:
                task = task_row(int(m.group(1)))
                if task["platform"] != "baidu":
                    return self.send_json({"success": False, "message": "该功能仅用于百度任务"}, 400)
                name = fetch_baidu_share_name(task["share_url"], task["passcode"])
                if not name:
                    return self.send_json({"success": False, "message": "暂时未能从百度链接读取原名，请点击‘修正名称’手动填写链接中显示的文件夹名。"}, 400)
                task = set_task_source_name(task["id"], name, f"重新识别成功：{name}")
                return self.send_json({"success": True, "message": f"已修正为 {task['save_path']}"})
            m = re.fullmatch(r"/api/tasks/(\d+)/rename", path)
            if m:
                p = self.read_json()
                name = str(p.get("name", "")).strip()
                task = set_task_source_name(int(m.group(1)), name, f"已手动按分享链接原名修正为：{name}")
                return self.send_json({"success": True, "message": f"已修正为 {task['save_path']}"})
            m = re.fullmatch(r"/api/tasks/(\d+)/toggle-auto", path)
            if m:
                p = self.read_json()
                value = 1 if p.get("auto_update") else 0
                with connect_db() as conn:
                    conn.execute("UPDATE tasks SET auto_update=? WHERE id=?", (value, int(m.group(1))))
                    conn.commit()
                return self.send_json({"success": True, "message": "自动更新设置已保存"})
            m = re.fullmatch(r"/api/tasks/(\d+)/toggle-enabled", path)
            if m:
                p = self.read_json()
                value = 1 if p.get("enabled") else 0
                with connect_db() as conn:
                    conn.execute("UPDATE tasks SET enabled=? WHERE id=?", (value, int(m.group(1))))
                    conn.commit()
                return self.send_json({"success": True, "message": "任务状态已保存"})
            if path == "/api/settings/quark":
                p = self.read_json()
                cookie = str(p.get("cookie", "")).strip() or get_setting("quark_cookie")
                account = QuarkClient(cookie).account()
                set_setting("quark_cookie", cookie)
                return self.send_json({"success": True, "message": f"夸克账号验证成功：{account.get('nickname') or '已登录'}"})
            if path == "/api/settings/baidu":
                p = self.read_json()
                cookies = str(p.get("cookies", "")).strip() or get_setting("baidu_cookies")
                transfer_dir = str(p.get("transfer_dir", "/资源数据")).strip() or "/自动追更"
                msg = ensure_baidu_ready(cookies, transfer_dir)
                set_setting("baidu_cookies", cookies)
                set_setting("baidu_transfer_dir", transfer_dir)
                return self.send_json({"success": True, "message": "百度账号验证成功，内置直连引擎可用。" + ("\n" + msg[-300:] if msg else "")})
            if path == "/api/logs/clear":
                with connect_db() as conn:
                    conn.execute("DELETE FROM logs")
                    conn.commit()
                return self.send_json({"success": True, "message": "日志已清空"})
            return self.send_json({"success": False, "message": "Not found"}, 404)
        except Exception as exc:
            add_log(None, "ERROR", traceback.format_exc())
            return self.send_json({"success": False, "message": str(exc)}, 400)

    def do_DELETE(self) -> None:
        if self.auth_required():
            return
        m = re.fullmatch(r"/api/tasks/(\d+)", urllib.parse.urlsplit(self.path).path)
        if not m:
            return self.send_json({"success": False, "message": "Not found"}, 404)
        task_id = int(m.group(1))
        with connect_db() as conn:
            conn.execute("DELETE FROM change_items WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM source_snapshots WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()
        self.send_json({"success": True})


def main() -> None:
    init_db()
    seed_settings_from_env()
    for worker_no in range(1, IMPORT_WORKERS + 1):
        threading.Thread(target=import_worker_loop, args=(worker_no,), daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"网盘自动追更控制台运行在 http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
