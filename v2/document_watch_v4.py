# V4: public shared-document change detection, manual delta apply, and browser notifications.
import ipaddress
import socket
import urllib.error
import urllib.request

DOCUMENT_WATCH_LOCK = threading.Lock()


def _watch_key(item: dict[str, Any]) -> str:
    return f"{str(item.get('platform') or '').strip()}|{str(item.get('source_url') or '').strip()}"


def _watch_clean_record(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": normalize_title(str(item.get("title") or "未命名资源")),
        "platform": str(item.get("platform") or "").strip(),
        "source_url": clean_url(str(item.get("source_url") or "")),
        "source_code": str(item.get("source_code") or "").strip()[:16],
    }


def _watch_snapshot(records: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for raw in records:
        item = _watch_clean_record(raw)
        if item["platform"] in SUPPORTED_PLATFORMS and item["source_url"]:
            output[_watch_key(item)] = item
    return output


def _watch_json_load(value: Any) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(str(value or "{}"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _watch_validate_public_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("共享文档地址必须是公开的 http/https 链接")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("不允许读取本机或局域网地址")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"共享文档域名无法解析：{exc}") from exc
    for entry in addresses:
        address = entry[4][0].split("%", 1)[0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("共享文档地址指向内网，已阻止访问")
    return value


def _watch_json_records(data: bytes) -> list[dict[str, str]]:
    payload = json.loads(data.decode("utf-8-sig"))
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("JSON 文档必须是数组，或包含 items 数组")
    records: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("name") or row.get("剧名") or row.get("电视剧名字") or row.get("资源名称")
        url = row.get("source_url") or row.get("url") or row.get("link") or row.get("网盘链接") or row.get("原链接")
        if not url:
            continue
        platform = row.get("platform") or platform_from_url(str(url))
        if platform not in SUPPORTED_PLATFORMS:
            platform = platform_from_url(str(url))
        if not platform:
            continue
        records.append({
            "title": normalize_title(str(title or "未命名资源")),
            "platform": platform,
            "source_url": clean_url(str(url)),
            "source_code": str(row.get("source_code") or row.get("code") or row.get("提取码") or "").strip(),
        })
    return dedupe_records(records)


def _watch_fetch_records(url: str) -> list[dict[str, str]]:
    safe_url = _watch_validate_public_url(url)
    request = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "Mozilla/5.0 NetdiskDocumentWatch/4.0",
            "Accept": "text/csv,application/json,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            final_url = _watch_validate_public_url(response.geturl())
            content_type = str(response.headers.get("Content-Type") or "").lower()
            disposition = str(response.headers.get("Content-Disposition") or "")
            data = response.read(25_000_001)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"共享文档读取失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"共享文档读取失败：{exc.reason}") from exc
    if len(data) > 25_000_000:
        raise ValueError("共享文档不能超过 25MB")
    filename = Path(urllib.parse.urlsplit(final_url).path).name or "shared.txt"
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    if match:
        filename = urllib.parse.unquote(match.group(1).strip())
    suffix = Path(filename).suffix.lower()
    if "text/html" in content_type or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("该链接返回的是网页，不是文档数据。请使用公开的 CSV/Excel 导出链接")
    if "json" in content_type or suffix == ".json":
        records = _watch_json_records(data)
    elif "spreadsheetml" in content_type or suffix == ".xlsx":
        records = parse_xlsx(data)
    elif "wordprocessingml" in content_type or suffix == ".docx":
        records = parse_docx(data)
    elif "csv" in content_type or suffix == ".csv":
        records = parse_csv_bytes(data)
    else:
        records = parse_uploaded_file(filename if suffix in {".txt", ".md", ".log"} else "shared.txt", data)
    if not records:
        raise ValueError("共享文档中没有识别到受支持的网盘链接")
    return records


def _watch_diff(old: dict[str, dict[str, str]], new: dict[str, dict[str, str]], allow_removed: bool = True) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(new.keys() - old.keys()):
        changes.append({"type": "added", "key": key, "old": None, "new": new[key]})
    for key in sorted(old.keys() & new.keys()):
        before = old[key]
        after = new[key]
        fields = [name for name in ("title", "source_code") if str(before.get(name) or "") != str(after.get(name) or "")]
        if fields:
            changes.append({"type": "modified", "key": key, "old": before, "new": after, "fields": fields})
    if allow_removed:
        for key in sorted(old.keys() - new.keys()):
            changes.append({"type": "removed", "key": key, "old": old[key], "new": None})
    return changes


def _watch_db_snapshot() -> dict[str, dict[str, str]]:
    with connect_db() as conn:
        rows = conn.execute("SELECT title,platform,source_url,source_code FROM resources").fetchall()
    return _watch_snapshot([dict(row) for row in rows])


def _watch_settings_row() -> dict[str, Any]:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM document_watch WHERE id=1").fetchone()
    return dict(row) if row else {
        "id": 1,
        "source_url": "",
        "enabled": False,
        "interval_minutes": 15,
        "applied_snapshot": "{}",
        "pending_snapshot": "{}",
        "pending_count": 0,
        "last_checked_at": None,
        "last_changed_at": None,
        "last_error": "",
    }


def _watch_changes_rows() -> list[dict[str, Any]]:
    with connect_db() as conn:
        rows = conn.execute("SELECT id,change_type,resource_key,old_data,new_data,detected_at FROM document_watch_changes ORDER BY id").fetchall()
    output = []
    for row in rows:
        item = dict(row)
        for field in ("old_data", "new_data"):
            try:
                item[field] = json.loads(item[field]) if item[field] else None
            except Exception:
                item[field] = None
        output.append(item)
    return output


def document_watch_state() -> dict[str, Any]:
    row = _watch_settings_row()
    changes = _watch_changes_rows()
    return {
        "source_url": row.get("source_url") or "",
        "enabled": bool(row.get("enabled")),
        "interval_minutes": int(row.get("interval_minutes") or 15),
        "pending_count": len(changes),
        "last_checked_at": row.get("last_checked_at"),
        "last_changed_at": row.get("last_changed_at"),
        "last_error": row.get("last_error") or "",
        "changes": changes,
    }


def save_document_watch_config(source_url: str, enabled: bool, interval_minutes: int) -> dict[str, Any]:
    source_url = str(source_url or "").strip()
    if source_url:
        _watch_validate_public_url(source_url)
    interval_minutes = max(15, min(int(interval_minutes or 15), 10080))
    with connect_db() as conn:
        old = conn.execute("SELECT source_url FROM document_watch WHERE id=1").fetchone()
        changed_url = bool(old) and str(old["source_url"] or "") != source_url
        conn.execute(
            """
            INSERT INTO document_watch(id,source_url,enabled,interval_minutes,updated_at)
            VALUES(1,%s,%s,%s,NOW())
            ON CONFLICT(id) DO UPDATE SET source_url=EXCLUDED.source_url,enabled=EXCLUDED.enabled,
                interval_minutes=EXCLUDED.interval_minutes,updated_at=NOW()
            """,
            (source_url, enabled, interval_minutes),
        )
        if changed_url:
            conn.execute("UPDATE document_watch SET applied_snapshot='{}',pending_snapshot='{}',pending_count=0,last_error='' WHERE id=1")
            conn.execute("DELETE FROM document_watch_changes")
        conn.commit()
    return document_watch_state()


def check_document_watch(force: bool = False) -> dict[str, Any]:
    if not DOCUMENT_WATCH_LOCK.acquire(blocking=False):
        raise RuntimeError("共享文档正在检查，请稍后刷新")
    try:
        row = _watch_settings_row()
        source_url = str(row.get("source_url") or "").strip()
        if not source_url:
            raise ValueError("请先填写共享文档公开导出链接")
        if not force and not bool(row.get("enabled")):
            return document_watch_state()
        records = _watch_fetch_records(source_url)
        current = _watch_snapshot(records)
        applied = _watch_json_load(row.get("applied_snapshot"))
        first_check = not applied
        if first_check:
            database_snapshot = _watch_db_snapshot()
            baseline = {key: database_snapshot[key] for key in current if key in database_snapshot}
        else:
            baseline = applied
        changes = _watch_diff(baseline, current, allow_removed=not first_check)
        now_changed = bool(changes)
        with connect_db() as conn:
            conn.execute("DELETE FROM document_watch_changes")
            for change in changes:
                conn.execute(
                    "INSERT INTO document_watch_changes(change_type,resource_key,old_data,new_data) VALUES(%s,%s,%s,%s)",
                    (
                        change["type"],
                        change["key"],
                        json.dumps(change.get("old"), ensure_ascii=False) if change.get("old") is not None else "",
                        json.dumps(change.get("new"), ensure_ascii=False) if change.get("new") is not None else "",
                    ),
                )
            if changes:
                conn.execute(
                    "UPDATE document_watch SET applied_snapshot=%s,pending_snapshot=%s,pending_count=%s,last_checked_at=NOW(),last_changed_at=NOW(),last_error='',updated_at=NOW() WHERE id=1",
                    (json.dumps(baseline, ensure_ascii=False), json.dumps(current, ensure_ascii=False), len(changes)),
                )
            else:
                conn.execute(
                    "UPDATE document_watch SET applied_snapshot=%s,pending_snapshot=%s,pending_count=0,last_checked_at=NOW(),last_error='',updated_at=NOW() WHERE id=1",
                    (json.dumps(current, ensure_ascii=False), json.dumps(current, ensure_ascii=False)),
                )
            conn.commit()
        if now_changed:
            counts = {kind: sum(1 for item in changes if item["type"] == kind) for kind in ("added", "modified", "removed")}
            log_event("warning", "document-watch", f"共享文档发现变化：新增 {counts['added']}，修改 {counts['modified']}，删除 {counts['removed']}")
        return document_watch_state()
    except Exception as exc:
        try:
            with connect_db() as conn:
                conn.execute("UPDATE document_watch SET last_checked_at=NOW(),last_error=%s,updated_at=NOW() WHERE id=1", (str(exc)[:1000],))
                conn.commit()
        except Exception:
            pass
        raise
    finally:
        DOCUMENT_WATCH_LOCK.release()


def _run_document_selected_sync(resource_ids: list[int]) -> None:
    ids = sorted({int(value) for value in resource_ids if int(value) > 0})
    if not ids or not SYNC_LOCK.acquire(blocking=False):
        return
    try:
        placeholders = ",".join(["%s"] * len(ids))
        with connect_db() as conn:
            rows = conn.execute(f"SELECT * FROM resources WHERE id IN ({placeholders}) AND enabled=TRUE ORDER BY id", ids).fetchall()
        set_job_state(running=True, message="正在同步共享文档变化", started_at=iso(now_utc()), finished_at=None,
                      trigger="document-watch", total=len(rows), done=0, success=0, failed=0)
        success_count = 0
        failed_count = 0
        for index, resource in enumerate(rows, 1):
            ok = sync_one(resource)
            success_count += int(ok)
            failed_count += int(not ok)
            set_job_state(done=index, success=success_count, failed=failed_count)
        set_job_state(running=False, message=f"变化同步完成：成功 {success_count}，失败 {failed_count}", finished_at=iso(now_utc()))
    except Exception as exc:
        set_job_state(running=False, message=f"变化同步异常：{exc}", finished_at=iso(now_utc()))
        log_event("error", "document-watch-sync", str(exc), details=traceback.format_exc())
    finally:
        SYNC_LOCK.release()


def apply_document_watch_changes() -> dict[str, Any]:
    if DOCUMENT_WATCH_LOCK.locked():
        raise RuntimeError("共享文档正在检查，请稍后再同步")
    row = _watch_settings_row()
    pending_snapshot = _watch_json_load(row.get("pending_snapshot"))
    changes = _watch_changes_rows()
    if not changes:
        return {"added": 0, "modified": 0, "removed": 0, "transfer_count": 0, "started": False}
    counts = {"added": 0, "modified": 0, "removed": 0}
    transfer_ids: list[int] = []
    with connect_db() as conn:
        for change in changes:
            kind = change["change_type"]
            before = change.get("old_data") or {}
            after = change.get("new_data") or {}
            counts[kind] += 1
            if kind in {"added", "modified"}:
                existing = conn.execute(
                    "SELECT id,title,source_code FROM resources WHERE platform=%s AND source_url=%s",
                    (after.get("platform"), after.get("source_url")),
                ).fetchone()
                needs_transfer = kind == "added" or str(before.get("source_code") or "") != str(after.get("source_code") or "")
                conn.execute(
                    """
                    INSERT INTO resources(title,platform,source_url,source_code,interval_minutes,enabled,status,next_run,updated_at)
                    VALUES(%s,%s,%s,%s,%s,TRUE,'pending',NOW(),NOW())
                    ON CONFLICT(platform,source_url) DO UPDATE SET title=EXCLUDED.title,source_code=EXCLUDED.source_code,
                        interval_minutes=EXCLUDED.interval_minutes,enabled=TRUE,
                        status=CASE WHEN %s THEN 'pending' ELSE resources.status END,
                        next_run=CASE WHEN %s THEN NOW() ELSE resources.next_run END,updated_at=NOW()
                    """,
                    (
                        after.get("title") or "未命名资源",
                        after.get("platform"),
                        after.get("source_url"),
                        after.get("source_code") or "",
                        int(row.get("interval_minutes") or DEFAULT_INTERVAL),
                        needs_transfer,
                        needs_transfer,
                    ),
                )
                resource = conn.execute(
                    "SELECT id FROM resources WHERE platform=%s AND source_url=%s",
                    (after.get("platform"), after.get("source_url")),
                ).fetchone()
                if needs_transfer and resource:
                    transfer_ids.append(int(resource["id"]))
            elif kind == "removed":
                conn.execute(
                    "DELETE FROM resources WHERE platform=%s AND source_url=%s",
                    (before.get("platform"), before.get("source_url")),
                )
        conn.execute(
            "UPDATE document_watch SET applied_snapshot=%s,pending_snapshot=%s,pending_count=0,last_error='',updated_at=NOW() WHERE id=1",
            (json.dumps(pending_snapshot, ensure_ascii=False), json.dumps(pending_snapshot, ensure_ascii=False)),
        )
        conn.execute("DELETE FROM document_watch_changes")
        conn.commit()
    log_event("info", "document-watch-apply", f"已应用共享文档变化：新增 {counts['added']}，修改 {counts['modified']}，删除 {counts['removed']}")
    started = False
    if transfer_ids:
        thread = threading.Thread(target=_run_document_selected_sync, args=(transfer_ids,), daemon=True, name="document-watch-sync")
        thread.start()
        started = True
    return {**counts, "transfer_count": len(set(transfer_ids)), "started": started}


def _watch_check_if_due() -> None:
    try:
        row = _watch_settings_row()
        if not row.get("enabled") or not row.get("source_url"):
            return
        last_checked = row.get("last_checked_at")
        due = not last_checked or last_checked <= now_utc() - timedelta(minutes=int(row.get("interval_minutes") or 15))
        if due:
            check_document_watch(force=False)
    except Exception as exc:
        log_event("error", "document-watch", str(exc), details=traceback.format_exc())


def _watch_check_if_due_async() -> None:
    threading.Thread(target=_watch_check_if_due, daemon=True, name="document-watch-check").start()


_base_init_db_v4 = init_db


def init_db() -> None:
    _base_init_db_v4()
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_watch(
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                interval_minutes INTEGER NOT NULL DEFAULT 15,
                applied_snapshot TEXT NOT NULL DEFAULT '{}',
                pending_snapshot TEXT NOT NULL DEFAULT '{}',
                pending_count INTEGER NOT NULL DEFAULT 0,
                last_checked_at TIMESTAMPTZ,
                last_changed_at TIMESTAMPTZ,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_watch_changes(
                id BIGSERIAL PRIMARY KEY,
                change_type TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                old_data TEXT NOT NULL DEFAULT '',
                new_data TEXT NOT NULL DEFAULT '',
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute("INSERT INTO document_watch(id) VALUES(1) ON CONFLICT(id) DO NOTHING")
        conn.commit()


_base_start_sync_job_v4 = start_sync_job


def start_sync_job(trigger: str, resource_id: int | None = None, due_only: bool = False) -> bool:
    if trigger == "github-actions":
        _watch_check_if_due_async()
    return _base_start_sync_job_v4(trigger, resource_id, due_only)


_base_handler_get_v4 = Handler.do_GET
_base_handler_post_v4 = Handler.do_POST


def _watch_do_GET(self) -> None:
    path = urllib.parse.urlsplit(self.path).path
    if path == "/api/document-watch":
        if not self.require_auth():
            return
        try:
            self.send_json({"success": True, "state": document_watch_state()})
        except Exception as exc:
            self.send_json({"success": False, "message": str(exc)}, 500)
        return
    return _base_handler_get_v4(self)


def _watch_do_POST(self) -> None:
    path = urllib.parse.urlsplit(self.path).path
    if path in {"/api/document-watch/config", "/api/document-watch/check", "/api/document-watch/apply"}:
        if not self.require_auth():
            return
        try:
            if path == "/api/document-watch/config":
                data = self.read_json()
                state = save_document_watch_config(
                    str(data.get("source_url") or ""),
                    bool(data.get("enabled")),
                    int(data.get("interval_minutes") or 15),
                )
                self.send_json({"success": True, "message": "共享文档监听配置已保存", "state": state})
            elif path == "/api/document-watch/check":
                state = check_document_watch(force=True)
                self.send_json({"success": True, "message": f"识别完成，发现 {state['pending_count']} 项变化", "state": state})
            else:
                result = apply_document_watch_changes()
                self.send_json({
                    "success": True,
                    "message": f"已应用：新增 {result['added']}，修改 {result['modified']}，删除 {result['removed']}；待转存 {result['transfer_count']}",
                    "result": result,
                })
        except ValueError as exc:
            self.send_json({"success": False, "message": str(exc)}, 400)
        except Exception as exc:
            log_event("error", "document-watch-api", str(exc), details=traceback.format_exc())
            self.send_json({"success": False, "message": str(exc)}, 500)
        return
    return _base_handler_post_v4(self)


Handler.do_GET = _watch_do_GET
Handler.do_POST = _watch_do_POST


_WATCH_CARD = r'''
<section class="card">
<h3>④ 共享文档变化监听</h3>
<div class="notice">填写共享文档的公开 CSV、XLSX、DOCX、TXT 或 JSON 导出地址。系统只检测变化并通知；点击“只同步变化数据”后，才会新增转存、修改名称或移除已删除记录。删除记录不会删除你网盘里已经转存的文件。</div>
<div class="field"><label>共享文档公开导出链接</label><input id="watchSourceUrl" placeholder="https://.../export.csv"></div>
<div class="field"><label>监听周期</label><select id="watchInterval"><option value="15">每15分钟</option><option value="30">每30分钟</option><option value="60">每小时</option><option value="180">每3小时</option><option value="360">每6小时</option><option value="720">每12小时</option><option value="1440">每天</option></select></div>
<label class="field" style="display:flex;flex-direction:row;align-items:center;gap:8px"><input id="watchEnabled" type="checkbox" style="width:auto"><span>启用定时监听（GitHub Actions 唤醒时检查）</span></label>
<div class="actions"><button class="btn secondary" onclick="saveWatchConfig()">保存监听配置</button><button class="btn primary" onclick="checkWatchNow()">重新识别变化</button><button class="btn ok" id="applyWatchButton" onclick="applyWatchChanges()">只同步变化数据</button><button class="btn secondary" onclick="enableWatchNotifications()">开启浏览器通知</button><a class="btn secondary" href="/api/export.csv">导出最新CSV</a></div>
<div id="watchState" class="status" style="margin-top:12px">正在读取共享文档状态…</div>
<div class="table-wrap" style="margin-top:12px"><table style="min-width:760px"><thead><tr><th>变化</th><th>电视剧/动漫</th><th>平台</th><th>具体修改</th></tr></thead><tbody id="watchChanges"><tr><td colspan="4" class="muted">暂无待处理变化</td></tr></tbody></table></div>
</section>
'''

_WATCH_SCRIPT = r'''
<script>
const watchEsc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function watchApi(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});let j;try{j=await r.json()}catch(e){throw new Error('后台没有返回JSON')}if(!r.ok||!j.success)throw new Error(j.message||'操作失败');return j}
function watchChangeText(x){const old=x.old_data||{},now=x.new_data||{};if(x.change_type==='added')return '新增资源，将自动转存并生成新链接';if(x.change_type==='removed')return '文档已删除：系统记录和CSV将移除，网盘文件保留';const parts=[];if(old.title!==now.title)parts.push(`名称：${old.title||'-'} → ${now.title||'-'}`);if((old.source_code||'')!==(now.source_code||''))parts.push('提取码发生变化，将重新转存');return parts.join('；')||'内容发生变化'}
async function loadWatchState(){try{const j=await watchApi('/api/document-watch');const x=j.state||{};const url=document.getElementById('watchSourceUrl');if(document.activeElement!==url)url.value=x.source_url||'';document.getElementById('watchEnabled').checked=!!x.enabled;document.getElementById('watchInterval').value=String(x.interval_minutes||15);const count=Number(x.pending_count||0);document.getElementById('watchState').textContent=`${count?`发现 ${count} 项变化，等待手动同步`:'当前没有待处理变化'}\n最后检查：${x.last_checked_at||'-'}\n最后发现变化：${x.last_changed_at||'-'}${x.last_error?`\n检查错误：${x.last_error}`:''}`;document.getElementById('applyWatchButton').disabled=!count;document.getElementById('watchChanges').innerHTML=(x.changes||[]).map(c=>{const d=c.new_data||c.old_data||{};const label={added:'新增',modified:'修改',removed:'删除'}[c.change_type]||c.change_type;return `<tr><td><span class="tag">${watchEsc(label)}</span></td><td><b>${watchEsc(d.title||'未命名资源')}</b></td><td>${watchEsc(platformNames[d.platform]||d.platform||'-')}</td><td>${watchEsc(watchChangeText(c))}</td></tr>`}).join('')||'<tr><td colspan="4" class="muted">暂无待处理变化</td></tr>';const previous=Number(localStorage.getItem('watchPendingCount')||0);if(count>0&&count!==previous&&Notification.permission==='granted')new Notification('共享文档有新变化',{body:`发现 ${count} 项变化，请打开后台确认并同步。`});localStorage.setItem('watchPendingCount',String(count))}catch(e){document.getElementById('watchState').textContent=e.message}}
async function saveWatchConfig(){try{const j=await watchApi('/api/document-watch/config',{method:'POST',body:JSON.stringify({source_url:document.getElementById('watchSourceUrl').value,enabled:document.getElementById('watchEnabled').checked,interval_minutes:Number(document.getElementById('watchInterval').value)})});alert(j.message);loadWatchState()}catch(e){alert(e.message)}}
async function checkWatchNow(){try{const j=await watchApi('/api/document-watch/check',{method:'POST',body:'{}'});alert(j.message);loadWatchState()}catch(e){alert(e.message)}}
async function applyWatchChanges(){if(!confirm('确定只应用当前识别出的变化吗？\n\n新增项会开始转存；名称修改会更新数据库和CSV；删除项会移除系统记录，但不会删除网盘文件。'))return;try{const j=await watchApi('/api/document-watch/apply',{method:'POST',body:'{}'});alert(j.message);loadAll();loadWatchState()}catch(e){alert(e.message)}}
async function enableWatchNotifications(){if(!('Notification'in window)){alert('当前浏览器不支持通知');return}const p=await Notification.requestPermission();alert(p==='granted'?'浏览器通知已开启':'未获得通知权限')}
loadWatchState();setInterval(loadWatchState,10000);
</script>
'''

INDEX_HTML = INDEX_HTML.replace(
    '</div>\n<section class="card"><h3>资源对应表</h3>',
    _WATCH_CARD + '</div>\n<section class="card"><h3>资源对应表</h3>',
    1,
)
INDEX_HTML = INDEX_HTML.replace("</body></html>", _WATCH_SCRIPT + "\n</body></html>", 1)
