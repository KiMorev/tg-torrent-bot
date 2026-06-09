# История загрузок

`download_history.jsonl` - внутренняя append-only память PlexLoader о скачиваниях. Она нужна не как экран для пользователя, а как база для будущих сценариев вроде "скачать как в прошлый раз": бот сможет посмотреть, какую раздачу, качество, трекер и профиль пользователь выбирал раньше.

## Где хранится

Файл лежит в `STATE_DIR/download_history.jsonl`. В Docker это обычно `/data/download_history.jsonl`.

Формат - JSONL: одна строка равна одному событию. Запись идёт через `JsonStateStore.append_download_history()`, чтение - через `load_download_history()` и `find_latest_download_history()`.

## События

Минимальный набор событий:

- `download_added` - задача добавлена в Download Station;
- `download_completed` - Download Station сообщил обычное завершение;
- `download_soft_completed` - DS показал `error`, но BT-задача скачана >=99.9% и без конкретного `error_detail`;
- `download_failed` - добавление или финальный статус завершились ошибкой;
- `files_normalized` - исходные файлы сериала переименованы в Plex-формат после подтверждения пользователя;
- `plex_found` - Plex нашёл фильм или сезон после скачивания;
- `plex_not_found` - Plex не подтвердил появление за окно ожидания.

## Привязка к пользователю

Каждая запись по возможности содержит:

- `chat_id` - основной владелец действия;
- `chat_ids` - получатели уведомления, если событие рассылалось нескольким чатам;
- `task_id` - Download Station task id, когда он уже известен.

Для будущего пользовательского сценария искать историю нужно по `chat_id`, чтобы настройки одного пользователя не влияли на другого.

## Что можно сохранять

Допустимые поля:

- название раздачи и нормализованное название;
- `kind`, `year`, `quality`, `series_query`, `season`;
- `tracker`, `indexer`, `source`, `topic_id`, безопасная `topic_url`;
- профиль релиза из parsed meta: качество, source, HDR, аудио, языки, группа, edition;
- статус DS, прогресс, размер, `error_detail`;
- результат Plex lookup: rating key, тип metadata, причина timeout.

## Что нельзя сохранять

В историю не пишем:

- токены, пароли, API keys;
- полные magnet-ссылки;
- Jackett proxy/download URL вида `/dl/...` и ссылки с `apikey`;
- содержимое `.torrent`;
- raw HTML/API-ответы трекеров.

Если нужен URL, сохраняется только безопасная страница темы трекера, например `https://rutracker.org/forum/viewtopic.php?t=12345`.

## Точки записи

История пополняется в `bot.py`:

- `_download_and_add()` - скачивание из поисковой выдачи;
- `_notify_pending_success()` - отложенная очередь;
- `search_series_bulk_run()` и `_series_bulk_add_download()` - bulk-сезоны;
- `_do_process_magnet()` и `_do_process_torrent()` - ручные magnet/`.torrent`;
- `_check_subscriptions()` и Jackett subscription paths - автоскачивание по подпискам;
- `_run_task_notifications_once()` - финальные статусы DS;
- `_handle_normalization_callback()` - подтверждённое переименование файлов сериала;
- `_plex_poll_after_finish()` - итог поиска в Plex.

Перед добавлением новых точек записи проверь, что событие не дублируется при повторном фоне и что в payload не попали секретные ссылки.
