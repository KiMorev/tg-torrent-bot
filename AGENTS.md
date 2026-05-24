# Правила работы с проектом

## Рабочий процесс с Codex

- **Сначала согласуем план и границы.** Перед нетривиальными правками Codex должен кратко сформулировать, что именно будет менять, где проходит граница задачи и какие проверки нужны. Работу начинаем после согласования плана пользователем.
- **После согласованной работы commit/push можно делать сразу.** Если изменения выполнены в рамках уже согласованного плана, тесты/проверки пройдены или явно объяснено почему они не запускались, Codex может сразу коммитить и пушить без отдельного повторного подтверждения.

## Установка и onboarding

- **Установка — часть продукта.** При изменениях, которые влияют на запуск, конфигурацию, переменные окружения, Docker, интеграции или первый пользовательский опыт, обязательно проверять, как это пройдёт новый пользователь.
- **Максимум автоматизации.** Всё, что установщик может обнаружить, вычислить или проверить сам, он не должен спрашивать у пользователя: Docker/Compose, архитектура, timezone, дефолтные пути, state/cache-файлы, Plex-секции, Jackett-индексеры, доступность сервисов.
- **Пользователь выбирает возможности, а не заполняет конфиг.** Сначала спрашивать, что включить: базовый бот, Rutracker, Jackett, Plex, `/new`, OpenAI. Потом собирать только нужные данные.
- **Если нужен ввод пользователя — давать инструкцию в моменте.** Для каждого секрета или внешнего доступа объяснять: зачем это нужно, куда зайти, что нажать, что именно скопировать, как выглядит пример, можно ли пропустить и что будет недоступно.
- **После ввода сразу проверять результат.** Token, URL, логин, пароль, API key и пути проверяются сразу, а не в конце установки.
- **Ручной ввод — fallback, не основной путь.** Например: Telegram `chat_id` получать через `/start` + `getUpdates`, Plex token — через auth flow, Jackett indexers — через API; ручной `ADMIN_CHAT_IDS`, `X-Plex-Token` и пути использовать только если автоматический путь не сработал.
- **Тесты для установщика обязательны.** Любая новая логика установщика, генерации `.env`, auto-detect, resume/fallback или проверки внешних настроек должна иметь unit-тесты на успешный путь и минимум один понятный отказ.

## Тесты

### Когда запускать локально (`pytest tests/`)

- **Refactoring, новые фичи, серьёзная логика** — полный `python -m pytest tests/ -v` **обязательно** перед коммитом.
- **Изменения в одной функции / небольшая правка** — можно запустить только затронутый модуль: `pytest tests/test_<module>.py -v` (быстрее, меньше output).
- **Только docs / README / комментарии** — пропустить локально, CI всё равно проверит на push.
- **После того как CI поймал regression** — воспроизвести локально (`pytest tests/test_<failing>.py`), фикснуть, push.

### CI (GitHub Actions) как страховка

После каждого `git push origin main` в GitHub Actions автоматически запускается `pytest tests/ -v` на чистой Ubuntu+Python 3.14. См. `.github/workflows/test.yml`.

Hook `PostToolUse(Bash, git push*)` в `~/.Codex/settings.json` watches CI runs в фоне. При failure меня будит через `asyncRewake` — не нужно помнить проверять самому.

Tl;dr: **обязательно** локально для серьёзных изменений; CI достаточно для тривиальных.

### Правила про тесты

- **Все тесты должны быть зелёными** перед завершением задачи. Если тест упал — разобраться и починить, не оставлять сломанным.
- **Добавлять новые тесты** при каждом добавлении новой функциональности или исправлении бага:
  - новая функция → тест на её поведение
  - новая логика установщика / генерации `.env` / auto-detect → тест успешного пути и отказа
  - новый обработчик callback → тест на callback-data кнопки в keyboards.py
  - изменение логики фильтрации / scoring → тест на граничные случаи
  - исправление бага → тест, который воспроизводил бы этот баг
- **Обновлять существующие тесты** если изменился интерфейс (переименование кнопок, новые параметры функций, изменение формата вывода).

## README.md

Обновлять **в рамках той же задачи**, не откладывать на потом.

### Общие требования к README

- README — это клиентская витрина и рабочая инструкция для продукта **PlexLoader**, а не журнал внутренних решений.
- Перед крупной правкой README нужно сверить текст с текущим кодом, `.env.example`, `compose.yaml`, командами, клавиатурами и тестами.
- Не оставлять устаревшие режимы, legacy-совместимость и временные костыли, если они больше не нужны пользователю.
- Описывать пользовательские сценарии простым языком: поиск, скачивание, подписки, `/new`, Plex, `/status`, `/admin`, диагностика.
- Технические детали оставлять только там, где они помогают установить, настроить, диагностировать или развивать проект.
- Визуалы допустимы: Mermaid-схемы прямо в Markdown, SVG/PNG-диаграммы в репозитории, реальные скриншоты из бота. Не рисовать выдуманные скриншоты Telegram UI, которые могут не совпасть с фактическим интерфейсом.

