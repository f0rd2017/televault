# Архитектура проекта — гайд по директориям

**TG_bd** («Telegram Cloud Cache Manager») — десктоп-приложение на PySide6 +
Telethon: использует пользовательские аккаунты Telegram как облачное хранилище,
ведёт локальный SQLite-индекс загруженных файлов. UI на русском.

Этот документ — карта «что где». Краткое описание каждого модуля и поток данных.

---

## С чего начать чтение
1. `app/main.py` — точка входа: загрузка конфига, создание `MainWindow`, запуск Qt.
2. `app/ui/window_main.py` — `MainWindow`, собирается из mixin'ов `app/ui/panels/`.
3. `app/core/worker.py` — `TelegramWorker`: мост между UI-джобами и операциями Telegram.
4. `app/tg/upload/` и `app/tg/download/` — ядро загрузки/выгрузки.

---

## Дерево `app/`

```
app/
├── main.py                  Точка входа: конфиг → MainWindow → Qt event loop
│
├── config/
│   ├── config.py            Загрузка/сохранение config.json + .env (API id/hash)
│   └── defaults.py          Значения по умолчанию
│
├── core/                    Бизнес-логика, не зависящая от Qt и Telethon напрямую
│   ├── worker.py            TelegramWorker (QThread): выполняет джобы upload/download/delete/scan
│   ├── jobs.py              JobManager, JobContext, CancelToken — очередь и отмена джоб
│   ├── accounts.py          AccountManager, ConnectedAccount — мультиаккаунт, пул клиентов
│   ├── cache.py             CacheManager — локальный кэш скачанных файлов
│   ├── transfer_progress.py TransferProgressAggregator — агрегирует прогресс частей → проценты
│   ├── rate_limiter.py      AdaptiveRateLimiter — адаптивный лимит запросов к Telegram
│   ├── types.py             Датаклассы: AppConfig, TelegramAccount, PartRecord, JobEvent, …
│   ├── utils.py             Хелперы: sha256, AES-GCM шифрование, пути, имена файлов
│   └── logging.py           Настройка логирования
│
├── db/                      Локальный SQLite-индекс
│   ├── database.py          Подключение/инициализация БД
│   ├── repo/                DbRepo — все операции с индексом (папки, объекты, части, джобы, батчи, аккаунты); см. ниже
│   └── models.py            Описание строк/таблиц
│
├── tg/                      Работа с Telegram (Telethon)
│   ├── client.py            TgClientManager / TgSession / TgClientEndpoint / ChannelAccessCheck
│   ├── scan.py              TgScanner — скан канала, восстановление индекса из сообщений
│   ├── parser.py            Сборка/разбор подписей (caption) сообщений-частей
│   ├── delete/              TgDeleter — удаление/переименование на стороне Telegram; см. ниже
│   ├── adaptive.py          _AdaptiveUploadController / _AdaptiveDownloadController (адаптив параллелизма)
│   ├── compression.py       zip / 7z-MT / архив батча мелких файлов (для upload)
│   ├── partition.py         Математика разбиения файла на логические части (для upload)
│   ├── upload/              Пакет загрузки (см. ниже)
│   └── download/            Пакет выгрузки (см. ниже)
│
└── ui/                      GUI на PySide6
    ├── window_main.py       MainWindow (композиция mixin'ов из panels/)
    ├── models_qt/           Qt-модели проводника: FolderTreeModel, ExplorerGridModel, items; см. ниже
    ├── dialogs/             SetupDialog, SettingsDialog, CreateFolder/Rename/Confirm, свойства файла/папки, AccountsDialog; см. ниже
    ├── text_editor/         Встроенный текстовый редактор + подсветка синтаксиса; см. ниже
    ├── media_viewer.py      Просмотр изображений/видео
    ├── job_toasts.py        Всплывающие карточки прогресса джоб
    ├── theme.py             Общий stylesheet приложения (не только MainWindow)
    ├── widgets.py           Переиспользуемые виджеты (ProgressLogWidget)
    └── panels/              Mixin'ы MainWindow (тот же паттерн, что в tg/upload,download)
        ├── folder_panel.py    FolderPanelMixin — дерево папок
        ├── explorer_panel.py  ExplorerPanelMixin — сетка файлов
        ├── upload_drop.py     UploadDropMixin — drag&drop загрузка
        ├── transfer_ops.py    TransferOpsMixin — запуск/контроль трансферов + watchdog
        ├── job_events.py      JobEventsMixin — обработка событий джоб
        ├── misc.py            MiscMixin — прочие действия меню/тулбара
        └── drag_export.py     ExplorerListView / ExplorerDropFrame — drag-export наружу
```

