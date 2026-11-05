import os
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", "/data/library.db")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login TEXT
);

-- Индекс файлов из Google Диска. Сами файлы не хранятся,
-- они скачиваются с Диска в момент запроса.
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,          -- id элемента в Диске (или ярлыка)
    drive_id TEXT NOT NULL,       -- id содержимого (для ярлыков — id цели)
    parent_id TEXT NOT NULL,      -- id родителя в этой таблице, 'root' для корня
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER,
    modified TEXT,
    is_folder INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    sync_token TEXT
);
CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    username TEXT,
    item_id TEXT,
    name TEXT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        # LOWER() в SQLite не понимает кириллицу — нужна своя функция для поиска
        c.create_function("ulower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        _local.c = c
    return c


def init():
    conn().executescript(SCHEMA)


def get_setting(key, default=None):
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    conn().execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