| Что изменилось | Какой раздел README |
|---|---|
| Кнопки или структура админ-панели | «Админ-панель `/admin`» |
| Новая команда бота | «Команды бота» |
| Новая переменная окружения | Соответствующий раздел «Настройка» |
| Новые тест-классы или покрытие | «Разработка и тесты» → список покрытия |
| Изменение поведения диагностики | «Админ-панель» → блок «Диагностика» |
| Изменение блока новинок | «Новинки фильмов и мультфильмов» |

## Чеклист перед завершением задачи

- [ ] Тесты запущены и все зелёные
- [ ] Новые тесты написаны (если добавлялась функциональность)
- [ ] Существующие тесты обновлены (если менялся интерфейс)
- [ ] README обновлён в затронутых разделах

## Правила кнопок (InlineKeyboard)

### Какая кнопка для какой ситуации

| Кнопка | callback_data | Когда добавлять |
|---|---|---|
| `✖️ Закрыть` | `task:close:` | В **каждом** финальном экране: список задач, детали задачи, сообщения об ошибках — везде, где нет следующего шага |
| `❌ Отмена` | `srch:cancel` | Только внутри активного ConversationHandler (поиск, настройки) — удаляет сообщение и показывает «Отменено» |
| `⬅️ Назад` | зависит от контекста | Переход на предыдущий экран внутри одного флоу (диалог подтверждения, выбор трекера и т.п.) |
| `🔄 Повторить` / `🔄 Попробовать снова` | `srch:retry` | На экранах ошибок поиска — перезапускает последний поисковый запрос |

### Обязательные правила

1. **Промежуточные состояния без кнопок запрещены.**  
   Если для действия нужен сетевой запрос (fetch задачи, список задач), не показывай «Загружаю…» без клавиатуры. Либо покажи подтверждение сразу (без fetch), либо оставь предыдущие кнопки до завершения загрузки.

2. **Каждый экран с ошибкой должен иметь как минимум две кнопки:**  
   `🔄 Попробовать снова` + `✖️ Закрыть` → используй `_search_error_keyboard()` для ошибок поиска.

3. **Список задач и детали задачи** всегда оканчиваются кнопкой `✖️ Закрыть` (`task:close:`).

4. **Диалоги подтверждения** (удаление, очистка кэша и т.п.) оканчиваются `⬅️ Назад` (не «Закрыть»), потому что пользователь ожидает вернуться к предыдущему экрану.

5. **Финальный экран** (задача добавлена, задача удалена, операция завершена) — нет кнопки «Назад»; используй `✖️ Закрыть` если нет следующего логичного шага.

### Поведение после нажатия «Закрыть» / «Отмена»

Оба действия **удаляют** сообщение с кнопками **и** отправляют короткое авто-удаляемое уведомление (через `_send_auto_delete`, исчезает через 3 сек):

| Кнопка | Текст уведомления |
|---|---|
| `✖️ Закрыть` | «Закрыто» |
| `❌ Отмена` | «Отменено» |

Это уведомление нужно, чтобы пользователь видел отклик — иначе кажется что кнопка не сработала.  
**Всегда добавляй вызов `asyncio.create_task(_send_auto_delete(context.bot, chat_id, "Закрыто"))` после удаления сообщения.**

### Callback-обработчики «закрыть»

- `task:close:` — обрабатывается в `task_callback` (глобальный хендлер, работает в любом состоянии); удаляет сообщение + «Закрыто»
- `srch:cancel` — обрабатывается внутри ConversationHandler (только когда разговор активен); удаляет сообщение + «Отменено»
- `admin:close` — обрабатывается в `admin_callback` (только для администратора); удаляет сообщение + «Закрыто»

Добавляй новые «закрыть»-кнопки через `task:close:` — он уже зарегистрирован глобально.

## Структура проекта

| Файл | Назначение |
|---|---|
| `bot.py` | Telegram-обработчики, точка входа |
| `keyboards.py` | Inline-клавиатуры и callback-константы |
| `movie_discovery.py` | Фильтрация и скоринг новинок (`/new`) |
| `kinopoisk.py` | Клиент KP API (kinopoiskapiunofficial.tech) |
| `diagnostics.py` | Диагностика внешних сервисов для `/admin` |
| `state_store.py` | Хранение состояния в JSON-файлах |
| `rutracker.py` | Клиент Rutracker |
| `jackett.py` | Клиент Jackett |
| `tests/` | Юнит-тесты (pytest) |

## Диагностические логи

Когда что-то идёт не так на проде — искать в логах эти маркеры (logger name: `tg_torrent_drop`).

