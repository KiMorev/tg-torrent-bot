# Plex Webhooks: план внедрения

## Критерий успеха

Фича считается готовой, когда PlexLoader умеет принимать webhook от Plex и использовать его как быстрый триггер повторной проверки уже завершённых Download Station задач в Plex. Если webhook выключен, не настроен или не дошёл, текущий polling продолжает работать как раньше.

## Граница задачи

- Webhook не заменяет существующий Plex polling.
- Webhook не матчится напрямую по `payload` как источник истины.
- Webhook только валидирует запрос, логирует событие и запускает немедленную Plex-проверку ожидающих задач.
- Уведомление пользователю отправляется только после подтверждения через текущую Plex lookup-логику.
- В первой версии не трогаем play/pause/scrobble/rating-события Plex.
- В первой версии не делаем публичный reverse-proxy сценарий; целевой режим - LAN endpoint на NAS/контейнере.

## Текущее состояние кода

- `bot.py` уже содержит основной путь: `_run_task_notifications_once` видит `finished`/`seeding`, отправляет уведомление о завершении и запускает `_plex_poll_after_finish`.
- `_plex_poll_after_finish` делает цикл до 20 попыток по 30 секунд: `_refresh_plex_library` -> `_plex_poll_lookup_target` -> `plex_found` или `plex_not_found`.
- `_plex_poll_lookup_target` уже умеет искать фильмы по task meta/title и сериалы по `series_query` + `season_num`, с fallback на file path match.
- `_refresh_plex_library` уже single-flight через `_plex_refresh_lock` и coalesce на `_PLEX_REFRESH_COALESCE_SECONDS`, то есть webhook-triggered recheck не должен создавать параллельный шторм запросов в Plex.
- `notified_tasks.json` уже хранит `plex_done`, а `_mark_plex_poll_done` блокирует повторный запуск polling после рестарта.
- `download_history.jsonl` уже пишет события `plex_found` / `plex_not_found`.
- `diagnostics.py` уже имеет Plex diagnostic и может принять дополнительное состояние webhook.
- В `requirements.txt` сейчас только `python-telegram-bot`, `requests`, `beautifulsoup4`; async HTTP-сервера в зависимостях нет.

## Подводные камни

- Plex webhook обычно приходит как `multipart/form-data` с JSON в поле `payload`, а не как обычный `application/json`.
- Endpoint должен быть защищён shared token; даже для LAN endpoint нельзя принимать любой POST как триггер тяжёлой проверки Plex.
- Нельзя запускать второй `_plex_poll_after_finish` для задачи, у которой уже идёт polling: сейчас это защищено `_PLEX_POLLING_TASKS`, но webhook-путь должен использовать тот же guard.
- Нельзя считать `plex_done=True` поводом навсегда игнорировать задачу в ручном webhook-triggered recheck, если `plex_done` был выставлен после timeout. Нужно решить отдельно: MVP уважает `plex_done`, иначе можно получить повторные timeout/уведомления.
- После webhook могут прийти несколько событий подряд на один и тот же media item. Нужен debounce/cooldown для webhook-triggered scans.
- `state_store.prune_stale_task_state` удаляет stale task state, поэтому webhook не сможет восстановить проверку по задачам, уже исчезнувшим из DS и state.
- `compose.yaml` сейчас не публикует порт `tg_torrent_drop`; включение webhook потребует точной инструкции `docker compose up -d`, а для локальной сборки - `docker compose up -d --build`.
- Установщик пока не настраивает Plex; изменения env/compose должны быть отражены в `.env.example`, README и, при необходимости, setup wizard.

## Предлагаемая архитектура MVP

### Компоненты

- Новый модуль `plex_webhooks.py`.
- Новые настройки в `config.py`:
  - `PLEX_WEBHOOK_ENABLED=false`
  - `PLEX_WEBHOOK_HOST=0.0.0.0`
  - `PLEX_WEBHOOK_PORT=8099`
  - `PLEX_WEBHOOK_TOKEN=`
  - `PLEX_WEBHOOK_DEBOUNCE_SECONDS=10`
- Runtime state для диагностики:
  - enabled/listening address
  - last received time
  - last accepted time
  - last event
  - invalid token count
  - trigger count
  - last error

