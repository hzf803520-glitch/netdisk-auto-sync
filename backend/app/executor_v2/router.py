from __future__ import annotations

import os
from html import escape
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.executor_v2.providers import SUPPORTED_PROVIDERS
from app.executor_v2.runtime import ExecutorRuntime, get_runtime
from app.executor_v2.store import settings_interval_seconds


router = APIRouter()

PROVIDER_ALIASES = {
    "baidu": "baidu",
    "百度": "baidu",
    "百度网盘": "baidu",
    "quark": "quark",
    "夸克": "quark",
    "夸克网盘": "quark",
    "uc": "uc",
    "UC": "uc",
    "UC网盘": "uc",
    "xunlei": "xunlei",
    "迅雷": "xunlei",
    "迅雷云盘": "xunlei",
    "迅雷网盘": "xunlei",
}

CAPABILITIES = {
    "directLogin": True,
    "encryptedSessions": True,
    "jobQueue": True,
    "scheduler": True,
    "retries": True,
    "sourceDiff": True,
    "autoTransfer": True,
    "createShare": True,
    "validateShare": True,
    "resourceStatus": True,
    "githubOidcWake": True,
    "postgresPersistence": True,
    "nearRealtimePolling": True,
}


class AuthStart(BaseModel):
    provider: str


class AuthPoll(BaseModel):
    provider: str
    sessionId: str = Field(min_length=8, max_length=200)


class ResourceBatch(BaseModel):
    resources: list[dict[str, Any]] = Field(default_factory=list)


class ResourceStatusRequest(BaseModel):
    resourceKeys: list[str] = Field(default_factory=list)


class ResourceCommand(BaseModel):
    requestId: str = ""
    resourceKey: str = Field(min_length=1, max_length=300)
    provider: str
    title: str = Field(min_length=1, max_length=300)
    sourceUrl: str = Field(min_length=8, max_length=2000)
    sourceCode: str = Field(default="", max_length=100)
    targetFolder: str = Field(min_length=1, max_length=800)
    currentShareUrl: str = Field(default="", max_length=2000)
    monitorEnabled: bool = True


class LoginControl(BaseModel):
    action: str
    x: float | None = None
    y: float | None = None
    text: str | None = Field(default=None, max_length=512)
    key: str | None = Field(default=None, max_length=40)


class MaintenanceRequest(BaseModel):
    waitSeconds: int = Field(default=240, ge=10, le=840)


def _runtime_with_auth(request: Request) -> ExecutorRuntime:
    runtime = get_runtime()
    if not runtime.valid_bearer(request.headers.get("authorization", "")):
        raise HTTPException(status_code=401, detail="执行器访问令牌无效")
    return runtime


def _runtime_with_maintenance_auth(request: Request) -> ExecutorRuntime:
    runtime = get_runtime()
    if not runtime.valid_maintenance_bearer(request.headers.get("authorization", "")):
        raise HTTPException(status_code=401, detail="定时唤醒令牌无效")
    return runtime


def _provider(value: str) -> str:
    normalized = PROVIDER_ALIASES.get(str(value or "").strip())
    if not normalized or normalized not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="不支持这个网盘")
    return normalized


def _account_payload(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": str(account.get("provider") or ""),
        "state": str(account.get("state") or "login-required"),
        "displayName": str(account.get("display_name") or ""),
        "lastVerifiedAt": account.get("last_verified_at"),
        "message": str(account.get("message") or ""),
    }


def _resource_payload(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceKey": str(resource.get("resource_key") or ""),
        "status": str(resource.get("status") or "待处理"),
        "shareUrl": str(resource.get("share_url") or ""),
        "shareCode": str(resource.get("share_code") or ""),
        "episodeInfo": str(resource.get("episode_info") or "等待识别"),
        "targetFolder": str(resource.get("target_folder") or ""),
        "lastCheckedAt": resource.get("last_checked_at"),
        "lastSyncedAt": resource.get("last_synced_at"),
        "message": str(resource.get("message") or ""),
    }


def _expires_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _public_origin(request: Request) -> str:
    configured = os.getenv("EXECUTOR_PUBLIC_URL", "").strip().rstrip("/")
    if configured.startswith("https://"):
        return configured
    request_origin = str(request.base_url).rstrip("/")
    if request_origin.startswith("https://") or request.url.hostname in {
        "127.0.0.1",
        "localhost",
        "testserver",
    }:
        return request_origin
    raise HTTPException(status_code=503, detail="登录窗口必须通过 HTTPS 打开")


