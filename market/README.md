# QSICK plugin market

A dependency-free (Python stdlib) HTTP + SQLite service. It stores only plugin
metadata; archives stay on GitHub/Gitee.

Live: <http://sickcc.online/QSICK/plugins-market/>

HTTPS is not enabled yet: every ACME HTTP-01/TLS-ALPN validation from Let's Encrypt,
ZeroSSL and Buypass is reset or answered by a middlebox, so the deployment stays on
HTTP. DNS-01 is the only working path and needs a DNSPod API token.

When a token is available:

```bash
export DNSPOD_API_KEY="<id>,<token>"
sh deploy/enable_https.sh     # issues via DNS-01, installs Caddy on 443, keeps NPS on 80
sh deploy/renew_https.sh      # monthly renewal (wire it to a timer)
```

## Data model

| Field | Meaning |
| --- | --- |
| `id` | slug, also the plugin id |
| `name`, `description`, `icon_url`, `version` | display metadata |
| `repo_url` | GitHub/Gitee repository (required) |
| `archive_url` | optional direct zip; otherwise derived from the repo |
| `qsick_min` / `qsick_max` | compatible QSICK client range |
| `published_at` / `updated_at` | timestamps |
| `stars`, `views`, `likes` | counters managed by the market |

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/plugins?q=&sort=&order=&client=&page=&per=` | list + search + sort |
| GET | `/api/plugins/all` | full dump (for sync) |
| GET | `/api/plugins/{id}` | detail, `?view=1` increments views |
| GET | `/api/plugins/{id}/install` | install metadata (`repo_url`, `archive_url`) |
| POST | `/api/plugins/{id}/like` | toggle like for a voter |
| POST | `/api/plugins` | admin upsert (single) |
| PUT | `/api/plugins` | admin full replace: `{"items":[...]}` |
| DELETE | `/api/plugins/{id}` | admin delete |
| POST | `/api/plugins/{id}/refresh` | admin refresh stars/updated from GitHub/Gitee |
| GET | `/api/health` | health + count |

Admin calls need `X-Market-Token: <token>` (or `Authorization: Bearer`). The token
lives in `/etc/qsick-market.env`, root-only:

```bash
TOKEN=$(sed -n 's/^QSICK_MARKET_TOKEN=//p' /etc/qsick-market.env)
curl -X POST http://127.0.0.1:8787/QSICK/plugins-market/api/plugins \
  -H "X-Market-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"guard","name":"Guard","repo_url":"https://github.com/you/qsick-guard","description":"...","version":"1.0.0","qsick_min":"0.2.0","qsick_max":"0.4.0"}'
```

Full push:

```bash
curl -X PUT .../api/plugins -H "X-Market-Token: $TOKEN" \
  -H 'Content-Type: application/json' --data-binary @plugins.json
```

## Sorting

`sort` = `published` | `updated` | `name` | `stars` | `views` | `likes` | `match`,
`order` = `asc` | `desc`. `match` ranks entries compatible with `client` first.

## Deployment

Files live in `/opt/qsick-market`, the service is `qsick-market.service`, and NPS
routes `sickcc.online/QSICK/plugins-market` to `127.0.0.1:8787`.

```bash
sh /opt/qsick-market/deploy/install.sh   # user, dirs, env, unit, restart
```

NPS route lives in `/etc/nps/conf/hosts.json`; `deploy/add_nps_host.py` appends it
(backup written to `/root/nps-hosts-backup-*.json`).

## Tests

```bash
uv run pytest -q market/tests
```
