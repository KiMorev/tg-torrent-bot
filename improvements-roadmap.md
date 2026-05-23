# PlexLoader — Improvements Roadmap

Живой документ. Источник правды для приоритизации работы между сессиями.
Обновлять при завершении пунктов (✅) или появлении новых из эксплуатации.

**Последнее обновление:** 2026-05-23 (после 1.2)

---

## Структура

Три источника пунктов:

1. **Paket 1-3** — из Codex development brief (`tg-torrent-bot-development-brief.md`),
   отфильтровано через мою аналитику (✅ Agree / ⚠️ С оговорками).
2. **Recommendations** — мои предложения сверх брифа, из эксплуатационной практики.
3. **Deferred** — пункты брифа отложенные до явного решения (нужно явное «да» от @morev).

---

## Paket 1 — Stabilization (immediate)

Текущий рабочий поток. Каждый пункт = один коммит.

### ✅ 1.1 GPT cost diagnostics fix
**Статус:** ГОТОВО (`feat(gpt): real token/model accounting`, см. log).
Plumb реальных `input_tokens`/`output_tokens`/`model` из OpenAI response через
`usage_sink` kwarg во всех 4 wrapper'ах. Pricing table per-model + longest-prefix matching.
Unknown-model fallback с counter. 11 новых тестов. README обновлён.

### ✅ 1.2 P0 subscription bugs sweep
**Статус:** ГОТОВО.

Под-задачи:
- [x] **Bug A:** `_check_jackett_sub_via_rutracker_direct` — не двигать
      `last_episode_end` если download failed (DSM error или RT error).
- [x] **Bug B:** Jackett-fast-path (`_check_jackett_sub_via_rutracker_direct`)
      теперь уважает `notify_mode=season_complete` — silent advance вместо
      intermediate push, push только когда season done.
- [x] **Bug C:** `_check_jackett_subscriptions` — silent-advance в
      season_complete только если task_id успешен. При failed download
      падает в notify-with-error ветку для retry на следующем check.
- [x] **Bug D:** Plex duplicate confirm для series-ветки теперь содержит
      `notify_mode` в `plex_pending` — раньше при подтверждении сезонной
      подписки тихо downgrade'илась до per_episode.
- [x] **Bug F:** Retry/queue сохраняют subscribe-intent + notify_mode через
      `srch_last_subscribe` / `srch_last_notify_mode` в user_data; pending-loop
      на successful download автоматически восстанавливает подписку (Jackett
      и Rutracker) с сохранённым notify_mode + шлёт юзеру подтверждение.
- [ ] **Bug E (RMW race)** — отложено в roadmap отдельным пунктом 1.2.E.
      Реальный риск редкий (только при одновременном unsubscribe-клике во
      время фонового check), а правильный фикс (re-load+merge при save или
      asyncio.Lock на весь loop) тянет на отдельный коммит с осторожностью.

12 новых тестов в `tests/test_subscription_p0_bugs.py`.

### ✅ 1.3a notify_policy + download_policy split (storage + plumbing)
**Статус:** ГОТОВО. UI ещё не открыт (см. 1.3b).

Сделано:
- Новый модуль `subscription_policy.py` — source of truth для всех решений
  по подписке + миграция legacy `notify_mode` → `(notify_policy, download_policy)`
- Helpers `should_notify(sub, is_complete)` и `should_download(sub, is_complete)`
  с lazy-fallback на legacy notify_mode (устойчивы к не-мигрированным subs)
- `state_store.load_topic_subscriptions()` мигрирует in-flight (идемпотентно)
- `build_jackett_subscription()` принимает `notify_policy`/`download_policy`
  и всегда эмитит мигрированную форму
- 3 background loop'а (`_check_subscriptions`, `_check_jackett_subscriptions`,
  `_check_jackett_sub_via_rutracker_direct`) переписаны под helpers вместо
  inline-if'ов по `notify_mode`
