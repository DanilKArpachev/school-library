import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, drive, sync
from .security import LoginLimiter, generate_password, hash_password, verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

BASE = Path(__file__).parent
SECRET_KEY = os.environ.get("SECRET_KEY", "")
SITE_NAME = os.environ.get("SITE_NAME", "Библиотека учителя")
SYNC_INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MIN", "60"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"

if len(SECRET_KEY) < 32:
    raise SystemExit("Задайте SECRET_KEY длиной от 32 символов в файле .env")

limiter = LoginLimiter()


def bootstrap_admin():
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    c = db.conn()
    if c.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
        return
    if not username or len(password) < 8:
        raise SystemExit("Задайте ADMIN_USERNAME и ADMIN_PASSWORD (от 8 символов) в файле .env")
    c.execute(
        "INSERT INTO users(username, full_name, password_hash, is_admin) VALUES(?, ?, ?, 1)",
        (username, "Администратор", hash_password(password)),
    )
    log.info("Создан администратор %s", username)


async def sync_loop():
    while True:
        try:
            if drive.is_connected() and db.get_setting("root_folder_id"):
                msg = await asyncio.to_thread(sync.run_sync)
                log.info("Автосинхронизация: %s", msg)
        except Exception:
            log.exception("sync loop")
        await asyncio.sleep(SYNC_INTERVAL_MIN * 60)


