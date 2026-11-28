#!/bin/sh
# One-time bootstrap for the QSICK plugin market.
# Usage (as root): sh /opt/qsick-market/deploy/install.sh
set -eu

APP_DIR=/opt/qsick-market
DATA_DIR=/var/lib/qsick-market
ENV_FILE=/etc/qsick-market.env
UNIT=/etc/systemd/system/qsick-market.service

id -u qsickmarket >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin qsickmarket

mkdir -p "$DATA_DIR/files"
chown -R qsickmarket:qsickmarket "$DATA_DIR"
chmod 750 "$DATA_DIR"
chown -R qsickmarket:qsickmarket "$APP_DIR"
chmod 755 "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
    token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    admin_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    umask 077
    cat >"$ENV_FILE" <<EOF
QSICK_MARKET_HOST=127.0.0.1
QSICK_MARKET_PORT=8787
QSICK_MARKET_DB=$DATA_DIR/market.db
QSICK_MARKET_FILES=$DATA_DIR/files
QSICK_MARKET_TOKEN=$token
QSICK_MARKET_ADMIN_SECRET=$admin_secret
# 生成管理密码哈希后填入，例如：
# python3 -c 'from market.app import password_hash; print(password_hash("..."))'
QSICK_MARKET_ADMIN_HASH=
EOF
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

install -m 0644 "$APP_DIR/deploy/qsick-market.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now qsick-market.service
sleep 1
systemctl is-active --quiet qsick-market.service
curl -fsS http://127.0.0.1:8787/QSICK/plugins-market/api/health