### Уведомления о завершении задач (`_run_task_notifications_once`)

- `Recipient skipped (failures cap) task=... chat=... failures=N/3 key=done` — счётчик failures исчерпан, push не отправлен. Решение: «🔄 Сбросить счётчики» в `/admin`.
- `Task notification failed (permanent: <label>) chat_id=... attempt=N/3` — отправка не удалась. `label`:
  - `blocked` — бот заблокирован пользователем
  - `chat_not_found` — чат удалён или `user is deactivated`
  - `message_format_bug` — наш баг в формате сообщения (НЕ считается против chat'а)
  - `permanent` — неизвестная ошибка, treat as permanent
- `Task notification deferred (transient: <label>) chat_id=... — will retry` — transient ошибка, счётчик НЕ растёт. INFO для `rate_limit`/`timeout`/`network`, ERROR для `message_format_bug`.

### Plex polling (`_plex_poll_after_finish` / `_plex_poll_lookup_target`)

- `Plex polling started task_id=... title=... kind=... chat_ids=[...]` — запуск (после finished).
- `Plex polling: found 'TITLE' after N attempt(s)` — нашли в библиотеке, шлём push.
- `Plex lookup: series show not found query=... year=... shows_cached=N` — show не нашёлся в кэше TV-секции. Проверить: 1) подключена ли секция в Plex, 2) совпадает ли normalised title (`_normalize_movie_title`), 3) если ничего не помогает — посмотреть `_plex_shows_library.keys()` через debug.
- `Plex lookup: show 'X' found but season N missing (have: [...])` — show нашёлся, но нужного сезона ещё нет в Plex (новый сезон ещё не вышел, либо файл не индексирован).
- `Plex lookup: movie not found task_title=... meta_title=... year=... movies_cached=N` — для фильмов: ни canonical, ни substring lookup не сработали.
- `Plex poll: failed to send found-notification chat_id=...` — push нашёлся, но send_message упал. Смотреть `Task notification failed/deferred` рядом — обычно та же причина (rate-limit / blocked / format-bug).

### Pending downloads (`_run_pending_downloads_once`)

- `Pending download queued: id=... title=... chat_id=...` — задача поставлена в очередь.
- `Pending download retry failed: id=... attempts=N err=...` — фоновая попытка не сработала.
- `Pending download succeeded: id=... task_id=... method=...` — успех, push отправлен.

### Скачивание через Jackett (`_download_and_add`)

- `Jackett download failed (...), trying rutracker_client direct: topic_id=...` — fallback на прямой Rutracker.
- `rutracker_client direct also failed: ... — falling back` — fallback тоже не сработал, идём на re-search / magnet.
- `Download failed for index=N: <error>` — финальная ошибка, юзеру показана кнопка retry/queue.

### Movie discovery (`_movie_discovery_loop` / `_refresh_movie_discovery_cache` / `_run_movie_discovery_notifications`)

Все маркеры начинаются с `movie_discovery:` — `docker logs tg_torrent_drop | grep movie_discovery:` даёт полную цепочку refresh → notify → render.

