"""Синхронизация индекса файлов с папкой Google Диска."""
import logging
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import db, drive

log = logging.getLogger("sync")
_lock = threading.Lock()

ROOT = "root"
# Файлы, которые при первом появлении сразу скрываются от учителей
HIDE_PREFIXES = [p.strip() for p in os.environ.get("HIDE_PREFIXES", "Регистрация").split(",") if p.strip()]
ALWAYS_SKIP = {"application/vnd.google-apps.form"}
WORKERS = 8  # сколько папок читаем с Диска одновременно

# Прогресс текущей синхронизации — показывается в админке
progress = {"folders": 0, "items": 0}


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
    """Обходит дерево папок уровень за уровнем, читая папки параллельно."""
    progress.update(folders=0, items=0)
    rows = []
    visited = {root_id}
    level = [(root_id, ROOT)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while level:
            results = pool.map(lambda f: list(drive.list_children(f[0])), level)
            next_level = []
            for (_, parent_key), children in zip(level, results):
                for f in children:
                    row = _row(f, parent_key)
                    if not row:
                        continue
                    rows.append(row)
                    if row["is_folder"] and row["drive_id"] not in visited:
                        visited.add(row["drive_id"])
                        next_level.append((row["drive_id"], row["id"]))
                progress["folders"] += 1
                progress["items"] = len(rows)
            log.info("Прочитано папок: %d, найдено элементов: %d", progress["folders"], len(rows))
            level = next_level
    return rows


def _row(f: dict, parent_key: str):
    mime = f["mimeType"]
    content_id = f["id"]
    size = f.get("size")
    if mime == drive.SHORTCUT:
        sd = f.get("shortcutDetails") or {}
        content_id = sd.get("targetId")
        mime = sd.get("targetMimeType", "")
        size = None
        if not content_id:
            return None
    if mime in ALWAYS_SKIP:
        return None
    return {
        "id": f["id"],
        "drive_id": content_id,
        "parent_id": parent_key,
        "name": f["name"],
        "mime": mime,
        "size": int(size) if size else None,
        "modified": f.get("modifiedTime"),
        "is_folder": int(mime == drive.FOLDER),
    }


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
