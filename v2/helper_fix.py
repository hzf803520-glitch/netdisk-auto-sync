# 覆盖主程序中的登录助手生成函数：使用 raw 模板，避免 \n 在生成阶段变成真实换行。
def helper_python(platform: str, code: str, base_url: str) -> str:
    login_url = SUPPORTED_PLATFORMS[platform]["login"]
    template = r'''#!/usr/bin/env python3
import json
import sys
import urllib.request
from playwright.sync_api import sync_playwright

BACKEND = __BACKEND__
PAIR_CODE = __PAIR_CODE__
PLATFORM = __PLATFORM__
LOGIN_URL = __LOGIN_URL__


def find_tokens(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if "refresh" in low and "token" in low and isinstance(item, str):
                found.setdefault("refresh_token", item)
            if "access" in low and "token" in low and isinstance(item, str):
                found.setdefault("access_token", item)
            find_tokens(item, found)
    elif isinstance(value, list):
        for item in value:
            find_tokens(item, found)
    elif isinstance(value, str) and value[:1] in "[{":
        try:
            find_tokens(json.loads(value), found)
        except Exception:
            pass


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120000)
    print("\n请在打开的浏览器中完成登录。登录成功后，回到这个终端窗口按回车。\n")
    input()
    cookies = context.cookies()
    cookie_text = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    payload = {"cookie": cookie_text}
    if PLATFORM == "xunlei":
        found = {}
        for pg in context.pages:
            try:
                values = pg.evaluate("Object.fromEntries(Object.entries(localStorage))")
                find_tokens(values, found)
            except Exception:
                pass
        find_tokens(cookies, found)
        payload.update(found)
    request_body = json.dumps({"code": PAIR_CODE, "platform": PLATFORM, "credentials": payload}).encode("utf-8")
    req = urllib.request.Request(
        BACKEND.rstrip("/") + "/api/helper/credential",
        data=request_body,
        headers={"Content-Type": "application/json", "User-Agent": "NetdiskLoginHelper/2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("success"):
            raise RuntimeError(result.get("message") or "后台拒绝保存")
        print("\n✅ 登录状态已安全保存到你的后台。可以关闭此窗口。\n")
    except Exception as exc:
        print("\n❌ 上传失败：", exc)
        print("请不要关闭窗口，截图这个错误返回给系统管理员。")
        input("按回车退出...")
        sys.exit(1)
    finally:
        browser.close()
'''
    return (
        template.replace("__BACKEND__", repr(base_url))
        .replace("__PAIR_CODE__", repr(code))
        .replace("__PLATFORM__", repr(platform))
        .replace("__LOGIN_URL__", repr(login_url))
    )


# 手动录入是默认绑定方式。凭证仍复用原有加密保存逻辑，保存在 Neon PostgreSQL，
# Render 休眠、重启或重新部署不会清空。这里只调整更明确的输入校验提示。
_save_credentials_encrypted = save_credentials


def save_credentials(platform: str, data: dict[str, Any], source: str) -> None:
    if platform in {"baidu", "quark", "uc"} and not str(data.get("cookie") or "").strip():
        raise ValueError("请粘贴完整 Cookie 后再保存")
    if platform == "xunlei" and not (
        str(data.get("refresh_token") or "").strip()
        or str(data.get("access_token") or "").strip()
    ):
        raise ValueError("请填写迅雷 Access Token 或 Refresh Token")
    _save_credentials_encrypted(platform, data, source)


