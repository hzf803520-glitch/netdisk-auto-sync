from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer
from typing import Any

import app

SHARE_LOCK = threading.RLock()
SHARE_RUNNING: set[int] = set()
SHARE_BATCH = {"running": False, "message": ""}


def init_share_schema() -> None:
    with app.connect_db() as conn:
        app.add_column(conn, "tasks", "own_share_url TEXT DEFAULT ''")
        app.add_column(conn, "tasks", "own_passcode TEXT DEFAULT ''")
        app.add_column(conn, "tasks", "own_share_status TEXT DEFAULT ''")
        app.add_column(conn, "tasks", "own_share_error TEXT DEFAULT ''")
        app.add_column(conn, "tasks", "own_shared_at TEXT")
        conn.commit()


def random_passcode() -> str:
    return uuid.uuid4().hex[:4]


def set_share_state(
    task_id: int,
    status: str,
    url: str = "",
    passcode: str = "",
    error: str = "",
) -> None:
    with app.connect_db() as conn:
        conn.execute(
            """UPDATE tasks
               SET own_share_status=?,own_share_url=?,own_passcode=?,
                   own_share_error=?,own_shared_at=?
               WHERE id=?""",
            (
                status,
                url,
                passcode,
                error,
                app.now_iso() if url else None,
                task_id,
            ),
        )
        conn.commit()


