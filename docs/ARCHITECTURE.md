# Project architecture — a directory guide

**TeleVault** is a desktop app built on PySide6 + Telethon: it uses Telegram
user accounts as cloud storage and keeps a local SQLite index of uploaded files.
The UI defaults to English (Russian and Ukrainian also available).

This document is a "what lives where" map — a short description of each module
and the data flow.

---

## Where to start reading
1. `app/main.py` — entry point: load config, create `MainWindow`, start Qt.
2. `app/ui/window_main.py` — `MainWindow`, composed from the mixins in `app/ui/panels/`.
3. `app/core/worker.py` — `TelegramWorker`: the bridge between UI jobs and Telegram operations.
4. `app/tg/upload/` and `app/tg/download/` — the upload/download core.

---

## The `app/` tree

```
app/
├── main.py                  Entry point: config → MainWindow → Qt event loop
│
├── config/
│   ├── config.py            Load/save config.json + .env (API id/hash)
│   └── defaults.py          Default values
│
├── core/                    Business logic, not directly tied to Qt or Telethon
│   ├── worker.py            TelegramWorker (QThread): runs upload/download/delete/scan jobs
│   ├── jobs.py              JobManager, JobContext, CancelToken — job queue and cancellation
│   ├── accounts.py          AccountManager, ConnectedAccount — multi-account, client pool
│   ├── cache.py             CacheManager — local cache of downloaded files
│   ├── transfer_progress.py TransferProgressAggregator — aggregates part progress → percent
│   ├── rate_limiter.py      AdaptiveRateLimiter — adaptive request limit against Telegram
│   ├── types.py             Dataclasses: AppConfig, TelegramAccount, PartRecord, JobEvent, …
│   ├── utils.py             Helpers: sha256, AES-GCM encryption, paths, file names
│   ├── logging.py           Logging setup
│   └── i18n.py              Language registry (en/ru/uk), QSettings persistence, install_language()
│
├── db/                      Local SQLite index
│   ├── database.py          DB connection/initialization
│   ├── repo/                DbRepo — all index operations (folders, objects, parts, jobs, batches, accounts); see below
│   └── models.py            Row/table definitions
│
├── tg/                      Telegram integration (Telethon)
│   ├── client.py            TgClientManager / TgSession / TgClientEndpoint / ChannelAccessCheck
│   ├── scan.py              TgScanner — channel scan, index recovery from messages
│   ├── parser.py            Build/parse message-part captions
│   ├── delete/              TgDeleter — delete/rename on the Telegram side; see below
│   ├── adaptive.py          _AdaptiveUploadController / _AdaptiveDownloadController (adaptive parallelism)
│   ├── compression.py       zip / 7z-MT / small-file batch archive (for upload)
│   ├── partition.py         Math for splitting a file into logical parts (for upload)
│   ├── upload/              Upload package (see below)
│   └── download/            Download package (see below)
│
└── ui/                      PySide6 GUI
    ├── window_main.py       MainWindow (composition of mixins from panels/)
    ├── models_qt/           Qt explorer models: FolderTreeModel, ExplorerGridModel, items; see below
    ├── dialogs/             SetupDialog, SettingsDialog, CreateFolder/Rename/Confirm, file/folder properties, AccountsDialog; see below
    ├── text_editor/         Built-in text editor + syntax highlighting; see below
    ├── media_viewer.py      Image/video viewer
    ├── job_toasts.py        Popup job-progress cards
    ├── theme.py             App-wide stylesheet (not just MainWindow)
    ├── widgets.py           Reusable widgets (ProgressLogWidget)
    └── panels/              MainWindow mixins (same pattern as tg/upload,download)
        ├── folder_panel.py    FolderPanelMixin — folder tree
        ├── explorer_panel.py  ExplorerPanelMixin — file grid
        ├── upload_drop.py     UploadDropMixin — drag&drop upload
        ├── transfer_ops.py    TransferOpsMixin — start/control transfers + watchdog
        ├── job_events.py      JobEventsMixin — job event handling
        ├── misc.py            MiscMixin — other menu/toolbar actions
        └── drag_export.py     ExplorerListView / ExplorerDropFrame — drag-export outward
```

From this slice onward, the "god modules" with a wide public API (`DbRepo`,
`TgDeleter`, UI dialogs, explorer models, text editor) are split into `name/`
subpackages with a thin facade in `__init__.py` and private `_part.py` files
inside — the same technique as in `tg/upload`/`tg/download`/`ui/panels`. The
public import path (`from app.db.repo import DbRepo`, etc.) does not change.

