# V3 增量监控、目标目录和按更新同步覆盖层。
# 本文件在主程序所有定义完成后、main() 启动前加载。

_original_init_db = init_db
_original_do_get = Handler.do_GET
_original_do_post = Handler.do_POST


def _v3_safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', str(value or ''))
    value = re.sub(r'\s+', ' ', value).strip(' .')
    return value[:120] or '未命名资源'


def _v3_target_path(title: str) -> str:
    return '/资源/' + _v3_safe_name(title)


def init_db() -> None:
    _original_init_db()
    statements = [
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS target_path TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS source_title TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS source_snapshot TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS pending_snapshot TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS changes_json TEXT NOT NULL DEFAULT '{\"added\":[],\"modified\":[],\"deleted\":[]}'",
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS has_updates BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE resources ADD COLUMN IF NOT EXISTS last_check TIMESTAMPTZ",
        "UPDATE resources SET target_path='/资源/' || regexp_replace(title, '[\\/:*?\"<>|]+', ' ', 'g') WHERE target_path=''",
    ]
    with connect_db() as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


def _v3_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or '')
    except Exception:
        return fallback


def _v3_file_key(item: dict[str, Any]) -> str:
    return str(item.get('path') or item.get('name') or item.get('id') or '')


def _v3_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(item.get('id') or ''),
        int(item.get('size') or 0),
        str(item.get('updated_at') or ''),
        bool(item.get('is_dir')),
    )


