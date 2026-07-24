# V3.1：修复未命名资源、夸克顶层文件夹名称回填与目标目录自动纠正。
# 本文件在 incremental_v3.py 之后加载。


def _v31_is_placeholder(value: Any) -> bool:
    text = re.sub(r'\s+', '', str(value or '')).lower()
    return text in {'', '未命名', '未命名资源', '未知资源', 'unknown', 'untitled', 'none', 'null'}


def _v31_top_items(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in files
        if '/' not in str(item.get('path') or item.get('name') or '').strip('/')
    ]


def _v31_root_name(files: list[dict[str, Any]]) -> str:
    top = _v31_top_items(files)
    dirs = [item for item in top if bool(item.get('is_dir'))]
    if len(dirs) == 1:
        return _v3_safe_name(str(dirs[0].get('name') or dirs[0].get('path') or ''))
    if len(top) == 1:
        return _v3_safe_name(str(top[0].get('name') or top[0].get('path') or ''))
    return ''


def _v31_should_replace_target(resource: dict[str, Any], target_path: str) -> bool:
    if not target_path:
        return True
    base = target_path.rstrip('/').rsplit('/', 1)[-1]
    if _v31_is_placeholder(base):
        return True
    old_title = str(resource.get('title') or '')
    return _v31_is_placeholder(old_title) and target_path.rstrip('/') == _v3_target_path(old_title).rstrip('/')


def _v3_check_one(resource: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    resource_id = int(resource['id'])
    try:
        result = _v3_inspect(resource)
        files = result.get('files') or []
        root_name = _v31_root_name(files)
        worker_title = _v3_safe_name(str(result.get('title') or '')) if result.get('title') else ''
        current_title = str(resource.get('title') or '')

        detected_title = worker_title
        if not detected_title or _v31_is_placeholder(detected_title):
            detected_title = root_name
        if not detected_title or _v31_is_placeholder(detected_title):
            detected_title = str(resource.get('source_title') or current_title or '未命名资源')

        display_title = current_title
        if _v31_is_placeholder(display_title) and not _v31_is_placeholder(detected_title):
            display_title = _v3_safe_name(detected_title)

        source_title = worker_title or root_name or display_title
        old_items = _v3_json(resource.get('source_snapshot'), [])
        changes = _v3_diff(old_items, files)
        has_updates = _v3_change_count(changes) > 0

        target_path = str(resource.get('target_path') or '').strip()
        if _v31_should_replace_target(resource, target_path):
            folder_name = root_name or display_title or source_title
            target_path = _v3_target_path(folder_name)

        message = ('发现更新：' + _v3_summary(changes)) if has_updates else '检查完成：没有变化'
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE resources SET title=%s,source_title=%s,target_path=%s,pending_snapshot=%s,changes_json=%s,
                    has_updates=%s,last_check=NOW(),last_message=%s,status=%s,updated_at=NOW()
                WHERE id=%s
                """,
                (
                    display_title,
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