### HTTP server

Решение для MVP: добавить `aiohttp`.

Почему:

- бот уже живёт в `asyncio`;
- server lifecycle проще привязать к Telegram app;
- multipart `payload` Plex обрабатывается штатно;
- не нужен отдельный thread и передача событий через `loop.call_soon_threadsafe`;
- HTTP handler проще тестировать отдельно.

Цена решения: новая зависимость и пересборка Docker image.

### Trigger flow

```text
Plex POST /plex/webhook?token=...
-> validate token
-> parse payload best-effort
-> update webhook diagnostics state
-> schedule webhook-triggered Plex recheck
-> recheck scans only tasks that are finished/seeding and still waiting for Plex
-> each candidate uses existing _refresh_plex_library + _plex_poll_lookup_target
-> if found: existing found-notification path
```

## План реализации

- [x] Уточнить финальное техническое решение по HTTP server: используем `aiohttp`.
- [x] Добавить настройки webhook в `AppSettings`, `load_settings`, `.env.example`.
- [x] Добавить `plex_webhooks.py` с token validation, payload parsing и in-memory diagnostics state.
- [x] Добавить запуск/остановку webhook server в lifecycle Telegram app рядом с `setup_bot_commands`.
- [x] Добавить функцию в `bot.py`, которая по webhook будит ожидающие Plex polling задачи без создания дублей.
- [x] Вынести общий код отправки `plex_found` из `_plex_poll_after_finish`, если это нужно для переиспользования без полного polling loop. Не потребовалось: webhook будит существующий polling loop.
- [x] Добавить debounce/cooldown для пачки webhook-событий.
- [x] Добавить диагностический блок в `/admin`: включён ли webhook, URL/порт, последний webhook, invalid token count, last error.
- [x] Обновить `compose.yaml` port mapping так, чтобы порт был опубликован через env/default.
- [x] Добавить защищённый `GET /plex/webhook/health?token=...`, который возвращает минимальный `{"ok": true}` без подробной диагностики.
- [x] Обновить README: как включить env, какой URL добавить в Plex, какая команда применяет изменения.
- [x] Обновить `docs/bot-structure.md`: новый endpoint, настройки, background responsibility.
- [x] Добавить unit-тесты на token validation, multipart payload, debounce, disabled mode, duplicate guard и успешный trigger lookup.
- [x] Запустить релевантные тесты, затем полный `python -m pytest tests/ -v` перед commit/push.

## Технические пометки выполнения

### Анализ текущего кода

- [x] Проверена цепочка `_run_task_notifications_once` -> `_plex_poll_after_finish`.
- [x] Проверены guards от дублей: `_PLEX_POLLING_TASKS` и `plex_done`.
- [x] Проверен Plex cache refresh: `_refresh_plex_library` уже single-flight/coalesced.
- [x] Проверено состояние: `notified_tasks.json`, `task_meta.json`, `download_history.jsonl`.
- [x] Проверены зависимости: отдельного HTTP server dependency сейчас нет.
- [x] Проверены места документации: README и `docs/bot-structure.md` требуют обновления при реализации.

### Реализация

- [x] Настройки добавлены.
- [x] Webhook server добавлен.
- [x] Recheck trigger добавлен.
- [x] Диагностика добавлена.
- [x] README обновлён.
- [x] `docs/bot-structure.md` обновлён.
- [x] Тесты добавлены/обновлены.
- [x] Локальные проверки пройдены.

## Открытые решения перед стартом

- [x] HTTP server: используем `aiohttp`.
- [x] `plex_done=True` после timeout в webhook-triggered recheck уважаем, чтобы не плодить повторные уведомления. Ручной retry можно сделать отдельной задачей позже.
- [x] Port mapping: делаем явный env-controlled mapping, чтобы установка была проще. Сервер всё равно слушает порт только при `PLEX_WEBHOOK_ENABLED=true`.
- [x] Healthcheck: добавляем минимальный защищённый endpoint `GET /plex/webhook/health?token=...`, который отвечает `{"ok": true}`. Подробная диагностика остаётся только в `/admin`.