@asynccontextmanager
async def lifespan(app):
    db.init()
    bootstrap_admin()
    task = asyncio.create_task(sync_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="session",
    max_age=30 * 24 * 3600,
    same_site="lax",
    https_only=COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.middleware("http")
async def origin_check(request: Request, call_next):
    # Защита от подделки запросов с чужих сайтов
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return HTMLResponse("Запрос отклонён", status_code=403)
    return await call_next(request)


# ---------- помощники ----------

class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def need_login_handler(request, exc):
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u or not u["is_active"] or u["session_version"] != request.session.get("sv"):
        request.session.clear()
        return None
    return u


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise NeedLogin()
    return u


def require_admin(request: Request):
    u = require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Только для администратора")
    return u


def flash(request: Request, text: str, kind: str = "ok"):
    request.session["flash"] = [kind, text]


def render(request: Request, name: str, user=None, **ctx):
    ctx.update(request=request, user=user, site_name=SITE_NAME,
               flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def get_item(item_id: str):
    return db.conn().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def path_to(item_id: str):
    """Цепочка папок от корня до элемента (включительно)."""
    chain = []
    seen = set()
    while item_id and item_id != sync.ROOT and item_id not in seen:
        seen.add(item_id)
        it = get_item(item_id)
        if not it:
            return None
        chain.append(it)
        item_id = it["parent_id"]
    return list(reversed(chain))


def visible(item_id: str) -> bool:
    chain = path_to(item_id)
    return chain is not None and not any(i["hidden"] for i in chain)


def list_folder(folder: str, include_hidden: bool):
    sql = "SELECT * FROM items WHERE parent_id=?"
    if not include_hidden:
        sql += " AND hidden=0"
    sql += " ORDER BY is_folder DESC, ulower(name)"
    return db.conn().execute(sql, (folder,)).fetchall()


def search(q: str, include_hidden: bool):
    like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.conn().execute(
        "SELECT * FROM items WHERE ulower(name) LIKE ? ESCAPE '\\' ORDER BY is_folder DESC, ulower(name) LIMIT 300",
        (like,),
    ).fetchall()
    return rows if include_hidden else [r for r in rows if visible(r["id"])]


# ---------- вход ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if limiter.blocked(ip):
        flash(request, "Слишком много попыток. Подождите 15 минут.", "error")
        return RedirectResponse("/login", status_code=303)
    u = db.conn().execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not u or not u["is_active"] or not verify_password(password, u["password_hash"]):
        limiter.fail(ip)
        flash(request, "Неверный логин или пароль", "error")
        return RedirectResponse("/login", status_code=303)
    limiter.reset(ip)
    request.session.clear()
    request.session.update(uid=u["id"], sv=u["session_version"])
    db.conn().execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?", (u["id"],))
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    return render(request, "account.html", require_user(request))


@app.post("/account/password")
def change_password(request: Request, old: str = Form(...), new: str = Form(...)):
    u = require_user(request)
    if not verify_password(old, u["password_hash"]):
        flash(request, "Старый пароль неверный", "error")
    elif len(new) < 8:
        flash(request, "Новый пароль должен быть не короче 8 символов", "error")
    else:
        db.conn().execute(
            "UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?",
            (hash_password(new), u["id"]),
        )
        request.session["sv"] = u["session_version"] + 1
        flash(request, "Пароль изменён")
    return RedirectResponse("/account", status_code=303)


# ---------- материалы ----------

@app.get("/", response_class=HTMLResponse)
def browse(request: Request, folder: str = sync.ROOT, q: str = ""):
    u = require_user(request)
    return _browse(request, u, folder, q, admin_mode=False)


def _browse(request, u, folder, q, admin_mode):
    q = q.strip()
    crumbs = []
    if q:
        items = search(q, include_hidden=admin_mode)
    else:
        if folder != sync.ROOT:
            crumbs = path_to(folder)
            if not crumbs or not crumbs[-1]["is_folder"] or (not admin_mode and not visible(folder)):
                raise HTTPException(404, "Папка не найдена")
        items = list_folder(folder, include_hidden=admin_mode)
    return render(request, "browse.html", u, items=items, crumbs=crumbs, folder=folder, q=q,
                  admin_mode=admin_mode, exports=drive.EXPORTS)


@app.get("/download/{item_id}")
def download(request: Request, item_id: str, inline: int = 0):
    u = require_user(request)
    it = get_item(item_id)
    if not it or it["is_folder"] or (not u["is_admin"] and not visible(item_id)):
        raise HTTPException(404, "Файл не найден")
    try:
        resp, mime, ext = drive.open_download(it["drive_id"], it["mime"])
    except drive.DriveError as e:
        log.warning("download failed %s: %s", item_id, e)
        raise HTTPException(502, "Не удалось получить файл с Google Диска. Попробуйте позже.")
    name = it["name"]
    if ext and not name.lower().endswith(ext):
        name += ext
    db.conn().execute(
        "INSERT INTO downloads(user_id, username, item_id, name) VALUES(?, ?, ?, ?)",
        (u["id"], u["username"], item_id, name),
    )
    disp = "inline" if inline and mime == "application/pdf" else "attachment"
    headers = {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(name)}"}
    if resp.headers.get("Content-Length"):
        headers["Content-Length"] = resp.headers["Content-Length"]

    def body():
        try:
            yield from resp.iter_content(64 * 1024)
        finally:
            resp.close()

    return StreamingResponse(body(), media_type=mime, headers=headers)


# ---------- админ: Google и синхронизация ----------

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    u = require_admin(request)
    c = db.conn()
    stats = {
        "files": c.execute("SELECT COUNT(*) FROM items WHERE is_folder=0").fetchone()[0],
        "folders": c.execute("SELECT COUNT(*) FROM items WHERE is_folder=1").fetchone()[0],
        "users": c.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
        "downloads": c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0],
    }
    return render(
        request, "admin.html", u, stats=stats,
        configured=drive.is_configured(), connected=drive.is_connected(),
        google_email=db.get_setting("google_email"),
        root_name=db.get_setting("root_folder_name"),
        last_sync=db.get_setting("last_sync"), last_error=db.get_setting("last_sync_error"),
        sync_running=sync.is_running(), interval=SYNC_INTERVAL_MIN,
    )


@app.get("/admin/google/connect")
def google_connect(request: Request):
    require_admin(request)
    url, state = drive.auth_url()
    request.session["oauth_state"] = state
    return RedirectResponse(url, status_code=303)


@app.post("/admin/google/finish")
def google_finish(request: Request, url: str = Form(...)):
    require_admin(request)
    try:
        drive.finish_auth(url, request.session.pop("oauth_state", ""))
        flash(request, "Google-аккаунт подключён")
    except drive.DriveError as e:
        flash(request, str(e), "error")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/google/disconnect")
def google_disconnect(request: Request):
    require_admin(request)
    drive.disconnect()
    flash(request, "Google-аккаунт отключён")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/root")
def set_root(request: Request, link: str = Form(...)):
    require_admin(request)
    try:
        fid = drive.folder_id_from_link(link)
        meta = drive.get_meta(fid)
        if meta.get("mimeType") != drive.FOLDER:
            raise drive.DriveError("Это не папка")
    except drive.DriveError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/admin", status_code=303)
    if fid != db.get_setting("root_folder_id"):
        db.conn().execute("DELETE FROM items")
    db.set_setting("root_folder_id", fid)
    db.set_setting("root_folder_name", meta.get("name", ""))
    flash(request, f"Папка «{meta.get('name')}» выбрана. Запустите синхронизацию.")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/sync")
async def sync_now(request: Request):
    require_admin(request)
    msg = await asyncio.to_thread(sync.run_sync)
    flash(request, msg, "error" if msg.startswith("Ошибка") else "ok")
    return RedirectResponse("/admin", status_code=303)


# ---------- админ: файлы ----------

@app.get("/admin/files", response_class=HTMLResponse)
def admin_files(request: Request, folder: str = sync.ROOT, q: str = ""):
    u = require_admin(request)
    return _browse(request, u, folder, q, admin_mode=True)


@app.post("/admin/files/{item_id}/toggle")
def toggle_hidden(request: Request, item_id: str, back: str = Form("/admin/files")):
    require_admin(request)
    db.conn().execute("UPDATE items SET hidden=1-hidden WHERE id=?", (item_id,))
    if not back.startswith("/admin/files"):
        back = "/admin/files"
    return RedirectResponse(back, status_code=303)


# ---------- админ: пользователи ----------

def _users_page(request, u, new_password=None, new_user=None):
    users = db.conn().execute("SELECT * FROM users ORDER BY is_admin DESC, ulower(full_name), username").fetchall()
    return render(request, "users.html", u, users=users, new_password=new_password, new_user=new_user)


@app.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request):
    return _users_page(request, require_admin(request))