def create_quark_share(task: app.Row) -> tuple[str, str]:
    client = app.QuarkClient(app.get_setting("quark_cookie"))
    target = "/" + str(task["save_path"] or "").strip("/")
    info = client.path_info(target)
    if not info:
        raise RuntimeError(f"夸克目标目录不存在：{target}")
    fid = str(info.get("fid") or "")
    if not fid:
        raise RuntimeError("夸克目标目录缺少文件ID")

    code = random_passcode()
    created = client.call(
        "POST",
        "/1/clouddrive/share",
        {"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        {
            "fid_list": [fid],
            "title": str(task["name"] or info.get("file_name") or "我的分享"),
            "url_type": 2,
            "expired_type": 1,
            "passcode": code,
        },
        timeout=90,
    )
    if created.get("code") != 0:
        raise RuntimeError(created.get("message") or "夸克创建分享任务失败")

    data = created.get("data") or {}
    share_id = str(data.get("share_id") or "")
    share_task_id = str(data.get("task_id") or "")
    if share_task_id and not share_id:
        finished = client.poll(share_task_id, timeout=120)
        result = finished.get("data") or {}
        share_id = str(
            result.get("share_id")
            or (result.get("save_as") or {}).get("share_id")
            or ""
        )
    if not share_id:
        raise RuntimeError("夸克分享任务未返回 share_id")

    published = client.call(
        "POST",
        "/1/clouddrive/share/password",
        {"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        {"share_id": share_id},
        timeout=60,
    )
    if published.get("code") != 0:
        raise RuntimeError(published.get("message") or "夸克发布分享链接失败")
    result = published.get("data") or {}
    url = str(result.get("share_url") or "").strip()
    code = str(result.get("passcode") or code).strip()
    if not url:
        raise RuntimeError("夸克没有返回分享链接")
    if code and "pwd=" not in url:
        url += ("&" if "?" in url else "?") + "pwd=" + urllib.parse.quote(code)
    return url, code


def create_baidu_share(task: app.Row) -> tuple[str, str]:
    client = app.BaiduDirectClient(app.get_setting("baidu_cookies"))
    target = app._bd_norm_remote(str(task["save_path"] or ""))
    if not client.path_item(target):
        raise RuntimeError(f"百度目标目录不存在：{target}")

    code = random_passcode()
    result = client.request(
        "POST",
        client.BASE + "/share/pset",
        params={
            "bdstoken": client.get_bdstoken(),
            "clienttype": "0",
            "channel": "chunlei",
            "web": "1",
            "app_id": app.BAIDU_APP_ID,
        },
        form={
            "path_list": json.dumps([target], ensure_ascii=False),
            "schannel": "4",
            "channel_list": "[]",
            "period": "0",
            "pwd": code,
            "share_type": "9",
        },
        referer=client.BASE + "/disk/main",
        timeout=90,
    )
    errno = int(result.get("errno", 0) or 0)
    if errno != 0:
        raise RuntimeError(f"百度创建分享失败，错误码 {errno}")
    url = str(
        result.get("link")
        or result.get("shorturl")
        or result.get("short_url")
        or ""
    ).strip()
    if not url:
        raise RuntimeError("百度创建成功，但未返回分享链接")
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    if "pwd=" not in url:
        url += ("&" if "?" in url else "?") + "pwd=" + urllib.parse.quote(code)
    return url, code


def generate_share(task_id: int, force: bool = False) -> dict[str, Any]:
    with SHARE_LOCK:
        if task_id in SHARE_RUNNING:
            return {"ok": False, "message": "该任务正在生成分享链接"}
        SHARE_RUNNING.add(task_id)
    try:
        task = app.task_row(task_id)
        if not task:
            raise RuntimeError("任务不存在")
        if task["own_share_url"] and not force:
            return {"ok": True, "message": "已有自己的分享链接", "url": task["own_share_url"]}

        set_share_state(task_id, "生成中")
        if task["platform"] == "quark":
            url, code = create_quark_share(task)
        elif task["platform"] == "baidu":
            url, code = create_baidu_share(task)
        else:
            raise RuntimeError("不支持的网盘类型")

        set_share_state(task_id, "已生成", url, code, "")
        app.add_log(task_id, "INFO", f"已生成自己的分享链接：{url}")
        return {"ok": True, "message": "分享链接生成成功", "url": url}
    except Exception as exc:
        message = app.friendly_error_message(exc)
        set_share_state(task_id, "失败", "", "", message)
        app.add_log(task_id, "ERROR", f"生成分享链接失败：{message}｜{type(exc).__name__}: {exc}")
        return {"ok": False, "message": message}
    finally:
        with SHARE_LOCK:
            SHARE_RUNNING.discard(task_id)


def batch_worker(force: bool) -> None:
    with SHARE_LOCK:
        if SHARE_BATCH["running"]:
            return
        SHARE_BATCH["running"] = True
        SHARE_BATCH["message"] = "正在准备批量生成"
    try:
        with app.connect_db() as conn:
            if force:
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE status<>'失败' ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id FROM tasks
                       WHERE status<>'失败'
                         AND COALESCE(own_share_url,'')=''
                       ORDER BY id"""
                ).fetchall()
        total = len(rows)
        for index, row in enumerate(rows, 1):
            SHARE_BATCH["message"] = f"{index}/{total}：处理任务 {row['id']}"
            generate_share(int(row["id"]), force=force)
            time.sleep(0.8)
        SHARE_BATCH["message"] = f"批量生成完成，共处理 {total} 个任务"
    except Exception:
        SHARE_BATCH["message"] = "批量生成失败"
        app.add_log(None, "ERROR", traceback.format_exc())
    finally:
        SHARE_BATCH["running"] = False


def after_transfer(original, task: app.Row, *args: Any, **kwargs: Any) -> str:
    result = original(task, *args, **kwargs)
    refreshed = app.task_row(int(task["id"]))
    if refreshed and not refreshed["own_share_url"]:
        shared = generate_share(int(task["id"]), force=False)
        if shared.get("ok") and shared.get("url"):
            result += "；已自动生成自己的分享链接"
        elif not shared.get("ok"):
            result += "；转存成功，但自动分享失败，可在分享链接中心重试"
    return result


_original_quark = app.apply_quark_task
_original_baidu = app.apply_baidu_task


def patched_quark(task: app.Row, folder_path: str | None = None) -> str:
    return after_transfer(_original_quark, task, folder_path)


def patched_baidu(task: app.Row) -> str:
    return after_transfer(_original_baidu, task)


app.apply_quark_task = patched_quark
app.apply_baidu_task = patched_baidu


def rows() -> list[dict[str, Any]]:
    with app.connect_db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT id,platform,name,share_url,passcode,save_path,status,
                          own_share_url,own_passcode,own_share_status,
                          own_share_error,own_shared_at
                   FROM tasks ORDER BY id DESC"""
            )
        ]


