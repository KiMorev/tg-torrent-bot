# Карта структуры бота

Цель файла - быстро понять, куда идти за правкой. Это не замена `README.md`
для пользователей и не замена `ARCHITECTURE.md` с общей архитектурой системы.

## Как поддерживать актуальность

- При любом изменении в проекте проверь, затрагивает ли оно эту карту.
- Обновляй файл в той же задаче, если менялись команды, callback-data,
  пользовательские флоу, фоновые циклы, JSON-состояние, переменные окружения,
  интеграции, установщик или ответственность модулей.
- Если изменение локальное и не меняет карту маршрутов (например, фикс внутри
  уже описанной функции без нового поведения), отдельная правка этого файла не
  нужна.
- Держи файл коротким: это навигатор, а подробности должны оставаться в коде,
  тестах, `README.md` или `ARCHITECTURE.md`.

## Быстрый маршрут по задачам

| Что меняем | Куда смотреть сначала | Обычно затронутые тесты |
|---|---|---|
| Команды Telegram, доступ, приветствие | `bot.py`: `main`, `start`, `help_command`, `text_message_entry`; `access_control.py`; `keyboards.py` | `tests/test_handlers.py`, `tests/test_keyboards.py`, `tests/test_access_control.py` |
| Кнопки и callback-data | `keyboards.py`; регистрация обработчиков в `bot.py::main`; правила в `AGENTS.md` | `tests/test_keyboards.py`, `tests/test_handlers.py` |
| Поиск релизов | `bot.py`: `search_got_query`, `_run_search`, `search_*`; `formatters.py`; `jackett.py`; `rutracker.py`; `kinopoisk.py`; `gpt_features.py` | `tests/test_handlers.py`, `tests/test_search_fallback.py`, `tests/test_search_quality_failure.py`, `tests/test_jackett.py`, `tests/test_rutracker_backoff.py` |
| Скачивание и очередь | `bot.py`: `_download_and_add`, `search_direct_download`, `_do_process_magnet`, `_do_process_torrent`, `_run_pending_downloads_once`; `download_station.py`; `torrent_utils.py` | `tests/test_handlers.py`, `tests/test_background.py`, `tests/test_download_station_locking.py`, `tests/test_disk_space_guard.py`, `tests/test_torrent_utils.py` |
| История загрузок | `bot.py`: `_record_download_history`, `_record_download_added_history`, `_record_task_notification_history`, `_plex_poll_after_finish`; `state_store.py`: `download_history.jsonl`; подробности в `docs/download-history.md` | `tests/test_state_store.py`, `tests/test_handlers.py`, `tests/test_background.py` |
| Сериалы и подписки | `bot.py`: `search_subscribe_*`, `_check_subscriptions`, `_check_jackett_subscriptions`; `jackett_subscriptions.py`; `subscription_policy.py`; `series_bulk_planner.py`; `formatters.py` | `tests/test_subscription_policy.py`, `tests/test_subscription_picker_ui.py`, `tests/test_jackett_subscriptions.py`, `tests/test_series_bulk_planner.py`, `tests/test_background.py` |
| `/new` и подбор новинок | `bot.py`: `_refresh_movie_discovery_cache*`, `_run_movie_discovery_notifications`, `movie_new_*`; `movie_discovery.py`; `kinopoisk.py`; `gpt_features.py` | `tests/test_movie_discovery.py`, `tests/test_handlers.py`, `tests/test_kinopoisk.py`, `tests/test_gpt_features.py` |
| Plex-проверки и уведомления | `bot.py`: `_plex_*`, `_run_task_notifications_once`; `plex.py`; `diagnostics.py`; `keyboards.py` | `tests/test_plex.py`, `tests/test_plex_series_context.py`, `tests/test_background.py`, `tests/test_keyboards.py` |
| `/status`, карточки задач, автообновление | `bot.py`: `status`, `task_callback`, `_task_card_refresh_loop`; `task_views.py`; `task_policies.py`; `formatters.py` | `tests/test_task_views.py`, `tests/test_task_policies.py`, `tests/test_handlers.py` |
| Админ-панель и диагностика | `bot.py`: `admin_command`, `admin_callback`; `diagnostics.py`; `keyboards.py`; `storage.py` | `tests/test_handlers.py`, `tests/test_diagnostics.py`, `tests/test_storage.py`, `tests/test_keyboards.py` |
| Фоновые циклы | `bot.py`: `setup_bot_commands`, `_tracker_background_loop`, `_task_maintenance_loop`, `_subscription_check_loop`, `_movie_discovery_loop`, `_jackett_warmup_loop`, `_plex_cache_loop`; профильная логика в отдельных модулях | `tests/test_background.py`, профильные тесты блока |
| Конфигурация и `.env` | `config.py`; `app_context.py`; `compose.yaml`; `README.md`; `install.sh`; `scripts/setup_wizard.py` | `tests/test_config.py`, `tests/test_setup_wizard.py` |
| Установка на Synology | `install.sh`; `scripts/setup_wizard.py`; `compose.yaml`; `README.md` | `tests/test_setup_wizard.py`, `tests/test_config.py` |
| Тестовое окружение | `tests/conftest.py`; профильные helper-функции в `tests/test_*.py` | полный `python -m pytest tests/ -v` |