- `movie_discovery: loop started — first refresh now, interval=Nh` — старт фонового цикла. Один раз на запуск бота.
- `movie_discovery: first refresh after startup BEGIN/DONE` — границы самого первого refresh после старта. Между BEGIN и DONE может быть несколько строк refresh/notify.
- `movie_discovery: refresh started prev_cards=N rutracker_paused=bool jackett=bool` — старт refresh. **`rutracker_paused=True` сразу после старта = вероятная причина «пропавшего» фильма** (cooldown активен → Rutracker не опрашивается → пул карт меньше).
- `movie_discovery: sources fetched jackett_raw=N rutracker_raw=N accepted=N errors=jackett:N,rutracker:N` — сколько релизов отдали источники, сколько прошло первичные фильтры, какие источники упали с ошибкой.
- `movie_discovery: cache built cards=N prev_cards=N added=N removed=N top10=[title=kp,…]` — финальный кэш перед записью на диск, состав топ-10. Сравни с предыдущим refresh.
- `movie_discovery: cards diff added_kp=… removed_kp=…` — точечный diff kp_id. **Главное место для разбора race / transient ошибок.** Если в одном refresh kp_id есть, а в следующем убрался — найди что упало в `sources fetched`.
- `movie_discovery: query 'YYYY 1080p' had errors and 0 accepted results — will supplement from prev cache to avoid losing year=X/quality=Y` — все источники для этого (год, качество) упали и реальных релизов не пришло. Подозрительный пустой результат → фоллбэк на прошлый кэш для этой комбинации (не позволяем деградированному refresh'у переписать хороший топ-10).
- `movie_discovery: supplemented N releases from prev cache (failed_indexers=... failed_specs=...)` — сколько релизов перенесли из прошлого кэша. **`failed_indexers`** — Jackett-индексеры с Status=1 или Results=0+Error в ответе (per-indexer detection). **`failed_specs`** — query-уровень, когда вообще ни один источник не вернул контент. Подтягиваем prev releases по `tracker in failed_indexers` (точечно) или по (year, quality) для full-fail-specs.
- `movie_discovery: Jackett indexer 'X' failed for 'Y' (status=N results=N error=...) — will supplement from prev` — сработала per-indexer detection: Jackett OK overall, но конкретный индексер X молчал на запросе Y. Будем использовать prev данные только для этого X.
- `movie_discovery: failed indexers not in rating (info-only, no retry): [...]` — список индексеров что упали, но юзер их отключил в `/admin → 🎬 Трекеры новинок`. Их failures **не считаются** degradation — лог есть, но retry не запускается, ready signal не блокируется. Пример: `noname-club` без FlareSolverr навсегда вернёт error.
- Admin ready notification теперь делит failures на две группы: **⚠️ Индексеры рейтинга с проблемами** (gates ready пока backoff не исчерпан) и **ℹ️ Прочие индексеры (не влияют на /new)** (info-only, никогда не блокируют ready).
- `movie_discovery: degraded refresh (streak=N, failed_specs=...) — retry in Xs` — частичная неудача обнаружена, следующий refresh **opportunistically** через 3 → 10 → 30 мин (вместо обычных 12ч). После 3 неудач возвращаемся к 12ч интервалу — нет смысла долбить упавший источник вечно.
- `movie_discovery: recovered after N failed refreshes` — успешный refresh после серии деградаций → админ получает push «✅ Поиск восстановился после N неудачных попыток».
- **Админ-нотификации**: «✅ Поиск разогрет, бот полноценно функционирует» — первый успешный refresh после старта бота (один раз за процесс). «✅ Поиск восстановился после N неудачных попыток» — после streak ≥ 1 неудачного refresh'а.
- `movie_discovery: notify start subscribers=N top10_kp=[…]` — начали рассылку, какой топ-10 берётся.
- `movie_discovery: notify chat=N candidates=N kp_ids=[…]` — что собрался пушить конкретному пользователю (с учётом его notified/shown флагов + consensus C-lite через prev_top10).
- `movie_discovery: notify chat=N no_new (all top10 already notified/shown)` — нечего пушить, пользователь всё уже видел.
- `movie_discovery: notify skipped — first refresh after startup` — Layer A защиты от ложных push'ей: на самом первом refresh после рестарта Jackett ещё «холодный», его выдача нестабильна, push подавлен. Следующий регулярный refresh уже пушит нормально.
- `movie_discovery: notify skipped — regression detected removed_pct=X% prev_top10=N current_common=M` — Layer B: новый top-10 потерял слишком много фильмов из прошлого top-10 (>60%). Признак нестабильного refresh (Jackett прогревается дольше одного цикла / временный сбой одного из источников). Push подавлен целиком.
- `movie_discovery: notify sent chat=N pushed=N kp_ids=[…]` — успешная отправка, `notified_at` обновлён.
- `movie_discovery: notify failed chat=N candidates=N kp_ids=[…]` — отправка упала, `notified_at` НЕ обновлён (попробуем ещё раз на следующем рефреше).
- `movie_discovery: /new render path=command|refresh_callback|open_callback chat=N cache_cards=N top10_kp=[…]` — каждый раз когда пользователь видит `/new`. **Сопоставь kp_ids в `notify sent` и в `/new render path=open_callback` после клика «🎬 Открыть /new»** — если push'нутый kp_id отсутствует в render, баг воспроизвёлся.
- `movie_discovery: /new render path=refresh_callback chat=N — refreshing now` + `… post_refresh cache_cards=N top10_kp=[…]` — пользователь нажал «🔄 Обновить»; первая строка перед refresh, вторая после. Сравни с предыдущим состоянием.

**Как использовать для диагностики бага «пушнули, а фильма нет»:**
1. Найди `notify sent chat=<твой_chat_id>` с kp_id злосчастного фильма.
2. Найди ближайшую следующую `render path=open_callback chat=<твой_chat_id>` — там видно что лежит в кэше на момент клика.
3. Если kp_id отсутствует — смотри между этими событиями `refresh started` → `cards diff removed_kp=…` → ищи фильм там.
4. Сопоставь с `sources fetched`: если в refresh, который удалил фильм, `rutracker_raw=0` или `errors=rutracker:N>0` — это transient ошибка Rutracker после рестарта.

### Когда найден новый баг — сюда же

Если выяснили причину «почему что-то не работало» по логам, и логи помогли — стоит добавить новый маркер в этот список. Лог-строки в коде живут вместе с фичей, эта таблица — оглавление для будущих сессий.