def export_csv() -> bytes:
    grouped: dict[str, dict[str, str]] = {}
    for row in rows():
        name = str(row.get("name") or "")
        target = grouped.setdefault(
            name,
            {
                "名称": name,
                "百度原链接": "",
                "百度新链接": "",
                "百度新提取码": "",
                "夸克原链接": "",
                "夸克新链接": "",
                "夸克新提取码": "",
            },
        )
        if row.get("platform") == "baidu":
            target["百度原链接"] = str(row.get("share_url") or "")
            target["百度新链接"] = str(row.get("own_share_url") or "")
            target["百度新提取码"] = str(row.get("own_passcode") or "")
        elif row.get("platform") == "quark":
            target["夸克原链接"] = str(row.get("share_url") or "")
            target["夸克新链接"] = str(row.get("own_share_url") or "")
            target["夸克新提取码"] = str(row.get("own_passcode") or "")

    fields = list(next(iter(grouped.values())).keys()) if grouped else [
        "名称", "百度原链接", "百度新链接", "百度新提取码",
        "夸克原链接", "夸克新链接", "夸克新提取码",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(grouped.values())
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


SHARE_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>我的分享链接</title>
<style>
:root{--p:#4f46e5;--ok:#059669;--bad:#dc2626;--bg:#f4f6fb;--bd:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:#182033}
header{padding:24px 5vw;color:#fff;background:linear-gradient(135deg,#17203a,#4f46e5)}
main{max-width:1320px;margin:20px auto;padding:0 18px}.card{background:#fff;border:1px solid var(--bd);border-radius:16px;padding:18px}
.head,.actions{display:flex;gap:9px;align-items:center;justify-content:space-between;flex-wrap:wrap}.btn{border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:650;text-decoration:none;display:inline-block}.primary{background:var(--p);color:#fff}.success{background:var(--ok);color:#fff}.warning{background:#fff2db;color:#965700}.secondary{background:#eef0f7;color:#30384c}.small{padding:7px 9px;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid var(--bd);vertical-align:top}.wrap{overflow:auto}.muted{color:#6b7280}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:#d97706}.tag{display:inline-block;padding:4px 8px;border-radius:99px;background:#eef0f7}.quark{background:#fff0d9;color:#945500}.baidu{background:#e7f0ff;color:#1559a0}.busy{padding:10px;background:#edf4ff;color:#24549d;border-radius:10px;margin:12px 0}.url{word-break:break-all;max-width:290px}
</style>
</head>
<body>
<header><h1>我的分享链接</h1><p>转存完成后自动生成属于你自己的百度和夸克分享链接。</p></header>
<main><div class="card">
<div class="head"><div><b>转存后新分享链接</b><div class="muted">原链接保留核对，导出文件按名称合并百度与夸克。</div></div>
<div class="actions"><a class="btn secondary" href="/">返回控制台</a><button class="btn success" onclick="allShare(false)">批量补齐</button><button class="btn warning" onclick="allShare(true)">全部重新生成</button><a class="btn primary" href="/api/own-shares/export.csv">导出覆盖后CSV</a></div></div>
<div id="batch"></div><div class="wrap"><table><thead><tr><th>平台/名称</th><th>保存目录</th><th>原链接</th><th>我的新链接</th><th>状态</th><th>操作</th></tr></thead><tbody id="rows"></tbody></table></div>
</div></main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});const j=await r.json();if(!j.success)throw new Error(j.message||'操作失败');return j}
async function load(){try{const d=(await api('/api/own-shares')).data;document.querySelector('#batch').innerHTML=d.batch.message?`<div class="busy">${esc(d.batch.message)}</div>`:'';document.querySelector('#rows').innerHTML=d.rows.length?d.rows.map(t=>{const c=t.own_share_status==='已生成'?'ok':t.own_share_status==='失败'?'bad':'warn';return `<tr><td><span class="tag ${t.platform}">${t.platform==='quark'?'夸克':'百度'}</span><br><b>${esc(t.name)}</b></td><td>${esc(t.save_path)}</td><td class="url"><a target="_blank" href="${esc(t.share_url)}">${esc(t.share_url)}</a></td><td class="url">${t.own_share_url?`<a target="_blank" href="${esc(t.own_share_url)}">${esc(t.own_share_url)}</a><br><span class="muted">提取码：${esc(t.own_passcode||'')}</span>`:'<span class="muted">尚未生成</span>'}</td><td class="${c}">${esc(t.own_share_status||'等待转存')}<br><span class="muted">${esc(t.own_share_error||t.own_shared_at||'')}</span></td><td><button class="btn success small" onclick="one(${t.id},${t.own_share_url?'true':'false'})">${t.own_share_url?'重新生成':'生成'}</button>${t.own_share_url?` <button class="btn secondary small" onclick="copy('${encodeURIComponent(t.own_share_url)}')">复制</button>`:''}</td></tr>`}).join(''):'<tr><td colspan="6" class="muted">暂无任务</td></tr>'}catch(e){alert(e.message)}}
async function one(id,force){if(force&&!confirm('旧链接不会自动删除，确定重新生成？'))return;try{alert((await api(`/api/tasks/${id}/reshare`,{method:'POST',body:JSON.stringify({force})})).message);setTimeout(load,1000)}catch(e){alert(e.message)}}
async function allShare(force){if(force&&!confirm('确定为全部任务重新生成？旧链接不会自动删除。'))return;try{alert((await api('/api/tasks/reshare-all',{method:'POST',body:JSON.stringify({force})})).message);setTimeout(load,1000)}catch(e){alert(e.message)}}
async function copy(v){const s=decodeURIComponent(v);try{await navigator.clipboard.writeText(s);alert('已复制')}catch(e){prompt('复制链接',s)}}
load();setInterval(load,10000);
</script></body></html>"""


def inject_home(page: str) -> str:
    if 'href="/shares"' in page:
        return page
    return page.replace(
        "</header>",
        '<div style="margin-top:14px"><a href="/shares" style="display:inline-block;background:#fff;color:#3730a3;padding:10px 15px;border-radius:10px;text-decoration:none;font-weight:700">打开我的分享链接中心</a></div></header>',
        1,
    )


class ExtendedHandler(app.Handler):
    server_version = "NetdiskSync/4.0"

    def send_bytes(self, body: bytes, content_type: str, filename: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if filename:
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
            )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            if self.auth_required():
                return
            page = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
            return self.send_bytes(inject_home(page).encode("utf-8"), "text/html; charset=utf-8")
        if path == "/shares":
            if self.auth_required():
                return
            return self.send_bytes(SHARE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/own-shares":
            if self.auth_required():
                return
            return self.send_json({"success": True, "data": {"rows": rows(), "batch": dict(SHARE_BATCH)}})
        if path == "/api/own-shares/export.csv":
            if self.auth_required():
                return
            return self.send_bytes(export_csv(), "text/csv; charset=utf-8", "网盘转存后新分享链接.csv")
        return super().do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        match = re.fullmatch(r"/api/tasks/(\d+)/reshare", path)
        if match:
            if self.auth_required():
                return
            payload = self.read_json()
            task_id = int(match.group(1))
            with SHARE_LOCK:
                if task_id in SHARE_RUNNING:
                    return self.send_json({"success": False, "message": "该任务正在生成"}, 409)
            threading.Thread(
                target=generate_share,
                args=(task_id, bool(payload.get("force"))),
                daemon=True,
            ).start()
            return self.send_json({"success": True, "message": "已开始生成自己的分享链接"})
        if path == "/api/tasks/reshare-all":
            if self.auth_required():
                return
            payload = self.read_json()
            with SHARE_LOCK:
                if SHARE_BATCH["running"]:
                    return self.send_json({"success": False, "message": "批量生成正在运行"}, 409)
            threading.Thread(
                target=batch_worker,
                args=(bool(payload.get("force")),),
                daemon=True,
            ).start()
            return self.send_json({"success": True, "message": "已开始批量生成"})
        return super().do_POST()


def main() -> None:
    app.init_db()
    init_share_schema()
    app.seed_settings_from_env()
    for worker_no in range(1, app.IMPORT_WORKERS + 1):
        threading.Thread(
            target=app.import_worker_loop,
            args=(worker_no,),
            daemon=True,
        ).start()
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    server = ThreadingHTTPServer((host, port), ExtendedHandler)
    print(f"网盘自动追更控制台（自动分享版）运行在 http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
