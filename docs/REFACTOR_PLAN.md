# План дробления God-модулей

Базовое состояние: `pytest -q` → **423 passed**. Цель — снизить размер крупных
модулей без изменения поведения. Главный приём безопасности: **модуль остаётся
тонким фасадом-реэкспортом**, поэтому все существующие импорты
(`from app.db.repo import DbRepo` и т.п.) продолжают работать без правок по всему
коду.

## Кандидаты (LOC, по убыванию) и покрытие тестами

| # | Модуль | LOC | Профильные тесты | Риск |
|---|--------|-----|------------------|------|
| 1 | `app/core/utils.py` | 731 | test_utils, test_utils_full, test_mtproxy, test_proxy_fallback | низкий |
| 2 | `app/db/repo.py` | 1671 | test_repo, test_repo_lost, test_repo_single_chat_harden, test_folder_sync | низкий |
| 3 | `app/tg/delete.py` | 1203 | test_delete | средний |
| 4 | `app/tg/upload/uploader.py` | 1217 | test_upload_planning/resume/adaptive, test_upload_download_mock | средний |
| 5 | `app/tg/download/downloader.py` | 1013 | test_stream, test_thumbnails, test_upload_download_mock | средний |
| 6 | `app/core/worker.py` | 1225 | test_worker_lifecycle, test_scheduler_lanes | средний |
| 7 | `app/api/server.py` | 969 | test_api_server, test_sharing | средний |
| 8 | `app/ui/models_qt.py` | 1660 | test_folder_tree_model, test_object_state | высокий (Qt) |
| 9 | `app/ui/dialogs.py` | 1193 | test_settings_dialog, test_file_properties_dialog | высокий (Qt) |
| 10 | `app/ui/text_editor.py` | 1240 | — (нет прямых) | высокий (Qt, без тестов) |
| 11 | `app/ui/window_main.py` (+ panels) | 914 | test_ui_mainwindow, test_startup_overlay | высокий (Qt) |

Порядок выбран по возрастанию риска: сначала чистая логика с сильным покрытием,
UI — в конце; `text_editor.py` без тестов делаем предпоследним и максимально
осторожно (или откладываем).

## Единый ритуал на КАЖДЫЙ модуль (Definition of Done шага)

1. **Baseline:** `pytest -q` → зелёно. Зафиксировать число passed.
2. **Прочитать файл целиком** (`cat -n`) перед любой правкой — границы функций/классов
   берём из реального кода, не из памяти.
3. **Создать подпакет** рядом (`<module>/` с `__init__.py`) и перенести связные
   блоки по темам (см. ниже). Имена, отступы, докстринги, идиомы — как в оригинале.
4. **Оставить старый путь фасадом:** в исходном `<module>.py` оставить только
   реэкспорт (`from .<module>.<part> import X` / `__all__`), чтобы внешние импорты
   не менялись.
5. **Импорт-совместимость:** `python -c "import app.<...>; print('ok')"` для всех
   публичных имён, которые были у модуля.
6. **Профильные тесты:** `pytest -q tests/unit/<profile>.py` → зелёно.
7. **Полный прогон:** `pytest -q` → то же число passed, что в п.1.
8. **Линт/типы:** `flake8 app/<path>` и `mypy app/<path>` — без новых ошибок.
9. **Коммит:** один модуль = один коммит (`refactor(<area>): split <module> into package`).
   Никогда не держим два модуля «в полёте» одновременно.

Откат шага = `git checkout -- <paths>` (коммит ещё не сделан) либо revert коммита.

## Предлагаемая разбивка по модулям (уточняется чтением перед правкой)

### 1. `app/core/utils.py` → `app/core/utils/`
- `proxy.py` — proxy_endpoint, resolve_working_proxy, select_working_proxy_from_chain,
  proxy_for_set_proxy, telethon_client_kwargs.
- `fs.py` — ensure_parent_dir и файловые помощники.
- `media.py` — extract_video_poster_png и видео/превью утилиты.
- `format.py` — форматирование размеров/скоростей/времени.
- `__init__.py` реэкспортит всё прежнее API.

### 2. `app/db/repo.py` → `app/db/repo/`
Класс `DbRepo` разбить на миксины по доменам, собрать в фасадный `DbRepo`:
- `_accounts.py` (CRUD аккаунтов), `_objects.py` (объекты/части),
  `_jobs.py` (очередь задач), `_folders.py` (папки/дерево),
  `_aggregates.py` (rebuild_objects_aggregates и сводки), `_schema.py` (DDL/миграции).
- `__init__.py`: `class DbRepo(_AccountsMixin, _ObjectsMixin, ...)`.

### 3. `app/tg/delete.py` → `app/tg/delete/`
- `routes.py` (построение `_routes_by_chat_id`), `single.py` (delete_remote),
  `folder.py` (delete_folder), `rename.py` (rename_file). Фасад `TgDeleter`.

### 4. `app/tg/upload/uploader.py`
Подпакет `upload/` уже есть (multipart/batch/parallel/single). `uploader.py` —
оркестратор; вынести: `session.py` (chunked_upload_session), `group.py`
(chunked_upload_group), оставить в `uploader.py` тонкий `TgUploader`-фасад.

### 5. `app/tg/download/downloader.py`
Подпакет `download/` уже есть (fetch). Вынести: `blob.py`
(download_blob_members), `assemble.py` (chunked_download/сборка),
`parts.py` (fetch_parts_decrypted). Фасад `TgDownloader`.

### 6. `app/core/worker.py`
- `runners.py` — фабрики job-раннеров из `_build_runner` (download/upload/refresh/
  reindex/reconcile/delete/rename).
- `thumbnails.py` — fetch_thumbnail / build_video_poster / *_remote хелперы.
- `worker.py` оставить: жизненный цикл `QThread`, loop teardown, submit/persist.

### 7. `app/api/server.py`
- `routes/` по ресурсам (share, stream, files), `handlers.py`, `app_factory.py`.
  Фасад поднятия сервера сохранить.

### 8–11. UI (`models_qt`, `dialogs`, `text_editor`, `window_main`+panels)
- `models_qt.py` → `models/` (folder_tree_model, object_state, list/table models).
- `dialogs.py` → `dialogs/` (settings, file_properties, accounts, confirm).
- `text_editor.py` → `text_editor/` (widget, syntax, actions) — **без тестов**,
  поэтому только механический перенос + ручной smoke-запуск UI.
- `window_main.py` — вынести построение меню/тулбара/сигналов в отдельные mixins.
Для UI-тестов гонять с `QT_QPA_PLATFORM=offscreen`.

## Общий прогон в конце всей работы

После всех шагов: `pytest -q` (то же число passed), `flake8 app`, `mypy app`,
ручной smoke запуск `python run.py`. Итоговый коммит — обновление этого плана
(отметки «сделано» по каждому модулю).