def _login_response(
    request: Request,
    session: dict[str, Any],
) -> dict[str, Any]:
    status = str(session.get("status") or "pending")
    public_token = str(session.get("public_token") or "")
    return {
        "sessionId": str(session.get("session_id") or ""),
        "method": "browser",
        "status": status
        if status in {"pending", "connected", "expired", "error"}
        else "pending",
        "message": str(session.get("message") or "等待你完成登录"),
        "qrImage": "",
        "loginUrl": (
            f"{_public_origin(request)}/executor-login#{public_token}"
            if public_token
            else ""
        ),
        "expiresAt": _expires_at(float(session.get("expires_at") or 0)),
    }


@router.get("/v1/health")
def health(runtime: ExecutorRuntime = Depends(_runtime_with_auth)):
    return {
        "service": "netdisk-sync-executor",
        "protocolVersion": "2.0",
        "version": "2.0.0",
        "instanceId": runtime.store.instance_id(),
        "capabilities": CAPABILITIES,
        "accounts": [
            _account_payload(account) for account in runtime.store.list_accounts()
        ],
        "settings": runtime.store.get_settings(),
    }


@router.get("/v1/accounts")
def accounts(runtime: ExecutorRuntime = Depends(_runtime_with_auth)):
    return {
        "accounts": [
            _account_payload(account) for account in runtime.store.list_accounts()
        ]
    }


@router.get("/v1/settings")
def get_settings(runtime: ExecutorRuntime = Depends(_runtime_with_auth)):
    return {"settings": runtime.store.get_settings()}


@router.put("/v1/settings")
async def put_settings(
    request: Request,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="设置格式无效")
    raw_settings = payload.get("settings", payload)
    if not isinstance(raw_settings, dict):
        raise HTTPException(status_code=400, detail="设置格式无效")
    return {"settings": runtime.store.update_settings(raw_settings)}


