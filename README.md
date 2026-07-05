# GlideDrive

> **Turn your own Telegram accounts and private channels into unlimited personal cloud storage.**
> A desktop app (PySide6 + Telethon) with a file explorer, a local index, multi-account parallel transfers, optional encryption, video streaming, and a REST API.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Qt](https://img.shields.io/badge/UI-PySide6-41cd52)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow)
![Tests](https://img.shields.io/badge/tests-513%20passed-brightgreen)
![Languages](https://img.shields.io/badge/UI-EN%20%7C%20RU%20%7C%20UA-informational)

---

## Features

**Storage**
- 📁 File explorer with folders, recursive search, trash, and per-file notes
- ✂️ Files of any size — transparent splitting into parts and reassembly on download
- 📦 Small-file batching into blobs, so thousands of tiny files don't clutter the channel
- 🔐 Optional AES-GCM encryption of file contents
- ♻️ SHA-256 deduplication and replace-by-name
- 🗃️ Local SQLite index, fully rebuildable by rescanning the channel (`reconcile`) — your data lives in Telegram, not in the local database

**Speed**
- 🚀 Multi-account striping — parallel upload/download across several accounts and channels
- 📈 Adaptive concurrency and a rate limiter that backs off on Telegram `FloodWait`
- ⏯️ Resumable uploads and downloads after an interrupted transfer
- 🌐 SOCKS5 / HTTP / MTProto proxy support with a fallback chain and on-the-fly escalation
- ⚡ Native `cryptg` AES backend (~30× faster than the pure-Python fallback)

**Media & integrations**
- 🖼️ Image previews, video posters, and a built-in media viewer
- 🎬 Stream video straight from the cloud without a full download, transcoded to fMP4 on the fly
- 📝 Built-in text editor with syntax highlighting
- 🔗 Share files by link and drive everything from a local REST API ([docs/REST_API.md](docs/REST_API.md))
- 🌍 UI in English (default), Russian, and Ukrainian — switch instantly from the top bar, no restart

## Screenshots

<!-- TODO: add screenshots -->
| Explorer | Media viewer |
|---|---|
| *coming soon* | *coming soon* |

## How it works

GlideDrive uses your **regular Telegram user accounts** (via the MTProto API) and a **private channel per account** as a storage backend:

1. **Upload.** Each file is chunked into parts sized for Telegram's per-message limit, optionally encrypted, and sent as messages to a channel. Many small files are packed into a single compressed *blob* to avoid flooding the channel with tiny messages.
2. **Index.** Every part is recorded in a local SQLite index (folder path, original name, size, SHA-256, message IDs). The index is a **cache, not the source of truth** — if you lose it or move to a new machine, `reconcile` rebuilds it by rescanning the channel, and blob contents are recoverable from an embedded manifest inside each blob.
3. **Download.** Parts are fetched in parallel across your accounts, decrypted if needed, reassembled, and verified (SHA-256 / CRC) before the file is considered complete.
4. **Stream & share.** Video can be streamed with range requests and remuxed to fMP4 on the fly; any file can be exposed through a share link or the local REST API.

With several accounts attached, transfers are **striped** across them in parallel, multiplying throughput while each account stays within its own rate limits.

## Quick start

Requires Python **3.11+**.

```bash
git clone https://github.com/f0rd2017/glidedrive
cd glidedrive

# with uv (recommended)
uv sync
uv run python run.py

# or the classic way
python -m venv .venv && source .venv/bin/activate
pip install -e .
python run.py
```

Full installation guide (Linux / macOS / Windows): [SETUP.txt](SETUP.txt).

## First-time setup

1. Get your `TG_API_ID` and `TG_API_HASH` from [my.telegram.org](https://my.telegram.org) and enter them in the first-run dialog **or** in a `.env` file (see [.env.example](.env.example)).
2. Create a private channel to use as storage (one per account).
3. Add accounts right in the app: menu → **Accounts** → **➕ Add account** (phone → code from Telegram → 2FA password if enabled).

The first account becomes the primary one. Each account is assigned its own channel — those are the lanes transfers are striped across.

## Configuration

All settings live in `config.json` next to the app (created on first run) and most are editable from the GUI. Environment variables in `.env` (`TG_API_ID`, `TG_API_HASH`, `TG_CRYPTO_KEY_B64`) take priority over `config.json`, so the app can also be configured entirely from the UI on any machine.

### Connection & accounts

| Setting | Default | What it does |
|---|---|---|
| `tg_api_id` / `tg_api_hash` | — | Telegram API credentials from my.telegram.org |
| `tg_session_path` | `./var/data/session.session` | Where Telethon session files are stored |
| `main_channel_index` | `0` | Which account/channel is treated as primary |
| `channel_sharding_mode` | auto | How data is distributed across multiple channels |
| `tg_proxy` | — | Proxy URL (`socks5://`, `http://`, or MTProto) with fallback chain |

### Storage & folders

| Setting | Default | What it does |
|---|---|---|
| `cache_dir` | `./var/cache` | Local cache: thumbnails, blob cache, stream cache |
| `download_dir` | app folder | Default destination for downloaded files |
| `cache_max_size_mb` | `0` (unlimited) | Cap on the on-disk cache size |
| `stream_cache_max_mb` | `2048` | Cap on the video-streaming cache |

### Transfers & performance

| Setting | Default | What it does |
|---|---|---|
| `chunk_size_mb` | `32` | Size of each upload part |
| `concurrency` | `6` | Parallel network requests per transfer |
| `max_active_jobs` | `8` | How many upload/download jobs run at once |
| `lane_upload_small_max` / `lane_upload_large_max` | `4` / `4` | Parallel upload lanes for small / large files |
| `lane_download_max` | `6` | Parallel download lanes |
| `send_media_rate_limit` / `get_file_rate_limit` | `8` / `24` | Requests-per-second caps (auto-tuned against FloodWait) |
| `upload_throttle_mbps` / `download_throttle_mbps` | `0` (off) | Optional bandwidth caps |

### Chunking & small-file batching

| Setting | Default | What it does |
|---|---|---|
| `balanced_part_sizing_enabled` | `true` | Pick part sizes to balance parallelism vs. overhead |
| `balanced_part_target_regular_mb` / `_premium_mb` | `1024` / `2560` | Target part size for regular / Premium accounts |
| `small_file_batching_enabled` | `true` | Pack tiny files into a single blob |
| `small_file_threshold_kb` | `512` | Files below this size are batched |
| `small_file_batch_target_mb` | `16` | Target size of one blob |
| `small_batch_max_files` | `256` | Max files per blob |

### Integrity, dedup & encryption

| Setting | Default | What it does |
|---|---|---|
| `use_sha_as_key` | `true` | Deduplicate identical files by SHA-256 |
| `download_integrity_mode` | `strict` | Verify hash/CRC on download (`strict` aborts on mismatch) |
| `keep_partial_on_failure` | `true` | Keep partial files to allow resume after failure |
| `upload_compression_mode` | `auto` | Compress compressible content before upload |
| `crypto.enabled` | `false` | Enable AES-GCM encryption (key from `TG_CRYPTO_KEY_B64`) |

### REST API

| Setting | Default | What it does |
|---|---|---|
| `api.enabled` | `false` | Enable the local REST API |
| `api.host` / `api.port` | `127.0.0.1` / `20451` | Where the API listens |
| `api.token` | — | Bearer token required for API requests |

See [docs/REST_API.md](docs/REST_API.md) for endpoints.

## Building a portable app

No Python required on the target machine; all data (`config.json`, `var/`) is created next to the executable, so the folder can be moved anywhere.

```bash
./scripts/build.sh      # Linux
scripts\build.bat       # Windows
```

Prebuilt Linux and Windows binaries are also published automatically to **Releases** when a `vX.Y.Z` tag is pushed (`.github/workflows/release.yml`).

## Architecture

```text
app/
  config/   configuration and validation
  core/     worker, jobs, accounts, cache, rate limiter
  db/       SQLite schema and repository
  tg/       Telegram: upload/download/scan/delete, adaptive limits, proxy
  ui/       PySide6: explorer, dialogs, media viewer, editor
  api/      local REST API and sharing
```

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```bash
uv run pytest          # 513 tests
uv run ruff check .    # linter
```

## ⚠️ Disclaimer

GlideDrive works through ordinary Telegram user accounts (the MTProto API). Heavy automation may violate the [Telegram Terms of Service](https://core.telegram.org/api/terms) and could, in theory, get an account limited. Use at your own risk, never keep the only copy of important data here, and don't use your main account.

## 💖 Support the project

If you find the app useful, you can support development:

**[☕ Ko-fi — ko-fi.com/yutix](https://ko-fi.com/yutix)** (cards & PayPal)

Or via crypto:

| Coin | Address |
|---|---|
| USDT (TRC-20) / Tron | `TLmkJf2x4bqqf6bGf35wXXB5S78AoeLvoF` |
| BTC | `bc1q0w3qyfavnrc8mjj2cfhn3y0u5xth5gcv7dy2ha` |
| EVM (ETH / USDT / USDC, ERC-20) | `0xd6a1B8ab387a3CC30d94f8D4836830ACc3F52Ecd` |
| Solana | `9Snv19GoyoAu1dmkJ7GEER6MBjXtJGW3A63fXstsuHG4` |
| TON | `UQCy7CyH3yUMw6APzVhO5PINjfVEYUTCpOyfG6kQ8oxHpQf7` |

A ⭐ star on GitHub, bug reports, and pull requests help too.

## License

[MIT](LICENSE)
