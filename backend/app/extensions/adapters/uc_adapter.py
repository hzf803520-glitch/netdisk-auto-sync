# -*- coding: utf-8 -*-
"""
UC网盘适配器
基于夸克网盘适配器模板（同属阿里系，API结构类似）
"""
import hashlib
import re
import time
import random
import logging
import requests
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime

from app.extensions.adapters.base_adapter import BaseCloudDriveAdapter


logger = logging.getLogger(__name__)


class UCAdapter(BaseCloudDriveAdapter):
    """UC网盘适配器"""

    DRIVE_TYPE = "uc"
    DRIVE_NAME = "UC 网盘"
    CONFIG_FORMAT = "raw"
    default_config = {
        "cookie": "",
        "refresh_token": "",
        "device_id": "",
        "query_token": "",
        "lsdir_cache_path": "",
        "static_lsdir_cache_path": "",
        "strm_scan_path": "",
    }
    config_fields = [
        {
            "key": "cookie",
            "label": "Cookie",
            "description": "UC 网盘登录 Cookie 原文。",
            "input_type": "textarea",
            "required": True,
            "secret": True,
            "placeholder": "service_ticket=...; __uid=...",
        },
        {
            "key": "refresh_token",
            "label": "TV 刷新令牌",
            "description": "UC TV 端 refresh_token；扫码成功后会自动回写，也可手动填写保存。",
            "input_type": "textarea",
            "required": False,
            "secret": True,
            "placeholder": "refresh_token",
        },
        {
            "key": "device_id",
            "label": "TV 设备 ID",
            "description": "UC TV 登录签名所需 device_id；为空时发起扫码会自动生成。",
            "input_type": "text",
            "required": False,
            "secret": False,
            "placeholder": "自动生成或手动填写",
        },
        {
            "key": "query_token",
            "label": "TV 查询令牌",
            "description": "UC TV 扫码轮询使用的 query_token；支持手动保存以续接未完成登录。",
            "input_type": "textarea",
            "required": False,
            "secret": True,
            "placeholder": "query_token",
        },
        {
            "key": "lsdir_cache_path",
            "label": "缓存路径",
            "description": "lsdir 缓存刷新与网盘同步默认范围使用的根目录（网盘内路径）。",
            "input_type": "text",
            "required": True,
            "secret": False,
            "placeholder": "/",
        },
        {
            "key": "static_lsdir_cache_path",
            "label": "静态缓存路径",
            "description": "只做一次首扫并生成 lsdir 缓存的静态目录，适合完结类资源；仅在首次成功扫描或修改此配置后重扫，若位于缓存路径子树内会被普通扫描自动忽略。",
            "input_type": "text",
            "required": False,
            "secret": False,
            "placeholder": "/电影库",
        },
        {
            "key": "strm_scan_path",
            "label": "STRM 扫描路径",
            "description": "STRM/CAS 使用的扫描根目录（网盘内路径）；支持英文逗号分隔多路径，为空时默认与缓存路径一致。",
            "input_type": "text",
            "required": False,
            "secret": False,
            "placeholder": "/影视,/动漫",
        },
    ]
    
    # UC 网盘 API 域名
    BASE_URL = "https://pc-api.uc.cn"
    BASE_URL_DRIVE = "https://drive.uc.cn"
    TV_API_BASE_URL = "https://open-api-drive.uc.cn"
    TV_CODE_API_BASE_URL = "http://api.extscreen.com/ucdrive"
    TV_CLIENT_ID = "5acf882d27b74502b7040b0c65519aa7"
    TV_SIGN_KEY = "l3srvtd7p42l0d0x1u8d7yc8ye9kki4d"
    TV_APP_VER = "1.7.2.2"
    TV_CHANNEL = "UCTVOFFICIALWEB"
    TV_USER_AGENT = "Mozilla/5.0 (Linux; U; Android 13; zh-cn; M2004J7AC Build/UKQ1.231108.001) AppleWebKit/533.1 (KHTML, like Gecko) Mobile Safari/533.1"
    TV_DEVICE_BRAND = "Xiaomi"
    TV_PLATFORM = "tv"
    TV_DEVICE_NAME = "M2004J7AC"
    TV_DEVICE_MODEL = "M2004J7AC"
    TV_BUILD_DEVICE = "M2004J7AC"
    TV_BUILD_PRODUCT = "M2004J7AC"
    TV_DEVICE_GPU = "Adreno (TM) 550"
    TV_ACTIVITY_RECT = "{}"
    
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        cookie: str = "",
        index: int = 0,
        config: dict | None = None,
        account_name: str = "",
        no_login: bool = False,
    ):
        super().__init__(cookie, index, config=config, no_login=no_login)
        self._cookies_dict: Dict[str, str] = {}
        
        self._share_folder_fid: Optional[str] = None
        self.refresh_token = str(self.config.get("refresh_token") or "").strip()
        self.device_id = self._normalize_device_id(self.config.get("device_id"))
        self.query_token = str(self.config.get("query_token") or "").strip()
        self._save_task_contexts: dict[str, dict[str, Any]] = {}
        
        # 解析 cookie
        if cookie:
            for item in cookie.split(";"):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    self._cookies_dict[k.strip()] = v.strip()

    @staticmethod
    def _clean_message(payload: dict[str, Any] | None, default: str) -> str:
        body = payload or {}
        for key in ("message", "msg", "error_info", "error", "status_text"):
            value = str(body.get(key) or "").strip()
            if value:
                return value
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("message", "msg", "error_info", "error"):
                value = str(data.get(key) or "").strip()
                if value:
                    return value
        return default

    @staticmethod
    def _normalize_device_id(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _ensure_tv_device_id(cls, config: dict[str, Any] | None) -> str:
        device_id = cls._normalize_device_id((config or {}).get("device_id"))
        if device_id:
            return device_id
        raw = f"{time.time_ns()}:{cls.DRIVE_TYPE}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _tv_req_sign(cls, method: str, pathname: str, device_id: str) -> tuple[str, str, str]:
        timestamp = str(int(time.time() * 1000))
        req_id = hashlib.md5(f"{device_id}{timestamp}".encode("utf-8")).hexdigest()
        token_raw = f"{method.upper()}&{pathname}&{timestamp}&{cls.TV_SIGN_KEY}"
        token = hashlib.sha256(token_raw.encode("utf-8")).hexdigest()
        return timestamp, token, req_id

    @classmethod
    def _tv_base_query(cls, *, device_id: str, access_token: str = "") -> dict[str, str]:
        return {
            "req_id": hashlib.md5(f"{device_id}{int(time.time() * 1000)}".encode("utf-8")).hexdigest(),
            "access_token": access_token,
            "app_ver": cls.TV_APP_VER,
            "device_id": device_id,
            "device_brand": cls.TV_DEVICE_BRAND,
            "platform": cls.TV_PLATFORM,
            "device_name": cls.TV_DEVICE_NAME,
            "device_model": cls.TV_DEVICE_MODEL,
            "build_device": cls.TV_BUILD_DEVICE,
            "build_product": cls.TV_BUILD_PRODUCT,
            "device_gpu": cls.TV_DEVICE_GPU,
            "activity_rect": cls.TV_ACTIVITY_RECT,
            "channel": cls.TV_CHANNEL,
        }

    @classmethod
    def _tv_api_request(
        cls,
        method: str,
        pathname: str,
        *,
        device_id: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> dict[str, Any]:
        tm, token, req_id = cls._tv_req_sign(method, pathname, device_id)
        query = cls._tv_base_query(device_id=device_id, access_token=access_token)
        query["req_id"] = req_id
        if params:
            for key, value in params.items():
                if value is None:
                    continue
                query[key] = value
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": cls.TV_USER_AGENT,
            "x-pan-tm": tm,
            "x-pan-token": token,
            "x-pan-client-id": cls.TV_CLIENT_ID,
        }
        response = requests.request(
            method.upper(),
            f"{cls.TV_API_BASE_URL}{pathname}",
            headers=headers,
            params=query,
            json=json_body,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("TV 登录接口响应格式错误")
        errno = payload.get("errno")
        error_info = str(payload.get("error_info") or "").strip()
        status = payload.get("status")
        if isinstance(errno, int) and errno != 0:
            raise RuntimeError(error_info or cls._clean_message(payload, "TV 登录接口返回错误"))
        if isinstance(status, int) and status >= 400:
            raise RuntimeError(error_info or cls._clean_message(payload, "TV 登录接口请求失败"))
        return payload

    @classmethod
    def _tv_exchange_token(cls, *, device_id: str, value: str, is_refresh: bool) -> dict[str, Any]:
        pathname = "/token"
        _, _, req_id = cls._tv_req_sign("POST", pathname, device_id)
        body = cls._tv_base_query(device_id=device_id)
        body["req_id"] = req_id
        if is_refresh:
            body["refresh_token"] = value
        else:
            body["code"] = value
        response = requests.post(
            f"{cls.TV_CODE_API_BASE_URL}{pathname}",
            json=body,
            timeout=30,
            headers={"Content-Type": "application/json", "User-Agent": cls.TV_USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("TV 换取令牌响应格式错误")
        if int(payload.get("code") or 0) != 200:
            raise RuntimeError(cls._clean_message(payload, "TV 换取令牌失败"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("TV 换取令牌缺少 data")
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("TV 换取令牌失败：refresh_token 为空")
        return data

    @classmethod
    def start_tv_qrcode_auth(cls, config: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime_config = cls.normalize_config(config)
        device_id = cls._ensure_tv_device_id(runtime_config)
        payload = cls._tv_api_request(
            "GET",
            "/oauth/authorize",
            device_id=device_id,
            params={
                "auth_type": "code",
                "client_id": cls.TV_CLIENT_ID,
                "scope": "netdisk",
                "qrcode": "1",
                "qr_width": "460",
                "qr_height": "460",
            },
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        qr_data = str(payload.get("qr_data") or data.get("qr_data") or data.get("qrData") or "").strip()
        query_token = str(payload.get("query_token") or data.get("query_token") or data.get("queryToken") or "").strip()
        if not qr_data or not query_token:
            return {"success": False, "message": cls._clean_message(payload, "生成 UC TV 二维码失败")}
        image_src = qr_data if qr_data.startswith("data:image/") else f"data:image/jpeg;base64,{qr_data}"
        return {
            "success": True,
            "data": {
                "status": "NEW",
                "message": "等待扫码",
                "device_id": device_id,
                "query_token": query_token,
                "qrcode_url": image_src,
                "qrcode_image": image_src,
            },
        }

    @classmethod
    def poll_tv_qrcode_auth(cls, session_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = dict(session_meta or {})
        device_id = cls._normalize_device_id(meta.get("device_id"))
        query_token = str(meta.get("query_token") or "").strip()
        if not device_id:
            return {"success": False, "message": "缺少 TV 设备 ID"}
        if not query_token:
            return {"success": False, "message": "缺少 TV 查询令牌"}
        try:
            payload = cls._tv_api_request(
                "GET",
                "/oauth/code",
                device_id=device_id,
                params={
                    "client_id": cls.TV_CLIENT_ID,
                    "scope": "netdisk",
                    "query_token": query_token,
                },
            )
        except Exception as exc:
            message = str(exc).strip() or "等待扫码确认"
            lowered = message.lower()
            status = "PENDING"
            if "expired" in lowered or "过期" in message:
                status = "EXPIRED"
            elif "cancel" in lowered or "取消" in message:
                status = "CANCELED"
            return {
                "success": True,
                "data": {
                    "status": status,
                    "message": message,
                    "device_id": device_id,
                    "query_token": query_token,
                },
            }
        code_value = payload.get("code")
        if not isinstance(code_value, str):
            data = payload.get("data")
            code_value = str((data or {}).get("code") or "") if isinstance(data, dict) else ""
        code_value = code_value.strip()
        if not code_value:
            return {
                "success": True,
                "data": {
                    "status": "PENDING",
                    "message": cls._clean_message(payload, "等待扫码确认"),
                    "device_id": device_id,
                    "query_token": query_token,
                },
            }
        token_data = cls._tv_exchange_token(device_id=device_id, value=code_value, is_refresh=False)
        return {
            "success": True,
            "data": {
                "status": "CONFIRMED",
                "message": "TV 凭据已保存",
                "refresh_token": str(token_data.get("refresh_token") or "").strip(),
                "access_token": str(token_data.get("access_token") or "").strip(),
                "device_id": device_id,
                "query_token": query_token,
            },
        }

    def _send_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送 HTTP 请求"""
        self._throttle_request()
        headers = {
            "cookie": self.cookie,
            "content-type": "application/json",
            "user-agent": self.USER_AGENT,
            "origin": self.BASE_URL_DRIVE,
            "referer": f"{self.BASE_URL_DRIVE}/",
        }
        if "headers" in kwargs:
            headers.update(kwargs["headers"])
            del kwargs["headers"]

        try:
            response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            return response
        except Exception as e:
            logger.error(f"[UC] 请求失败: {e}")
            fake_response = requests.Response()
            fake_response.status_code = 500
            fake_response._content = b'{"status": 500, "code": 1, "message": "request error"}'
            return fake_response

    def _safe_json(self, response: requests.Response) -> Dict:
        """安全解析 JSON 响应"""
        try:
            return response.json()
        except Exception as e:
            logger.warning(f"[UC] JSON解析失败: {e}")
            return {"code": 1, "status": 500, "message": "响应解析失败"}

    def init(self) -> Any:
        """初始化账户"""
        if not str(self.cookie or "").strip() and self._has_tv_credentials():
            raise RuntimeError("当前仅支持保存/扫码 TV 凭据，账号运行仍需 Cookie")
        account_info = self.get_account_info()
        if account_info:
            self.is_active = True
            self.nickname = account_info.get("nickname", f"UC用户{self.index}")
            return account_info
        return False

    def _has_tv_credentials(self) -> bool:
        return any((self.refresh_token, self.device_id, self.query_token))

    def get_account_info(self) -> Any:
        """获取账户信息"""
        # UC 网盘账户信息 API
        url = f"{self.BASE_URL_DRIVE}/account/info"
        params = {"pr": "UCBrowser", "fr": "pc"}
        
        try:
            response = self._send_request("GET", url, params=params)
            data = self._safe_json(response)
            if data.get("data"):
                return data["data"]
        except Exception as e:
            logger.error(f"[UC] 获取账户信息失败: {e}")
        
        return False

    def get_account_config(self) -> Dict[str, Any]:
        """获取 UC 账户配置/容量信息"""
        account_info = self.get_account_info() or {}
        member_info = self._get_member_info()
        member_data = member_info.get("data") if isinstance(member_info, dict) else None

        nickname = (
            account_info.get("nickname")
            or account_info.get("nick_name")
            or self.nickname
            or f"UC用户{self.index}"
        )
        if nickname:
            self.nickname = nickname

        return {
            "drive_type": self.DRIVE_TYPE,
            "drive_name": self.DRIVE_NAME,
            "nickname": nickname,
            "username": nickname,
            "used_space": member_data.get("use_capacity") if isinstance(member_data, dict) else None,
            "total_space": member_data.get("total_capacity") if isinstance(member_data, dict) else None,
            "member_type": member_data.get("member_type") if isinstance(member_data, dict) else None,
            "member_status": member_data.get("member_status") if isinstance(member_data, dict) else None,
            "raw": {
                "account_info": account_info or None,
                "member_info": member_data,
            },
        }

    def _get_member_info(self) -> Dict[str, Any]:
        """获取 UC 会员/容量信息"""
        url = f"{self.BASE_URL}/1/clouddrive/member"
        params = {
            "pr": "UCBrowser",
            "fr": "pc",
            "fetch_subscribe": "true",
            "_ch": "home",
        }

        try:
            response = self._send_request("GET", url, params=params)
            result = self._safe_json(response)
            if result.get("code") == 0 and result.get("data"):
                return result
        except Exception as e:
            logger.error(f"[UC] 获取会员信息失败: {e}")

        return {}

    def get_stoken(self, pwd_id: str, passcode: str = "") -> Dict:
        """获取分享令牌"""
        url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/token"
        params = {"pr": "UCBrowser", "fr": "pc"}
        payload = {"pwd_id": pwd_id, "passcode": passcode}

        try:
            response = self._send_request("POST", url, json=payload, params=params)
            result = self._safe_json(response)
            return result
        except Exception as e:
            logger.error(f"[UC] 获取分享令牌失败: {e}")
            return {"status": 500, "code": 1, "message": f"获取分享令牌失败: {e}"}

    def get_detail(
        self,
        pwd_id: str,
        stoken: str,
        pdir_fid: str,
        _fetch_share: int = 0,
        fetch_share_full_path: int = 0,
    ) -> Dict:
        """获取分享文件详情"""
        list_merge = []
        page = 1
        result = {}

        while True:
            url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/detail"
            params = {
                "pr": "UCBrowser",
                "fr": "pc",
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": pdir_fid,
                "force": "0",
                "_page": page,
                "_size": "50",
                "_fetch_banner": "0",
                "_fetch_share": _fetch_share,
                "_fetch_total": "1",
                "_sort": "file_type:asc,updated_at:desc",
                "fetch_share_full_path": fetch_share_full_path,
            }

            try:
                response = self._send_request("GET", url, params=params)
                result = self._safe_json(response)
                
                if result.get("code") != 0:
                    return result
                
                file_list = result.get("data", {}).get("list", [])
                if file_list:
                    list_merge.extend(file_list)
                    page += 1
                else:
                    break
                
                total = result.get("metadata", {}).get("_total", 0)
                if len(list_merge) >= total:
                    break
                    
            except Exception as e:
                logger.error(f"[UC] 获取分享详情失败: {e}")
                return {"code": 1, "message": f"获取分享详情失败: {e}", "data": {"list": []}}

        # 保留完整的API响应（包含full_path等字段），仅替换合并后的文件列表
        if result.get("data"):
            result["data"]["list"] = list_merge
        else:
            result["data"] = {"list": list_merge}
        return result

    def ls_dir(self, pdir_fid: str, max_items: int = 0, **kwargs) -> Dict:
        """列出目录内容"""
        list_merge = []
        page = 1

        while True:
            url = f"{self.BASE_URL}/1/clouddrive/file/sort"
            params = {
                "pr": "UCBrowser",
                "fr": "pc",
                "pdir_fid": pdir_fid,
                "_page": page,
                "_size": "1000",
                "_fetch_total": "1",
                "_fetch_sub_dirs": "0",
                "_sort": "file_type:asc,updated_at:desc",
                "_fetch_full_path": kwargs.get("fetch_full_path", 0),
            }

            try:
                response = self._send_request("GET", url, params=params)
                result = self._safe_json(response)
                
                if result.get("code") != 0:
                    return result
                
                file_list = result.get("data", {}).get("list", [])
                if file_list:
                    list_merge.extend(file_list)
                    page += 1
                else:
                    break
                
                # max_items 限量：达到上限后提前终止分页
                if max_items > 0 and len(list_merge) >= max_items:
                    list_merge = list_merge[:max_items]
                    break

                total = result.get("metadata", {}).get("_total", 0)
                if len(list_merge) >= total:
                    break
                    
            except Exception as e:
                logger.error(f"[UC] 列出目录失败: {e}")
                return {"code": 1, "message": f"列出目录失败: {e}", "data": {"list": []}}

        return {
            "code": 0,
            "message": "success",
            "data": {"list": list_merge},
            "metadata": {"_total": len(list_merge)},
        }

    def save_file(
        self,
        fid_list: List[str],
        fid_token_list: List[str],
        to_pdir_fid: str,
        pwd_id: str,
        stoken: str,
        file_names: List[str] | None = None,
    ) -> Dict:
        """转存文件"""
        before_items = self.snapshot_dest_dir_items(to_pdir_fid, max_items=1000) if file_names else {}
        url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/save"
        params = {
            "entry": "update_share",
            "pr": "UCBrowser",
            "fr": "pc",
            "__dt": int(random.uniform(1, 5) * 60 * 1000),
            "__t": datetime.now().timestamp(),
        }
        payload = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        logger.debug(f"[UC] 转存文件参数: {payload}")
        try:
            response = self._send_request("POST", url, json=payload, params=params)
            result = self._safe_json(response)

            # 检查容量限制错误
            msg = result.get("message", "")
            if "capacity limit" in msg.lower():
                logger.error("[UC] 网盘容量不足，无法转存")
                return {"code": 1, "status": 400, "message": "UC网盘容量不足，请清理空间后重试", "data": {}}
            if file_names and result.get("status") in (200, "200", None):
                data = result.get("data") or {}
                task_id = str(data.get("task_id") or result.get("task_id") or "").strip()
                if task_id:
                    self._save_task_contexts[task_id] = {
                        "pdir_fid": str(to_pdir_fid),
                        "file_names": [str(name or "") for name in file_names],
                        "before_items": before_items,
                    }
                else:
                    aligned = self.align_saved_fids_from_dir(
                        str(to_pdir_fid),
                        file_names,
                        before_items=before_items,
                        max_items=1000,
                        timeout_seconds=15,
                        interval_seconds=0.5,
                        accept_partial_best=True,
                    )
                    data["task_id"] = f"uc_sync_{int(time.time() * 1000)}"
                    data["save_as_top_fids"] = aligned
                    data["_sync"] = True
                    result["data"] = data
            logger.debug(f"[UC] 转存结果: {result}")
            return result
        except Exception as e:
            logger.error(f"[UC] 转存失败: {e}")
            return {"code": 1, "message": f"转存失败: {e}", "data": {}}

    def unarchive(self, fid: str, to_pdir_fid: str) -> Dict:
        url = f"{self.BASE_URL}/1/clouddrive/archive/unarchive"
        params = {
            "pr": "UCBrowser",
            "fr": "pc",
            "__dt": int(random.uniform(1, 5) * 60 * 1000),
            "__t": datetime.now().timestamp(),
        }
        payload = {
            "fid": fid,
            "pwd": "",
            "select_mode": 0,
            "path_no_list": [],
            "curr_path_no": 0,
            "remember_pwd": False,
            "conflict_mode": 3,
            "suffix_type": 0,
            "to_pdir_fid": to_pdir_fid,
        }
        try:
            response = self._send_request("POST", url, json=payload, params=params)
            return self._safe_json(response)
        except Exception as e:
            logger.error(f"[UC] 解压失败: {e}")
            return {"status": 500, "code": 1, "message": f"解压失败: {e}", "data": {}}

    def query_task(self, task_id: str) -> Dict:
        """查询任务状态"""
        retry_index = 0
        max_retries = 60
        result = {"status": 500, "code": 1, "message": "任务查询超时"}
        logger.debug(f"[UC] 查询任务: {task_id}")
        while retry_index < max_retries:
            url = f"{self.BASE_URL}/1/clouddrive/task"
            params = {
                "pr": "UCBrowser",
                "fr": "pc",
                "task_id": task_id,
                "retry_index": retry_index,
                "__dt": int(random.uniform(1, 5) * 60 * 1000),
                "__t": datetime.now().timestamp(),
            }

            try:
                response = self._send_request("GET", url, params=params)
                result = self._safe_json(response)

                # 检查容量限制错误
                msg = result.get("message", "")
                if "capacity limit" in msg.lower():
                    logger.error("[UC] 网盘容量不足")
                    return {"status": 400, "code": 1, "message": "UC网盘容量不足，请清理空间后重试", "data": {"status": -1}}

                if result.get("status") != 200:
                    return result

                task_status = result.get("data", {}).get("status")

                # 任务完成
                if task_status == 2:
                    ctx = self._save_task_contexts.pop(str(task_id), None)
                    if ctx:
                        save_as = result.setdefault("data", {}).get("save_as") or {}
                        existing = save_as.get("save_as_select_top_fids")
                        existing_fids = [str(x).strip() for x in existing] if isinstance(existing, list) else []
                        aligned = self.align_saved_fids_from_dir(
                            str(ctx.get("pdir_fid") or "0"),
                            ctx.get("file_names") or [],
                            before_items=ctx.get("before_items") or {},
                            max_items=1000,
                            timeout_seconds=15,
                            interval_seconds=0.5,
                            accept_partial_best=True,
                        )
                        if sum(1 for fid in aligned if fid) >= sum(1 for fid in existing_fids if fid):
                            save_as["save_as_select_top_fids"] = aligned
                            result["data"]["save_as"] = save_as
                    if retry_index > 0:
                        logger.info("")
                    break

                # 任务失败
                if task_status == -1:
                    msg = result.get("data", {}).get("message", "任务执行失败")
                    logger.error(f"[UC] 任务失败: {msg}")
                    return result

                # 任务进行中
                if retry_index == 0:
                    task_title = result.get("data", {}).get("task_title", "任务")
                    logger.info(f"[UC] 等待任务[{task_title}]执行结果...")

                retry_index += 1
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"[UC] 查询任务失败: {e}")
                return {"status": 500, "code": 1, "message": f"查询任务失败: {e}"}
        logger.debug(f"[UC] 任务结果: {result}")
        return result

    def mkdir(self, dir_path: str) -> Dict:
        """创建目录"""
        url = f"{self.BASE_URL}/1/clouddrive/file"
        params = {"pr": "UCBrowser", "fr": "pc"}
        payload = {
            "pdir_fid": "0",
            "file_name": "",
            "dir_path": dir_path,
            "dir_init_lock": False,
        }

        try:
            response = self._send_request("POST", url, json=payload, params=params)
            result = self._safe_json(response)
            return result
        except Exception as e:
            logger.error(f"[UC] 创建目录失败: {e}")
            return {"code": 1, "message": f"创建目录失败: {e}"}

    def rename(self, fid: str, file_name: str) -> Dict:
        """重命名文件"""
        url = f"{self.BASE_URL}/1/clouddrive/file/rename"
        params = {"pr": "UCBrowser", "fr": "pc"}
        payload = {"fid": fid, "file_name": file_name}

        try:
            response = self._send_request("POST", url, json=payload, params=params)
            result = self._safe_json(response)
            return result
        except Exception as e:
            logger.error(f"[UC] 重命名失败: {e}")
            return {"code": 1, "message": f"重命名失败: {e}"}

    def delete(self, filelist: List[str]) -> Dict:
        """删除文件"""
        url = f"{self.BASE_URL}/1/clouddrive/file/delete"
        params = {"pr": "UCBrowser", "fr": "pc"}
        payload = {"action_type": 2, "filelist": filelist, "exclude_fids": []}

        try:
            response = self._send_request("POST", url, json=payload, params=params)
            result = self._safe_json(response)
            return result
        except Exception as e:
            logger.error(f"[UC] 删除失败: {e}")
            return {"code": 1, "message": f"删除失败: {e}"}

    def move_file(self, filelist: List[str], to_pdir_fid: str) -> Dict:
        """移动文件到指定目录"""
        url = f"{self.BASE_URL}/1/clouddrive/file/move"
        params = {"pr": "UCBrowser", "fr": "pc"}
        payload = {
            "action_type": 1,
            "to_pdir_fid": to_pdir_fid,
            "filelist": filelist,
            "exclude_fids": [],
        }
        logger.debug(f"[UC] 移动文件: {filelist} -> {to_pdir_fid}")
        try:
            response = self._send_request("POST", url, json=payload, params=params)
            result = self._safe_json(response)
            logger.debug(f"[UC] 移动文件结果: {result}")
            return result
        except Exception as e:
            logger.error(f"[UC] 移动文件失败: {e}")
            return {"code": 1, "message": f"移动文件失败: {e}"}

    def move_files(self, fids: List[str], to_pdir_fid: str) -> Dict:
        return self.move_files_to_target(fids, to_pdir_fid)

    def get_or_create_share_folder(self) -> Optional[str]:
        """获取或创建'来自：分享'文件夹，返回其fid"""
        if self._share_folder_fid:
            return self._share_folder_fid

        # 列出根目录查找"来自：分享"文件夹
        try:
            root_list = self.ls_dir("0")
            if root_list.get("code") == 0:
                for item in root_list.get("data", {}).get("list", []):
                    if item.get("file_name") == "来自：分享" and item.get("dir"):
                        self._share_folder_fid = item["fid"]
                        return self._share_folder_fid
            else:
                logger.warning(f"[UC] 列出根目录失败: {root_list.get('message')}")
                return None
        except Exception as e:
            logger.warning(f"[UC] 列出根目录异常: {e}")
            return None

        # 未找到，创建文件夹
        try:
            mkdir_result = self.mkdir("/来自：分享")
            if mkdir_result.get("code") == 0:
                self._share_folder_fid = mkdir_result["data"]["fid"]
                logger.debug("[UC] 创建中转文件夹: 来自：分享")
                return self._share_folder_fid
            else:
                logger.warning(f"[UC] 创建'来自：分享'文件夹失败: {mkdir_result.get('message')}")
                return None
        except Exception as e:
            logger.warning(f"[UC] 创建'来自：分享'文件夹异常: {e}")
            return None

    def move_files_to_target(self, fid_list: List[str], to_pdir_fid: str) -> Dict:
        """将指定文件移动到目标目录，支持分批+轮询"""
        if not fid_list:
            return {"code": 0, "message": "无文件需要移动"}

        logger.debug(f"[UC] 移动 {len(fid_list)} 个文件到目标目录...")
        remaining = fid_list[:]
        while remaining:
            batch = remaining[:100]
            remaining = remaining[100:]

            move_result = self.move_file(batch, to_pdir_fid)
            if move_result.get("code") != 0:
                return move_result

            task_id = move_result.get("data", {}).get("task_id")
            if task_id:
                query_result = self.query_task(task_id)
                if query_result.get("code") != 0 or query_result.get("data", {}).get("status") == -1:
                    msg = query_result.get("data", {}).get("message", query_result.get("message", "移动任务失败"))
                    return {"code": 1, "message": msg}

        return {"code": 0, "message": "移动完成"}

    def get_fids(self, file_paths: List[str]) -> List[Dict]:
        """根据路径获取文件 ID"""
        fids = []
        
        while file_paths:
            url = f"{self.BASE_URL}/1/clouddrive/file/info/path_list"
            params = {"pr": "UCBrowser", "fr": "pc"}
            payload = {"file_path": file_paths[:50], "namespace": "0"}

            try:
                response = self._send_request("POST", url, json=payload, params=params)
                result = self._safe_json(response)
                
                if result.get("code") == 0:
                    fids.extend(result.get("data", []))
                    file_paths = file_paths[50:]
                else:
                    logger.error(f"[UC] 获取目录ID失败: {result.get('message')}")
                    break
                    
            except Exception as e:
                logger.error(f"[UC] 获取目录ID失败: {e}")
                break

        return fids

    def extract_url(self, url: str) -> Tuple[Optional[str], str, Any, List]:
        """
        解析UC网盘分享链接
        
        支持格式:
        - https://drive.uc.cn/s/{share_id}
        - https://drive.uc.cn/s/{share_id}?password=xxxx
        """
        import urllib.parse

        # pwd_id
        match_id = re.search(r"/s/(\w+)", url)
        pwd_id = match_id.group(1) if match_id else None
        
        # passcode
        match_pwd = re.search(r"(?:pwd|password)=(\w+)", url)
        passcode = match_pwd.group(1) if match_pwd else ""
        
        # path: fid-name
        paths = []
        matches = re.findall(r"/(\w{32})-?([^/]+)?", url)
        for match in matches:
            fid = match[0]
            name = urllib.parse.unquote(match[1]).replace("*101", "-") if match[1] else ""
            paths.append({"fid": fid, "name": name})
        
        pdir_fid = paths[-1]["fid"] if matches else 0

        return pwd_id, passcode, pdir_fid, paths

    def export_runtime_config(self) -> dict[str, Any]:
        payload = dict(self.config)
        payload["cookie"] = str(self.cookie or "").strip()
        payload["refresh_token"] = self.refresh_token
        payload["device_id"] = self.device_id
        payload["query_token"] = self.query_token
        return self.normalize_config(payload)
