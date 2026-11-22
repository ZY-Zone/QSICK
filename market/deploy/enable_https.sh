#!/bin/sh
# Enable HTTPS for sickcc.online via DNSPod DNS-01 + Caddy TLS termination.
#
# Requires a DNSPod API token (DNS console -> key management), exported as:
#   DNSPOD_API_KEY="<id>,<token>"
#
# The HTTP listener stays on NPS (port 80); Caddy only terminates TLS on 443 and
# proxies to NPS. Re-run this script after token changes; renew with renew_https.sh.
set -eu

: "${DNSPOD_API_KEY:?export DNSPOD_API_KEY=<id>,<token> first}"
EMAIL=${EMAIL:-admin@sickcc.online}
DOMAIN=${DOMAIN:-sickcc.online}
ZERO_KID=${ZERO_KID:-TAVodrlH9QhLt8mygiOPwQ}
ZERO_HMAC=${ZERO_HMAC:-V2T68a3n7hpGwebgiO-44PSntOEizOOsluICmEhPQhs640GTVYfoPA_Edi1lAo5NANn_q4Z7r1Sm-RMHclXwKQ}

export DNSPOD_API_KEY

echo "[https] issuing certificate via DNSPod DNS-01"
lego --accept-tos -m "$EMAIL" \
    -s https://acme.zerossl.com/v2/DV90 \
    --eab --kid "$ZERO_KID" --hmac "$ZERO_HMAC" \
    --dns dnspod --domains "$DOMAIN" --path /etc/lego run

echo "[https] installing Caddy"
DEBIAN_FRONTEND=noninteractive apt-get install -y caddy >/dev/null

echo "[https] publishing certificate for Caddy"
install -d -m 0750 /etc/caddy/certs
install -m 0640 -o root -g caddy "/etc/lego/certificates/$DOMAIN.crt" "/etc/caddy/certs/$DOMAIN.crt"
install -m 0640 -o root -g caddy "/etc/lego/certificates/$DOMAIN.key" "/etc/caddy/certs/$DOMAIN.key"

echo "[https] freeing 443 for Caddy (NPS keeps 80)"
sed -i '/^https_proxy_port=/d' /etc/nps/conf/nps.conf
echo 'https_proxy_port=8443' >>/etc/nps/conf/nps.conf
systemctl restart Nps

cat >/etc/caddy/Caddyfile <<EOF
$DOMAIN {
    tls /etc/caddy/certs/$DOMAIN.crt /etc/caddy/certs/$DOMAIN.key
    reverse_proxy 127.0.0.1:80
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl restart caddy
sleep 2

curl -fsS --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/QSICK/plugins-market/api/health"
echo
echo "[https] done: https://$DOMAIN/QSICK/plugins-market/"
