#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/dta/review-app}"
DATA_DIR="${DATA_DIR:-/var/lib/dta-review}"
PUBLIC_PATH="${PUBLIC_PATH:-/dta/review}"
PORT="${PORT:-8788}"
SERVICE="dta-review"
SNIPPET="/etc/nginx/snippets/dta-review.conf"
DEFAULT_SITE="/etc/nginx/sites-available/default"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this script as root on the server."
  exit 1
fi
if [ ! -f "$APP_DIR/scripts/potential_notes.py" ] || [ ! -f "$APP_DIR/deploy/incoming.sqlite3" ]; then
  echo "Application code or incoming database is missing."
  exit 1
fi

PUBLIC_PATH="/${PUBLIC_PATH#/}"
PUBLIC_PATH="${PUBLIC_PATH%/}"
install -d -m 0755 "$APP_DIR"
install -d -o www-data -g www-data -m 0750 "$DATA_DIR"
python3 "$APP_DIR/deploy/merge_database.py" "$APP_DIR/deploy/incoming.sqlite3" "$DATA_DIR/potential_notes.sqlite3"
chown www-data:www-data "$DATA_DIR/potential_notes.sqlite3"
chmod 0640 "$DATA_DIR/potential_notes.sqlite3"
chown -R www-data:www-data "$APP_DIR"

install -m 0644 "$APP_DIR/deploy/dta-review.service" "/etc/systemd/system/${SERVICE}.service"
sed -i "s/--port 8788/--port ${PORT}/" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl restart "$SERVICE"

install -d -m 0755 /etc/nginx/snippets
cat >"$SNIPPET" <<NGINX
location = ${PUBLIC_PATH} {
    return 301 ${PUBLIC_PATH}/;
}

location ${PUBLIC_PATH}/ {
    auth_basic off;
    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
}
NGINX

include_line="    include ${SNIPPET};"
if ! grep -Fq "$include_line" "$DEFAULT_SITE"; then
  cp "$DEFAULT_SITE" "${DEFAULT_SITE}.before-dta-review"
  python3 - "$DEFAULT_SITE" "$include_line" <<'PY'
import sys
from pathlib import Path

site = Path(sys.argv[1])
include_line = sys.argv[2]
text = site.read_text()
marker = "    root /var/www/html;"
if marker in text:
    text = text.replace(marker, marker + "\n\n" + include_line, 1)
else:
    text = text.replace("server {", "server {\n" + include_line, 1)
site.write_text(text)
PY
fi

if ! nginx -t; then
  test -f "${DEFAULT_SITE}.before-dta-review" && cp "${DEFAULT_SITE}.before-dta-review" "$DEFAULT_SITE"
  rm -f "$SNIPPET"
  nginx -t
  exit 1
fi
systemctl reload nginx
curl --fail --silent "http://127.0.0.1:${PORT}/api/health"
echo
echo "Review UI is ready at http://<ECS-IP>${PUBLIC_PATH}/"