@router.post("/v1/auth/start")
def auth_start(
    body: AuthStart,
    request: Request,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    provider = _provider(body.provider)
    try:
        session = runtime.start_login(provider)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _login_response(request, session)


@router.post("/v1/auth/poll")
def auth_poll(
    body: AuthPoll,
    request: Request,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    provider = _provider(body.provider)
    try:
        session = runtime.poll_login(provider, body.sessionId)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _login_response(request, session)


@router.post("/v1/resources/register")
def register_resources(
    body: ResourceBatch,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    settings = runtime.store.get_settings()
    accepted, rejected = runtime.store.register_resources(
        body.resources[:500],
        interval_seconds=settings_interval_seconds(settings),
    )
    if accepted:
        runtime.scheduler.notify()
    return {
        "accepted": accepted,
        "rejected": rejected + max(0, len(body.resources) - 500),
        "message": "资源已登记，首次转存和定时监控已进入持久化队列",
    }


@router.post("/v1/resources/status")
def resource_status(
    body: ResourceStatusRequest,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    keys = list(dict.fromkeys(body.resourceKeys[:500]))
    return {
        "resources": [
            _resource_payload(resource)
            for resource in runtime.store.get_resources(keys)
        ]
    }


def _queue_command(
    body: ResourceCommand,
    action: str,
    runtime: ExecutorRuntime,
) -> dict[str, Any]:
    provider = _provider(body.provider)
    settings = runtime.store.get_settings()
    accepted, _ = runtime.store.register_resources(
        [
            {
                "resourceKey": body.resourceKey,
                "provider": provider,
                "title": body.title,
                "sourceUrl": body.sourceUrl,
                "sourceCode": body.sourceCode,
                "targetFolder": body.targetFolder,
                "currentShareUrl": body.currentShareUrl,
                "monitorEnabled": body.monitorEnabled,
            }
        ],
        interval_seconds=settings_interval_seconds(settings),
    )
    if not accepted or not runtime.scheduler.enqueue(body.resourceKey, action):
        raise HTTPException(status_code=500, detail="任务进入队列失败")
    resource = runtime.store.get_resource(body.resourceKey)
    payload = _resource_payload(resource or {})
    payload["message"] = (
        "转存任务已进入持久化队列"
        if action == "transfer"
        else "更新检查已进入持久化队列"
    )
    return payload


@router.post("/v1/transfer")
def transfer(
    body: ResourceCommand,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    return _queue_command(body, "transfer", runtime)


@router.post("/v1/check")
def check(
    body: ResourceCommand,
    runtime: ExecutorRuntime = Depends(_runtime_with_auth),
):
    return _queue_command(body, "check", runtime)


@router.post("/v1/maintenance/run")
def maintenance_run(
    body: MaintenanceRequest,
    runtime: ExecutorRuntime = Depends(_runtime_with_maintenance_auth),
):
    return {
        "ok": True,
        "persistence": runtime.store.backend,
        **runtime.scheduler.run_due_and_wait(body.waitSeconds),
    }


def _public_login_token(request: Request) -> str:
    token = request.headers.get("x-login-token", "").strip()
    if len(token) < 24:
        raise HTTPException(status_code=401, detail="登录窗口令牌无效")
    return token


@router.get("/executor-login", response_class=HTMLResponse)
def login_page():
    source_url = os.getenv(
        "EXECUTOR_SOURCE_URL",
        "https://github.com/OzoO0/cloud-auto-save-x",
    ).strip()
    if not source_url.startswith("https://"):
        source_url = "https://github.com/OzoO0/cloud-auto-save-x"
    return HTMLResponse(
        LOGIN_PAGE.replace("__SOURCE_URL__", escape(source_url, quote=True)),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' blob:; "
                "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@router.get("/executor-login/status")
def login_status(request: Request):
    runtime = get_runtime()
    try:
        session = runtime.session_from_public_token(_public_login_token(request))
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "provider": str(session.get("provider") or ""),
        "status": str(session.get("status") or "pending"),
        "message": str(session.get("message") or ""),
        "browserReady": runtime.browsers.has_session(
            str(session.get("session_id") or "")
        ),
        "expiresAt": _expires_at(float(session.get("expires_at") or 0)),
    }


@router.get("/executor-login/screenshot")
def login_screenshot(request: Request):
    runtime = get_runtime()
    try:
        image = runtime.screenshot(_public_login_token(request))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        image,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/executor-login/control")
def login_control(body: LoginControl, request: Request):
    runtime = get_runtime()
    try:
        runtime.control(
            _public_login_token(request),
            body.action,
            body.model_dump(exclude_none=True),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


LOGIN_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>网盘官方登录</title>
  <style>
    :root{font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif;color:#22223a;background:#f6f5fb}
    *{box-sizing:border-box}body{margin:0}.shell{max-width:1120px;margin:0 auto;padding:24px}
    .card{background:#fff;border:1px solid #e7e4f0;border-radius:20px;box-shadow:0 16px 50px #322d5b16;overflow:hidden}
    header{display:flex;gap:14px;align-items:center;padding:20px 24px;border-bottom:1px solid #eeeaf5}
    .mark{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#eee9ff;color:#7048f4;font-size:22px}
    h1{font-size:18px;margin:0 0 4px}p{margin:0;color:#77738a;font-size:13px;line-height:1.6}
    .notice{margin:18px 24px 0;padding:12px 14px;border-radius:12px;background:#f5f1ff;color:#5942a8;font-size:13px}
    .screen-wrap{margin:18px 24px;background:#171622;border-radius:14px;min-height:400px;display:grid;place-items:center;overflow:hidden;position:relative}
    #screen{display:block;max-width:100%;height:auto;cursor:crosshair;user-select:none}
    #loading{position:absolute;color:#d6d1e8;font-size:14px;line-height:1.7;text-align:center;max-width:78%}.tools{display:flex;gap:10px;flex-wrap:wrap;padding:0 24px 24px}
    input{min-width:260px;flex:1;border:1px solid #dcd7e8;border-radius:10px;padding:11px 12px;font:inherit}
    button{border:1px solid #dcd7e8;background:#fff;color:#403b52;border-radius:10px;padding:10px 14px;cursor:pointer}
    button.primary{background:#6d4aff;border-color:#6d4aff;color:#fff}.status{padding:0 24px 20px;font-size:13px;color:#656075}
    @media(max-width:640px){.shell{padding:0}.card{border-radius:0;min-height:100vh}.screen-wrap{margin:14px 12px}.tools{padding:0 12px 20px}header{padding:16px}}
  </style>
</head>
<body>
  <main class="shell"><section class="card">
    <header><div class="mark">☁</div><div><h1>在官方网盘页面完成登录</h1><p>优先用网盘 App 扫码；完成后窗口会自动确认并关闭登录会话。</p></div></header>
    <div class="notice">这是你自己的短时受保护窗口。执行器只加密保存登录会话，不会把账号信息返回管理站。</div>
    <div class="screen-wrap"><span id="loading">正在安全打开官方登录页面…</span><img id="screen" alt="官方网盘登录页面"></div>
    <div class="tools">
      <input id="text" type="password" autocomplete="off" placeholder="仅在官方页面需要输入时使用">
      <button id="send" class="primary">输入到当前框</button>
      <button data-key="Tab">Tab</button><button data-key="Enter">Enter</button>
      <button data-key="Backspace">退格</button><button id="refresh">刷新页面</button>
    </div>
    <div class="status" id="status">正在建立登录会话…</div>
    <div class="status">本执行器按 AGPL-3.0 提供且不附带担保；<a href="__SOURCE_URL__" target="_blank" rel="noreferrer">查看对应源码</a>。</div>
  </section></main>
  <script>
    (() => {
      const token = decodeURIComponent(location.hash.slice(1));
      history.replaceState(null, "", location.pathname);
      const headers = {"x-login-token": token};
      const screen = document.querySelector("#screen");
      const loading = document.querySelector("#loading");
      const status = document.querySelector("#status");
      let stopped = false, imageUrl = "";
      let browserReady = false, screenFailures = 0;
      const json = async (url, options={}) => {
        const response = await fetch(url, {...options, headers:{...headers,...(options.headers||{})}, cache:"no-store"});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || data.message || "请求失败");
        return data;
      };
      const control = (payload) => json("/executor-login/control", {
        method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(payload)
      });
      async function updateStatus(){
        if(!token){status.textContent="登录窗口令牌缺失，请从管理站重新打开";stopped=true;return}
        try{
          const data=await json("/executor-login/status");
          status.textContent=data.message || "等待登录";
          browserReady=Boolean(data.browserReady);
          if(data.status==="pending" && !browserReady){
            loading.style.display="block";
            loading.textContent="正在启动安全登录浏览器，请稍候…";
          }
          if(data.status!=="pending"){
            stopped=true;
            loading.style.display="block";
            loading.textContent=data.status==="connected"
              ?"登录成功，可以关闭此窗口"
              :(data.message || "本次登录未完成，请返回管理页重试");
          }
        }catch(error){status.textContent=error.message}
      }
      async function updateScreen(){
        if(stopped || !browserReady)return;
        try{
          const response=await fetch("/executor-login/screenshot",{headers,cache:"no-store"});
          if(!response.ok){
            const data=await response.json().catch(() => ({}));
            throw new Error(data.detail || "暂时无法读取登录画面");
          }
          const blob=await response.blob();
          if(imageUrl)URL.revokeObjectURL(imageUrl);
          imageUrl=URL.createObjectURL(blob);screen.src=imageUrl;
          screenFailures=0;loading.style.display="none";
        }catch(error){
          screenFailures+=1;loading.style.display="block";
          loading.textContent=screenFailures>2
            ?`${error.message}。正在自动重试…`
            :"正在读取官方网盘登录画面…";
        }
      }
      screen.addEventListener("click", async (event) => {
        const rect=screen.getBoundingClientRect();
        const x=(event.clientX-rect.left)*screen.naturalWidth/rect.width;
        const y=(event.clientY-rect.top)*screen.naturalHeight/rect.height;
        await control({action:"click",x,y});setTimeout(updateScreen,250);
      });
      document.querySelector("#send").addEventListener("click", async () => {
        const field=document.querySelector("#text");await control({action:"text",text:field.value});field.value="";setTimeout(updateScreen,250);
      });
      document.querySelectorAll("[data-key]").forEach(button => button.addEventListener("click", async () => {
        await control({action:"key",key:button.dataset.key});setTimeout(updateScreen,250);
      }));
      document.querySelector("#refresh").addEventListener("click", async () => {
        await control({action:"refresh"});setTimeout(updateScreen,500);
      });
      updateStatus().then(updateScreen);
      setInterval(updateStatus,1500);setInterval(updateScreen,2000);
    })();
  </script>
</body>
</html>"""