## Точки входа runtime

| Точка | Назначение |
|---|---|
| `bot.py::main` | Создаёт Telegram `Application`, регистрирует команды, callback handlers, `ConversationHandler`, обработчики документов и реакций. |
| `bot.py::setup_bot_commands` | Обновляет меню команд, чистит temp-dir, запускает фоновые задачи. |
| `config.py::load_settings` | Единственная точка чтения переменных окружения и дефолтов. |
| `app_context.py::build_app_context` | Создаёт клиентов внешних сервисов из `AppSettings`. |
| `install.sh` | Bootstrap для Synology: скачивает compose/wizard, генерирует `.env`, поднимает контейнер. |
| `scripts/setup_wizard.py` | Интерактивный мастер базовой настройки Telegram + Download Station. |

## Основные пользовательские флоу

| Флоу | Основной путь в коде |
|---|---|
| Текстовый поиск | `text_message_entry` -> `search_got_query` -> `_run_search` -> `SEARCH_RESULTS`. |
| Голосовой поиск | `voice_message_entry` -> `voice_transcription.py` -> тот же поиск через `_run_search`. |
| Скачивание из результата | `search_download_pick` или `search_direct_download` -> Plex pre-check -> `_download_and_add` -> Download Station. |
| План недостающих сезонов | `search_download_pick` -> `search_series_bulk_plan` показывает профиль -> `search_series_bulk_profile_callback` меняет профиль -> `search_series_bulk_build_plan` запускает wide/targeted tracker search неблокирующим handler'ом; при fetch-limit широкий поиск расширяет targeted-pass до всех нужных сезонов; ожидательный экран использует search animation, обновляет этапы сборки и при долгой работе добавляет мягкий статус; `ConversationHandler.WAITING` принимает `srch:cancel`, выставляет cancel-token, сборка останавливается между сетевыми этапами; далее `series_bulk_planner.py` -> `series_bulk_jobs.json` job -> `search_series_bulk_confirm` -> `search_series_bulk_run` для уверенных сезонов; временные ошибки добавления уходят в `pending_downloads.json` с `series_bulk`-ссылкой и фоновый retry обновляет job; `/bulk` -> `series_bulk_command` -> `search_series_bulk_open` восстанавливает сохранённую job в search-context после рестарта; `search_series_bulk_pack_list` -> `search_series_bulk_pack_confirm` -> `search_series_bulk_pack_run` вручную добавляет выбранный pack и помечает покрытые сезоны в job; `search_series_bulk_rebuild` возвращает готовый план к профилю для новой сборки; `search_series_bulk_review` разбирает `missing`/`needs_decision`/`partial` и постоянные ошибки добавления, `search_series_bulk_soft_search` мягко добирает кандидатов для текущего сезона, `search_series_bulk_retry` повторяет failed-сезон, результат пишется в job. |
| Докачивание сезона из Plex | `/continue` -> `series_continue_command` -> `_series_continue_build_state` собирает Plex-сериалы с сезонами и `download_history.jsonl`; `series_continue_callback` листает режимы `Моё` / `Всё`, открывает карточку сезона; `cont:update_topic:*` проверяет текущий title той же Rutracker-темы, не создаёт дубль при активной задаче, добавляет обновлённый torrent и при неполном сезоне сохраняет подписку; если тема не обновилась, `cont:subscribe_topic:*` создаёт подписку без скачивания, а `cont:search_alt:*` показывает похожие Rutracker-кандидаты как обновлённые раздачи. |
| Magnet или `.torrent` файлом | `text_message_entry` или `handle_doc` -> `_process_magnet_uri` / `_do_process_torrent` -> Download Station. |
| Подписка на сериал | `search_subscribe_pick` -> `search_subscribe_preset` или advanced callbacks -> запись в `topic_subscriptions.json`; `/subs` -> `sub:settings:*` меняет `notify_policy`/`download_policy`. |
| Проверка подписок | `_subscription_check_loop` -> `_check_jackett_subscriptions` и `_check_subscriptions`. |
| `/new` | `movie_new_command` -> чтение cache/settings -> `movie_new_*` callbacks; refresh делает `_refresh_movie_discovery_cache`. |
| Прогрев Jackett | `_jackett_warmup_loop` -> `_run_jackett_warmup_once` -> `JackettClient.warmup`; индексеры прогреваются ротационными пачками и статус виден в диагностике. |
| Уведомление о завершении | `_task_maintenance_loop` -> `_run_task_notifications_once` -> Telegram push; при Plex включён может стартовать `_plex_poll_after_finish`; BT-задача `error` без конкретного `error_detail` (`unknown` считается неконкретным) и с прогрессом >=99.9% считается мягко завершённой для уведомлений/Plex polling; итоговые события пишутся в `download_history.jsonl`. |
| `/status` и список задач | `status` / `task_callback` -> `task_views.py` + `keyboards.py`; admin-view берёт владельцев из `task_owners.json` и подписи из `approved_users.json`. |
| `/admin` | `admin_command` / `admin_callback` -> короткая диагностика и drill-down `admin:diag_*`, настройки `/new`, пользователи, подписки, сброс счётчиков. |