---

## The large-class pattern: mixins
Large classes are split into mixin modules, and a composition class inherits
them. This is already used in the project for `MainWindow` (`app/ui/panels/`) and
applied to `TgUploader` and `TgDownloader`. All constants and `__init__` live on
the composition class; mixins contain only methods (`self.*`/`cls.*` resolve via
the MRO). Pure-functional helpers without `self` are extracted into separate
modules (`compression.py`, `partition.py`, `*/merge.py`, `*/_common.py`).

### `app/tg/upload/` — upload
| Module | Role |
|---|---|
| `__init__.py` | re-export `TgUploader`, `_AdaptiveUploadController` |
| `uploader.py` | core: `__init__`, `chunked_upload` (main pipeline), client/chat pool, limits, keys |
| `send.py` | `_UploadSendMixin` — send to Telegram with retries |
| `parallel.py` | `_ParallelUploadMixin` — parallel upload of big-file parts |
| `multipart.py` | `_MultipartUploadMixin` — multipart for huge files (parts from disk) |
| `single.py` | `_SinglePartUploadMixin` — single-part path |
| `batch.py` | `_SmallBatchMixin` — batch small files into one archive + indexing |
| `analytics.py` | `_UploadAnalyticsMixin._build_upload_analytics` — single builder for the `analytics` block across all three branches (single / in-memory multipart / disk-multipart) |
| `records.py` | `_UploadRecordBuffer` — batch buffer for writing parts to `DbRepo` (`add()`/`flush()`, DB work outside locks) |
| `_common.py` | `_is_retryable_error` (retry predicate) |

### `app/tg/download/` — download
| Module | Role |
|---|---|
| `__init__.py` | re-export `TgDownloader`, `_AdaptiveDownloadController` |
| `downloader.py` | core: `__init__`, `chunked_download` (main pipeline), routes, stride strategy |
| `fetch.py` | `_DownloadFetchMixin` — fetch parts/messages with retries, strided streams |
| `batch.py` | `_DownloadBatchMixin` — extract one file from a batch archive |
| `merge.py` | `_DownloadMergeMixin` — manifest, part assembly, decryption, sha256 verification (single part → `os.replace`, no copy) |
| `analytics.py` | `_DownloadAnalyticsMixin._build_download_analytics` — single builder for the `analytics` block for `chunked_download` and `_download_batch_member` |
| `_common.py` | `_is_retryable_error`, `_SHA_PREFIX_RE`, `_preallocate_file`, `_sha256_file_sync` |

### `app/db/repo/` — local index
| Module | Role |
|---|---|
| `__init__.py` | facade `DbRepo(_IndexMixin, _ObjectsMixin, _TrashShareSyncMixin, _JobsMixin, _BatchMixin, _TailMixin)` |
| `_index.py` | `_IndexMixin` — scan state, folders, message index |
| `_objects.py` | `_ObjectsMixin` — objects and their aggregates |
| `_trash.py` | `_TrashShareSyncMixin` — trash, sharing, folder sync, links, batch-blob keys |
| `_jobs.py` | `_JobsMixin` — background jobs |
| `_batch.py` | `_BatchMixin` — batch blobs and their members |
| `_tail.py` | `_TailMixin` — object rename/delete, accounts |
| `_sql.py` | SQL constants shared by several mixins |

### `app/tg/delete/` — delete/rename in Telegram
| Module | Role |
|---|---|
| `__init__.py` | facade `TgDeleter(_OpsMixin, _RetryMixin, _RoutesMixin)` — `__init__`, per-chat route registration |
| `_ops.py` | `_OpsMixin` — `delete_remote`, `delete_folder`, `rename_file` |
| `_retry.py` | `_RetryMixin` — retry wrappers over the operations in `_ops.py` |
| `_routes.py` | `_RoutesMixin` — build/pick a route (`client`, `chat`) by `chat_id` |
| `_helpers.py` | shared functional helpers without `self` |

