# -*- coding: utf-8 -*-
"""
夸克网盘适配器
适配现有 Quark 类到统一接口
"""
import sys
import os
import logging
import hashlib
import time
from typing import Dict, List, Tuple, Optional, Any

# 添加父目录到路径，以便导入 quark_auto_save
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.extensions.adapters.base_adapter import BaseCloudDriveAdapter


logger = logging.getLogger(__name__)


class QuarkAdapter(BaseCloudDriveAdapter):
    """夸克网盘适配器"""

    DRIVE_TYPE = "quark"
    DRIVE_NAME = "夸克网盘"
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
            "description": "夸克网盘登录 Cookie 原文；如需移动端分享兼容，可一并带上 kps、sign、vcode。",
            "input_type": "textarea",
            "required": True,
            "secret": True,
            "placeholder": "__puus=...; kps=...; sign=...; vcode=...",
        },
        {
            "key": "refresh_token",
            "label": "TV 刷新令牌",
            "description": "夸克 TV 端 refresh_token；扫码成功后会自动回写，也可手动填写保存。",
            "input_type": "textarea",
            "required": False,
            "secret": True,
            "placeholder": "refresh_token",
        },
        {
            "key": "device_id",
            "label": "TV 设备 ID",
            "description": "夸克 TV 登录签名所需 device_id；为空时发起扫码会自动生成。",
            "input_type": "text",
            "required": False,
            "secret": False,
            "placeholder": "自动生成或手动填写",
        },
        {
            "key": "query_token",
            "label": "TV 查询令牌",
            "description": "夸克 TV 扫码轮询使用的 query_token；支持手动保存以续接未完成登录。",
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
    BASE_URL = "https://drive-pc.quark.cn"
    BASE_URL_APP = "https://drive-m.quark.cn"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
    TV_API_BASE_URL = "https://open-api-drive.quark.cn"
    TV_CODE_API_BASE_URL = "http://api.extscreen.com/quarkdrive"
    TV_CLIENT_ID = "d3194e61504e493eb6222857bccfed94"
    TV_SIGN_KEY = "kw2dvtd7p4t3pjl2d9ed9yc8yej8kw2d"
    TV_APP_VER = "1.8.2.2"
    TV_CHANNEL = "GENERAL"
    TV_USER_AGENT = "Mozilla/5.0 (Linux; U; Android 13; zh-cn; M2004J7AC Build/UKQ1.231108.001) AppleWebKit/533.1 (KHTML, like Gecko) Mobile Safari/533.1"
    TV_DEVICE_BRAND = "Xiaomi"
    TV_PLATFORM = "tv"
    TV_DEVICE_NAME = "M2004J7AC"
    TV_DEVICE_MODEL = "M2004J7AC"
    TV_BUILD_DEVICE = "M2004J7AC"
    TV_BUILD_PRODUCT = "M2004J7AC"
    TV_DEVICE_GPU = "Adreno (TM) 550"
    TV_ACTIVITY_RECT = "{}"

    def __init__(
        self,
        cookie: str = "",
        index: int = 0,
        config: dict | None = None,
        account_name: str = "",
        no_login: bool = False,
    ):
        super().__init__(cookie, index, config=config, no_login=no_login)
        self.mparam = self._match_mparam_form_cookie(cookie)
        self.refresh_token = str(self.config.get("refresh_token") or "").strip()
        self.device_id = self._normalize_device_id(self.config.get("device_id"))
        self.query_token = str(self.config.get("query_token") or "").strip()
        self._save_task_contexts: dict[str, dict[str, Any]] = {}

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
        import requests

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
        import requests

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
            return {"success": False, "message": cls._clean_message(payload, "生成夸克 TV 二维码失败")}
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

    def _match_mparam_form_cookie(self, cookie: str) -> Dict:
        """从 cookie 中提取移动端参数"""
        import re

        mparam = {}
        kps_match = re.search(r"(?<!\w)kps=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
        sign_match = re.search(r"(?<!\w)sign=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
        vcode_match = re.search(r"(?<!\w)vcode=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
        if kps_match and sign_match and vcode_match:
            mparam = {
                "kps": kps_match.group(1).replace("%25", "%"),
                "sign": sign_match.group(1).replace("%25", "%"),
                "vcode": vcode_match.group(1).replace("%25", "%"),
            }
        return mparam
    def convert_bytes(self, b):
        '''
        将字节转换为 MB GB TB
        :param b: 字节数
        :return: 返回 MB GB TB
        '''
        units = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        i = 0
        while b >= 1024 and i < len(units) - 1:
            b /= 1024
            i += 1
        return f"{b:.2f} {units[i]}"
    def _send_request(self, method: str, url: str, **kwargs):
        """发送 HTTP 请求"""
        import requests
        self._throttle_request()
        
        headers = {
            "cookie": self.cookie,
            "content-type": "application/json",
            "user-agent": self.USER_AGENT,
        }
        if "headers" in kwargs:
            headers = kwargs["headers"]
            del kwargs["headers"]
        if self.mparam and "share" in url and self.BASE_URL in url:
            url = url.replace(self.BASE_URL, self.BASE_URL_APP)
            kwargs["params"].update(
                {
                    "device_model": "M2011K2C",
                    "entry": "default_clouddrive",
                    "_t_group": "0%3A_s_vp%3A1",
                    "dmn": "Mi%2B11",
                    "fr": "android",
                    "pf": "3300",
                    "bi": "35937",
                    "ve": "7.4.5.680",
                    "ss": "411x875",
                    "mi": "M2011K2C",
                    "nt": "5",
                    "nw": "0",
                    "kt": "4",
                    "pr": "ucpro",
                    "sv": "release",
                    "dt": "phone",
                    "data_from": "ucapi",
                    "kps": self.mparam.get("kps"),
                    "sign": self.mparam.get("sign"),
                    "vcode": self.mparam.get("vcode"),
                    "app": "clouddrive",
                    "kkkk": "1",
                }
            )
            del headers["cookie"]
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
            return response
        except Exception as e:
            logger.exception("_send_request error: %s", e)
            fake_response = requests.Response()
            fake_response.status_code = 500
            fake_response._content = (
                b'{"status": 500, "code": 1, "message": "request error"}'
            )
            return fake_response

    def init(self) -> Any:
        """初始化账户"""
        if not str(self.cookie or "").strip() and self._has_tv_credentials():
            raise RuntimeError("当前仅支持保存/扫码 TV 凭据，账号运行仍需 Cookie")
        account_info = self.get_account_info()
        if account_info:
            self.is_active = True
            self.nickname = account_info["nickname"]
            return account_info
        else:
            return False

    def _has_tv_credentials(self) -> bool:
        return any((self.refresh_token, self.device_id, self.query_token))

    def get_account_info(self) -> Any:
        """获取账户信息"""
        url = "https://pan.quark.cn/account/info"
        querystring = {"fr": "pc", "platform": "pc"}
        response = self._send_request("GET", url, params=querystring).json()
        if response.get("data"):
            return response["data"]
        else:
            return False

    def get_account_config(self) -> Dict[str, Any]:
        """获取夸克账户配置/容量信息"""
        account_info = self.get_account_info() or {}
        member_info = self._get_member_info()
        member_data = member_info.get("data") if isinstance(member_info, dict) else None

        nickname = (
            account_info.get("nickname")
            or account_info.get("nick_name")
            or self.nickname
            or f"夸克用户{self.index}"
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
        url = f"{self.BASE_URL}/1/clouddrive/member"
        querystring = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "fetch_subscribe": "true",
            "_ch": "home",
            "fetch_identity": "true",
        }
        response = self._send_request("GET", url, params=querystring).json()
        if response.get("code") == 0 and response.get("data"):
            return response
        return {}

    def get_stoken(self, pwd_id: str, passcode: str = "") -> Dict:
        """获取分享令牌"""
        url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/token"
        querystring = {"pr": "ucpro", "fr": "pc"}
        payload = {"pwd_id": pwd_id, "passcode": passcode}
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

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
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/detail"
            querystring = {
                "pr": "ucpro",
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
                "ver": "2",
                "fetch_share_full_path": fetch_share_full_path,
            }
            response = self._send_request("GET", url, params=querystring).json()
            if response["code"] != 0:
                return response
            if response["data"]["list"]:
                list_merge += response["data"]["list"]
                page += 1
            else:
                break
            if len(list_merge) >= response["metadata"]["_total"]:
                break
        response["data"]["list"] = list_merge
        return response

    def ls_dir(self, pdir_fid: str, max_items: int = 0, **kwargs) -> Dict:
        """列出目录内容"""
        list_merge = []
        page = 1
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/file/sort"
            querystring = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "pdir_fid": pdir_fid if pdir_fid else "0",
                "_page": page,
                "_size": "1000",
                "_fetch_total": "1",
                "_fetch_sub_dirs": "0",
                "_sort": "file_type:asc,updated_at:desc",
                "_fetch_full_path": kwargs.get("fetch_full_path", 0),
                "fetch_all_file": 1,
                "fetch_risk_file_name": 1,
            }
            response = self._send_request("GET", url, params=querystring).json()
            if response["code"] != 0:
                return response
            if response["data"]["list"]:
                list_merge += response["data"]["list"]
                page += 1
            else:
                break
            # max_items 限量：达到上限后提前终止分页
            if max_items > 0 and len(list_merge) >= max_items:
                list_merge = list_merge[:max_items]
                break
            if len(list_merge) >= response["metadata"]["_total"]:
                break
        response["data"]["list"] = list_merge
        return response

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
        import random
        from datetime import datetime

        before_items = self.snapshot_dest_dir_items(to_pdir_fid, max_items=1000) if file_names else {}
        url = f"{self.BASE_URL}/1/clouddrive/share/sharepage/save"
        querystring = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "app": "clouddrive",
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
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        data = response.get("data") if isinstance(response, dict) else {}
        task_id = str((data or {}).get("task_id") or response.get("task_id") or "").strip() if isinstance(response, dict) else ""
        if file_names and isinstance(response, dict) and response.get("status") in (200, "200", None):
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
                response_data = response.setdefault("data", {})
                response_data["task_id"] = f"quark_sync_{int(time.time() * 1000)}"
                response_data["save_as_top_fids"] = aligned
                response_data["_sync"] = True
        return response

    def query_task(self, task_id: str) -> Dict:
        """查询任务状态"""
        import time
        import random
        from datetime import datetime

        retry_index = 0
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/task"
            querystring = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "task_id": task_id,
                "retry_index": retry_index,
                "__dt": int(random.uniform(1, 5) * 60 * 1000),
                "__t": datetime.now().timestamp(),
            }
            response = self._send_request("GET", url, params=querystring).json()
            if response["status"] != 200:
                logger.warning("查询任务状态失败：%s", response)
                return response
            if response["data"]["status"] == 2:
                ctx = self._save_task_contexts.pop(str(task_id), None)
                if ctx:
                    save_as = response["data"].get("save_as") or {}
                    existing = save_as.get("save_as_top_fids")
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
                        save_as["save_as_top_fids"] = aligned
                        response["data"]["save_as"] = save_as
                logger.info("任务[%s]执行完成", response["data"].get("task_title"))
                break
            else:
                if retry_index == 0:
                    logger.debug("正在等待[%s]执行结果", response["data"].get("task_title"))
                retry_index += 1
                time.sleep(0.500)
        return response

    def mkdir(self, dir_path: str) -> Dict:
        """创建目录"""
        url = f"{self.BASE_URL}/1/clouddrive/file"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {
            "pdir_fid": "0",
            "file_name": "",
            "dir_path": dir_path,
            "dir_init_lock": False,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def rename(self, fid: str, file_name: str) -> Dict:
        """重命名文件"""
        url = f"{self.BASE_URL}/1/clouddrive/file/rename"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {"fid": fid, "file_name": file_name}
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def delete(self, filelist: List[str]) -> Dict:
        """删除文件"""
        url = f"{self.BASE_URL}/1/clouddrive/file/delete"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {"action_type": 2, "filelist": filelist, "exclude_fids": []}
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def get_fids(self, file_paths: List[str]) -> List[Dict]:
        """根据路径获取文件ID"""
        fids = []
        while True:
            url = f"{self.BASE_URL}/1/clouddrive/file/info/path_list"
            querystring = {"pr": "ucpro", "fr": "pc"}
            payload = {"file_path": file_paths[:50], "namespace": "0"}
            response = self._send_request(
                "POST", url, json=payload, params=querystring
            ).json()
            if response["code"] == 0:
                fids += response["data"]
                file_paths = file_paths[50:]
            else:
                logger.warning("获取目录ID：失败, %s", response.get("message"))
                break
            if len(file_paths) == 0:
                break
        return fids

    def extract_url(self, url: str) -> Tuple[Optional[str], str, Any, List]:
        """解析分享链接"""
        import re
        import urllib.parse

        # pwd_id
        match_id = re.search(r"/s/(\w+)", url)
        pwd_id = match_id.group(1) if match_id else None
        # passcode
        match_pwd = re.search(r"pwd=(\w+)", url)
        passcode = match_pwd.group(1) if match_pwd else ""
        # path: fid-name
        paths = []
        matches = re.findall(r"/(\w{32})-?([^/]+)?", url)
        for match in matches:
            fid = match[0]
            name = urllib.parse.unquote(match[1]).replace("*101", "-")
            paths.append({"fid": fid, "name": name})
        pdir_fid = paths[-1]["fid"] if matches else 0
        return pwd_id, passcode, pdir_fid, paths

    # 以下为夸克特有方法，不在基类接口中

    def sign_in(self) -> Dict[str, Any]:
        growth_info = self.get_growth_info()
        if growth_info:
            if growth_info["cap_sign"]["sign_daily"]:
                tmp = (
                    f"签到日志: 今日已签到+{self.convert_bytes(growth_info['cap_sign']['sign_daily_reward'])}，"
                    f"连签进度({growth_info['cap_sign']['sign_progress']}/{growth_info['cap_sign']['sign_target']})"
                )
                return {"supported": True, "ok": True, "message": tmp}
            else:
                sign, sign_return = self.get_growth_sign()
                if sign:
                    tmp = (
                        f"执行签到: 今日签到+{self.convert_bytes(sign_return)}，"
                        f"连签进度({growth_info['cap_sign']['sign_progress'] + 1}/{growth_info['cap_sign']['sign_target']})"
                    )
                    return {"supported": True, "ok": bool(sign), "message": tmp}
                else:
                    return {"supported": True, "ok": bool(sign), "message": sign_return}
        else:
            return {"supported": True, "ok": False, "message": "获取成长信息失败"}

    def get_growth_info(self) -> Any:
        """获取成长信息（签到用）"""
        url = f"{self.BASE_URL_APP}/1/clouddrive/capacity/growth/info"
        querystring = {
            "pr": "ucpro",
            "fr": "android",
            "kps": self.mparam.get("kps"),
            "sign": self.mparam.get("sign"),
            "vcode": self.mparam.get("vcode"),
        }
        headers = {
            "content-type": "application/json",
        }
        response = self._send_request(
            "GET", url, headers=headers, params=querystring
        ).json()
        if response.get("data"):
            return response["data"]
        else:
            return False

    def get_growth_sign(self) -> Tuple[bool, Any]:
        """执行签到"""
        url = f"{self.BASE_URL_APP}/1/clouddrive/capacity/growth/sign"
        querystring = {
            "pr": "ucpro",
            "fr": "android",
            "kps": self.mparam.get("kps"),
            "sign": self.mparam.get("sign"),
            "vcode": self.mparam.get("vcode"),
        }
        payload = {
            "sign_cyclic": True,
        }
        headers = {
            "content-type": "application/json",
        }
        response = self._send_request(
            "POST", url, json=payload, headers=headers, params=querystring
        ).json()
        logger.info("执行签到: %s", response)
        if response.get("data"):
            return True, response["data"]["sign_daily_reward"]
        else:
            return False, response["message"]

    def recycle_list(self, page: int = 1, size: int = 30) -> List:
        """获取回收站列表"""
        url = f"{self.BASE_URL}/1/clouddrive/file/recycle/list"
        querystring = {
            "_page": page,
            "_size": size,
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
        }
        response = self._send_request("GET", url, params=querystring).json()
        return response["data"]["list"]

    def recycle_remove(self, record_list: List) -> Dict:
        """清空回收站"""
        url = f"{self.BASE_URL}/1/clouddrive/file/recycle/remove"
        querystring = {"uc_param_str": "", "fr": "pc", "pr": "ucpro"}
        payload = {
            "select_mode": 2,
            "record_list": record_list,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def unarchive(self, fid, to_pdir_fid):
        url = f"{self.BASE_URL}/1/clouddrive/archive/unarchive"
        querystring = {"uc_param_str": "", "fr": "pc", "pr": "ucpro"}
        payload = {
            "fid": fid,
            "to_pdir_fid": to_pdir_fid,
            "conflict_mode": 3,
            "suffix_type": 0,
            "pwd": "",
            "select_mode": 0,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def move_files(self, fids, to_pdir_fid):
        url = f"{self.BASE_URL}/1/clouddrive/file/move"
        querystring = {"uc_param_str": "", "fr": "pc", "pr": "ucpro"}
        payload = {
            "filelist": fids,
            "to_pdir_fid": to_pdir_fid,
            "exclude_fids": [],
            "action_type": 1,
        }
        response = self._send_request(
            "POST", url, json=payload, params=querystring
        ).json()
        return response

    def download(self, fids: List[str]) -> Tuple[Dict, str]:
        """获取下载链接"""
        url = f"{self.BASE_URL}/1/clouddrive/file/download"
        querystring = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        payload = {"fids": fids}
        response = self._send_request("POST", url, json=payload, params=querystring)
        set_cookie = response.cookies.get_dict()
        cookie_str = "; ".join([f"{key}={value}" for key, value in set_cookie.items()])
        return response.json(), cookie_str

    def export_runtime_config(self) -> dict[str, Any]:
        payload = dict(self.config)
        payload["cookie"] = str(self.cookie or "").strip()
        payload["refresh_token"] = self.refresh_token
        payload["device_id"] = self.device_id
        payload["query_token"] = self.query_token
        return self.normalize_config(payload)
