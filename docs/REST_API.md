# Локальный REST API

Тонкий HTTP-сервер поверх ядра (`app/api/`). На stdlib `http.server` — **без
новых зависимостей**. **Выключен по умолчанию.**

## Включение

В настройках приложения секция **REST API** (или вручную в `config.json`):

```json
"api": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 20451,
  "token": ""
}
```

Применяется после перезапуска приложения.

- `host` — `127.0.0.1` означает доступ только с этого компьютера (рекомендуется).
- `token` пустой → **авторизация отключена** (полагаемся на привязку к localhost).
  Если задан — каждый запрос (кроме `/api/health`) требует заголовок
  `Authorization: Bearer <token>` (или `?token=<token>`).

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/health` | Живость (без авторизации) |
| GET | `/api/folders` | Список папок |
| GET | `/api/files?folder=&search=&recursive=0\|1&status=` | Список объектов |
| GET | `/api/jobs?limit=N` | Последние джобы |
| GET | `/api/jobs/{id}` | Одна джоба (для опроса прогресса) |
| POST | `/api/upload` | `{"paths": ["/abs/file", ...], "folder": "Docs"}` |
| POST | `/api/download` | `{"folder": "Docs", "file_key": "...", "allow_incomplete": false}` |
| POST | `/api/delete` | `{"folder": "Docs", "file_key": "..."}` — удаление из облака |
| POST | `/api/shares` | создать шар-ссылку: `{"folder","file_key","password"?,"expires_in_sec"?}` |
| GET | `/api/shares` | список шар-ссылок |
| POST | `/api/shares/{token}/revoke` | отозвать ссылку (перестаёт работать) |
| DELETE | `/api/shares/{token}` | удалить запись ссылки |
| GET | `/share/{token}` | **публично** (без API-токена): скачать файл по ссылке. `?pw=` если задан пароль. Поддерживает `Range` (стрим/перемотка) |

Запись (`upload`/`download`/`delete`) ставит задачу в очередь и возвращает
`202 {"accepted": true}`. Задача исполняется тем же путём, что и из GUI
(`worker.submit_job`). **Id задачи синхронно не возвращается** (постановка
асинхронна) — прогресс отслеживается опросом `GET /api/jobs`.

### Шар-ссылки

`POST /api/shares` → `201 {"token","url","has_password","expires_ts"}`. Раздаётся
по `GET /share/{token}` тем же сервером: файл собирается из зашифрованных чанков
(или берётся уже скачанный/собранный) и отдаётся с поддержкой HTTP Range —
браузерный плеер может стримить/перематывать. Токен — это и есть секрет, поэтому
`/share/` публичный; пароль (`?pw=`) и срок (`expires_in_sec`) — опциональны.
**Ссылки работают только при включённом API** (тот же HTTP-сервер).

### Стрим без полного скачивания

Для чанкованных объектов `/share/{token}` отдаёт **настоящий стрим**: по
заголовку `Range` сервер скачивает и расшифровывает ТОЛЬКО те части, что
перекрывают запрошенный диапазон — а не весь файл. Перемотка видео тянет
1–2 части, а не гигабайты. Plaintext-смещения частей выводятся из индекса без
скачивания (зашифрованная часть хранит на 32 байта больше plaintext:
`ENC1`+nonce+GCM-tag). Расшифрованные части кэшируются на время сессии
(`.share_cache/.stream/<file_key>`), так что соседние Range-запросы их
переиспользуют. Для мелких файлов в общем blob (batch-member) и неполных
объектов сервер откатывается на полную сборку файла (тоже с Range).

## Примеры (curl)

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

## Безопасность

- По умолчанию выключен; по умолчанию слушает только `127.0.0.1`.
- Без токена API даёт полный доступ к хранилищу любому локальному процессу —
  для общей машины **задайте token** и не выставляйте `host` наружу.
- Тело запроса ограничено 1 МБ.