Начиная с этого разреза, «god-модули» с широким публичным API (`DbRepo`, `TgDeleter`,
UI-диалоги, модели проводника, текстовый редактор) разложены как подпакеты
`имя/` с тонким фасадом в `__init__.py` и приватными `_часть.py` внутри —
тот же приём, что и в `tg/upload`/`tg/download`/`ui/panels`. Публичный путь
импорта (`from app.db.repo import DbRepo` и т.п.) не меняется.

---

## Паттерн больших классов: mixin'ы
Крупные классы разбиты на mixin-модули, а композиционный класс наследует их.
Это уже принято в проекте для `MainWindow` (`app/ui/panels/`) и применено к
`TgUploader` и `TgDownloader`. Все константы и `__init__` — на композиционном
классе; mixin'ы содержат только методы (`self.*`/`cls.*` резолвятся по MRO).
Чисто-функциональные хелперы без `self` вынесены отдельными модулями
(`compression.py`, `partition.py`, `*/merge.py`, `*/_common.py`).

### `app/tg/upload/` — загрузка
| Модуль | Роль |
|---|---|
| `__init__.py` | re-export `TgUploader`, `_AdaptiveUploadController` |
| `uploader.py` | core: `__init__`, `chunked_upload` (главный конвейер), пул клиентов/чатов, лимиты, ключи |
| `send.py` | `_UploadSendMixin` — отправка в Telegram с ретраями |
| `parallel.py` | `_ParallelUploadMixin` — параллельная заливка частей big-file |
| `multipart.py` | `_MultipartUploadMixin` — multipart для огромных файлов (части с диска) |
| `single.py` | `_SinglePartUploadMixin` — путь одиночной части |
| `batch.py` | `_SmallBatchMixin` — батч мелких файлов в один архив + индексация |
| `analytics.py` | `_UploadAnalyticsMixin._build_upload_analytics` — единый сборщик блока `analytics` для всех трёх веток (single / in-memory multipart / disk-multipart) |
| `records.py` | `_UploadRecordBuffer` — батч-буфер записи частей в `DbRepo` (`add()`/`flush()`, БД-работа вне локов) |
| `_common.py` | `_is_retryable_error` (предикат ретрая) |

### `app/tg/download/` — выгрузка
| Модуль | Роль |
|---|---|
| `__init__.py` | re-export `TgDownloader`, `_AdaptiveDownloadController` |
| `downloader.py` | core: `__init__`, `chunked_download` (главный конвейер), маршруты, stride-стратегия |
| `fetch.py` | `_DownloadFetchMixin` — выкачка частей/сообщений с ретраями, strided-стримы |
| `batch.py` | `_DownloadBatchMixin` — извлечение одного файла из батч-архива |
| `merge.py` | `_DownloadMergeMixin` — манифест, сборка частей, расшифровка, проверка sha256 (одна часть → `os.replace` без копии) |
| `analytics.py` | `_DownloadAnalyticsMixin._build_download_analytics` — единый сборщик блока `analytics` для `chunked_download` и `_download_batch_member` |
| `_common.py` | `_is_retryable_error`, `_SHA_PREFIX_RE`, `_preallocate_file`, `_sha256_file_sync` |

### `app/db/repo/` — локальный индекс
| Модуль | Роль |
|---|---|
| `__init__.py` | фасад `DbRepo(_IndexMixin, _ObjectsMixin, _TrashShareSyncMixin, _JobsMixin, _BatchMixin, _TailMixin)` |
| `_index.py` | `_IndexMixin` — состояние сканов, папки, индекс сообщений |
| `_objects.py` | `_ObjectsMixin` — объекты и их агрегаты |
| `_trash.py` | `_TrashShareSyncMixin` — корзина, шеринг, синхронизация папок, ссылки, батч-блоб-ключи |
| `_jobs.py` | `_JobsMixin` — фоновые задания (jobs) |
| `_batch.py` | `_BatchMixin` — батч-блобы и их участники |
| `_tail.py` | `_TailMixin` — переименование/удаление объектов, аккаунты |
| `_sql.py` | SQL-константы, общие для нескольких миксинов |

### `app/tg/delete/` — удаление/переименование в Telegram
| Модуль | Роль |
|---|---|
| `__init__.py` | фасад `TgDeleter(_OpsMixin, _RetryMixin, _RoutesMixin)` — `__init__`, регистрация маршрутов по чатам |
| `_ops.py` | `_OpsMixin` — `delete_remote`, `delete_folder`, `rename_file` |
| `_retry.py` | `_RetryMixin` — обёртки с ретраями поверх операций из `_ops.py` |
| `_routes.py` | `_RoutesMixin` — построение/выбор маршрута (`client`, `chat`) по `chat_id` |
| `_helpers.py` | общие функциональные хелперы без `self` |