- Уведомления получили 3-ю ветку «авто-загрузка отключена для этой подписки»
  (для `download_policy=notify_only`)
- Plex pending + retry/queue + `_notify_pending_success` пробрасывают
  новые поля через всю цепочку (как 1.2 для notify_mode)
- Открыт новый режим `download_policy=only_when_complete` — реальный
  «жди полный сезон перед загрузкой»
- 21 новый тест в `test_subscription_policy.py`

### ⏳ 1.3b UI (presets + advanced) — NEXT
- 4 пресет-кнопки + ⚙️ Advanced → двухшаговое меню
- Hint-line над клавиатурой про "push" / "качать"
- Финальная клавиатура:
  ```
  📺 Каждую серию + push
  🎯 Каждую серию, push в конце
  📦 Скачать после финала сезона
  🔕 Без скачивания, только push
  ⚙️ Настроить вручную
  ```

---

## Paket 2 — Architectural prep

Делаем после Paket 1. Каждый — отдельный pre-work commit + потенциальный refactor.

### ⏳ 2.1 Search provider contract abstraction
Рефакторинг 3-way fallback (Jackett → RuTracker direct → ничего) в provider layer
с единым контрактом `SearchProvider.search(query, quality) → list[Result]`.
Подготовка под Freedomist и другие источники.

### ⏳ 2.2 Storage boundary decision
Решение: SQLite для новых stateful сущностей (subscriptions с историей,
notifications log, потенциальный MediaRequest) — или продолжаем JSON?
Pros JSON: простой backup, читается tail/jq. Pros SQLite: атомарные writes,
запросы по индексам. Триггер решения — следующая stateful фича.

---

## Paket 3 — Prototypes (parallel-safe)

Не блокирует Paket 1/2. Можно делать когда есть отдельный час.

### ⏳ 3.1 Freedomist API hand-test
curl-уровень прототип на 2-3 запросах для оценки:
- Скорость отклика, формат данных, наличие seeders.
- Доля overlap с RuTracker/Jackett (если ≥80% → нет смысла).
- Качество для русскоязычных запросов.

Решение «делать integration» — только после прототипа.

---

## Recommendations (мои предложения сверх брифа)

Топ-3 после Paket 1+2.

### ✅ R.1 Disk-space guard for DS
**Статус:** ГОТОВО.

Реализовано:
- `DownloadStationClient.get_volume_info()` через `SYNO.Core.Storage.Volume.list`
  с longest-prefix matching между volume_path и DS_DESTINATION
- 60s client-side cache (`use_cache=False` для diagnostics force-refresh)
- `bot._check_disk_space_for_download()` с порогами 5% (block) / 15% (warn)
- Гард внутри `_download_and_add` — при <5% возвращает error с retry-кнопкой
  и `ConversationHandler.END`; при <15% продолжает + лог
- Diagnostics: строка «💾 Место: свободно X ГБ из Y ГБ (Z%) [/volume1]»
  в Download Station блоке + автоматический warn/error статус
- Graceful: missing `get_volume_info`, None response, любые исключения →
  silent skip (никогда не блокирует download из-за check-failure)
- 20 новых тестов, README обновлён

### ⏳ R.2 Plex pre-existence по сезонам
**Риск без:** 🟡 дубликаты сезонов лежат и занимают место.
**Сложность:** 4-6 часов.

При поиске сериала + S0X: распарсить какие сезоны уже в Plex.
- `✅ S01-S04 уже в Plex` если ищется S05 → ок, ничего особенного
- `⚠️ У вас уже есть S03` если ищется именно S03 → confirm «добавить дубликат?»

### ⏳ R.3 Health endpoint + weekly backup
**Риск без:** 🟡 один partial-write JSON и subscriptions потерялись.
**Сложность:** 3-4 часа.

