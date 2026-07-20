# Local REST API

A thin HTTP server on top of the core (`src/televault/api/`). Built on the stdlib
`http.server` — **no extra dependencies**. **Disabled by default.**

## Enabling

In the app settings, section **REST API** (or manually in `config.json`):

```json
"api": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 20451,
  "token": ""
}
```

Takes effect after an app restart.

- `host` — `127.0.0.1` means access from this machine only (recommended).
- An empty `token` → **authorization is disabled** (relying on the localhost
  binding). If set, every request (except `/api/health`) requires the header
  `Authorization: Bearer <token>` (or `?token=<token>`).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness (no auth) |
| GET | `/api/folders` | List folders |
| GET | `/api/files?folder=&search=&recursive=0\|1&status=` | List objects |
| GET | `/api/jobs?limit=N` | Recent jobs |
| GET | `/api/jobs/{id}` | A single job (for polling progress) |
| POST | `/api/upload` | `{"paths": ["/abs/file", ...], "folder": "Docs"}` |
| POST | `/api/download` | `{"folder": "Docs", "file_key": "...", "allow_incomplete": false}` |
| POST | `/api/delete` | `{"folder": "Docs", "file_key": "..."}` — delete from the cloud |
| POST | `/api/shares` | create a share link: `{"folder","file_key","password"?,"expires_in_sec"?}` |
| GET | `/api/shares` | list share links |
| POST | `/api/shares/{token}/revoke` | revoke a link (stops working) |
| DELETE | `/api/shares/{token}` | delete the link record |
| GET | `/share/{token}` | **public** (no API token): download the file by link. `?pw=` if a password is set. Supports `Range` (streaming/seeking) |

A write (`upload`/`download`/`delete`) enqueues a job and returns
`202 {"accepted": true}`. The job runs through the same path as from the GUI
(`worker.submit_job`). **The job id is not returned synchronously** (enqueue is
asynchronous) — track progress by polling `GET /api/jobs`.

### Share links

`POST /api/shares` → `201 {"token","url","has_password","expires_ts"}`. Served at
`GET /share/{token}` by the same server: the file is assembled from encrypted
chunks (or taken already-downloaded/assembled) and returned with HTTP Range
support — a browser player can stream/seek. The token itself is the secret, so
`/share/` is public; a password (`?pw=`) and an expiry (`expires_in_sec`) are
optional. **Links work only while the API is enabled** (the same HTTP server).

### Streaming without a full download

For chunked objects, `/share/{token}` returns a **real stream**: based on the
`Range` header the server downloads and decrypts ONLY the parts that overlap the
requested range — not the whole file. Seeking in a video pulls 1–2 parts, not
gigabytes. Plaintext part offsets are derived from the index without downloading
(an encrypted part stores 32 bytes more than the plaintext:
`ENC1`+nonce+GCM-tag). Decrypted parts are cached for the session
(`.share_cache/.stream/<file_key>`), so adjacent Range requests reuse them. For
small files inside a shared blob (batch member) and for incomplete objects, the
server falls back to a full file assembly (also with Range).

## Examples (curl)

```bash
TOKEN=secret
curl localhost:20451/api/health
curl -H "Authorization: Bearer $TOKEN" localhost:20451/api/folders
curl -H "Authorization: Bearer $TOKEN" "localhost:20451/api/files?folder=Docs&recursive=1"
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:20451/api/upload \
  -d '{"paths":["/home/me/report.pdf"],"folder":"Docs"}'
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:20451/api/download \
  -d '{"folder":"Docs","file_key":"abc123"}'
curl -H "Authorization: Bearer $TOKEN" "localhost:20451/api/jobs?limit=10"
```

## Security

- Disabled by default; by default listens on `127.0.0.1` only.
- Without a token, the API gives full access to the storage to any local process —
  on a shared machine **set a token** and don't expose `host` externally.
- The request body is capped at 1 MB.
