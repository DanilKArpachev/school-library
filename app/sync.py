"""Синхронизация индекса файлов с папкой Google Диска."""
import logging
import os
import secrets
import threading
from datetime import datetime, timezone

from . import db, drive

log = logging.getLogger("sync")
_lock = threading.Lock()

ROOT = "root"
# Файлы, которые при первом появлении сразу скрываются от учителей
HIDE_PREFIXES = [p.strip() for p in os.environ.get("HIDE_PREFIXES", "Регистрация").split(",") if p.strip()]
ALWAYS_SKIP = {"application/vnd.google-apps.form"}


def is_running() -> bool:
    return _lock.locked()


def _auto_hidden(name: str) -> int:
    return int(any(name.startswith(p) for p in HIDE_PREFIXES))


def run_sync() -> str:
    if not _lock.acquire(blocking=False):
        return "Синхронизация уже идёт"
    try:
        root_id = db.get_setting("root_folder_id")
        if not root_id:
            return "Папка не выбрана"
        rows = _collect(root_id)
        _save(rows)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.set_setting("last_sync", now)
        db.set_setting("last_sync_error", None)
        msg = f"Готово: {len(rows)} элементов"
        log.info(msg)
        return msg
    except Exception as e:
        log.exception("sync failed")
        db.set_setting("last_sync_error", str(e)[:500])
        return "Ошибка: " + str(e)[:300]
    finally:
        _lock.release()


def _collect(root_id: str) -> list[dict]:
    rows = []
    visited = {root_id}
    queue = [(root_id, ROOT)]
    while queue:
        folder_drive_id, parent_key = queue.pop(0)
        for f in drive.list_children(folder_drive_id):
            mime = f["mimeType"]
            content_id = f["id"]
            size = f.get("size")
            if mime == drive.SHORTCUT:
                sd = f.get("shortcutDetails") or {}
                content_id = sd.get("targetId")
                mime = sd.get("targetMimeType", "")
                size = None
                if not content_id:
                    continue
            if mime in ALWAYS_SKIP:
                continue
            is_folder = mime == drive.FOLDER
            rows.append({
                "id": f["id"],
                "drive_id": content_id,
                "parent_id": parent_key,
                "name": f["name"],
                "mime": mime,
                "size": int(size) if size else None,
                "modified": f.get("modifiedTime"),
                "is_folder": int(is_folder),
            })
            if is_folder and content_id not in visited:
                visited.add(content_id)
                queue.append((content_id, f["id"]))
    return rows


def _save(rows: list[dict]):
    token = secrets.token_hex(8)
    c = db.conn()
    c.execute("BEGIN")
    try:
        for r in rows:
            c.execute(
                """INSERT INTO items(id, drive_id, parent_id, name, mime, size, modified,
                                     is_folder, hidden, sync_token)
                   VALUES(:id, :drive_id, :parent_id, :name, :mime, :size, :modified,
                          :is_folder, :hidden, :token)
                   ON CONFLICT(id) DO UPDATE SET
                     drive_id=excluded.drive_id, parent_id=excluded.parent_id,
                     name=excluded.name, mime=excluded.mime, size=excluded.size,
                     modified=excluded.modified, is_folder=excluded.is_folder,
                     sync_token=excluded.sync_token""",
                {**r, "hidden": _auto_hidden(r["name"]), "token": token},
            )
        c.execute("DELETE FROM items WHERE sync_token IS NOT ? ", (token,))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