@app.post("/admin/users", response_class=HTMLResponse)
def create_user(request: Request, username: str = Form(...), full_name: str = Form(""),
                password: str = Form("")):
    u = require_admin(request)
    username = username.strip()
    if not username or " " in username:
        flash(request, "Логин не должен быть пустым или содержать пробелы", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if db.conn().execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        flash(request, "Такой логин уже есть", "error")
        return RedirectResponse("/admin/users", status_code=303)
    password = password.strip() or generate_password()
    if len(password) < 8:
        flash(request, "Пароль должен быть не короче 8 символов", "error")
        return RedirectResponse("/admin/users", status_code=303)
    db.conn().execute(
        "INSERT INTO users(username, full_name, password_hash) VALUES(?, ?, ?)",
        (username, full_name.strip(), hash_password(password)),
    )
    return _users_page(request, u, new_password=password, new_user=username)


@app.post("/admin/users/{uid}/reset", response_class=HTMLResponse)
def reset_password(request: Request, uid: int):
    u = require_admin(request)
    target = db.conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        raise HTTPException(404)
    password = generate_password()
    db.conn().execute(
        "UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?",
        (hash_password(password), uid),
    )
    if uid == u["id"]:
        request.session["sv"] = u["session_version"] + 1
    return _users_page(request, u, new_password=password, new_user=target["username"])


@app.post("/admin/users/{uid}/toggle")
def toggle_user(request: Request, uid: int):
    u = require_admin(request)
    if uid == u["id"]:
        flash(request, "Нельзя заблокировать самого себя", "error")
    else:
        db.conn().execute(
            "UPDATE users SET is_active=1-is_active, session_version=session_version+1 WHERE id=?", (uid,))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/delete")
def delete_user(request: Request, uid: int):
    u = require_admin(request)
    if uid == u["id"]:
        flash(request, "Нельзя удалить самого себя", "error")
    else:
        db.conn().execute("DELETE FROM users WHERE id=?", (uid,))
        flash(request, "Пользователь удалён")
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/log", response_class=HTMLResponse)
def download_log(request: Request):
    u = require_admin(request)
    rows = db.conn().execute("SELECT * FROM downloads ORDER BY id DESC LIMIT 300").fetchall()
    return render(request, "log.html", u, rows=rows)