def _v3_diff(old_items: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    old_map = {_v3_file_key(x): x for x in old_items if _v3_file_key(x)}
    new_map = {_v3_file_key(x): x for x in new_items if _v3_file_key(x)}
    added = [new_map[key] for key in sorted(new_map.keys() - old_map.keys())]
    deleted = [old_map[key] for key in sorted(old_map.keys() - new_map.keys())]
    modified = []
    for key in sorted(old_map.keys() & new_map.keys()):
        if _v3_signature(old_map[key]) != _v3_signature(new_map[key]):
            item = dict(new_map[key])
            item['previous_size'] = int(old_map[key].get('size') or 0)
            item['previous_updated_at'] = str(old_map[key].get('updated_at') or '')
            modified.append(item)
    return {'added': added, 'modified': modified, 'deleted': deleted}


def _v3_change_count(changes: dict[str, list[dict[str, Any]]]) -> int:
    return sum(len(changes.get(key) or []) for key in ('added', 'modified', 'deleted'))


def _v3_summary(changes: dict[str, list[dict[str, Any]]]) -> str:
    return f"新增 {len(changes.get('added') or [])}，修改 {len(changes.get('modified') or [])}，删除 {len(changes.get('deleted') or [])}"


def _v3_load_resource(resource_id: int) -> dict[str, Any] | None:
    with connect_db() as conn:
        return conn.execute('SELECT * FROM resources WHERE id=%s', (resource_id,)).fetchone()


def _v3_inspect(resource: dict[str, Any]) -> dict[str, Any]:
    credentials = get_credentials(resource['platform'])
    if not credentials:
        raise RuntimeError(f"尚未绑定{SUPPORTED_PLATFORMS[resource['platform']]['name']}账号")
    return worker_call({
        'action': 'inspect',
        'url': resource['source_url'],
        'code': resource['source_code'],
        'credentials': credentials,
    }, timeout=150)


def _v3_check_one(resource: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    resource_id = int(resource['id'])
    try:
        result = _v3_inspect(resource)
        files = result.get('files') or []
        source_title = str(result.get('title') or resource.get('source_title') or resource['title'])
        old_items = _v3_json(resource.get('source_snapshot'), [])
        changes = _v3_diff(old_items, files)
        has_updates = _v3_change_count(changes) > 0
        target_path = str(resource.get('target_path') or '').strip()
        if not target_path:
            root_dirs = [x for x in files if not str(x.get('path') or '').strip('/').count('/') and x.get('is_dir')]
            root_name = root_dirs[0].get('name') if len(root_dirs) == 1 and len(files) >= 1 else source_title
            target_path = _v3_target_path(str(root_name or resource['title']))
        message = ('发现更新：' + _v3_summary(changes)) if has_updates else '检查完成：没有变化'
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE resources SET source_title=%s,target_path=%s,pending_snapshot=%s,changes_json=%s,
                    has_updates=%s,last_check=NOW(),last_message=%s,status=%s,updated_at=NOW()
                WHERE id=%s
                """,
                (
                    source_title,
                    target_path,
                    json.dumps(files, ensure_ascii=False, separators=(',', ':')),
                    json.dumps(changes, ensure_ascii=False, separators=(',', ':')),
                    has_updates,
                    message,
                    'update_available' if has_updates else 'no_change',
                    resource_id,
                ),
            )
            conn.commit()
        log_event('info', 'check', message, resource_id, json.dumps(changes, ensure_ascii=False))
        return has_updates, changes
    except Exception as exc:
        message = f'检查失败：{exc}'
        with connect_db() as conn:
            conn.execute(
                "UPDATE resources SET status='check_failed',last_message=%s,last_check=NOW(),updated_at=NOW() WHERE id=%s",
                (message[:1000], resource_id),
            )
            conn.commit()
        log_event('error', 'check', message, resource_id, traceback.format_exc())
        raise


def _v3_parent_for_transfer(resource: dict[str, Any], snapshot: list[dict[str, Any]]) -> str:
    target = str(resource.get('target_path') or _v3_target_path(resource['title'])).strip() or _v3_target_path(resource['title'])
    top = [x for x in snapshot if '/' not in str(x.get('path') or '').strip('/')]
    if len(top) == 1 and bool(top[0].get('is_dir')):
        source_name = _v3_safe_name(str(top[0].get('name') or ''))
        if target.rstrip('/').split('/')[-1] == source_name:
            parent = target.rstrip('/').rsplit('/', 1)[0]
            return parent or '/'
    return target


def _v3_transfer(resource: dict[str, Any], force: bool = False) -> bool:
    resource_id = int(resource['id'])
    current = _v3_load_resource(resource_id) or resource
    if not force and not current.get('has_updates'):
        log_event('info', 'sync', '没有检测到更新，已跳过', resource_id)
        return True
    credentials = get_credentials(current['platform'])
    if not credentials:
        raise RuntimeError(f"尚未绑定{SUPPORTED_PLATFORMS[current['platform']]['name']}账号")
    pending = _v3_json(current.get('pending_snapshot'), [])
    if not pending:
        result = _v3_inspect(current)
        pending = result.get('files') or []
    save_parent = _v3_parent_for_transfer(current, pending)
    with connect_db() as conn:
        conn.execute("UPDATE resources SET status='running',last_message=%s,last_run=NOW(),updated_at=NOW() WHERE id=%s", (f'正在同步到 {current.get("target_path") or save_parent}', resource_id))
        conn.commit()
    try:
        result = worker_call({
            'action': 'transfer',
            'url': current['source_url'],
            'code': current['source_code'],
            'share_pwd': '',
            'expired_type': 1,
            'save_dir': save_parent,
            'credentials': credentials,
        }, timeout=300)
        old_fid = str(current.get('target_fid') or '')
        new_fid = str(result.get('fid') or '')
        cleanup = ''
        if old_fid and old_fid != new_fid:
            try:
                worker_call({
                    'action': 'delete',
                    'url': current['source_url'],
                    'fids': [x for x in old_fid.split(',') if x],
                    'credentials': credentials,
                }, timeout=100)
                cleanup = '；旧版本已清理'
            except Exception as cleanup_exc:
                cleanup = f'；旧版本清理失败：{cleanup_exc}'
        changes = _v3_json(current.get('changes_json'), {'added': [], 'modified': [], 'deleted': []})
        message = f"同步成功（{_v3_summary(changes)}）{cleanup}"
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE resources SET target_url=%s,target_code=%s,target_fid=%s,save_dir=%s,
                    source_snapshot=%s,pending_snapshot='[]',has_updates=FALSE,status='success',last_message=%s,
                    last_run=NOW(),next_run=NOW()+(interval_minutes||' minutes')::interval,updated_at=NOW()
                WHERE id=%s
                """,
                (
                    str(result.get('share_url') or ''),
                    str(result.get('code') or ''),
                    new_fid,
                    save_parent,
                    json.dumps(pending, ensure_ascii=False, separators=(',', ':')),
                    message,
                    resource_id,
                ),
            )
            conn.commit()
        log_event('success', 'sync', message, resource_id, json.dumps(result, ensure_ascii=False))
        return True
    except Exception as exc:
        message = f'同步失败：{exc}'
        with connect_db() as conn:
            conn.execute(
                "UPDATE resources SET status='failed',last_message=%s,last_run=NOW(),next_run=NOW()+(interval_minutes||' minutes')::interval,updated_at=NOW() WHERE id=%s",
                (message[:1000], resource_id),
            )
            conn.commit()
        log_event('error', 'sync', message, resource_id, traceback.format_exc())
        return False


def run_sync_job(trigger: str, resource_id: int | None = None, due_only: bool = False) -> None:
    if not SYNC_LOCK.acquire(blocking=False):
        return
    try:
        clauses = ['enabled=TRUE']
        params: list[Any] = []
        if resource_id is not None:
            clauses.append('id=%s')
            params.append(resource_id)
        elif due_only:
            clauses.append('(next_run IS NULL OR next_run<=NOW())')
        with connect_db() as conn:
            rows = conn.execute('SELECT * FROM resources WHERE ' + ' AND '.join(clauses) + ' ORDER BY id', params).fetchall()
        check_only = trigger in {'check-all', 'check-one'}
        force = trigger in {'full-all', 'full-one', 'manual-one'}
        set_job_state(running=True, message='正在检查网盘更新' if check_only else '正在检查并同步有更新的资源', started_at=iso(now_utc()), finished_at=None, trigger=trigger, total=len(rows), done=0, success=0, failed=0, updated=0, skipped=0)
        success_count = failed_count = updated_count = skipped_count = 0
        for index, row in enumerate(rows, 1):
            try:
                has_updates, _ = _v3_check_one(row)
                if check_only:
                    success_count += 1
                    updated_count += int(has_updates)
                elif force or has_updates:
                    ok = _v3_transfer(_v3_load_resource(int(row['id'])) or row, force=force)
                    success_count += int(ok)
                    failed_count += int(not ok)
                    updated_count += int(ok)
                else:
                    success_count += 1
                    skipped_count += 1
            except Exception:
                failed_count += 1
            set_job_state(done=index, success=success_count, failed=failed_count, updated=updated_count, skipped=skipped_count)
        message = f"任务完成：检查 {len(rows)}，同步 {updated_count}，无变化 {skipped_count}，失败 {failed_count}"
        set_job_state(running=False, message=message, finished_at=iso(now_utc()))
    except Exception as exc:
        set_job_state(running=False, message=f'任务异常：{exc}', finished_at=iso(now_utc()))
        log_event('error', 'job', str(exc), details=traceback.format_exc())
    finally:
        SYNC_LOCK.release()


def start_sync_job(trigger: str, resource_id: int | None = None, due_only: bool = False) -> bool:
    if get_job_state().get('running') or SYNC_LOCK.locked():
        return False
    threading.Thread(target=run_sync_job, args=(trigger, resource_id, due_only), daemon=True, name='netdisk-sync-v3').start()
    return True


def _v3_get(self: Handler) -> None:
    path = urllib.parse.urlsplit(self.path).path
    match = re.fullmatch(r'/api/resources/(\d+)/changes', path)
    if match:
        if not self.require_auth():
            return
        row = _v3_load_resource(int(match.group(1)))
        if not row:
            self.send_json({'success': False, 'message': '资源不存在'}, 404)
            return
        self.send_json({'success': True, 'changes': _v3_json(row.get('changes_json'), {}), 'has_updates': bool(row.get('has_updates')), 'target_path': row.get('target_path'), 'last_check': row.get('last_check')})
        return
    return _original_do_get(self)


def _v3_post(self: Handler) -> None:
    path = urllib.parse.urlsplit(self.path).path
    if path == '/api/run-all' or path == '/api/sync-updates':
        if not self.require_auth():
            return
        started = start_sync_job('sync-updates')
        self.send_json({'success': True, 'message': '已开始检查全部资源，并只同步有更新的项目' if started else '已有任务正在运行', 'started': started})
        return
    if path == '/api/check-all':
        if not self.require_auth():
            return
        started = start_sync_job('check-all')
        self.send_json({'success': True, 'message': '已开始检查全部资源' if started else '已有任务正在运行', 'started': started})
        return
    if path == '/api/full-all':
        if not self.require_auth():
            return
        started = start_sync_job('full-all')
        self.send_json({'success': True, 'message': '已开始完整重新转存全部资源' if started else '已有任务正在运行', 'started': started})
        return
    match = re.fullmatch(r'/api/resources/(\d+)/(check|sync|full|target)', path)
    if match:
        if not self.require_auth():
            return
        resource_id = int(match.group(1))
        action = match.group(2)
        if action == 'target':
            data = self.read_json()
            target_path = '/' + '/'.join(_v3_safe_name(x) for x in str(data.get('target_path') or '').split('/') if x.strip())
            if target_path == '/':
                raise ValueError('目标文件夹不能为空')
            with connect_db() as conn:
                conn.execute('UPDATE resources SET target_path=%s,updated_at=NOW() WHERE id=%s', (target_path, resource_id))
                conn.commit()
            self.send_json({'success': True, 'message': '目标文件夹已保存', 'target_path': target_path})
            return
        trigger = {'check': 'check-one', 'sync': 'sync-updates', 'full': 'full-one'}[action]
        started = start_sync_job(trigger, resource_id=resource_id)
        self.send_json({'success': True, 'message': {'check': '已开始检查更新', 'sync': '已开始同步本条更新', 'full': '已开始完整重新转存'}[action] if started else '已有任务正在运行', 'started': started})
        return
    return _original_do_post(self)


Handler.do_GET = _v3_get
Handler.do_POST = _v3_post


INDEX_HTML = (ROOT / 'v2' / 'index_v3.html').read_text(encoding='utf-8')