## Callback namespaces

| Namespace | Где формируется | Где обрабатывается |
|---|---|---|
| `srch:*` | `keyboards.py`, локально в search-блоке `bot.py` | `ConversationHandler` в `bot.py::main` |
| `task:*` | `keyboards.py` | `task_callback` |
| `admin:*` | `keyboards.py`, admin-блок `bot.py` | `admin_callback`: панель, `admin:diagnostics`, `admin:diagnostics_back`, подробные `admin:diag_downloads` / `admin:diag_jackett` / `admin:diag_trackers` / `admin:diag_plex` / `admin:diag_ai`, refresh подробностей `admin:diag_refresh:*` |
| `access:*` | `keyboards.py` | `access_callback` |
| `sub:*` | `bot.py`, частично `keyboards.py` | `sub_callback`: список/отписка/настройка подписок; entry point `search_jackett_check_entry` |
| `new:*` | `bot.py`, `keyboards.py` | `movie_new_*` callbacks, часть внутри search conversation |
| `cont:*` | `bot.py` | `series_continue_callback`: список `/continue`, переключение `Моё` / `Всё`, refresh, карточка сезона, `cont:update_topic:*`, `cont:subscribe_topic:*`, `cont:search_alt:*`, `cont:alt_dl:*` |
| `plex:*` | `keyboards.py` | `plex_confirm_download`, `plex_upgrade_download`, `plex_cancel_download`, standalone callbacks |

## Модули

