#!/usr/bin/env bash
# Установка на чистый сервер Ubuntu. Запуск: sudo bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  echo ">>> Устанавливаю Docker…"
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  echo ">>> Настройка (можно поменять позже в файле .env)"
  IP=$(curl -fsS https://api.ipify.org || echo "")
  DEF_DOMAIN="${IP//./-}.sslip.io"
  read -rp "Домен сайта [${DEF_DOMAIN}]: " DOMAIN; DOMAIN=${DOMAIN:-$DEF_DOMAIN}
  read -rp "Логин администратора [admin]: " ADMIN_USERNAME; ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
  while true; do
    read -rsp "Пароль администратора (от 8 символов): " ADMIN_PASSWORD; echo
    [ ${#ADMIN_PASSWORD} -ge 8 ] && break; echo "Слишком короткий."
  done
  read -rp "GOOGLE_CLIENT_ID: " GOOGLE_CLIENT_ID
  read -rp "GOOGLE_CLIENT_SECRET: " GOOGLE_CLIENT_SECRET
  SECRET_KEY=$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n')
  sed -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
      -e "s|^ADMIN_USERNAME=.*|ADMIN_USERNAME=${ADMIN_USERNAME}|" \
      -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASSWORD}|" \
      -e "s|^GOOGLE_CLIENT_ID=.*|GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}|" \
      -e "s|^GOOGLE_CLIENT_SECRET=.*|GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}|" \
      -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" \
      .env.example > .env
  chmod 600 .env
fi

echo ">>> Запускаю…"
docker compose up -d --build
DOMAIN=$(grep '^DOMAIN=' .env | cut -d= -f2)
echo
echo "Готово! Откройте https://${DOMAIN} (сертификат может получаться 1–2 минуты)."
