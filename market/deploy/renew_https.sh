#!/bin/sh
# Renew the sickcc.online certificate via DNSPod DNS-01 and reload Caddy.
# Intended for a monthly systemd timer. Requires DNSPOD_API_KEY.
set -eu

: "${DNSPOD_API_KEY:?export DNSPOD_API_KEY=<id>,<token> first}"
DOMAIN=${DOMAIN:-sickcc.online}
export DNSPOD_API_KEY

lego --accept-tos \
    -s https://acme.zerossl.com/v2/DV90 \
    --eab --kid "${ZERO_KID:-TAVodrlH9QhLt8mygiOPwQ}" \
    --hmac "${ZERO_HMAC:-V2T68a3n7hpGwebgiO-44PSntOEizOOsluICmEhPQhs640GTVYfoPA_Edi1lAo5NANn_q4Z7r1Sm-RMHclXwKQ}" \
    --dns dnspod --domains "$DOMAIN" --path /etc/lego renew --days 30

install -m 0640 -o root -g caddy "/etc/lego/certificates/$DOMAIN.crt" "/etc/caddy/certs/$DOMAIN.crt"
install -m 0640 -o root -g caddy "/etc/lego/certificates/$DOMAIN.key" "/etc/caddy/certs/$DOMAIN.key"
systemctl reload caddy