- HTTP `/healthz` на :8080 → `{"status","last_refresh","gpt","jackett","plex"}`
- Cron weekly: tar.gz всех state-файлов в `/data/backups/YYYY-MM-DD.tar.gz`
- Retention: 4 недели
- Watchtower может читать healthz, uptimerobot тоже

### Средний приоритет (по фидбеку)

- **R.4** Auto-cleanup завершённых задач DS (опция в /admin, off-default)
- **R.5** Re-search «лучшая раздача появилась» (seeders <5 + альтернатива >2x за 24ч)
- **R.6** Структурированный rate limit + backoff для GPT/KP с persisting через рестарт
- **R.7** Метрики в `/admin → 📊 Метрики` (refresh-частота, Jackett latency, % успешных downloads)
- **R.8** «Тихий час» 23:00-8:00 (per-chat или глобально), push'ы аккумулируются
- **R.9** Inline-команда `@PlexLoaderBot Дюна 2024` для быстрого поиска из любого чата

### Низкий приоритет / fun

- **R.10** Цитирование KP-обзоров в карточке /new (1 цитата ≤120 chars)
- **R.11** Plex deeplink с конкретным эпизодом (S0XE0Y) когда мы знаем какой добавился
- **R.12** `/stats` command — «сколько фильмов за месяц/год»
- **R.13** Web UI на :8081 (read-only, view subscriptions/queue без TG)
- **R.14** Telegram-stars / premium emoji для админа
- **R.15** Структурное логирование (JSON-logs) — для удобной grep/jq

---

## Deferred (большой scope, нужно явное решение)

Из брифа. Каждый = серьёзная архитектурная стройка, не делать наотмашь.

### D.1 MediaRequest Engine
Большой рефактор state. **Блокер:** сначала Paket 2.2 (storage decision).
После SQLite-решения возвращаемся.

### D.2 SeriesRequest / SeasonWatch entities
Зависит от D.1 + Paket 1.3 (policy split).
Естественная следующая ступень после policy split.

### D.3 Multi-season batch planner
Зависит от D.2. Не раньше.

### D.4 NL Intent parser («хочу что-то атмосферное»)
**Не блокер:** новый user-flow, не фикс существующего.
Полезность спорная, GPT-затраты на каждый запрос. Подождать конкретный фидбек.

### D.5 Descriptive media query resolver
Часть D.4. Тот же аргумент.

### D.6 Big ranker rewrite (формализованная модель)
Текущий `_score_result` работает. Делать когда появится конкретный мисфит.

---

## ❌ Не делаем (мои Disagree из брифа)

Зафиксированы чтобы не возвращаться без триггера.

- Replace JSON polling with push-event-bus — для нашего масштаба overengineering
- Pre-compile torrent metadata в общий DB-индекс — GPT кэш уже один раз парсит
- Унифицировать ALL background loops в один scheduler — текущая структура читается легче
- Отдельный «recommendation» микросервис — у нас один бот
- User roles / permission tiers — admin + allowed_chat_ids достаточно
- Webhooks вместо long-polling Telegram — добавляет ingress-сложность без выгоды
- Distributed tracing / Sentry — docker logs grep достаточно для one-instance

---

## 🤷 Neutral — решать по ходу

- Pre-emptive Plex library refresh trigger (polling работает)
- Per-user notification quiet hours (overlap с R.8)
- Search history с авто-рекомендациями
- A/B-тестинг GPT-промптов

---

## Текущий next-action

Сделано: 1.1 ✅ · R.1 ✅ · 1.2 ✅ · 1.3a ✅

**Следующее по приоритету:** **1.3b UI** — открыть новые режимы (особенно
`only_when_complete`) пользователю через клавиатуру при создании подписки.
Бэкенд полностью готов, нужны только handlers + клавиатура.

После него:
- 1.2.E (RMW race protection — нужен отдельный осторожный коммит)
- R.2 Plex pre-existence по сезонам (видимая ценность)
- 2.1 Search provider contract abstraction
- R.3 Health endpoint + weekly backup