| Файл | Ответственность |
|---|---|
| `bot.py` | Telegram handlers, пользовательские флоу, фоновые циклы, связывание модулей. |
| `keyboards.py` | Inline-клавиатуры, callback prefixes, правила расположения кнопок. |
| `config.py` | `.env` -> `AppSettings`, дефолты, feature flags, пути к state-файлам. |
| `app_context.py` | Общий runtime context и клиенты внешних сервисов. |
| `download_station.py` | Synology Download Station API, ошибки, lock вокруг HTTP-сессии. |
| `rutracker.py` | Прямой клиент Rutracker: login, search, download, unavailable topic. |
| `jackett.py` | Jackett API: search, indexers, warmup probe, download proxy, magnet redirect. |
| `jackett_subscriptions.py` | Якорь подписки и выбор новой серии/раздачи из Jackett results. |
| `subscription_policy.py` | Решение, уведомлять ли и скачивать ли по подписке. |
| `series_bulk_planner.py` | Чистый планировщик массовой загрузки сезонов: scoring кандидатов и статусы сезонов. |
| `series_continue.py` | Чистые модели `/continue`: Plex identity, detector, completeness resolver и проверка той же темы. |
| `kinopoisk.py` | KP API, извлечение id, поиск карточек и метаданных. |
| `movie_discovery.py` | Фильтрация релизов, нормализация названий, scoring и сбор карточек `/new`. |
| `plex.py` | Plex API, фильмы, сериалы, сезоны, качество, unmatched detection. |
| `diagnostics.py` | Короткая сводка и подробные разделы диагностики внешних сервисов для `/admin`. |
| `state_store.py` | Atomic JSON load/save через `JsonStateStore`, append-only JSONL для истории загрузок. |
| `task_views.py` | Форматирование списка задач и карточки задачи. |
| `task_policies.py` | Получатели уведомлений, дедуп статусов, текст финального push, автоудаление. |
| `formatters.py` | Общие форматтеры, progress, качество, сериал/сезон, короткие названия. |
| `torrent_utils.py` | Magnet, bencode, `.torrent`, private torrent detection, matching DS task id. |
| `tracker_service.py` | Публичные BT-трекеры: загрузка списка, cache, применение к задачам. |
| `storage.py` | Информация о диске и history для storage alerts. |
| `voice_transcription.py` | Whisper transcription, проверки ключа, расчёт стоимости. |
| `gpt_client.py`, `gpt_features.py` | GPT-запросы и функции: did-you-mean, KP confidence, parse title, explain card. |
| `access_control.py` | Проверка разрешённых/admin chat ids и подпись заявки на доступ. |
| `progressive_status.py` | Прогрессивные сообщения ожидания для поиска и голосового ввода. |
| `scripts/setup_wizard.py` | Wizard установки и генерация `.env`. |
| `tests/conftest.py` | Bootstrap тестового окружения: изолированные `TMP_DIR`/`STATE_DIR` до импорта `bot.py`. |

## State files

Все пути идут из `config.py::load_settings`. По умолчанию `STATE_DIR` берётся из
окружения, а в Docker compose обычно монтируется как `/data`.

| Файл по умолчанию | Что хранит |
|---|---|
| `approved_chat_ids.json` | Динамически одобренные пользователи. |
| `task_owners.json` | Владелец Download Station task id. |
| `task_meta.json` | Метаданные задачи: тип, title, year, quality, series query, season. |
| `notified_tasks.json` | Состояние delivery уведомлений, failures, subscribers, Plex polling done. |
| `auto_delete_tasks.json` | Задачи-кандидаты на автоудаление и timestamp. |
| `trackers_processed_v2.json` | Задачи, куда уже добавляли публичные трекеры. |
| `topic_subscriptions.json` | Rutracker/Jackett подписки на новые серии. |
| `movie_discovery.json` | Кэш карточек `/new`, KP cache, fingerprints. |
| `movie_discovery_settings.json` | Настройки `/new`, подписчики, per-user seen/shown flags, Jackett trackers. |
| `movie_discovery_debug.json` | Debug snapshot последнего refresh `/new`. |
| `pending_downloads.json` | Очередь отложенных скачиваний и retry-state; bulk-записи могут содержать `series_bulk: {job_id, season}`. |
| `series_bulk_jobs.json` | Планы массового скачивания сезонов, ручные решения, созданные task id, подписки и ошибки по сезонам. |
| `download_history.jsonl` | Append-only история событий загрузки по пользователям: добавление, завершение, soft-complete, ошибки и результат Plex polling. |
| `storage_history.json` | История свободного места. |
| `voice_usage.json` | Использование voice transcription. |
| `gpt_usage.json` | Использование GPT-функций; `last_error` очищается успешным GPT-вызовом, transient-ошибки старше 24 ч не желтят диагностику. |
| `torrent_titles_cache.json` | Кэш заголовков torrent/magnet для поиска task id. |
| `public_trackers.txt` | Текстовый кэш публичных BT-трекеров. |

## Где фиксировать документацию

- Пользовательское поведение, команды, установка, `.env`: `README.md`.
- Системные схемы, инфраструктура, большие флоу: `ARCHITECTURE.md`.
- Быстрый маршрут для разработчика/Codex: этот файл.
- Логи для диагностики продакшена: раздел "Диагностические логи" в `AGENTS.md`.
