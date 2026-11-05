"""Запуск сайта на своём компьютере для проверки: python run_local.py"""
import os
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
env_file = HERE / ".env.local"

if not env_file.exists():
    env_file.write_text(
        "# Настройки для локальной проверки\n"
        "ADMIN_USERNAME=admin\n"
        "ADMIN_PASSWORD=admin12345\n"
        "GOOGLE_CLIENT_ID=\n"
        "GOOGLE_CLIENT_SECRET=\n"
        f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
        "SYNC_INTERVAL_MIN=60\n",
        encoding="utf-8",
    )
    print("Создан файл .env.local — впишите в него GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET.")

for line in env_file.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

os.environ.setdefault("COOKIE_SECURE", "0")  # локально работаем без HTTPS
os.environ.setdefault("DB_PATH", str(HERE / "data" / "library.db"))

import uvicorn  # noqa: E402

print("\nОткройте в браузере: http://localhost:8000")
print("Логин и пароль администратора — в файле .env.local\n")
uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