### `app/ui/dialogs/` — диалоги (все классы — `QDialog`)
| Модуль | Роль |
|---|---|
| `__init__.py` | `SetupDialog`, `SettingsDialog`, `CreateFolderDialog`, `RenameDialog`, `ConfirmDialog` |
| `_properties.py` | `FilePropertiesDialog`, `ShareLinkDialog`, `FolderPropertiesDialog` |
| `_accounts.py` | `AccountsDialog` + `_StatusProbe` (фоновая проверка живости прокси/аккаунтов) |
| `_style.py` | `_DIALOG_STYLESHEET` — общий стиль, используется `__init__.py` и `_properties.py` |

### `app/ui/models_qt/` — Qt-модели проводника
| Модуль | Роль |
|---|---|
| `__init__.py` | `FolderTreeModel`, `ExplorerGridModel`, `ExplorerFileItem`, `ExplorerFolderItem` и т.д. |
| `_icons.py` | рендер-слой иконок (типовые/бейджи/миниатюры), детекторы типа файла (`is_video_name`, `is_pdf_name`, …) |

### `app/ui/text_editor/` — встроенный текстовый редактор
| Модуль | Роль |
|---|---|
| `__init__.py` | `TextEditorWindow`, `CodeEditor`, `open_text_editor` |
| `_highlighter.py` | `CodeHighlighter(QSyntaxHighlighter)` — подсветка синтаксиса |

---

## Поток данных

**Загрузка:**
UI (drag&drop / меню) → `JobManager` ставит джобу → `TelegramWorker` берёт её →
`TgUploader.chunked_upload[_group]`: при необходимости сжатие (`compression`),
разбиение на части (`partition`), параллельная отправка через пул аккаунтов
(`send`/`parallel`/`multipart`/`single`) → запись частей в `DbRepo` →
прогресс через `TransferProgressAggregator` обратно в UI.

**Выгрузка:**
UI → джоба → `TelegramWorker` → `TgDownloader.chunked_download`: ищет части в
`DbRepo`, качает их (`fetch`, при поддержке — strided-стримы), затем
`merge` собирает файл, расшифровывает (AES-GCM) и сверяет sha256 → файл в
`cache/`/назначение, прогресс в UI.

**Адаптив:** скорость/флуд-вейты во время трансфера управляют параллелизмом
через `_AdaptiveUploadController` / `_AdaptiveDownloadController` (`adaptive.py`).

**Надёжность (закрытые дыры):**
- **Upload resume** (`app/tg/upload/resume.py`): при перезапуске уже залитые части
  (есть в `msg_index` с тем же `parts_total` + payload-sha256 из подписи)
  пропускаются. Для случайного ключа `file_key` восстанавливается из sidecar
  `cache_dir/.upload_resume/`. Симметрия с download-resume.
- **Proxy fallback chain** (`accounts.py`): у аккаунта есть `proxy` и
  `proxy_backup`; при коннекте идём primary→backup→direct
  (`utils.select_working_proxy_from_chain`), на лету —
  `AccountManager.escalate_proxy` через Telethon `set_proxy`.
- **Статус объекта** (`app/core/object_state.py` `classify_object_state`):
  complete / incomplete (не дозалит) / offline (аккаунт части не подключён) /
  damaged (`msg_index.lost_ts` выставлен и аккаунт онлайн = сообщение пропало).
  Потерю помечает `repo.mark_messages_lost_refs` (download при missing-message).
  В сетке состояние накладывается «на лету» через `object_state.display_state`
  (дешёвые агрегаты `repo.get_part_chat_ids_by_folder`/`get_lost_file_keys_by_folder`
  + живые аккаунты): иконка-тинт + подпись-минипометка под именем.
  Свойства файла «что где лежит» + ручная заметка — `FilePropertiesDialog`
  (контекст-меню «Свойства»); заметки в таблице `object_notes`.

**Производительность (инварианты, которые легко сломать):**
- Телеметрия использует `psutil.cpu_percent(interval=None)` — не блокирует
  event-loop; `interval>0` вернул бы синхронный стопор и заморозку трансфера.
- Запись `manifest.json` при выгрузке троттлится (`_MANIFEST_WRITE_THROTTLE_SEC`)
  с финальным flush; resume и так перепроверяет part-файлы на диске.
- `merge` для **одной** части делает `os.replace` (rename), а не копию —
  лишнего полного прохода чтения+записи нет. Для multi-part склейка обязательна.
- Resume-контракт: при сбое part-файлы (`part_*.bin`) сохраняются и
  восстановление возможно даже без `manifest.json` — не ломать «download прямо
  в финальный файл по смещениям» без явного решения сменить эту гарантию.

---

## Тесты и качество
- Запуск: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -o addopts= -p no:warnings -q`
- Линтер: `ruff check app/ tests/ scripts/` (enforced через save-hook).
- End-to-end загрузки/выгрузки: `tests/integration_mock/test_upload_download_mock.py`.
