# PlexLoader Architecture

> Mid-level architecture of PlexLoader — Telegram-бот для домашнего киносервера
> на базе Synology + Plex. Документ описывает как взаимодействуют все
> компоненты: от железа и сетевой инфраструктуры до бизнес-флоу
> доставки фильма пользователю.

---

## Содержание

1. [Overview](#1-overview)
2. [Слои инфраструктуры](#2-слои-инфраструктуры)
3. [Модули бота](#3-модули-бота)
4. [Хранилище состояния](#4-хранилище-состояния-json-files)
5. [Фоновые циклы](#5-фоновые-циклы)
6. [Пользовательские флоу](#6-пользовательские-флоу)
7. [Cloudflare Proxied + DDNS — детально](#7-cloudflare-proxied--ddns--детально)
8. [Технологический стек](#8-технологический-стек)
9. [Что построили: timeline](#9-что-построили-timeline)

---

## 1. Overview

```mermaid
graph TD
    User[👤 Users<br/>Telegram<br/>iOS / Android / Desktop]

    subgraph Internet
        TG[Telegram Bot API]
        CF[☁️ Cloudflare<br/>DNS + Proxy + TLS]
        KP[Kinopoisk API]
        RT[Rutracker.org]
    end

    subgraph Home[Home network]
        Router[🛜 Router Keenetic<br/>XKeen TPROXY · inadyn DDNS<br/>NAT 80, 8080 → NAS]

        subgraph NAS[Synology DSM 7.3]
            Bot[🤖 tg-torrent-bot<br/>Container Manager]
            Web[🌐 Web Station<br/>plex.html redirect]
            DS[💾 Download Station]
            Plex[🎬 Plex Media Server]
            Jackett[🔍 Jackett]
        end
    end

    User -.poll updates.-> TG
    User -.tap Plex deeplink.-> CF
    User -.tap commands.-> TG

    TG <--> Bot
    Bot <--> Jackett
    Jackett --> RT
    Bot --> KP
    Bot <--> DS
    Bot <--> Plex
    Bot --> RT

    CF -.HTTPS:443.-> Router
    Router -.HTTP:80.-> Web

    Router <-.api updates.-> CF

    DS -.files.-> Plex
    Web -.serves plex.html.-> User
```

**Главные потоки:**

- **Command path**: User → Telegram → Bot → Jackett/RT/Plex/DS → ответ пользователю.
- **Deeplink path**: User → Cloudflare (HTTPS) → Router (NAT) → Web Station (HTTP) → JS-redirect → нативный Plex app или Plex Web.
- **Self-healing**: Router-side inadyn держит CF DNS-запись синхронной с реальным WAN-IP.

---

## 2. Слои инфраструктуры

### 2.1. Сеть и роутер

```mermaid
graph LR
    Internet([🌍 Internet]) --> Router[🛜 Keenetic<br/>WAN: 203.0.113.x<br/>LAN: 192.168.1.1/24]

    Router -- "port 80 → 192.168.1.X:80" --> NAS[💻 NAS<br/>192.168.1.X]
    Router -- "port 8080 → 192.168.1.X:80<br/>(legacy)" --> NAS

    Router --> Phones[📱 iOS / Android]
    Router --> Desktops[🖥 Desktop]

    XKeen{{XKeen TPROXY<br/>исходящий → VPN}} -.affects.- Phones
    XKeen -.affects.- Desktops
    XKeen -.NOT affects.- NAS_in["входящие на NAS<br/>через NAT"]
```

**Важно про XKeen:**
- TPROXY перехватывает **исходящий** трафик из LAN, идущий в интернет (для bypass-блокировок).
- **Входящие** соединения (CF → Router → NAS) идут через PREROUTING NAT **до** TPROXY-цепочек — XKeen их не трогает.
- inadyn запущен **на самом роутере** и читает IP с интерфейса eth3 локально, не делая внешних запросов → тоже минует XKeen.

### 2.2. NAS (Synology DSM 7.3.2)

```mermaid
graph TB
    subgraph NAS[💻 Synology NAS · DSM 7.3.2]
        CM[📦 Container Manager]
        WS[🌐 Web Station]
        DS[💾 Download Station]
        PMS[🎬 Plex Media Server]
        FS[(📁 /volume1/video<br/>shared folder)]
        State[(📋 /volume1/docker/.../state<br/>JSON state files)]

        CM --> Bot[tg-torrent-bot<br/>container]
        WS --> Portal1[Portal: plex.example.com<br/>HTTP:80<br/>document root: /web/plex-redirect]
        WS --> Portal2[Portal: morplex.sknt.ru<br/>HTTP:80<br/>legacy]

        Bot --> State
        Bot -.API.-> DS
        Bot -.API.-> PMS
        DS --> FS
        PMS --> FS
    end
```

| Сервис | Роль | Связь с ботом |
|---|---|---|
| **Container Manager** | Запускает Docker-контейнер `tg-torrent-bot` | Host: env-переменные, mount state-папки |
| **Web Station** | Отдаёт статический `plex.html` для redirect Plex deeplink | Используется как hosted page для CF-proxied URL |
| **Download Station** | Принимает .torrent / magnet через API, выполняет загрузку | Bot вызывает `create_torrent_file` / `create_magnet`, polls `list_tasks` |
| **Plex Media Server** | Индексирует библиотеку, отдаёт metadata | Bot вызывает PlexAPI для pre-check, polling после скачивания |
| **File Station** | Общая `/volume1/video` для DS и Plex | DS пишет туда, Plex читает оттуда |

### 2.3. Роутер (Keenetic + entware + XKeen + inadyn)

| Компонент | Назначение |
|---|---|
| **Keenetic OS** | Базовая прошивка, NAT, firewall, WAN PPPoE |
| **OPKG/entware** | Пакетный менеджер OpenWRT-style (`/opt/`) |
| **XKeen** | Xray-клиент для bypass-блокировок (исходящий трафик из LAN) |
| **inadyn** | DDNS-клиент: читает IP с `eth3`, обновляет Cloudflare DNS каждые 5 мин |
| **Port forwarding** | `80 → NAS:80` (для CF Proxied), `8080 → NAS:80` (legacy) |

### 2.4. Внешние сервисы

| Сервис | Назначение | Доступ |
|---|---|---|
| **Telegram Bot API** | Long-polling + sendMessage | HTTPS / Bot Token |
| **Cloudflare** | DNS zone `example.com`, Proxied (HTTPS+TLS+anti-DDoS) | API Token (Zone:DNS:Edit) |
| **Jackett** | Единая точка для трекеров (RT, NNM, BFG, RuTor…) | HTTP / API Key |
| **Rutracker.org** | Прямой клиент `rutracker_client` как fallback для Jackett-proxy 404 | Login/password сессия |
| **Kinopoisk API** | Обогащение `/new` (рейтинг, постер, год, жанры) | kinopoiskapiunofficial.tech / API Key |

---

## 3. Модули бота

```mermaid
graph TD
    bot[bot.py<br/>~7000 строк, Telegram handlers + точка входа]

    bot --> dl[download_station.py<br/>DS API client]
    bot --> rt[rutracker.py<br/>RT login + search + download]
    bot --> jk[jackett.py<br/>Jackett search + download]
    bot --> plex[plex.py<br/>PlexClient · movies + shows + library cache]
    bot --> kp[kinopoisk.py<br/>KP API · search + cache]
    bot --> ss[state_store.py<br/>JSON persistence]
    bot --> md[movie_discovery.py<br/>/new ranking · _compute_card_score]
    bot --> tp[task_policies.py<br/>notification rules · recipients]
    bot --> tv[task_views.py<br/>card formatting]
    bot --> kb[keyboards.py<br/>InlineKeyboard builders]
    bot --> fmt[formatters.py<br/>regex helpers · series · quality]
    bot --> ac[access_control.py<br/>allowed/admin chat_ids]
    bot --> ts[tracker_service.py<br/>публичные трекеры для BT]
    bot --> jsubs[jackett_subscriptions.py<br/>RT/Jackett подписки на новые серии]
    bot --> diag[diagnostics.py<br/>проверка всех сервисов для /admin]
```

**Структурно:**
- **Handlers** (внутри `bot.py`) — все Telegram update-callback'и (`movie_new_command`, `search_download`, `admin_callback`, …).
- **Clients** — отдельные файлы для каждого внешнего сервиса.
- **State** — `state_store.py` — единственная точка работы с JSON-файлами.
- **Domain logic** — `movie_discovery.py` (рейтинг), `task_policies.py` (правила уведомлений), `formatters.py` (парсинг сериалов / качества).

---

## 4. Хранилище состояния (JSON files)

Все файлы в `/volume1/docker/tg-torrent-bot/state/` (mounted в контейнер).

| Файл | Назначение |
|---|---|
| `approved_chat_ids.json` | Список одобренных пользователей (`{chat_id: {name, added_at}}`) |
| `task_owners.json` | `task_id → chat_id` владельца задачи |
| `task_meta.json` | Per-task: `kind`, `title`, `year`, `quality`, `series_query`, `season_num` |
| `notified_tasks.json` | Per-task: `status_key`, `sent[]`, `failures{chat: N}`, `subscribers[]`, `plex_done` |
| `auto_delete_tasks.json` | `task_id → timestamp` для авто-очистки |
| `tracker_processed.json` | Set задач куда уже добавлены публичные трекеры |
| `topic_subscriptions.json` | RT/Jackett подписки на новые серии сериалов |
| `movie_discovery_cache.json` | Top-N фильмов `/new` с score, кэш KP, fingerprints |
| `movie_discovery_settings.json` | `jackett_trackers_enabled`, `movie_seen_by_user` (per-user 🆕 плашка), `subs` |
| `pending_downloads.json` | Отложенные загрузки с retry-state и TTL |

**Persistence guarantee:**
- Atomic write (через `os.replace` от tmp-файла).
- В критических местах (notifications) — `_save_notified_tasks` после каждой задачи, не в конце цикла.
- Auto-prune: `_run_prune_stale_state_once` чистит записи для удалённых из DS задач.

---

## 5. Фоновые циклы

```mermaid
graph TD
    Start[Bot startup] --> L1[_movie_discovery_loop<br/>interval: 12h]
    Start --> L2[_tracker_background_loop<br/>interval: 180s]
    Start --> L3[_task_maintenance_loop<br/>interval: 180s]
    Start --> L4[_subscription_check_loop<br/>interval: 6h]

    L3 --> M1[_run_task_notifications_once<br/>push на finished/seeding/error]
    L3 --> M2[_run_auto_delete_finished_once<br/>удаление старых задач]
    L3 --> M3[_run_pending_downloads_gated<br/>gate 5 мин]
    L3 --> M4[_run_prune_stale_state_once<br/>чистка JSON]

    M1 -.spawn per task.-> Poll[_plex_poll_after_finish<br/>каждые 30с до 10 мин]
```

| Loop | Интервал | Что делает |
|---|---|---|
| `_movie_discovery_loop` | 12h | Refresh top-N фильмов из Jackett + KP enrichment; per-user push новинок |
| `_tracker_background_loop` | 180s | Добавление публичных трекеров к BT-задачам |
| `_task_maintenance_loop` | 180s | Объединяет 4 подзадачи (см. ниже) |
| → `_run_task_notifications_once` | каждый tick | Push при finished/seeding/error; classify transient/permanent |
| → `_run_auto_delete_finished_once` | каждый tick | Удаление DS-задач старше TTL |
| → `_run_pending_downloads_gated` | gate 5 мин | Retry отложенных скачиваний |
| → `_run_prune_stale_state_once` | каждый tick | Чистка записей для удалённых из DS задач |
| `_plex_poll_after_finish` | spawned per task, 30s × 20 | Поиск файла в Plex после finished; push «✅ добавлен в Plex» |
| `_subscription_check_loop` | 6h | Проверка RT/Jackett подписок на новые серии |

---

## 6. Пользовательские флоу

### 6.1. Поиск и скачивание

```mermaid
sequenceDiagram
    actor U as User
    participant B as Bot
    participant J as Jackett
    participant R as Rutracker (direct)
    participant K as Kinopoisk
    participant P as Plex
    participant D as Download Station

    U->>B: /search "Аркейн сезон 1"
    B->>J: search(query, indexers=[RT, NNM, …])
    J-->>B: results
    B->>K: enrich (rating, poster, year)
    K-->>B: KP data
    B-->>U: top-N карточек
    U->>B: tap «✅ Скачать»
    B->>P: pre-check (фильм уже в библиотеке?)
    P-->>B: not found
    B->>J: download_torrent(proxy_url)

    alt Jackett proxy успешен
        J-->>B: .torrent bytes
    else Jackett 404
        B->>R: download_torrent(topic_id) (fallback)
        alt RT direct успешен
            R-->>B: .torrent bytes
        else RT тоже failed
            B->>J: re-search для свежего URL
            B->>D: create_magnet (если magnet_url есть)
        end
    end

    B->>D: create_torrent_file(bytes, name)
    D-->>B: task_id (dbid_N)
    B->>B: remember owner + meta
    B-->>U: «✅ Задача добавлена»
```

### 6.2. Финальное уведомление + Plex polling

```mermaid
sequenceDiagram
    participant D as Download Station
    participant B as Bot
    participant T as Telegram
    actor U as User
    participant P as Plex

    Note over B: _task_maintenance_loop tick (180s)
    B->>D: list_tasks()
    D-->>B: task=dbid_471 status=finished
    B->>B: уже отправляли?<br/>(notified_tasks.json)

    alt push нужно отправить
        B->>T: sendMessage(chat_id, text, button=«▶️ Открыть Plex»)
        alt success
            T-->>B: ok
            B->>B: sent_recipients.add(chat_id)
        else BadRequest "url is invalid"
            T-->>B: 400
            B->>B: classify=message_format_bug<br/>(do NOT count against chat)
        else Forbidden
            T-->>B: 403
            B->>B: failures[chat_id]++<br/>(permanent)
        else RetryAfter / Timeout
            T-->>B: 429 / timeout
            B->>B: classify=transient<br/>retry next cycle
        end
    end

    B->>P: spawn _plex_poll_after_finish(task)
    loop каждые 30с, до 10 мин
        P-->>B: library refresh
        B->>P: lookup by meta.kind+title+series_query
        alt match
            P-->>B: found
            B->>T: sendMessage «✅ X добавлен в Plex»<br/>button «▶️ Смотреть в Plex»
            T-->>U: 🔔 push
        end
    end
```

### 6.3. Открытие фильма в Plex (deeplink)

```mermaid
sequenceDiagram
    actor U as User on iPhone
    participant T as Telegram
    participant C as Cloudflare
    participant R as Router
    participant W as Web Station
    participant App as Plex iOS app

    U->>T: tap «▶️ Смотреть в Plex»
    T->>U: launches https://plex.example.com/plex.html?key=...&server=...
    U->>C: HTTPS GET (CF cert)
    Note over C: TLS termination<br/>resolve A record → 203.0.113.42
    C->>R: HTTP GET 203.0.113.42:80
    R->>W: NAT 80 → 192.168.1.X:80
    W-->>C: 200 OK · plex.html
    C-->>U: HTTPS response with HTML

    Note over U: Safari renders<br/>JS executes
    U->>U: location.href = "plex://preplay/?key=...&server=..."

    alt iOS Plex app installed
        U->>App: открывается app<br/>(пока без deep link — только home)
    else
        U->>U: fallback link «в браузере»
    end

    Note over U: На desktop: вместо plex://<br/>идёт https://app.plex.tv/desktop/#!/server/.../details?key=...<br/>→ Plex Web показывает фильм
```

### 6.4. Смена WAN IP провайдером

```mermaid
sequenceDiagram
    participant ISP as Internet Provider
    participant R as Router (eth3)
    participant I as inadyn (on router)
    participant CF as Cloudflare API
    participant DNS as CF DNS

    Note over R: WAN: 203.0.113.42
    ISP->>R: PPPoE reconnect, new IP
    Note over R: WAN: 203.0.113.99

    loop every 5 min
        I->>R: ip -4 addr show eth3
        R-->>I: 203.0.113.99
        I->>I: cache mismatch detected
        I->>CF: PUT /zones/.../dns_records/RECORD_ID<br/>{content: "203.0.113.99", proxied: true}
        CF->>DNS: update
        CF-->>I: success
        I->>I: cache := 203.0.113.99
    end

    Note over DNS: ~30s propagation<br/>clients access same hostname<br/>plex.example.com
```

### 6.5. Pending download queue

```mermaid
sequenceDiagram
    actor U as User
    participant B as Bot
    participant Q as pending_downloads.json
    participant J as Jackett
    participant R as Rutracker direct
    participant T as Telegram

    U->>B: «✅ Скачать»
    B->>J: download_torrent (Jackett 404)
    B->>R: rutracker direct (RT down)
    B-->>U: ошибка + кнопки «🔄 Повторить / ⏳ В очередь / ✖️ Закрыть»
    U->>B: tap «⏳ В очередь»
    B->>Q: save entry (TTL 24h)
    B-->>U: «⏳ Поставлено в очередь»

    loop _run_pending_downloads_gated · каждые 5 мин
        B->>Q: load entries
        alt expired > 24h
            B->>T: «⌛ Не удалось за 24ч»
            B->>Q: remove entry
        else retry
            B->>J: download
            alt success
                B->>T: «✅ Отложенная загрузка стартовала»
                B->>Q: remove entry
            else fail
                B->>Q: attempts++, last_error
            end
        end
    end
```

### 6.6. /new push о новинках

```mermaid
sequenceDiagram
    participant L as _movie_discovery_loop
    participant J as Jackett
    participant K as Kinopoisk
    participant C as movie_discovery_cache.json
    participant B as Bot
    participant T as Telegram
    actor U as User (subscriber)

    Note over L: every 12h
    L->>J: search by discovery_queries (years × qualities)
    J-->>L: raw results
    L->>K: enrich (rating, votes, poster)
    K-->>L: enriched
    L->>L: score = rating·0.35 + recency·0.20 + popularity·0.20 + tech·0.25
    L->>C: save top-N (sorted by score)

    L->>L: for each subscriber: diff new in top-10 vs notified
    alt new films found
        L->>T: sendMessage<br/>«🎬 Новые фильмы в /new: …»<br/>button «🎬 Открыть /new»
        T-->>U: 🔔 push
        U->>B: tap «🎬 Открыть /new»
        B->>C: load cards
        B->>B: render per-user (🆕 на новых)
        B-->>U: list with badges
    end
```

### 6.7. Admin: сброс failure-счётчиков

```mermaid
sequenceDiagram
    actor A as Admin
    participant B as Bot
    participant N as notified_tasks.json
    participant T as Telegram

    A->>B: /admin
    B->>N: load
    B->>B: _count_stuck_notifications<br/>(any failures[chat] >= 3)
    B-->>A: «⚠️ Зависших уведомлений: 3<br/>тапни «🔄 Сбросить счётчики (3)»»
    A->>B: tap reset
    B->>N: for each entry: failures = {}
    B->>N: save
    B-->>A: «✅ Сброшено счётчиков для 3 задач»

    Note over B: следующий tick _task_maintenance_loop<br/>попытается доставить push'и заново
```

---

## 7. Cloudflare Proxied + DDNS — детально

```mermaid
sequenceDiagram
    actor U as User browser
    participant CF as Cloudflare Edge
    participant R as Router (203.0.113.42)
    participant N as NAS · Web Station
    participant I as inadyn (router)
    participant API as CF API

    rect rgba(245, 158, 11, 0.15)
        Note over U, N: Запрос на страницу
        U->>CF: GET https://plex.example.com/plex.html?key=...
        Note over CF: TLS termination<br/>cert: *.example.com<br/>DNS A: 203.0.113.42
        CF->>R: HTTP GET 203.0.113.42:80 (Flexible mode)
        R->>N: NAT 80 → 192.168.1.X:80
        N-->>R: 200 OK plex.html
        R-->>CF: response
        CF-->>U: HTTPS response (cached)
    end

    rect rgba(59, 130, 246, 0.15)
        Note over I, API: DDNS sync (5 мин)
        I->>R: read IP from eth3 (local)
        R-->>I: 203.0.113.42
        I->>I: compare with cache
        alt no change
            Note over I: do nothing
        else IP changed
            I->>API: PUT /dns_records/REC_ID<br/>(token: Bearer ...)
            API-->>I: success
            I->>I: update cache
        end
    end
```

**Ключевые моменты:**

| Уровень | Что делает |
|---|---|
| **TLS** | Сертификат `*.example.com` выдан Cloudflare автоматически (Universal SSL). Клиент видит HTTPS зелёный замок. |
| **Origin protocol** | SSL/TLS mode `Flexible` — CF идёт к origin по HTTP. Не нужен LE на NAS. |
| **WAN IP скрытие** | Клиент видит только CF IP, не наш реальный. Защита от direct attack. |
| **Anti-DDoS** | Бесплатный basic от CF, фильтрует bot traffic. |
| **DDNS** | inadyn на роутере читает локально WAN IP (минуя XKeen), обновляет CF DNS API. Сервис устойчив к смене IP провайдером. |

---

## 8. Технологический стек

| Слой | Технологии |
|---|---|
| **Bot runtime** | Python 3.14, `python-telegram-bot` 22.x, `asyncio`, `httpx` |
| **Bot deployment** | Docker (Synology Container Manager), Alpine base |
| **State** | Plain JSON-файлы с atomic write (через tmp + `os.replace`) |
| **Tests** | `pytest` 9.x, `unittest.mock`, 645+ тестов |
| **External APIs** | Telegram Bot API, Cloudflare API, Plex Media Server API, Kinopoisk API, Jackett (Torznab) |
| **NAS OS** | DSM 7.3.2 (Synology) |
| **Router** | Keenetic (OPKG/entware), XKeen (Xray), inadyn 2.12 |
| **DNS / CDN** | Cloudflare (free plan, Proxied) |
| **Doc** | Markdown + Mermaid (рендерится GitHub UI), PowerPoint (.pptx) |

---

## 9. Что построили: timeline

Хронологический порядок ключевых коммитов (новейший снизу — последовательность фикса/добавления фич):

| Commit | Что |
|---|---|
| `3fe6ea1` | fix(/new): resort cards on cache hit; rename Plex unmatched button |
| `d8339cf` | feat(search): support SxxExx series format in season filters |
| `240649f` | ui: shorten Plex unmatched notify toggle to fit on mobile |
| `7a5c17f` | feat(search): offer relaxed filter retries on 'no results' dead-ends |
| `98dd02d` | fix(notifications): retry transient errors without penalty; persist state per-task |
| `0497780` | feat(download): auto-fallback to rutracker_client on Jackett proxy failure |
| `a4c3096` | feat(download): compact error message with retry button on download failure |
| `141be72` | feat(download): pending download queue with auto-retry and TTL drop |
| `c1a4bc9` | fix(notifications): log skip reasons and recover recipients from task-card registry |
| `a015880` | fix(notifications): replace plex:// URL; protect classifier from format bugs; admin reset |
| `6c2f15d` | docs: rebrand to CineDownload and reorganise feature list by user-facing priority |
| `1e71e04` | feat(plex): configurable deep-link redirect for native iOS app; close buttons |
| `d3fec88` | fix(plex): fall back to title-only lookup when series year mismatches premiere year |
| **+ infra** | Web Station portal + Cloudflare Proxied + inadyn DDNS на роутере |

**Уроки:**
1. Telegram изменил политику URL-схем в inline-кнопках (отказались от `plex://`) — нужны https-redirect страницы.
2. Plex iOS app в последних версиях игнорирует deeplink-параметры — Plex Web остаётся единственным working путём для «открыть фильм X».
3. NAT-based home setup требует defensive DDNS (даже если у вас «белый» IP — провайдер может его сменить без предупреждения).
4. Per-chat failure counters в notification-логике должны различать **наш баг** (формат сообщения) и **реальные проблемы** chat — иначе один сломанный URL парализует доставку всех push для всех пользователей навсегда.

---

## Что дальше смотреть

- **README.md** — пользовательская инструкция (команды, env-переменные, setup).
- **CLAUDE.md** — правила проекта + карта диагностических логов (искать в логах эти маркеры при сбоях).
- **`.env.example`** — все настраиваемые env-переменные с дефолтами.
- **`compose.yaml`** — Docker-compose для Synology Container Manager.
