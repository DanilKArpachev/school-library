"""Работа с Google Drive API от имени аккаунта администратора (только чтение)."""
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import db

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
REDIRECT_URI = "http://localhost"
API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"

# Документы Google нельзя скачать как есть — их экспортируем в офисные форматы
EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


class DriveError(Exception):
    pass


def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def is_configured() -> bool:
    return bool(client_id() and client_secret())


def is_connected() -> bool:
    return bool(db.get_setting("google_refresh_token"))


def auth_url() -> tuple[str, str]:
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id(),
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE + " email",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), state


def finish_auth(pasted_url: str, expected_state: str):
    """Админ вставляет адрес из строки браузера (http://localhost/?code=...)."""
    qs = parse_qs(urlparse(pasted_url.strip()).query)
    if qs.get("error"):
        raise DriveError("Google вернул ошибку: " + qs["error"][0])
    code = (qs.get("code") or [""])[0]
    state = (qs.get("state") or [""])[0]
    if not code:
        raise DriveError("В ссылке нет кода. Скопируйте адрес целиком из строки браузера.")
    if not expected_state or state != expected_state:
        raise DriveError("Ссылка устарела. Нажмите «Подключить Google» ещё раз.")
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    data = r.json()
    if r.status_code != 200 or "refresh_token" not in data:
        raise DriveError("Не удалось получить доступ: " + str(data.get("error_description") or data))
    db.set_setting("google_refresh_token", data["refresh_token"])
    _token_cache.clear()
    try:
        info = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": "Bearer " + data["access_token"]},
            timeout=15,
        ).json()
        db.set_setting("google_email", info.get("email", ""))
    except Exception:
        pass


def disconnect():
    db.set_setting("google_refresh_token", None)
    db.set_setting("google_email", None)
    _token_cache.clear()


_token_cache: dict = {}
_token_lock = threading.Lock()


def access_token() -> str:
    with _token_lock:
        if _token_cache.get("exp", 0) > time.time() + 60:
            return _token_cache["token"]
        refresh = db.get_setting("google_refresh_token")
        if not refresh:
            raise DriveError("Google-аккаунт не подключён")
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id(),
                "client_secret": client_secret(),
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        data = r.json()
        if r.status_code != 200:
            raise DriveError("Google отклонил доступ, переподключите аккаунт: "
                             + str(data.get("error_description") or data.get("error")))
        _token_cache.update(token=data["access_token"], exp=time.time() + data.get("expires_in", 3600))
        return _token_cache["token"]


RETRY_STATUS = {429, 500, 502, 503, 504}


def _get(url, params=None, stream=False, timeout=(15, 300)):
    """GET к Drive API. При перегрузке Google повторяем запрос с паузой."""
    for attempt in range(6):
        r = requests.get(
            url,
            params=params,
            headers={"Authorization": "Bearer " + access_token()},
            stream=stream,
            timeout=timeout,
        )
        if r.status_code == 200:
            return r
        rate_limited = r.status_code == 403 and not stream and "ateLimitExceeded" in r.text
        if (r.status_code in RETRY_STATUS or rate_limited) and attempt < 5:
            r.close()
            time.sleep(2 ** attempt)
            continue
        msg = r.text[:300] if not stream else r.status_code
        r.close()
        raise DriveError(f"Ошибка Google Drive ({r.status_code}): {msg}")


def folder_id_from_link(text: str) -> str:
    text = text.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", text) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    raise DriveError("Не похоже на ссылку на папку Google Диска")


def get_meta(file_id: str) -> dict:
    return _get(f"{API}/files/{file_id}",
                {"fields": "id,name,mimeType", "supportsAllDrives": "true"}).json()


def list_children(folder_id: str):
    params = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,shortcutDetails)",
        "pageSize": 1000,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    while True:
        data = _get(f"{API}/files", params, timeout=(15, 60)).json()
        yield from data.get("files", [])
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token


def open_download(drive_id: str, mime: str):
    """Возвращает (поток ответа, mime для отдачи, расширение для имени)."""
    if mime in EXPORTS:
        out_mime, ext = EXPORTS[mime]
        r = _get(f"{API}/files/{drive_id}/export", {"mimeType": out_mime}, stream=True)
        return r, out_mime, ext
    if mime.startswith("application/vnd.google-apps."):
        raise DriveError("Этот тип файла Google нельзя скачать")
    r = _get(f"{API}/files/{drive_id}", {"alt": "media", "supportsAllDrives": "true"}, stream=True)
    return r, mime or "application/octet-stream", ""
