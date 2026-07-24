from __future__ import annotations

import html
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pipeline_app as base

app = base.app

DEFAULT_WPS_LINK = "https://www.kdocs.cn/l/cdvRaPxIV0yl"


def init_wps_share_settings() -> None:
    with app.connect_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            ("wps_browser_cookie", ""),
        )
        conn.commit()


def request_bytes(url: str, cookie: str = "", referer: str = "") -> tuple[bytes, str, str]:
    headers = {
        "User-Agent": app.QUARK_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as res:
        return (
            res.read(),
            str(res.headers.get("Content-Type") or ""),
            str(res.geturl() or url),
        )


def decode_page(data: bytes) -> str:
    return data.decode("utf-8", "ignore")


def unescape_url(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\\u002F", "/").replace("\\/", "/")
    return value.strip().strip('"\'').replace("&amp;", "&")


def download_from_payload(data: bytes, content_type: str, cookie: str, referer: str) -> bytes | None:
    if data[:2] == b"PK":
        return data
    text = decode_page(data)
    candidates: list[str] = []

    try:
        payload = json.loads(text)
    except Exception:
        payload = None

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and (
                    key.lower() in {
                        "url", "download_url", "downloadurl", "file_url",
                        "fileurl", "export_url", "exporturl"
                    }
                    or ".xlsx" in item.lower()
                ):
                    candidates.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if payload is not None:
        walk(payload)

    patterns = [
        r'https?:\\?/\\?/[^"\'<>\s]+?\.xlsx(?:\?[^"\'<>\s]+)?',
        r'https?://[^"\'<>\s]+?\.xlsx(?:\?[^"\'<>\s]+)?',
        r'"(?:download_url|downloadUrl|file_url|fileUrl|export_url|exportUrl)"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            candidates.append(match if isinstance(match, str) else match[0])

    seen: set[str] = set()
    for candidate in candidates:
        url = unescape_url(candidate)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        try:
            body, ctype, _ = request_bytes(url, cookie, referer)
            if body[:2] == b"PK":
                return body
            if "application/vnd.openxmlformats" in ctype and body:
                return body
        except Exception:
            continue
    return None


def extract_ids(text: str) -> tuple[str, str]:
    drive_id = ""
    file_id = ""
    drive_patterns = [
        r'"drive_id"\s*:\s*"([^"]+)"',
        r'"driveId"\s*:\s*"([^"]+)"',
        r'\bdrive_id=([A-Za-z0-9_-]+)',
    ]
    file_patterns = [
        r'"file_id"\s*:\s*"([^"]+)"',
        r'"fileId"\s*:\s*"([^"]+)"',
        r'"fileid"\s*:\s*"([^"]+)"',
        r'\bfile_id=([A-Za-z0-9_-]+)',
    ]
    for pattern in drive_patterns:
        match = re.search(pattern, text)
        if match:
            drive_id = match.group(1)
            break
    for pattern in file_patterns:
        match = re.search(pattern, text)
        if match:
            file_id = match.group(1)
            break
    return drive_id, file_id


def fetch_wps_share_bytes(source: dict[str, Any]) -> bytes:
    url = str(source.get("source_url") or "").strip()
    if not re.match(r"^https?://(?:www\.)?kdocs\.cn/", url):
        raise RuntimeError("请输入正确的 kdocs.cn WPS分享链接")

    cookie = app.get_setting("wps_browser_cookie", "").strip()
    data, content_type, final_url = request_bytes(url, cookie)
    direct = download_from_payload(data, content_type, cookie, final_url)
    if direct is not None:
        return direct

    text = decode_page(data)
    lower = (text[:20000] + final_url).lower()
    if (
        "passport" in final_url.lower()
        or "account.wps" in final_url.lower()
        or ("登录" in text[:5000] and "sheet" not in lower)
    ):
        raise RuntimeError(
            "这份WPS文档需要登录。请在本页面的“WPS登录凭证”中保存一次浏览器 Cookie，"
            "凭证只保存在你自己的后台数据库。"
        )

    drive_id, file_id = extract_ids(text)
    endpoints: list[str] = []
    if file_id:
        qid = urllib.parse.quote(file_id, safe="")
        endpoints.extend([
            f"https://www.kdocs.cn/api/v3/office/file/{qid}/download",
            f"https://www.kdocs.cn/api/v3/office/file/{qid}/download?format=xlsx",
            f"https://www.kdocs.cn/api/v3/office/file/{qid}/export?format=xlsx",
        ])
    if drive_id and file_id:
        qd = urllib.parse.quote(drive_id, safe="")
        qf = urllib.parse.quote(file_id, safe="")
        endpoints.extend([
            f"https://www.kdocs.cn/api/v7/drives/{qd}/files/{qf}/download",
            f"https://openapi.wps.cn/v7/drives/{qd}/files/{qf}/download",
        ])

    for endpoint in endpoints:
        try:
            body, ctype, resolved = request_bytes(endpoint, cookie, final_url)
            downloaded = download_from_payload(body, ctype, cookie, resolved)
            if downloaded is not None:
                return downloaded
        except Exception:
            continue

    raise RuntimeError(
        "WPS页面已打开，但暂时没有取得XLSX下载地址。请确认登录凭证有效、"
        "文档允许查看；点击测试读取后，后台会保留明确错误信息。"
    )


_original_fetch_source_bytes = base.fetch_source_bytes


def fetch_source_bytes(source: dict[str, Any]) -> bytes:
    if str(source.get("source_type") or "") == "wps_share":
        return fetch_wps_share_bytes(source)
    return _original_fetch_source_bytes(source)


base.fetch_source_bytes = fetch_source_bytes

PIPELINE_PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WPS自动同步流水线</title>
<style>
:root{--p:#4f46e5;--ok:#059669;--bg:#f4f6fb;--bd:#e5e7eb;--warn:#b45309}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:#182033}
header{padding:25px 5vw;color:white;background:linear-gradient(135deg,#17203a,#4f46e5)}
main{max-width:1380px;margin:20px auto;padding:0 18px}.card{background:#fff;border:1px solid var(--bd);border-radius:16px;padding:18px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.field{display:flex;flex-direction:column;gap:6px}
input,textarea{padding:11px;border:1px solid #d8dce7;border-radius:10px;font-size:14px}textarea{min-height:90px;resize:vertical}
.btn{border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:700;text-decoration:none;display:inline-block}.primary{background:var(--p);color:#fff}.success{background:var(--ok);color:#fff}.secondary{background:#eef0f7;color:#30384c}.danger{background:#fee2e2;color:#991b1b}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 7px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}.wrap{overflow:auto}.muted{color:#6b7280}.url{max-width:280px;word-break:break-all}.status{padding:12px;border-radius:10px;background:#eef2ff;margin-top:10px;white-space:pre-wrap}.notice{padding:12px;border-radius:10px;background:#fff7ed;color:#9a3412;margin:12px 0}
details{margin-top:14px;border-top:1px solid var(--bd);padding-top:12px}summary{cursor:pointer;font-weight:700}
</style></head><body>
<header><h1>WPS共享资源自动流水线</h1><p>只填写WPS分享链接，自动识别表格、导入百度与夸克资源并执行后续流水线。</p></header>
<main>
<div class="card">
<h3>添加WPS共享数据源</h3>
<div class="notice">这份文档属于别人且需要登录。Render运行在云端，不能直接读取你电脑Chrome的登录状态；首次需要把WPS浏览器Cookie保存到你自己的后台。Cookie不会显示在列表中。</div>
<div class="grid">
<div class="field"><label>名称</label><input id="sname" value="影视每日更新" placeholder="例如：影视每日更新"></div>
<div class="field"><label>WPS分享链接</label><input id="surl" value="https://www.kdocs.cn/l/cdvRaPxIV0yl" placeholder="https://www.kdocs.cn/l/..."></div>
<div class="field"><label>扫描周期（分钟）</label><input id="interval" type="number" value="60" min="5"></div>
</div>
<details><summary>首次登录授权：保存WPS登录凭证</summary>
<p class="muted">在你自己的浏览器中打开WPS并登录后，从开发者工具 Network 任意 kdocs.cn 请求中复制 Request Headers 的 Cookie 值，粘贴到这里。只保存在你的后台，不会回显。</p>
<div class="field"><label>WPS登录 Cookie</label><textarea id="wpscookie" placeholder="只粘贴 Cookie 值，不要发到聊天中"></textarea></div>
<div class="actions"><button class="btn secondary" onclick="saveCookie()">保存登录凭证</button><span id="authstate" class="muted"></span></div>
</details>
<div class="actions">
<button class="btn primary" onclick="addSource()">保存数据源</button>
<button class="btn success" onclick="testLatest()">测试读取最新数据源</button>
<button class="btn success" onclick="runAll()">一键扫描全部并自动转存</button>
<a class="btn secondary" href="/api/pipeline/export.xlsx">下载最新资源Excel</a>
<a class="btn secondary" href="/">返回原控制台</a>
</div>
<div id="state" class="status">正在读取状态…</div>
</div>

<div class="card"><h3>WPS数据源</h3><div class="wrap"><table><thead><tr><th>名称</th><th>分享链接</th><th>周期</th><th>最后扫描</th><th>结果</th><th>操作</th></tr></thead><tbody id="sources"></tbody></table></div></div>
<div class="card"><h3>影视资源库（无年份）</h3><div class="wrap"><table><thead><tr><th>封面/剧名</th><th>评分/类型</th><th>百度我的链接</th><th>夸克我的链接</th><th>更新</th><th>资源站</th></tr></thead><tbody id="media"></tbody></table></div></div>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
let sourceList=[];
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});let j;try{j=await r.json()}catch(_){throw new Error('后台没有返回JSON')}if(!j.success)throw new Error(j.message||'操作失败');return j}
async function load(){try{const d=(await api('/api/pipeline')).data;sourceList=d.sources||[];state.textContent=`${d.state.message||'等待操作'}　最后完成：${d.state.last_run||'-'}`;
sources.innerHTML=sourceList.length?sourceList.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td class="url">${esc(x.source_url||'-')}</td><td>${esc(x.interval_minutes)}分钟</td><td>${esc(x.last_scan||'-')}</td><td>${esc(x.last_message||'-')}</td><td><button class="btn success" onclick="scan(${x.id})">测试/扫描</button></td></tr>`).join(''):'<tr><td colspan="6">暂无数据源</td></tr>';
media.innerHTML=d.media.length?d.media.map(x=>`<tr><td>${x.poster_url?`<img src="${esc(x.poster_url)}" style="width:55px;height:76px;object-fit:cover;border-radius:6px">`:''}<br><b>${esc(x.title)}</b></td><td>豆瓣：${esc(x.douban_score||'待确认')}<br>${esc(x.category||'未分类')}</td><td class="url">${x.baidu.url?`<a target="_blank" href="${esc(x.baidu.url)}">${esc(x.baidu.url)}</a>`:'尚未生成'}</td><td class="url">${x.quark.url?`<a target="_blank" href="${esc(x.quark.url)}">${esc(x.quark.url)}</a>`:'尚未生成'}</td><td>${esc(x.update_text)}</td><td>${esc(x.publish_status||'未发布')}<br><button class="btn primary" onclick="publishItem(${x.id},${x.site_resource_id?'true':'false'})">${x.site_resource_id?'只更新标题/说明':'首次发布'}</button></td></tr>`).join(''):'<tr><td colspan="6">暂无资源</td></tr>';
const a=(await api('/api/pipeline/wps-auth')).data;authstate.textContent=a.configured?'已保存登录凭证':'尚未保存登录凭证'}catch(e){state.textContent=e.message}}
async function saveCookie(){try{const value=wpscookie.value.trim();if(!value)throw new Error('请粘贴Cookie值');await api('/api/pipeline/wps-auth',{method:'POST',body:JSON.stringify({cookie:value})});wpscookie.value='';authstate.textContent='已保存登录凭证'}catch(e){alert(e.message)}}
async function addSource(){try{const link=surl.value.trim();if(!/^https?:\/\/(www\.)?kdocs\.cn\//i.test(link))throw new Error('请输入正确的WPS分享链接');const r=await api('/api/pipeline/sources',{method:'POST',body:JSON.stringify({name:sname.value||'影视每日更新',source_type:'wps_share',source_url:link,drive_id:'',file_id:'',interval_minutes:Number(interval.value||60)})});alert(r.message);await load()}catch(e){alert(e.message)}}
async function testLatest(){if(!sourceList.length){await addSource();await load()}if(sourceList.length)await scan(sourceList[0].id)}
async function scan(id){try{state.textContent='正在读取WPS表格，请稍候…';const r=await api(`/api/pipeline/sources/${id}/scan`,{method:'POST',body:'{}'});alert(r.message);await load()}catch(e){state.textContent=e.message;alert(e.message)}}
async function runAll(){try{const r=await api('/api/pipeline/run-all',{method:'POST',body:'{}'});alert(r.message);setTimeout(load,1200)}catch(e){alert(e.message)}}
async function publishItem(id,progress){try{alert((await api(`/api/pipeline/media/${id}/publish`,{method:'POST',body:JSON.stringify({progress_only:progress})})).message);load()}catch(e){alert(e.message)}}
load();setInterval(load,10000);
</script></body></html>"""

base.PIPELINE_PAGE = PIPELINE_PAGE


class PipelineShareHandler(base.PipelineHandler):
    server_version = "NetdiskPipeline/6.0"

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/pipeline/wps-auth":
            if self.auth_required():
                return
            configured = bool(app.get_setting("wps_browser_cookie", "").strip())
            return self.send_json({"success": True, "data": {"configured": configured}})
        return super().do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/pipeline/wps-auth":
            if self.auth_required():
                return
            payload = self.read_json()
            cookie = str(payload.get("cookie") or "").strip()
            if not cookie:
                return self.send_json({"success": False, "message": "Cookie不能为空"}, 400)
            app.set_setting("wps_browser_cookie", cookie)
            return self.send_json({"success": True, "message": "WPS登录凭证已保存"})
        return super().do_POST()


def main() -> None:
    base.init_pipeline_schema()
    app.seed_settings_from_env()
    base.seed_pipeline_settings_from_env()
    init_wps_share_settings()
    for worker_no in range(1, app.IMPORT_WORKERS + 1):
        threading.Thread(
            target=app.import_worker_loop,
            args=(worker_no,),
            daemon=True,
        ).start()
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    threading.Thread(target=base.source_monitor_loop, daemon=True).start()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    server = ThreadingHTTPServer((host, port), PipelineShareHandler)
    print(f"WPS分享链接自动流水线运行在 http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
