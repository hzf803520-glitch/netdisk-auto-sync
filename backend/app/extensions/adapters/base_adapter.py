# -*- coding: utf-8 -*-
"""
云盘适配器基类
定义所有网盘适配器必须实现的接口
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Optional, Any
import logging
import random
import re
import threading
import time


logger = logging.getLogger(__name__)


class BaseCloudDriveAdapter(ABC):
    """云盘适配器抽象基类"""

    # 网盘类型标识
    DRIVE_TYPE = "base"
    DRIVE_NAME = "基础网盘"
    CONFIG_FORMAT = "raw"
    default_config: dict[str, Any] = {"cookie": ""}
    config_fields: list[dict[str, Any]] = [
        {
            "key": "cookie",
            "label": "Cookie",
            "description": "登录态原文，按驱动要求填写。",
            "input_type": "textarea",
            "required": True,
            "secret": True,
            "placeholder": "",
        }
    ]

    def __init__(
        self,
        cookie: str = "",
        index: int = 0,
        config: dict[str, Any] | None = None,
        *,
        no_login: bool = False,
    ):
        self.config = self.resolve_runtime_config(config=config, cookie=cookie)
        self.cookie = self.serialize_config(self.config)
        self.index = index + 1
        self.is_active = False
        self.nickname = ""
        self.no_login = bool(no_login)

        self._rate_limit_min_interval = 0.05
        self._rate_limit_max_interval = 0.10
        self._rate_limit_lock = threading.Lock()
        self._last_request_at = 0.0

        self.savepath_fid: Dict[str, str] = {"/": "0"}

    def _throttle_request(self) -> None:
        with self._rate_limit_lock:
            now = time.monotonic()
            if self._last_request_at > 0:
                interval = random.uniform(
                    self._rate_limit_min_interval, self._rate_limit_max_interval
                )
                elapsed = now - self._last_request_at
                if elapsed < interval:
                    time.sleep(interval - elapsed)
                    now = time.monotonic()
            self._last_request_at = now

    @classmethod
    def get_config_meta(cls) -> dict[str, Any]:
        return {
            "drive_name": getattr(cls, "DRIVE_NAME", getattr(cls, "DRIVE_TYPE", "base")),
            "config_format": getattr(cls, "CONFIG_FORMAT", "raw"),
            "default_config": dict(getattr(cls, "default_config", {}) or {}),
            "config_fields": list(getattr(cls, "config_fields", []) or []),
        }

    @classmethod
    def normalize_config(cls, config: dict[str, Any] | None) -> dict[str, Any]:
        result = dict(getattr(cls, "default_config", {}) or {})
        for key, value in (config or {}).items():
            result[key] = value
        return result

    @classmethod
    def deserialize_cookie(cls, cookie: str | None) -> dict[str, Any]:
        config = cls.normalize_config(None)
        raw_cookie = str(cookie or "").strip()
        if not raw_cookie:
            return config
        if getattr(cls, "CONFIG_FORMAT", "raw") == "kv":
            parsed: dict[str, Any] = {}
            for chunk in raw_cookie.split(";"):
                chunk = chunk.strip()
                if not chunk or "=" not in chunk:
                    continue
                key, value = chunk.split("=", 1)
                parsed[key.strip()] = value.strip()
            for key, default_value in config.items():
                if key not in parsed:
                    continue
                if isinstance(default_value, bool):
                    config[key] = str(parsed[key]).strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(default_value, int) and not isinstance(default_value, bool):
                    try:
                        config[key] = int(parsed[key])
                    except ValueError:
                        config[key] = parsed[key]
                else:
                    config[key] = parsed[key]
            for key, value in parsed.items():
                if key not in config:
                    config[key] = value
            return config
        config[cls.primary_config_key()] = raw_cookie
        return config

    @classmethod
    def resolve_runtime_config(
        cls,
        *,
        config: dict[str, Any] | None = None,
        cookie: str | None = None,
    ) -> dict[str, Any]:
        if config is not None:
            return cls.normalize_config(config)
        return cls.deserialize_cookie(cookie)

    @classmethod
    def serialize_config(cls, config: dict[str, Any] | None) -> str:
        payload = cls.normalize_config(config)
        if getattr(cls, "CONFIG_FORMAT", "raw") == "kv":
            parts: list[str] = []
            for key, value in payload.items():
                if not cls.keep_value(value):
                    continue
                parts.append(f"{key}={str(value).strip() if not isinstance(value, bool) else str(value)}")
            return ";".join(parts)
        return str(payload.get(cls.primary_config_key(), "") or "").strip()

    @classmethod
    def primary_config_key(cls) -> str:
        fields = getattr(cls, "config_fields", []) or []
        if fields and fields[0].get("key"):
            return str(fields[0]["key"])
        defaults = getattr(cls, "default_config", {}) or {}
        if defaults:
            return next(iter(defaults.keys()))
        return "cookie"

    @staticmethod
    def keep_value(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return True
        return str(value or "").strip() != ""

    @abstractmethod
    def init(self) -> Any:
        """
        初始化账户，验证 cookie 有效性
        Returns:
            成功返回账户信息 dict，失败返回 False
        """
        pass

    @abstractmethod
    def get_stoken(self, pwd_id: str, passcode: str = "") -> Dict:
        """
        获取分享令牌，验证资源有效性
        Args:
            pwd_id: 分享ID
            passcode: 提取码
        Returns:
            响应字典，包含 status, data, message 等字段
        """
        pass

    @abstractmethod
    def get_detail(
        self,
        pwd_id: str,
        stoken: str,
        pdir_fid: str,
        _fetch_share: int = 0,
        fetch_share_full_path: int = 0,
    ) -> Dict:
        """
        获取分享文件详情列表
        Args:
            pwd_id: 分享ID
            stoken: 分享令牌
            pdir_fid: 父目录ID
            _fetch_share: 是否获取分享信息
            fetch_share_full_path: 是否获取完整路径
        Returns:
            响应字典，包含 code, data.list 等字段
        """
        pass

    @abstractmethod
    def ls_dir(self, pdir_fid: str, max_items: int = 0, **kwargs) -> Dict:
        """
        列出目录内容
        Args:
            pdir_fid: 目录ID
            max_items: 最大返回条目数，0 表示不限制（全量加载）
        Returns:
            响应字典，包含 code, data.list 等字段
        """
        pass

    @abstractmethod
    def save_file(
        self,
        fid_list: List[str],
        fid_token_list: List[str],
        to_pdir_fid: str,
        pwd_id: str,
        stoken: str,
    ) -> Dict:
        """
        转存文件到指定目录
        Args:
            fid_list: 文件ID列表
            fid_token_list: 文件token列表
            to_pdir_fid: 目标目录ID
            pwd_id: 分享ID
            stoken: 分享令牌
        Returns:
            响应字典，包含 code, data.task_id 等字段
        """
        pass

    @abstractmethod
    def query_task(self, task_id: str) -> Dict:
        """
        查询转存任务状态
        Args:
            task_id: 任务ID
        Returns:
            响应字典，包含任务状态信息
        """
        pass

    @abstractmethod
    def mkdir(self, dir_path: str) -> Dict:
        """
        创建目录
        Args:
            dir_path: 目录路径
        Returns:
            响应字典，包含 code, data.fid 等字段
        """
        pass

    @abstractmethod
    def rename(self, fid: str, file_name: str) -> Dict:
        """
        重命名文件
        Args:
            fid: 文件ID
            file_name: 新文件名
        Returns:
            响应字典
        """
        pass

    @abstractmethod
    def delete(self, filelist: List[str]) -> Dict:
        """
        删除文件
        Args:
            filelist: 文件ID列表
        Returns:
            响应字典
        """
        pass

    @abstractmethod
    def get_fids(self, file_paths: List[str]) -> List[Dict]:
        """
        根据路径获取文件ID
        Args:
            file_paths: 文件路径列表
        Returns:
            包含 file_path 和 fid 的字典列表
        """
        pass

    @abstractmethod
    def extract_url(self, url: str) -> Tuple[Optional[str], str, Any, List]:
        """
        解析分享链接
        Args:
            url: 分享链接
        Returns:
            (pwd_id, passcode, pdir_fid, paths) 元组
        """
        pass

    def unarchive(self, fid: str, to_pdir_fid: str) -> Dict:
        """
        云解压文件（可选实现）
        Args:
            fid: 压缩包文件ID
            to_pdir_fid: 解压到的目录ID
        Returns:
            响应字典，包含 code, data.task_id 等字段
        """
        return {"code": 0, "message": "success", "data": {"task_id": ""}}

    def move_files(self, fids: List[str], to_pdir_fid: str) -> Dict:
        """
        批量移动文件（可选实现）
        Args:
            fids: 文件ID列表
            to_pdir_fid: 目标目录ID
        Returns:
            响应字典
        """
        return {"code": 0, "message": "success"}

    def get_account_info(self) -> Any:
        """获取账户信息（可选实现）"""
        return False

    def get_account_config(self) -> Dict[str, Any]:
        """获取账号配置/概览信息（用于管理页展示）"""
        return {
            "drive_type": self.DRIVE_TYPE,
            "drive_name": self.DRIVE_NAME,
            "nickname": self.nickname or "",
            "username": "",
            "used_space": None,
            "total_space": None,
            "raw": None,
        }

    def sign_in(self) -> Dict[str, Any]:
        return {"supported": False, "message": "not supported"}

    def export_runtime_config(self) -> dict[str, Any]:
        return self.deserialize_cookie(self.cookie)

    def update_savepath_fid(self, tasklist: List[Dict]) -> bool:
        """
        更新保存路径的 fid 映射
        Args:
            tasklist: 任务列表
        Returns:
            是否成功
        """
        # 通用实现，子类可重写
        import re
        from datetime import datetime

        dir_paths = [
            re.sub(r"/{2,}", "/", f"/{item['savepath']}")
            for item in tasklist
            if not item.get("enddate")
            or (
                datetime.now().date()
                <= datetime.strptime(item["enddate"], "%Y-%m-%d").date()
            )
        ]
        if not dir_paths:
            return False

        dir_paths_exist_arr = self.get_fids(dir_paths)
        dir_paths_exist = [item["file_path"] for item in dir_paths_exist_arr]

        # 创建不存在的目录
        dir_paths_unexist = list(set(dir_paths) - set(dir_paths_exist) - set(["/"]))
        for dir_path in dir_paths_unexist:
            mkdir_return = self.mkdir(dir_path)
            if mkdir_return.get("code") == 0:
                new_dir = mkdir_return["data"]
                dir_paths_exist_arr.append(
                    {"file_path": dir_path, "fid": new_dir["fid"]}
                )
                logger.info("创建文件夹：%s", dir_path)
            else:
                logger.warning("创建文件夹：%s 失败, %s", dir_path, mkdir_return.get("message", "未知错误"))

        # 储存目标目录的fid
        for dir_path in dir_paths_exist_arr:
            self.savepath_fid[dir_path["file_path"]] = dir_path["fid"]

        return True

    @staticmethod
    def _extract_dir_item_fid(item: dict[str, Any]) -> str:
        if not isinstance(item, dict):
            return ""
        for key in ("fid", "file_id", "id", "fileId", "fs_id"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _extract_dir_item_name(item: dict[str, Any]) -> str:
        if not isinstance(item, dict):
            return ""
        for key in ("file_name", "name", "server_filename", "fileName", "title"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _normalize_saved_name(name: str) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        return re.sub(r"[^\w\s\.]", "", text)

    def snapshot_dest_dir_items(self, pdir_fid: str, *, max_items: int = 1000) -> dict[str, str]:
        try:
            payload = self.ls_dir(pdir_fid, max_items=max_items) or {}
        except Exception:
            return {}
        if payload.get("code") not in (0, "0", None):
            return {}
        items = ((payload.get("data") or {}).get("list") or [])
        result: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            fid = self._extract_dir_item_fid(item)
            if not fid:
                continue
            result[fid] = self._extract_dir_item_name(item)
        return result

    def align_saved_fids_from_dir(
        self,
        pdir_fid: str,
        file_names: List[str] | None,
        *,
        before_items: dict[str, str] | None = None,
        max_items: int = 1000,
        timeout_seconds: float = 0.0,
        interval_seconds: float = 1.0,
        accept_partial_best: bool = True,
    ) -> list[str]:
        ordered_names = [str(name or "") for name in (file_names or [])]
        if not ordered_names:
            return []

        before_fids = set((before_items or {}).keys())
        best_aligned: list[str] = []
        best_matched = -1
        deadline = time.time() + max(0.0, float(timeout_seconds or 0.0))

        while True:
            current_items = self.snapshot_dest_dir_items(pdir_fid, max_items=max_items)
            if current_items:
                name_to_fids: dict[str, list[str]] = {}
                normalized_name_to_fids: dict[str, list[str]] = {}
                for fid, name in current_items.items():
                    if not fid or fid in before_fids:
                        continue
                    raw_name = str(name or "").strip()
                    if not raw_name:
                        continue
                    name_to_fids.setdefault(raw_name, []).append(fid)
                    normalized = self._normalize_saved_name(raw_name)
                    if normalized:
                        normalized_name_to_fids.setdefault(normalized, []).append(fid)

                aligned: list[str] = []
                used: set[str] = set()
                for file_name in ordered_names:
                    matched_fid = ""
                    raw_name = str(file_name or "").strip()
                    for candidate in name_to_fids.get(raw_name, []):
                        if candidate and candidate not in used:
                            matched_fid = candidate
                            break
                    if not matched_fid:
                        normalized = self._normalize_saved_name(raw_name)
                        for candidate in normalized_name_to_fids.get(normalized, []):
                            if candidate and candidate not in used:
                                matched_fid = candidate
                                break
                    if matched_fid:
                        used.add(matched_fid)
                    aligned.append(matched_fid)

                matched = sum(1 for fid in aligned if fid)
                if matched > best_matched:
                    best_aligned = aligned
                    best_matched = matched
                if matched >= len(ordered_names):
                    return aligned

            if time.time() >= deadline:
                break
            time.sleep(max(0.1, float(interval_seconds or 0.0)))

        if accept_partial_best:
            if best_aligned:
                return best_aligned
            return ["" for _ in ordered_names]
        return []
