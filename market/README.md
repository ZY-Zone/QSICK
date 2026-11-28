# QSICK plugin market

A dependency-free (Python stdlib) HTTP + SQLite service. It stores plugin
metadata and can also host uploaded plugin archives; archives pushed through the
API/upload UI are kept under `QSICK_MARKET_FILES` and served by the same service.

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
| `repo_url` | GitHub/Gitee repository (optional for uploaded archives) |
| `archive_url` | direct zip; uploaded archives point at this service, others are derived from the repo |
| `archive_sha256` / `archive_size` | uploaded archive integrity and size |
| `qsick_min` / `qsick_max` | compatible QSICK client range |
| `published_at` / `updated_at` | timestamps |
| `status` | `pending`（待审核）/ `approved`（已上架）/ `rejected`（已拒绝）/ `removed`（已下架） |
| `stars`, `views`, `likes` | counters managed by the market |

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/plugins?q=&sort=&order=&client=&page=&per=` | public list; only approved plugins |
| GET | `/api/plugins/all` | public full dump; only approved |
| GET | `/api/plugins/{id}` | detail, `?view=1` increments views; only approved |
| GET | `/api/plugins/{id}/install` | install metadata (`repo_url`, `archive_url`); only approved |
| POST | `/api/plugins/{id}/like` | toggle like for a voter |
| POST | `/api/plugins/upload` | **public** multipart upload (field `file`); creates a `pending` entry |
| GET | `/files/{id}/{name}.zip` | download an uploaded archive |
| POST | `/api/plugins` | operator upsert via `X-Market-Token` (immediately approved) |
| PUT | `/api/plugins` | operator full replace via token |
| DELETE | `/api/plugins/{id}` | admin token or admin Cookie; deletes archive |
| POST | `/api/plugins/{id}/refresh` | operator token; refresh stars/updated from GitHub/Gitee |
| GET | `/api/admin/session` | current admin session state |
| POST | `/api/admin/login` | `{"password":"..."}` → HttpOnly admin Cookie |
| POST | `/api/admin/logout` | clear admin Cookie |
| GET | `/api/admin/plugins` | admin list with all statuses |
| POST | `/api/admin/plugins/{id}/approve\|reject\|unpublish` | approve / reject / take down |
| PATCH | `/api/admin/plugins/{id}` | edit metadata |
| POST | `/api/admin/plugins/{id}/delete` | delete plugin + archives |
| GET | `/api/health` | health + approved count |

Public `POST /api/plugins/upload` does not require a token. Uploaded entries start
as `pending`; they are invisible to the public market until an admin approves
them. The operator API (`X-Market-Token`) is still available for scripted
publishes and immediately creates approved entries.

## Admin UI

Open `<http://sickcc.online/QSICK/plugins-market/admin>`; log in with the
configured admin password. The panel can approve/reject, edit metadata, take
plugins down, and delete them.

Server configuration in `/etc/qsick-market.env`:

```bash
# PBKDF2-SHA256 encoded password hash, generate with:
# python3 -c 'from market.app import password_hash; print(password_hash("..."))'
QSICK_MARKET_ADMIN_HASH=pbkdf2_sha256$260000$...
# random HMAC key used to sign the admin session cookie
QSICK_MARKET_ADMIN_SECRET=...
```

The plaintext password is never stored on the server; only the PBKDF2 hash and a
random cookie-signing secret live in the root-only env file. Admin cookies are
`HttpOnly`, `SameSite=Lax`, and valid for 8 hours.

## Operator API

The old token API is still available for scripted publishes; those entries are
approved immediately. The token lives in `/etc/qsick-market.env`, root-only:

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

Upload a plugin archive (public, no token; lands in the review queue):

```bash
curl -X POST http://127.0.0.1:8787/QSICK/plugins-market/api/plugins/upload \
  -F "file=@qsick-guard.zip" \
  -F "repo_url=https://github.com/you/qsick-guard"
```

The zip must contain `basic.json` at its root (or under one common top-level
directory). The server reads id/name/version/description from that manifest,
validates path traversal and size limits, stores the archive, and returns the
market entry. `repo_url`, `icon_url`, `qsick_min`, `qsick_max` are optional form
fields. An existing entry keeps archived files, but only the five newest zips per
plugin are retained.

## Web UI

Open `<http://sickcc.online/QSICK/plugins-market/>`:

- 浏览 / 搜索 / 自定义排序 / 分页 / 点赞 / 版本匹配
- 「查看 / 安装」详情：版本、兼容范围、归档大小、SHA-256、安装地址与下载
- 右上角「上传插件」：公开上传，选择 zip，可补充仓库/图标/兼容版本；提交后进入待审核
- 审核通过后才会出现在公开市场；管理员入口 `<http://sickcc.online/QSICK/plugins-market/admin>`


## Sorting

`sort` = `published` | `updated` | `name` | `stars` | `views` | `likes` | `match`,
`order` = `asc` | `desc`. `match` ranks entries compatible with `client` first.

## Deployment

Files live in `/opt/qsick-market`, the service is `qsick-market.service`, and NPS
routes `sickcc.online/QSICK/plugins-market` to `127.0.0.1:8787`.

Uploaded archives live under `/var/lib/qsick-market/files` (configured by
`QSICK_MARKET_FILES`, writable because the unit has
`ReadWritePaths=/var/lib/qsick-market`). Set `QSICK_MARKET_PUBLIC_BASE` when the
public URL differs from the request Host, e.g.
`http://sickcc.online/QSICK/plugins-market`; `archive_url` must be an absolute
http(s) URL because the QSICK installer rejects relative archive paths.

```bash
sh /opt/qsick-market/deploy/install.sh   # user, dirs, env, unit, restart
```

NPS route lives in `/etc/nps/conf/hosts.json`; `deploy/add_nps_host.py` appends it
(backup written to `/root/nps-hosts-backup-*.json`).

## Tests

```bash
uv run pytest -q market/tests
```