### `app/ui/dialogs/` — dialogs (all classes are `QDialog`)
| Module | Role |
|---|---|
| `__init__.py` | `SetupDialog`, `SettingsDialog`, `CreateFolderDialog`, `RenameDialog`, `ConfirmDialog` |
| `_properties.py` | `FilePropertiesDialog`, `ShareLinkDialog`, `FolderPropertiesDialog` |
| `_accounts.py` | `AccountsDialog` + `_StatusProbe` (background liveness probe for proxies/accounts) |
| `_add_account.py` | `AddAccountDialog` + `_AuthWorker` — add and authorize a new account right in the GUI (phone → code → 2FA) |
| `_style.py` | `_DIALOG_STYLESHEET` — shared style, used by `__init__.py` and `_properties.py` |

### `app/ui/models_qt/` — Qt explorer models
| Module | Role |
|---|---|
| `__init__.py` | `FolderTreeModel`, `ExplorerGridModel`, `ExplorerFileItem`, `ExplorerFolderItem`, etc. |
| `_icons.py` | icon render layer (type icons/badges/thumbnails), file-type detectors (`is_video_name`, `is_pdf_name`, …) |

### `app/ui/text_editor/` — built-in text editor
| Module | Role |
|---|---|
| `__init__.py` | `TextEditorWindow`, `CodeEditor`, `open_text_editor` |
| `_highlighter.py` | `CodeHighlighter(QSyntaxHighlighter)` — syntax highlighting |

---

## Data flow

**Upload:**
UI (drag&drop / menu) → `JobManager` enqueues a job → `TelegramWorker` picks it up →
`TgUploader.chunked_upload[_group]`: compression if needed (`compression`),
splitting into parts (`partition`), parallel sending across the account pool
(`send`/`parallel`/`multipart`/`single`) → writing parts to `DbRepo` →
progress via `TransferProgressAggregator` back to the UI.

**Download:**
UI → job → `TelegramWorker` → `TgDownloader.chunked_download`: finds parts in
`DbRepo`, fetches them (`fetch`, strided streams where supported), then `merge`
assembles the file, decrypts it (AES-GCM) and verifies sha256 → the file lands in
`cache/`/destination, progress to the UI.

**Adaptivity:** speed/flood-waits during a transfer control parallelism via
`_AdaptiveUploadController` / `_AdaptiveDownloadController` (`adaptive.py`).

**Reliability (closed gaps):**
- **Upload resume** (`app/tg/upload/resume.py`): on restart, already-uploaded
  parts (present in `msg_index` with the same `parts_total` + payload sha256 from
  the caption) are skipped. For a random `file_key`, it is recovered from the
  sidecar in `cache_dir/.upload_resume/`. Symmetric with download resume.
- **Proxy fallback chain** (`accounts.py`): an account has `proxy` and
  `proxy_backup`; on connect it goes primary→backup→direct
  (`utils.select_working_proxy_from_chain`), and on the fly via
  `AccountManager.escalate_proxy` through Telethon `set_proxy`.
- **Object state** (`app/core/object_state.py` `classify_object_state`):
  complete / incomplete (not fully uploaded) / offline (the part's account is not
  connected) / damaged (`msg_index.lost_ts` is set and the account is online =
  the message is gone). Loss is marked by `repo.mark_messages_lost_refs`
  (download on a missing message). In the grid, state is overlaid on the fly via
  `object_state.display_state` (cheap aggregates
  `repo.get_part_chat_ids_by_folder`/`get_lost_file_keys_by_folder` + live
  accounts): an icon tint + a small caption note under the name. The "what lives
  where" file properties + a manual note live in `FilePropertiesDialog` (context
  menu → "Properties"); notes are in the `object_notes` table.

**Performance (invariants that are easy to break):**
- Telemetry uses `psutil.cpu_percent(interval=None)` — it does not block the
  event loop; `interval>0` would introduce a synchronous stall and freeze the
  transfer.
- Writing `manifest.json` during download is throttled
  (`_MANIFEST_WRITE_THROTTLE_SEC`) with a final flush; resume re-checks part files
  on disk anyway.
- `merge` for a **single** part does `os.replace` (rename), not a copy — no extra
  full read+write pass. For multi-part, assembly is mandatory.
- Resume contract: on failure, part files (`part_*.bin`) are kept and recovery is
  possible even without `manifest.json` — don't break "download straight into the
  final file by offsets" without an explicit decision to change that guarantee.

---

## Tests and quality
- Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -o addopts= -p no:warnings -q`
- Linter: `ruff check app/ tests/ scripts/` (enforced via a save hook).
- End-to-end upload/download: `tests/integration_mock/test_upload_download_mock.py`.