# 将账号绑定页面改成“直接粘贴并持久化保存”。登录助手保留在折叠区域作为备用。
_old_css = 'summary{cursor:pointer;font-weight:700}.login-grid{display:grid;grid-template-columns:repeat(4,minmax(170px,1fr));gap:10px}@media(max-width:850px){.login-grid{grid-template-columns:1fr 1fr}header h1{font-size:23px}}'
_new_css = 'summary{cursor:pointer;font-weight:700}.login-grid{display:grid;grid-template-columns:repeat(4,minmax(170px,1fr));gap:10px}.credential-grid{display:grid;grid-template-columns:repeat(2,minmax(280px,1fr));gap:12px;margin-top:12px}.credential-card{border:1px solid var(--line);border-radius:13px;padding:14px;background:#fafbff}.credential-card h4{margin:0 0 5px}.credential-status{min-height:22px;margin-bottom:7px}.saved-badge{display:inline-block;padding:3px 8px;border-radius:99px;background:#d1fadf;color:#05603a;font-weight:700}.empty-badge{display:inline-block;padding:3px 8px;border-radius:99px;background:#f2f4f7;color:#667085}.cookie-input{min-height:92px}.persist-note{margin-top:8px;padding:10px;border-radius:9px;background:#ecfdf3;color:#05603a;font-size:13px}@media(max-width:850px){.login-grid,.credential-grid{grid-template-columns:1fr}header h1{font-size:23px}}'

_old_account = '''<section class="card">
<h3>② 绑定你的网盘账号</h3>
<div class="notice">点击平台按钮下载登录助手。解压后双击“开始登录.command”，在真实浏览器完成登录；系统会自动保存登录状态，不需要手动查找 Cookie。</div>
<div class="login-grid" id="loginGrid"></div>
<details><summary>迅雷令牌或高级手动绑定</summary><div class="field"><label>平台</label><select id="manualPlatform"><option value="baidu">百度</option><option value="quark">夸克</option><option value="uc">UC</option><option value="xunlei">迅雷</option></select></div><div class="field"><label>Cookie（百度/夸克/UC）</label><textarea id="manualCookie" placeholder="仅保存在加密数据库，不会回显"></textarea></div><div class="field"><label>迅雷 Refresh Token</label><input id="refreshToken"></div><div class="field"><label>迅雷 Access Token</label><input id="accessToken"></div><button class="btn secondary" onclick="saveManualCredential()">保存凭证</button></details>
</section>'''

_new_account = '''<section class="card">
<h3>② 手动保存你的网盘登录凭证</h3>
<div class="notice"><b>只需要保存一次。</b> 百度、夸克、UC 直接粘贴完整 Cookie；迅雷填写 Access Token 或 Refresh Token。凭证会加密保存到 Neon，不保存在 Render 临时磁盘。</div>
<div class="persist-note">✅ 保存成功后，即使 Render 休眠、重启或重新部署，凭证仍会保留。出于安全考虑，页面不会把已保存的 Cookie/Token 再显示出来。</div>
<div class="credential-grid">
<div class="credential-card"><h4>百度网盘</h4><div id="status-baidu" class="credential-status"><span class="empty-badge">正在读取…</span></div><div class="field"><label>完整 Cookie</label><textarea id="credential-baidu" class="cookie-input" spellcheck="false" autocomplete="off" placeholder="粘贴百度网盘 Cookie"></textarea></div><button class="btn primary" onclick="saveCookieCredential('baidu')">保存百度 Cookie</button></div>
<div class="credential-card"><h4>夸克网盘</h4><div id="status-quark" class="credential-status"><span class="empty-badge">正在读取…</span></div><div class="field"><label>完整 Cookie</label><textarea id="credential-quark" class="cookie-input" spellcheck="false" autocomplete="off" placeholder="粘贴夸克网盘 Cookie"></textarea></div><button class="btn primary" onclick="saveCookieCredential('quark')">保存夸克 Cookie</button></div>
<div class="credential-card"><h4>UC 网盘</h4><div id="status-uc" class="credential-status"><span class="empty-badge">正在读取…</span></div><div class="field"><label>完整 Cookie</label><textarea id="credential-uc" class="cookie-input" spellcheck="false" autocomplete="off" placeholder="粘贴 UC 网盘 Cookie"></textarea></div><button class="btn primary" onclick="saveCookieCredential('uc')">保存 UC Cookie</button></div>
<div class="credential-card"><h4>迅雷云盘</h4><div id="status-xunlei" class="credential-status"><span class="empty-badge">正在读取…</span></div><div class="field"><label>Access Token</label><input id="xunlei-access-token" type="password" autocomplete="off" placeholder="填写 Access Token"></div><div class="field"><label>Refresh Token（可选）</label><input id="xunlei-refresh-token" type="password" autocomplete="off" placeholder="填写 Refresh Token"></div><button class="btn primary" onclick="saveXunleiCredential()">保存迅雷 Token</button></div>
</div>
<details><summary>备用方式：下载网页登录助手</summary><p class="muted">手动 Cookie 无法使用时，再点击平台按钮下载登录助手。</p><div class="login-grid" id="helperGrid"></div></details>
</section>'''

