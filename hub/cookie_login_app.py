from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from typing import Any

import pipeline_share_app as current

app = current.app

# All login credentials are encrypted through the existing COOKIE_ENCRYPTION_KEY
# before they are written to Neon. Existing plaintext values remain readable and
# will be encrypted the next time the user saves them.
app.SENSITIVE_SETTINGS.update(
    {
        "wps_browser_cookie",
        "uc_cookie",
        "xunlei_credential_json",
    }
)

ACCOUNT_DEFAULTS = {
    "uc_cookie": "",
    "uc_cookie_verified_at": "",
    "xunlei_credential_json": "",
    "xunlei_credential_verified_at": "",
}


def init_account_settings() -> None:
    with app.connect_db() as conn:
        for key, value in ACCOUNT_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )
        conn.commit()


def _tail(value: str, keep: int = 4) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return "••••" + value[-keep:]


def _xunlei_bundle(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("请粘贴迅雷凭证包")
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError
        data = {str(k).strip(): str(v).strip() for k, v in parsed.items() if v is not None}
    except (json.JSONDecodeError, ValueError):
        data: dict[str, str] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()

    aliases = {
        "refreshToken": "refresh_token",
        "deviceId": "device_id",
        "accessToken": "access_token",
        "captchaToken": "captcha_token",
    }
    for old, new in aliases.items():
        if old in data and new not in data:
            data[new] = data[old]

    missing = [key for key in ("refresh_token", "device_id") if not data.get(key)]
    if missing:
        raise RuntimeError("迅雷不能只使用普通 Cookie，请提供 refresh_token 和 device_id 凭证包")
    if len(data["device_id"]) < 8:
        raise RuntimeError("迅雷 device_id 格式不正确")
    return data


def validate_uc_cookie(cookie: str) -> str:
    cookie = str(cookie or "").strip()
    if not cookie:
        raise RuntimeError("UC Cookie 不能为空")
    result = app.request_json(
        "GET",
        "https://pc-api.uc.cn/1/clouddrive/config",
        params={"pr": "UCBrowser", "fr": "pc"},
        headers={
            "Cookie": cookie,
            "Referer": "https://drive.uc.cn/",
            "Origin": "https://drive.uc.cn",
        },
        timeout=45,
    )
    code = result.get("code")
    if code not in (None, 0, "0"):
        raise RuntimeError(result.get("message") or result.get("msg") or "UC Cookie 无效或已过期")
    return "UC Cookie 验证成功"


def account_data() -> dict[str, Any]:
    quark = app.get_setting("quark_cookie")
    baidu = app.get_setting("baidu_cookies")
    uc = app.get_setting("uc_cookie")
    xunlei_raw = app.get_setting("xunlei_credential_json")
    try:
        xunlei = json.loads(xunlei_raw) if xunlei_raw else {}
    except json.JSONDecodeError:
        xunlei = {}

    return {
        "storage": "Neon PostgreSQL（加密保存）" if app.DATABASE_URL else str(app.DB_PATH),
        "accounts": {
            "quark": {
                "name": "夸克网盘",
                "configured": bool(quark),
                "verified_at": app.get_setting("quark_cookie_verified_at"),
                "preview": app.cookie_preview(quark),
                "transfer_ready": True,
            },
            "baidu": {
                "name": "百度网盘",
                "configured": bool(baidu),
                "verified_at": app.get_setting("baidu_cookie_verified_at"),
                "preview": app.cookie_preview(baidu),
                "transfer_ready": True,
                "transfer_dir": app.get_setting("baidu_transfer_dir", "/资源数据"),
            },
            "uc": {
                "name": "UC网盘",
                "configured": bool(uc),
                "verified_at": app.get_setting("uc_cookie_verified_at"),
                "preview": app.cookie_preview(uc),
                "transfer_ready": False,
            },
            "xunlei": {
                "name": "迅雷云盘",
                "configured": bool(xunlei_raw),
                "verified_at": app.get_setting("xunlei_credential_verified_at"),
                "preview": "; ".join(
                    x
                    for x in (
                        "refresh_token=" + _tail(str(xunlei.get("refresh_token") or "")),
                        "device_id=" + _tail(str(xunlei.get("device_id") or "")),
                    )
                    if not x.endswith("=")
                ),
                "transfer_ready": False,
            },
        },
    }


ACCOUNT_PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>网盘登录凭证中心</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--text:#172033;--muted:#667085;--p:#4f46e5;--ok:#059669;--warn:#b45309;--bad:#dc2626;--bd:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}header{padding:28px 5vw 48px;color:#fff;background:linear-gradient(135deg,#17203a,#4f46e5)}header h1{margin:0 0 8px}.nav{display:flex;gap:9px;flex-wrap:wrap;margin-top:17px}.nav a{background:#fff;color:#3730a3;padding:9px 13px;border-radius:9px;text-decoration:none;font-weight:700}main{max-width:1200px;margin:-25px auto 45px;padding:0 18px}.intro,.card{background:#fff;border:1px solid var(--bd);border-radius:16px;padding:18px;box-shadow:0 10px 30px rgba(20,30,55,.07)}.intro{margin-bottom:15px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.head{display:flex;justify-content:space-between;gap:10px;align-items:center}.badge{padding:5px 9px;border-radius:99px;background:#eef0f7;color:#667085;font-size:12px}.badge.ok{background:#e9faf3;color:#08724e}.badge.warn{background:#fff7ed;color:#9a3412}.muted{color:var(--muted);font-size:13px;line-height:1.65}.notice{padding:11px 13px;border-radius:10px;background:#edf4ff;color:#24549d;font-size:13px;line-height:1.65;margin:12px 0}.notice.warn{background:#fff7ed;color:#9a3412}textarea,input{width:100%;border:1px solid var(--bd);border-radius:10px;padding:11px;font:inherit}textarea{min-height:125px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.field{margin:10px 0}.field label{display:block;color:var(--muted);font-size:13px;margin-bottom:5px}.actions{display:flex;gap:8px;flex-wrap:wrap}.btn{border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:700}.primary{background:var(--p);color:#fff}.secondary{background:#eef0f7;color:#30384c}.danger{background:#fff0f1;color:var(--bad)}.msg{margin-top:10px;font-size:13px;white-space:pre-wrap}.oktext{color:var(--ok)}.badtext{color:var(--bad)}@media(max-width:780px){.grid{grid-template-columns:1fr}}
</style></head><body><header><h1>🔐 网盘登录凭证中心</h1><p>不再在云端执行器里扫码或网页登录，直接粘贴登录凭证并加密保存。</p><div class="nav"><a href="/console">打开转存控制台</a><a href="/pipeline">打开WPS自动流水线</a></div></header><main>
<div class="intro"><b>安全说明</b><div class="muted">完整凭证不会回显，页面只显示脱敏摘要。凭证使用 COOKIE_ENCRYPTION_KEY 加密后保存到 Neon。请勿把 Cookie 或 Token 发到聊天中。</div><div id="storage" class="muted"></div></div>
<div class="grid">
<div class="card"><div class="head"><h3>🟠 夸克网盘</h3><span id="quarkBadge" class="badge">读取中</span></div><div class="notice">登录夸克网页版，按 F12 → Network → 任意请求 → Request Headers，复制 Cookie 的完整值。</div><div class="field"><label>夸克 Cookie</label><textarea id="quarkValue" placeholder="粘贴完整 Cookie；已经保存时可留空后点击验证"></textarea></div><div class="actions"><button class="btn primary" onclick="save('quark')">保存并验证</button><button class="btn secondary" onclick="save('quark',true)">验证已保存凭证</button><button class="btn danger" onclick="clearAccount('quark')">删除绑定</button></div><div id="quarkMsg" class="msg"></div></div>
<div class="card"><div class="head"><h3>🔵 百度网盘</h3><span id="baiduBadge" class="badge">读取中</span></div><div class="notice">Cookie 需要包含 BDUSS 和 STOKEN。登录百度网盘网页版后，从任意 pan.baidu.com 请求复制完整 Cookie。</div><div class="field"><label>百度 Cookie</label><textarea id="baiduValue" placeholder="粘贴完整 Cookie；已经保存时可留空后点击验证"></textarea></div><div class="field"><label>统一保存目录</label><input id="baiduDir" value="/资源数据"></div><div class="actions"><button class="btn primary" onclick="save('baidu')">保存并验证</button><button class="btn secondary" onclick="save('baidu',true)">验证已保存凭证</button><button class="btn danger" onclick="clearAccount('baidu')">删除绑定</button></div><div id="baiduMsg" class="msg"></div></div>
<div class="card"><div class="head"><h3>🟣 UC网盘</h3><span id="ucBadge" class="badge">读取中</span></div><div class="notice">登录 drive.uc.cn，按 F12 → Network，从任意请求复制 Cookie 完整值。</div><div class="field"><label>UC Cookie</label><textarea id="ucValue" placeholder="粘贴 UC Cookie；已经保存时可留空后点击验证"></textarea></div><div class="actions"><button class="btn primary" onclick="save('uc')">保存并验证</button><button class="btn secondary" onclick="save('uc',true)">验证已保存凭证</button><button class="btn danger" onclick="clearAccount('uc')">删除绑定</button></div><div id="ucMsg" class="msg"></div></div>
<div class="card"><div class="head"><h3>⚡ 迅雷云盘</h3><span id="xunleiBadge" class="badge">读取中</span></div><div class="notice warn">迅雷当前接口不能只靠普通网页 Cookie 稳定登录。请粘贴凭证 JSON，至少包含 refresh_token 和 device_id。</div><div class="field"><label>迅雷凭证包</label><textarea id="xunleiValue" placeholder='{"refresh_token":"...","device_id":"...","access_token":"可选"}'></textarea></div><div class="actions"><button class="btn primary" onclick="save('xunlei')">保存凭证包</button><button class="btn danger" onclick="clearAccount('xunlei')">删除绑定</button></div><div id="xunleiMsg" class="msg"></div></div>
</div></main><script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});let j;try{j=await r.json()}catch(_){throw new Error('后台没有返回 JSON')}if(!j.success)throw new Error(j.message||'操作失败');return j}
async function load(){try{const d=(await api('/api/accounts')).data;$('#storage').textContent='凭证存储：'+d.storage;for(const [key,a] of Object.entries(d.accounts)){const badge=$('#'+key+'Badge'),msg=$('#'+key+'Msg');badge.textContent=a.configured?'已绑定':'未绑定';badge.className='badge '+(a.configured?'ok':'');let extra=a.transfer_ready?'已接入自动转存':'当前仅完成凭证绑定，转存执行器尚未接入';msg.className='msg '+(a.configured?'oktext':'');msg.innerHTML=a.configured?`已保存：${esc(a.preview||'凭证已加密')}<br>验证时间：${esc(a.verified_at||'未记录')}<br>${esc(extra)}`:esc(extra);if(key==='baidu'&&a.transfer_dir)$('#baiduDir').value=a.transfer_dir}}catch(e){alert(e.message)}}
async function save(platform,stored=false){const msg=$('#'+platform+'Msg');msg.className='msg';msg.textContent='正在验证…';try{let body={};if(platform==='baidu')body={cookie:stored?'':$('#baiduValue').value,transfer_dir:$('#baiduDir').value};else if(platform==='xunlei')body={credential:$('#xunleiValue').value};else body={cookie:stored?'':$('#'+platform+'Value').value};const j=await api('/api/accounts/'+platform,{method:'POST',body:JSON.stringify(body)});msg.className='msg oktext';msg.textContent=j.message;const box=$('#'+platform+'Value');if(box)box.value='';await load()}catch(e){msg.className='msg badtext';msg.textContent=e.message}}
async function clearAccount(platform){if(!confirm('删除这个网盘的已保存登录凭证？'))return;try{await api('/api/accounts/'+platform,{method:'DELETE'});await load()}catch(e){alert(e.message)}}
load();
</script></body></html>'''


class CookieLoginHandler(current.PipelineShareHandler):
    server_version = "NetdiskCookieLogin/1.0"

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            if self.auth_required():
                return
            return self.send_bytes(ACCOUNT_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/console":
            if self.auth_required():
                return
            page = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
            page = current.base.share.inject_home(page)
            page = current.base.inject_pipeline_link(page)
            return self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/accounts":
            if self.auth_required():
                return
            return self.send_json({"success": True, "data": account_data()})
        if path == "/api/settings":
            if self.auth_required():
                return
            data = account_data()
            accounts = data["accounts"]
            return self.send_json(
                {
                    "success": True,
                    "data": {
                        "quark_cookie_configured": accounts["quark"]["configured"],
                        "baidu_cookie_configured": accounts["baidu"]["configured"],
                        "uc_cookie_configured": accounts["uc"]["configured"],
                        "xunlei_credential_configured": accounts["xunlei"]["configured"],
                        "quark_cookie_preview": accounts["quark"]["preview"],
                        "baidu_cookie_preview": accounts["baidu"]["preview"],
                        "uc_cookie_preview": accounts["uc"]["preview"],
                        "xunlei_credential_preview": accounts["xunlei"]["preview"],
                        "quark_cookie_verified_at": accounts["quark"]["verified_at"],
                        "baidu_cookie_verified_at": accounts["baidu"]["verified_at"],
                        "uc_cookie_verified_at": accounts["uc"]["verified_at"],
                        "xunlei_credential_verified_at": accounts["xunlei"]["verified_at"],
                        "baidu_transfer_dir": accounts["baidu"]["transfer_dir"],
                        "cookie_storage": data["storage"],
                        "baidu_binary_installed": True,
                    },
                }
            )
        return super().do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        match = re.fullmatch(r"/api/accounts/(quark|baidu|uc|xunlei)", path)
        if not match:
            return super().do_POST()
        if self.auth_required():
            return

        platform = match.group(1)
        payload = self.read_json()
        try:
            if platform == "quark":
                cookie = str(payload.get("cookie") or "").strip() or app.get_setting("quark_cookie")
                account = app.QuarkClient(cookie).account()
                app.set_setting("quark_cookie", cookie)
                app.set_setting("quark_cookie_verified_at", app.now_iso())
                message = f"夸克账号验证成功：{account.get('nickname') or '已登录'}"
            elif platform == "baidu":
                cookie = str(payload.get("cookie") or payload.get("cookies") or "").strip() or app.get_setting("baidu_cookies")
                transfer_dir = str(payload.get("transfer_dir") or app.get_setting("baidu_transfer_dir", "/资源数据")).strip() or "/资源数据"
                app.ensure_baidu_ready(cookie, transfer_dir)
                app.set_setting("baidu_cookies", cookie)
                app.set_setting("baidu_transfer_dir", transfer_dir)
                app.set_setting("baidu_cookie_verified_at", app.now_iso())
                message = "百度账号验证成功，Cookie 已加密保存"
            elif platform == "uc":
                cookie = str(payload.get("cookie") or "").strip() or app.get_setting("uc_cookie")
                message = validate_uc_cookie(cookie)
                app.set_setting("uc_cookie", cookie)
                app.set_setting("uc_cookie_verified_at", app.now_iso())
            else:
                raw = str(payload.get("credential") or "").strip() or app.get_setting("xunlei_credential_json")
                bundle = _xunlei_bundle(raw)
                app.set_setting("xunlei_credential_json", json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))
                app.set_setting("xunlei_credential_verified_at", app.now_iso())
                message = "迅雷凭证包格式验证成功并已加密保存"
            return self.send_json({"success": True, "message": message})
        except Exception as exc:
            app.add_log(None, "ERROR", f"{platform} 凭证绑定失败：{type(exc).__name__}: {exc}")
            return self.send_json({"success": False, "message": app.friendly_error_message(exc)}, 400)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        match = re.fullmatch(r"/api/accounts/(quark|baidu|uc|xunlei)", path)
        if not match:
            return super().do_DELETE()
        if self.auth_required():
            return
        platform = match.group(1)
        keys = {
            "quark": ("quark_cookie", "quark_cookie_verified_at"),
            "baidu": ("baidu_cookies", "baidu_cookie_verified_at"),
            "uc": ("uc_cookie", "uc_cookie_verified_at"),
            "xunlei": ("xunlei_credential_json", "xunlei_credential_verified_at"),
        }[platform]
        for key in keys:
            app.set_setting(key, "")
        return self.send_json({"success": True, "message": "登录凭证已删除"})


def main() -> None:
    current.base.init_pipeline_schema()
    app.seed_settings_from_env()
    current.base.seed_pipeline_settings_from_env()
    current.init_wps_share_settings()
    init_account_settings()
    for worker_no in range(1, app.IMPORT_WORKERS + 1):
        threading.Thread(
            target=app.import_worker_loop,
            args=(worker_no,),
            daemon=True,
        ).start()
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    threading.Thread(target=current.base.source_monitor_loop, daemon=True).start()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    server = ThreadingHTTPServer((host, port), CookieLoginHandler)
    print(f"网盘登录凭证中心运行在 http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
