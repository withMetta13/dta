#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/dta}"
DOMAIN="${DTA_DOMAIN:-}"
PUBLIC_PATH="${DTA_PUBLIC_PATH:-/dta}"
NGINX_SITE_NAME="${NGINX_SITE_NAME:-dta}"
NGINX_SITE_AVAILABLE="${NGINX_SITE_AVAILABLE:-/etc/nginx/sites-available/${NGINX_SITE_NAME}}"
NGINX_SITE_ENABLED="${NGINX_SITE_ENABLED:-/etc/nginx/sites-enabled/${NGINX_SITE_NAME}}"
NGINX_DEFAULT_SITE="${NGINX_DEFAULT_SITE:-/etc/nginx/sites-available/default}"
NGINX_SNIPPET="${NGINX_SNIPPET:-/etc/nginx/snippets/dta-static.conf}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this script as root on the server."
  exit 1
fi

if [ ! -f "${APP_DIR}/index.html" ]; then
  echo "App entry file not found: ${APP_DIR}/index.html"
  echo "Clone or pull https://github.com/withMetta13/dta.git into ${APP_DIR} first."
  exit 1
fi

install -d -m 0755 "$APP_DIR"
chown -R www-data:www-data "$APP_DIR"

if [ -n "$DOMAIN" ]; then
  cat >"$NGINX_SITE_AVAILABLE" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    root ${APP_DIR};
    index index.html;

    access_log /var/log/nginx/${NGINX_SITE_NAME}.access.log;
    error_log /var/log/nginx/${NGINX_SITE_NAME}.error.log;

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX

  ln -sfn "$NGINX_SITE_AVAILABLE" "$NGINX_SITE_ENABLED"
  READY_URL="http://${DOMAIN}/"
else
  if [ "$PUBLIC_PATH" = "/" ]; then
    echo "DTA_PUBLIC_PATH cannot be / when DTA_DOMAIN is empty, because the IP root is already used by another site."
    exit 1
  fi

  PUBLIC_PATH="/${PUBLIC_PATH#/}"
  PUBLIC_PATH="${PUBLIC_PATH%/}"

  install -d -m 0755 "$(dirname "$NGINX_SNIPPET")"
  cat >"$NGINX_SNIPPET" <<NGINX
location = ${PUBLIC_PATH} {
    return 301 ${PUBLIC_PATH}/;
}

location ${PUBLIC_PATH}/ {
    alias ${APP_DIR}/;
    index index.html;
    try_files \$uri \$uri/ ${PUBLIC_PATH}/index.html;
}
NGINX

  python3 - <<PY
from pathlib import Path

site = Path("$NGINX_DEFAULT_SITE")
include_line = "    include $NGINX_SNIPPET;"
text = site.read_text()
if include_line not in text:
    marker = "    root /var/www/html;"
    if marker in text:
        text = text.replace(marker, marker + "\\n\\n" + include_line, 1)
    else:
        text = text.replace("server {", "server {\\n" + include_line, 1)
    site.write_text(text)
PY

  READY_URL="http://<ECS-IP>${PUBLIC_PATH}/"
fi

nginx -t
systemctl reload nginx

echo "DTA static site is ready at ${READY_URL}"