_old_functions = '''async function loadCredentials(){const j=await api('/api/credentials');document.getElementById('loginGrid').innerHTML=Object.keys(platformNames).map(p=>{const c=j.items.find(x=>x.platform===p);return `<button class="btn ${c?'ok':'secondary'}" onclick="pair('${p}')">${esc(platformNames[p])}<br><small>${c?'已绑定 '+esc(c.updated_at||''):'点击登录绑定'}</small></button>`}).join('')}
async function pair(platform){try{const j=await api('/api/pair',{method:'POST',body:JSON.stringify({platform})});window.location.href=j.download_url;toast('登录助手已下载。请解压后双击“开始登录.command”。')}catch(e){toast(e.message)}}
async function saveManualCredential(){try{const p=document.getElementById('manualPlatform').value;const credentials={cookie:document.getElementById('manualCookie').value,refresh_token:document.getElementById('refreshToken').value,access_token:document.getElementById('accessToken').value};await api('/api/credentials/'+p,{method:'POST',body:JSON.stringify({credentials})});document.getElementById('manualCookie').value='';document.getElementById('refreshToken').value='';document.getElementById('accessToken').value='';toast('保存成功');loadCredentials()}catch(e){toast(e.message)}}'''

_new_functions = '''async function loadCredentials(){const j=await api('/api/credentials');const saved=Object.fromEntries(j.items.map(x=>[x.platform,x]));Object.keys(platformNames).forEach(p=>{const el=document.getElementById('status-'+p);if(!el)return;const c=saved[p];el.innerHTML=c?`<span class="saved-badge">已加密保存</span> <span class="muted">${esc(c.updated_at||'')} · ${esc(c.source==='manual'?'手动录入':'登录助手')}</span>`:'<span class="empty-badge">尚未保存</span>'});const helper=document.getElementById('helperGrid');if(helper)helper.innerHTML=Object.keys(platformNames).map(p=>`<button class="btn secondary" onclick="pair('${p}')">${esc(platformNames[p])}登录助手</button>`).join('')}
async function pair(platform){try{const j=await api('/api/pair',{method:'POST',body:JSON.stringify({platform})});window.location.href=j.download_url;toast('登录助手已下载。请解压后双击“开始登录.command”。')}catch(e){toast(e.message)}}
async function saveCookieCredential(platform){try{const field=document.getElementById('credential-'+platform);const cookie=field.value.trim();if(!cookie)throw new Error('请先粘贴完整 Cookie');await api('/api/credentials/'+platform,{method:'POST',body:JSON.stringify({credentials:{cookie}})});field.value='';toast((platformNames[platform]||platform)+' Cookie 已加密保存，重新部署后仍会保留');await loadCredentials()}catch(e){toast(e.message)}}
async function saveXunleiCredential(){try{const access=document.getElementById('xunlei-access-token').value.trim();const refresh=document.getElementById('xunlei-refresh-token').value.trim();if(!access&&!refresh)throw new Error('请填写 Access Token 或 Refresh Token');await api('/api/credentials/xunlei',{method:'POST',body:JSON.stringify({credentials:{access_token:access,refresh_token:refresh}})});document.getElementById('xunlei-access-token').value='';document.getElementById('xunlei-refresh-token').value='';toast('迅雷 Token 已加密保存，重新部署后仍会保留');await loadCredentials()}catch(e){toast(e.message)}}'''

for old, new, label in (
    (_old_css, _new_css, "账号页面样式"),
    (_old_account, _new_account, "账号绑定区域"),
    (_old_functions, _new_functions, "账号绑定脚本"),
):
    if old not in INDEX_HTML:
        raise RuntimeError(f"无法更新{label}：页面模板版本不匹配")
    INDEX_HTML = INDEX_HTML.replace(old, new, 1)
