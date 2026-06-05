/**
 * Build architecture.pptx — 13 slides covering CineDownload bot architecture.
 * Source of truth: ../ARCHITECTURE.md
 *
 * Style: corporate, white background, dark gray text, accent orange (#E5A00D - Plex brand).
 * Fonts: Calibri 32pt titles, 18pt body.
 *
 * Run: node build_architecture_pptx.js
 */

const PPTX = require("pptxgenjs");

const pres = new PPTX();
pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5 inches
pres.title = "CineDownload Architecture";

// Palette
const ACCENT = "E5A00D";       // Plex orange — primary accent
const ACCENT_DEEP = "AB7805";  // darker for hover/secondary
const BG_DARK = "1A1A1A";       // for title/closing slides
const TEXT_PRIMARY = "2A2A2A";  // body text
const TEXT_MUTED = "777777";    // captions
const CARD_BG = "F8F8F8";       // light card backgrounds
const CARD_BORDER = "E5E5E5";   // subtle borders

const FONT_HEAD = "Calibri";
const FONT_BODY = "Calibri";

// === HELPERS ===

function addAccentMark(slide, x = 0.5, y = 0.5, w = 0.4, h = 0.06) {
  slide.addShape(pres.ShapeType.rect, {
    x, y, w, h,
    fill: { color: ACCENT },
    line: { type: "none" },
  });
}

function addFootline(slide, label) {
  slide.addText(label, {
    x: 0.5, y: 7.1, w: 12.33, h: 0.3,
    fontSize: 10, color: TEXT_MUTED, fontFace: FONT_BODY,
    align: "left",
  });
  slide.addText("CineDownload · v1.0", {
    x: 11.5, y: 7.1, w: 1.83, h: 0.3,
    fontSize: 10, color: TEXT_MUTED, fontFace: FONT_BODY,
    align: "right",
  });
}

function addTitle(slide, title, subtitle) {
  addAccentMark(slide, 0.5, 0.55);
  slide.addText(title, {
    x: 0.5, y: 0.7, w: 12.33, h: 0.7,
    fontSize: 32, bold: true, color: TEXT_PRIMARY,
    fontFace: FONT_HEAD,
  });
  if (subtitle) {
    slide.addText(subtitle, {
      x: 0.5, y: 1.4, w: 12.33, h: 0.4,
      fontSize: 16, color: TEXT_MUTED,
      fontFace: FONT_BODY, italic: true,
    });
  }
}

// === SLIDE 1: TITLE ===
{
  const s = pres.addSlide();
  s.background = { color: BG_DARK };
  s.addShape(pres.ShapeType.rect, {
    x: 0, y: 3.4, w: 13.33, h: 0.08,
    fill: { color: ACCENT }, line: { type: "none" },
  });
  s.addText("CineDownload", {
    x: 0.5, y: 2.0, w: 12.33, h: 1.0,
    fontSize: 64, bold: true, color: "FFFFFF",
    fontFace: FONT_HEAD, align: "left",
  });
  s.addText("Architecture overview", {
    x: 0.5, y: 3.7, w: 12.33, h: 0.6,
    fontSize: 28, color: ACCENT,
    fontFace: FONT_HEAD, align: "left",
  });
  s.addText("Telegram-бот для домашнего киносервера на Synology + Plex.\nКак все компоненты связаны: от железа до доставки фильма пользователю.", {
    x: 0.5, y: 4.6, w: 12.33, h: 1.0,
    fontSize: 18, color: "CCCCCC",
    fontFace: FONT_BODY, align: "left",
  });
  s.addText("docs/architecture.pptx · параллельная версия — ARCHITECTURE.md в корне репо", {
    x: 0.5, y: 6.8, w: 12.33, h: 0.3,
    fontSize: 11, color: "888888",
    fontFace: FONT_BODY, italic: true,
  });
}

// === SLIDE 2: OVERVIEW ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "Overview", "Главные действующие лица системы");

  // Three vertical zones
  const zones = [
    {
      x: 0.5, y: 2.2, w: 4.0, color: ACCENT,
      title: "👤 Пользователь",
      body: "Telegram-клиент\n(iOS / Android / Desktop)\n\n• /search, /new, /admin\n• inline-кнопки → callbacks\n• клики по deeplink-кнопкам",
    },
    {
      x: 4.7, y: 2.2, w: 4.0, color: "2563EB",
      title: "🌐 Edge & DNS",
      body: "Cloudflare Free Plan\n\n• Zone example.com\n• A-запись plex (Proxied)\n• HTTPS termination (CF cert)\n• anti-DDoS, скрытие WAN IP",
    },
    {
      x: 8.9, y: 2.2, w: 4.0, color: "059669",
      title: "🏠 Home network",
      body: "Router + NAS\n\n• Keenetic + XKeen + inadyn\n• Synology DSM 7.3\n• Container Manager: bot\n• Web Station, DS, Plex",
    },
  ];
  zones.forEach(z => {
    s.addShape(pres.ShapeType.roundRect, {
      x: z.x, y: z.y, w: z.w, h: 4.4,
      fill: { color: CARD_BG },
      line: { color: z.color, width: 2 },
      rectRadius: 0.1,
    });
    s.addText(z.title, {
      x: z.x + 0.25, y: z.y + 0.2, w: z.w - 0.5, h: 0.6,
      fontSize: 22, bold: true, color: z.color,
      fontFace: FONT_HEAD,
    });
    s.addText(z.body, {
      x: z.x + 0.25, y: z.y + 0.95, w: z.w - 0.5, h: 3.3,
      fontSize: 14, color: TEXT_PRIMARY, fontFace: FONT_BODY,
    });
  });

  // Bottom callout
  s.addShape(pres.ShapeType.roundRect, {
    x: 0.5, y: 6.9, w: 12.33, h: 0.4,
    fill: { color: ACCENT },
    line: { type: "none" },
    rectRadius: 0.05,
  });
  s.addText("Главное правило: входящий трафик идёт CF → Router NAT → NAS; исходящий из NAS — через XKeen TPROXY, не наоборот", {
    x: 0.5, y: 6.9, w: 12.33, h: 0.4,
    fontSize: 12, bold: true, color: "FFFFFF",
    fontFace: FONT_BODY, align: "center", valign: "middle",
  });

  addFootline(s, "Slide 2 · Overview");
}

// === SLIDE 3: INFRASTRUCTURE STACK ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "Infrastructure stack", "Что запущено на NAS и роутере");

  // NAS card (left)
  const nasX = 0.5, nasY = 2.0, nasW = 6.0;
  s.addShape(pres.ShapeType.roundRect, {
    x: nasX, y: nasY, w: nasW, h: 4.7,
    fill: { color: CARD_BG },
    line: { color: ACCENT, width: 2 },
    rectRadius: 0.1,
  });
  s.addText("💻 Synology NAS · DSM 7.3.2", {
    x: nasX + 0.25, y: nasY + 0.2, w: nasW - 0.5, h: 0.4,
    fontSize: 18, bold: true, color: ACCENT, fontFace: FONT_HEAD,
  });

  const nasItems = [
    ["📦 Container Manager", "Запускает Docker-контейнер tg-torrent-bot, mount state-папки"],
    ["🌐 Web Station", "Portal plex.example.com:80 → отдаёт plex.html (deeplink redirect)"],
    ["💾 Download Station", "Принимает .torrent/magnet через API; долгая загрузка"],
    ["🎬 Plex Media Server", "Индексирует /volume1/video, отдаёт metadata API"],
    ["📁 /volume1/video/", "Shared folder: DS пишет, Plex читает"],
    ["📋 state/*.json", "10 JSON-файлов с persistent state бота"],
  ];
  nasItems.forEach((item, i) => {
    const y = nasY + 0.8 + i * 0.62;
    s.addText(item[0], {
      x: nasX + 0.25, y, w: nasW - 0.5, h: 0.3,
      fontSize: 13, bold: true, color: TEXT_PRIMARY, fontFace: FONT_BODY,
    });
    s.addText(item[1], {
      x: nasX + 0.25, y: y + 0.28, w: nasW - 0.5, h: 0.3,
      fontSize: 11, color: TEXT_MUTED, fontFace: FONT_BODY,
    });
  });

  // Router card (right)
  const rtX = 6.8, rtY = 2.0, rtW = 6.0;
  s.addShape(pres.ShapeType.roundRect, {
    x: rtX, y: rtY, w: rtW, h: 4.7,
    fill: { color: CARD_BG },
    line: { color: "2563EB", width: 2 },
    rectRadius: 0.1,
  });
  s.addText("🛜 Router · Keenetic + entware", {
    x: rtX + 0.25, y: rtY + 0.2, w: rtW - 0.5, h: 0.4,
    fontSize: 18, bold: true, color: "2563EB", fontFace: FONT_HEAD,
  });

  const rtItems = [
    ["Keenetic OS", "NAT, firewall, PPPoE; WAN: 203.0.113.x"],
    ["OPKG / entware", "Пакетный менеджер OpenWRT-style (/opt/)"],
    ["XKeen (Xray)", "TPROXY на исходящем трафике из LAN (bypass-VPN)"],
    ["inadyn 2.12", "DDNS-клиент: читает eth3, пушит в CF API каждые 5 мин"],
    ["Port forwarding", "80 → NAS:80 (CF Proxied), 8080 → NAS:80 (legacy)"],
    ["Важно", "XKeen не трогает входящие — только исходящие LAN→WAN"],
  ];
  rtItems.forEach((item, i) => {
    const y = rtY + 0.8 + i * 0.62;
    s.addText(item[0], {
      x: rtX + 0.25, y, w: rtW - 0.5, h: 0.3,
      fontSize: 13, bold: true, color: TEXT_PRIMARY, fontFace: FONT_BODY,
    });
    s.addText(item[1], {
      x: rtX + 0.25, y: y + 0.28, w: rtW - 0.5, h: 0.3,
      fontSize: 11, color: TEXT_MUTED, fontFace: FONT_BODY,
    });
  });

  addFootline(s, "Slide 3 · Infrastructure stack");
}

// === SLIDE 4: BOT MODULES ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "Bot modules", "Внутренняя структура bot.py и зависимости");

  // Central node: bot.py
  s.addShape(pres.ShapeType.roundRect, {
    x: 5.5, y: 3.4, w: 2.3, h: 1.0,
    fill: { color: ACCENT }, line: { type: "none" },
    rectRadius: 0.08,
  });
  s.addText("bot.py", {
    x: 5.5, y: 3.45, w: 2.3, h: 0.5,
    fontSize: 22, bold: true, color: "FFFFFF",
    fontFace: FONT_HEAD, align: "center",
  });
  s.addText("Telegram handlers\n+ точка входа", {
    x: 5.5, y: 3.95, w: 2.3, h: 0.5,
    fontSize: 11, color: "FFE9B8",
    fontFace: FONT_BODY, align: "center",
  });

  // Surrounding modules
  const modules = [
    { x: 0.5, y: 2.2, label: "rutracker.py", desc: "RT login, search, download" },
    { x: 0.5, y: 3.4, label: "jackett.py", desc: "Jackett search + download" },
    { x: 0.5, y: 4.6, label: "plex.py", desc: "PlexClient: movies + shows" },
    { x: 0.5, y: 5.8, label: "kinopoisk.py", desc: "KP enrichment API" },
    { x: 10.5, y: 2.2, label: "download_station.py", desc: "DS REST API client" },
    { x: 10.5, y: 3.4, label: "state_store.py", desc: "JSON persistence" },
    { x: 10.5, y: 4.6, label: "movie_discovery.py", desc: "/new ranking & score" },
    { x: 10.5, y: 5.8, label: "task_policies.py", desc: "notification rules" },
    { x: 5.5, y: 1.95, label: "keyboards.py", desc: "InlineKeyboard builders" },
    { x: 5.5, y: 5.85, label: "formatters.py", desc: "regex: seasons, quality" },
  ];
  modules.forEach(m => {
    s.addShape(pres.ShapeType.roundRect, {
      x: m.x, y: m.y, w: 2.3, h: 0.8,
      fill: { color: CARD_BG },
      line: { color: CARD_BORDER, width: 1 },
      rectRadius: 0.06,
    });
    s.addText(m.label, {
      x: m.x + 0.1, y: m.y + 0.06, w: 2.1, h: 0.32,
      fontSize: 13, bold: true, color: TEXT_PRIMARY,
      fontFace: FONT_HEAD, align: "center",
    });
    s.addText(m.desc, {
      x: m.x + 0.1, y: m.y + 0.42, w: 2.1, h: 0.32,
      fontSize: 10, color: TEXT_MUTED,
      fontFace: FONT_BODY, align: "center",
    });
  });

  // Footer note
  s.addText("Все модули — pure-Python без heavy frameworks. Полное покрытие тестами в tests/ (645+ tests).", {
    x: 0.5, y: 6.85, w: 12.33, h: 0.3,
    fontSize: 11, color: TEXT_MUTED, fontFace: FONT_BODY, italic: true,
    align: "center",
  });
}

// === SLIDE 5: STATE & BACKGROUND LOOPS ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "State & background loops", "Что бот хранит и когда что-то делает в фоне");

  // State files (left)
  s.addText("📋 State files (state/*.json)", {
    x: 0.5, y: 2.0, w: 6.0, h: 0.4,
    fontSize: 16, bold: true, color: ACCENT, fontFace: FONT_HEAD,
  });
  const stateRows = [
    ["approved_chat_ids", "одобренные пользователи"],
    ["task_owners", "task_id → chat_id"],
    ["task_meta", "kind, title, year, series_query"],
    ["notified_tasks", "sent, failures, subscribers"],
    ["auto_delete_tasks", "TTL очистки задач"],
    ["topic_subscriptions", "подписки на новые серии"],
    ["movie_discovery_cache", "top-N с score + KP cache"],
    ["movie_discovery_settings", "per-user seen, trackers"],
    ["pending_downloads", "отложенные с retry"],
    ["tracker_processed", "куда добавлены public-трекеры"],
  ];
  stateRows.forEach((r, i) => {
    const y = 2.5 + i * 0.42;
    s.addShape(pres.ShapeType.rect, {
      x: 0.5, y, w: 6.0, h: 0.4,
      fill: { color: i % 2 ? CARD_BG : "FFFFFF" }, line: { type: "none" },
    });
    s.addText(r[0] + ".json", {
      x: 0.6, y: y + 0.05, w: 2.5, h: 0.3,
      fontSize: 11, bold: true, color: TEXT_PRIMARY,
      fontFace: "Consolas",
    });
    s.addText(r[1], {
      x: 3.2, y: y + 0.05, w: 3.2, h: 0.3,
      fontSize: 11, color: TEXT_MUTED, fontFace: FONT_BODY,
    });
  });

  // Background loops (right)
  s.addText("🔁 Background loops", {
    x: 6.8, y: 2.0, w: 6.0, h: 0.4,
    fontSize: 16, bold: true, color: "2563EB", fontFace: FONT_HEAD,
  });
  const loops = [
    ["6h", "_movie_discovery_loop", "обновляет top-N + KP + push новинок"],
    ["180s", "_tracker_background_loop", "публичные трекеры для BT-задач"],
    ["180s", "_task_maintenance_loop", "уведомления + auto-delete + pending + prune"],
    ["per task", "_plex_poll_after_finish", "Plex polling 30с × 20 (до 10 мин)"],
    ["6h", "_subscription_check_loop", "новые серии для RT/Jackett подписок"],
  ];
  loops.forEach((l, i) => {
    const y = 2.5 + i * 0.85;
    s.addShape(pres.ShapeType.roundRect, {
      x: 6.8, y, w: 6.0, h: 0.78,
      fill: { color: CARD_BG },
      line: { color: CARD_BORDER, width: 1 },
      rectRadius: 0.05,
    });
    s.addShape(pres.ShapeType.roundRect, {
      x: 6.95, y: y + 0.13, w: 0.85, h: 0.52,
      fill: { color: "2563EB" }, line: { type: "none" },
      rectRadius: 0.06,
    });
    s.addText(l[0], {
      x: 6.95, y: y + 0.13, w: 0.85, h: 0.52,
      fontSize: 12, bold: true, color: "FFFFFF",
      fontFace: FONT_BODY, align: "center", valign: "middle",
    });
    s.addText(l[1], {
      x: 7.9, y: y + 0.08, w: 4.8, h: 0.3,
      fontSize: 12, bold: true, color: TEXT_PRIMARY,
      fontFace: "Consolas",
    });
    s.addText(l[2], {
      x: 7.9, y: y + 0.38, w: 4.8, h: 0.32,
      fontSize: 10, color: TEXT_MUTED, fontFace: FONT_BODY,
    });
  });

  addFootline(s, "Slide 5 · State & background loops");
}

// === SLIDE 6-10: USER FLOWS (sequence diagrams) ===

function addFlowSlide(idx, title, subtitle, steps, actorColumns) {
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, title, subtitle);

  // Actor column headers
  const startX = 0.5;
  const colW = (12.33) / actorColumns.length;
  actorColumns.forEach((col, i) => {
    const x = startX + i * colW;
    s.addShape(pres.ShapeType.roundRect, {
      x: x + 0.05, y: 2.0, w: colW - 0.1, h: 0.5,
      fill: { color: col.color },
      line: { type: "none" }, rectRadius: 0.05,
    });
    s.addText(col.name, {
      x: x + 0.05, y: 2.0, w: colW - 0.1, h: 0.5,
      fontSize: 13, bold: true, color: "FFFFFF",
      fontFace: FONT_HEAD, align: "center", valign: "middle",
    });
    // vertical lifeline
    s.addShape(pres.ShapeType.line, {
      x: x + colW / 2, y: 2.5, w: 0, h: 4.3,
      line: { color: CARD_BORDER, width: 1, dashType: "dash" },
    });
  });

  // Steps
  let curY = 2.7;
  steps.forEach(step => {
    const fromIdx = step.from;
    const toIdx = step.to;
    const fromX = startX + fromIdx * colW + colW / 2;
    const toX = startX + toIdx * colW + colW / 2;
    const isLeftToRight = toX > fromX;
    const labelX = Math.min(fromX, toX);
    const labelW = Math.abs(toX - fromX);

    // Arrow line
    s.addShape(pres.ShapeType.line, {
      x: fromX, y: curY + 0.3, w: toX - fromX, h: 0,
      line: { color: step.color || ACCENT, width: 2,
        beginArrowType: "none", endArrowType: "triangle" },
    });

    // Label above arrow
    s.addText(step.label, {
      x: labelX, y: curY, w: labelW, h: 0.32,
      fontSize: 11, color: TEXT_PRIMARY, bold: true,
      fontFace: FONT_BODY, align: "center",
    });
    if (step.note) {
      s.addText(step.note, {
        x: labelX, y: curY + 0.32, w: labelW, h: 0.28,
        fontSize: 9, color: TEXT_MUTED, italic: true,
        fontFace: FONT_BODY, align: "center",
      });
    }
    curY += step.note ? 0.75 : 0.55;
  });

  addFootline(s, `Slide ${idx} · ${title}`);
}

// === SLIDE 6: Flow — Поиск и скачивание ===
addFlowSlide(6,
  "Flow: поиск и скачивание",
  "От /search до задачи в Download Station",
  [
    { from: 0, to: 1, label: "/search Аркейн сезон 1" },
    { from: 1, to: 2, label: "Jackett search (indexers)" },
    { from: 2, to: 1, label: "results", color: ACCENT_DEEP },
    { from: 1, to: 3, label: "KP enrichment", note: "рейтинг, постер, год" },
    { from: 3, to: 1, label: "KP data", color: ACCENT_DEEP },
    { from: 1, to: 0, label: "top-N карточек", color: ACCENT_DEEP },
    { from: 0, to: 1, label: "tap «✅ Скачать»" },
    { from: 1, to: 4, label: "Plex pre-check (есть уже?)" },
    { from: 1, to: 2, label: "Jackett download_torrent", note: "fallback chain: RT direct → magnet → pending" },
    { from: 1, to: 5, label: "DS create_torrent_file" },
    { from: 1, to: 0, label: "✅ Задача добавлена", color: "16A34A" },
  ],
  [
    { name: "User", color: "374151" },
    { name: "Bot", color: ACCENT },
    { name: "Jackett", color: "2563EB" },
    { name: "Kinopoisk", color: "7C3AED" },
    { name: "Plex", color: "C2410C" },
    { name: "DS", color: "059669" },
  ]
);

// === SLIDE 7: Flow — Финальное уведомление + Plex polling ===
addFlowSlide(7,
  "Flow: уведомление о завершении + Plex polling",
  "Что происходит когда DS-задача переходит в finished",
  [
    { from: 1, to: 3, label: "list_tasks() (180s tick)" },
    { from: 3, to: 1, label: "status=finished", color: ACCENT_DEEP },
    { from: 1, to: 1, label: "classify error / dedup" },
    { from: 1, to: 2, label: "sendMessage «✅ Загрузка завершена» + кнопка Plex" },
    { from: 2, to: 0, label: "🔔 push", color: "16A34A" },
    { from: 1, to: 4, label: "spawn _plex_poll_after_finish (10 мин)" },
    { from: 4, to: 4, label: "library refresh + lookup (каждые 30s)", note: "title-only fallback при year-mismatch (для сериалов)" },
    { from: 4, to: 1, label: "found", color: ACCENT_DEEP },
    { from: 1, to: 2, label: "sendMessage «✅ X добавлен в Plex»" },
    { from: 2, to: 0, label: "🔔 push с deeplink", color: "16A34A" },
  ],
  [
    { name: "User", color: "374151" },
    { name: "Bot", color: ACCENT },
    { name: "Telegram", color: "2563EB" },
    { name: "DS", color: "059669" },
    { name: "Plex", color: "C2410C" },
  ]
);

// === SLIDE 8: Flow — Открытие в Plex (deeplink) ===
addFlowSlide(8,
  "Flow: открытие фильма в Plex (deeplink)",
  "Что происходит при тапе кнопки «▶️ Смотреть в Plex»",
  [
    { from: 0, to: 1, label: "tap «▶️ Смотреть в Plex»" },
    { from: 1, to: 2, label: "HTTPS GET plex.example.com" },
    { from: 2, to: 3, label: "HTTP origin :80", note: "Flexible mode, CF cert" },
    { from: 3, to: 4, label: "NAT 80 → NAS:80" },
    { from: 4, to: 3, label: "plex.html", color: ACCENT_DEEP },
    { from: 3, to: 2, label: "response", color: ACCENT_DEEP },
    { from: 2, to: 1, label: "HTTPS html", color: ACCENT_DEEP },
    { from: 1, to: 0, label: "Safari/Chrome рендерит" },
    { from: 0, to: 5, label: "mobile: location.href = plex://", note: "iOS Plex app открывается" },
    { from: 0, to: 6, label: "desktop: → app.plex.tv/desktop/...", note: "Plex Web показывает фильм" },
  ],
  [
    { name: "User", color: "374151" },
    { name: "Telegram", color: "2563EB" },
    { name: "Cloudflare", color: "F38020" },
    { name: "Router", color: "059669" },
    { name: "Web Station", color: ACCENT },
    { name: "Plex iOS", color: "C2410C" },
    { name: "Plex Web", color: "7C3AED" },
  ]
);

// === SLIDE 9: Flow — Смена WAN IP (DDNS) ===
addFlowSlide(9,
  "Flow: смена WAN IP провайдером",
  "Как inadyn на роутере держит DNS-запись актуальной",
  [
    { from: 0, to: 1, label: "PPPoE reconnect, new IP" },
    { from: 1, to: 1, label: "eth3: 203.0.113.99", note: "IP интерфейса изменился" },
    { from: 2, to: 1, label: "ip -4 addr show eth3 (каждые 5 мин)" },
    { from: 1, to: 2, label: "203.0.113.99", color: ACCENT_DEEP },
    { from: 2, to: 2, label: "cache mismatch detected" },
    { from: 2, to: 3, label: "PUT /dns_records/REC_ID", note: "Authorization: Bearer <token>" },
    { from: 3, to: 4, label: "update zone" },
    { from: 3, to: 2, label: "success", color: ACCENT_DEEP },
    { from: 2, to: 2, label: "cache := new IP" },
    { from: 4, to: 4, label: "~30s propagation", note: "клиенты по hostname продолжают доступ" },
  ],
  [
    { name: "ISP", color: "374151" },
    { name: "Router", color: "059669" },
    { name: "inadyn", color: ACCENT },
    { name: "CF API", color: "F38020" },
    { name: "CF DNS", color: "7C3AED" },
  ]
);

// === SLIDE 10: Flow — Pending queue + Admin reset (two flows in one) ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "Flows: pending queue & admin reset", "Два сценария recovery в одном слайде");

  // Left: pending queue
  s.addText("⏳ Pending download queue", {
    x: 0.5, y: 2.0, w: 6.0, h: 0.4,
    fontSize: 16, bold: true, color: ACCENT, fontFace: FONT_HEAD,
  });
  const pendingSteps = [
    "User: tap «✅ Скачать»",
    "Bot → Jackett download (failed)",
    "Bot → rutracker direct (failed)",
    "Bot → User: ошибка + кнопки [🔄][⏳][✖️]",
    "User: tap «⏳ Поставить в очередь»",
    "Bot → pending_downloads.json (TTL 24h)",
    "─── every 5 min ───",
    "Loop: try chain Jackett→RT→magnet",
    "Success → push «✅ стартовала»",
    "TTL 24h → push «⌛ не удалось за 24ч»",
  ];
  pendingSteps.forEach((step, i) => {
    const y = 2.5 + i * 0.4;
    const isDivider = step.startsWith("─");
    s.addText(isDivider ? "🔁 every 5 min" : step, {
      x: 0.5, y, w: 6.0, h: 0.35,
      fontSize: 11,
      color: isDivider ? ACCENT : TEXT_PRIMARY,
      bold: isDivider,
      fontFace: isDivider ? FONT_HEAD : "Consolas",
    });
  });

  // Right: admin reset
  s.addText("🔄 Admin: сброс failure-счётчиков", {
    x: 6.8, y: 2.0, w: 6.0, h: 0.4,
    fontSize: 16, bold: true, color: "2563EB", fontFace: FONT_HEAD,
  });
  const adminSteps = [
    "Admin: /admin",
    "Bot → load notified_tasks.json",
    "Bot → подсчёт stuck (failures[X] ≥ 3)",
    "Bot → Admin: «⚠️ зависших 3 задач»",
    "Admin: tap «🔄 Сбросить счётчики (3)»",
    "Bot → for each entry: failures = {}",
    "Bot → save state",
    "Bot → Admin: «✅ сброшено для 3 задач»",
    "─── через 180s (next maintenance tick) ───",
    "Push'и снова доставляются",
  ];
  adminSteps.forEach((step, i) => {
    const y = 2.5 + i * 0.4;
    const isDivider = step.startsWith("─");
    s.addText(isDivider ? "⏱ next tick" : step, {
      x: 6.8, y, w: 6.0, h: 0.35,
      fontSize: 11,
      color: isDivider ? "2563EB" : TEXT_PRIMARY,
      bold: isDivider,
      fontFace: isDivider ? FONT_HEAD : "Consolas",
    });
  });

  addFootline(s, "Slide 10 · Recovery flows");
}

// === SLIDE 11: Cloudflare Proxied + DDNS in detail ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "Cloudflare Proxied + DDNS", "Как HTTPS-запрос идёт через CF + watchdog на роутере");

  // Top: request path
  s.addShape(pres.ShapeType.roundRect, {
    x: 0.5, y: 2.0, w: 12.33, h: 2.3,
    fill: { color: "FFF9E6" },
    line: { color: ACCENT, width: 2 },
    rectRadius: 0.1,
  });
  s.addText("📥 Запрос на страницу", {
    x: 0.65, y: 2.1, w: 12, h: 0.35,
    fontSize: 14, bold: true, color: ACCENT, fontFace: FONT_HEAD,
  });
  const reqSteps = [
    { x: 0.7, label: "Browser", desc: "GET https://plex.example.com/plex.html" },
    { x: 3.0, label: "CF Edge", desc: "TLS termination (CF cert)" },
    { x: 5.3, label: "Origin lookup", desc: "DNS A → 203.0.113.42" },
    { x: 7.6, label: "Router NAT", desc: "80 → NAS:80" },
    { x: 9.9, label: "Web Station", desc: "200 plex.html" },
  ];
  reqSteps.forEach((step, i) => {
    s.addShape(pres.ShapeType.roundRect, {
      x: step.x, y: 2.6, w: 2.1, h: 1.4,
      fill: { color: "FFFFFF" },
      line: { color: ACCENT, width: 1 },
      rectRadius: 0.06,
    });
    s.addText(step.label, {
      x: step.x, y: 2.7, w: 2.1, h: 0.4,
      fontSize: 12, bold: true, color: TEXT_PRIMARY,
      fontFace: FONT_HEAD, align: "center",
    });
    s.addText(step.desc, {
      x: step.x + 0.05, y: 3.15, w: 2.0, h: 0.8,
      fontSize: 9, color: TEXT_MUTED,
      fontFace: FONT_BODY, align: "center",
    });
    if (i < reqSteps.length - 1) {
      s.addShape(pres.ShapeType.line, {
        x: step.x + 2.1, y: 3.3, w: 0.2, h: 0,
        line: { color: ACCENT, width: 2,
          beginArrowType: "none", endArrowType: "triangle" },
      });
    }
  });

  // Bottom: DDNS sync
  s.addShape(pres.ShapeType.roundRect, {
    x: 0.5, y: 4.5, w: 12.33, h: 2.3,
    fill: { color: "EEF2FF" },
    line: { color: "2563EB", width: 2 },
    rectRadius: 0.1,
  });
  s.addText("🔁 DDNS sync (каждые 5 мин)", {
    x: 0.65, y: 4.6, w: 12, h: 0.35,
    fontSize: 14, bold: true, color: "2563EB", fontFace: FONT_HEAD,
  });
  const ddnsSteps = [
    { x: 0.7, label: "inadyn", desc: "read eth3 (local, минуя XKeen)" },
    { x: 3.0, label: "Router", desc: "203.0.113.99" },
    { x: 5.3, label: "compare cache", desc: "mismatch?" },
    { x: 7.6, label: "CF API", desc: "PUT /dns_records (Bearer token)" },
    { x: 9.9, label: "CF DNS", desc: "updated · ~30s propagate" },
  ];
  ddnsSteps.forEach((step, i) => {
    s.addShape(pres.ShapeType.roundRect, {
      x: step.x, y: 5.1, w: 2.1, h: 1.4,
      fill: { color: "FFFFFF" },
      line: { color: "2563EB", width: 1 },
      rectRadius: 0.06,
    });
    s.addText(step.label, {
      x: step.x, y: 5.2, w: 2.1, h: 0.4,
      fontSize: 12, bold: true, color: TEXT_PRIMARY,
      fontFace: FONT_HEAD, align: "center",
    });
    s.addText(step.desc, {
      x: step.x + 0.05, y: 5.65, w: 2.0, h: 0.8,
      fontSize: 9, color: TEXT_MUTED,
      fontFace: FONT_BODY, align: "center",
    });
    if (i < ddnsSteps.length - 1) {
      s.addShape(pres.ShapeType.line, {
        x: step.x + 2.1, y: 5.8, w: 0.2, h: 0,
        line: { color: "2563EB", width: 2,
          beginArrowType: "none", endArrowType: "triangle" },
      });
    }
  });

  addFootline(s, "Slide 11 · Cloudflare Proxied + DDNS");
}

// === SLIDE 12: Что построили (timeline) ===
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  addTitle(s, "Что построили", "Хронология ключевых изменений");

  const commits = [
    ["/new", "resort cards on cache hit; rename Plex unmatched button"],
    ["Search", "SxxExx series format в фильтре сезонов"],
    ["UI", "shorten Plex unmatched notify toggle для мобильных"],
    ["Search", "relaxed retries на «no results» dead-end"],
    ["Notifications", "retry transient errors без штрафа; persist state per-task"],
    ["Download", "auto-fallback на rutracker_client при Jackett 404"],
    ["Download", "compact error + кнопка «🔄 Повторить» при сбое"],
    ["Download", "pending queue с auto-retry и TTL 24ч"],
    ["Notifications", "log skip reasons + recover recipients from card registry"],
    ["Notifications", "plex:// → https Plex Universal Link; format-bug защита classifier"],
    ["Docs", "rebrand to CineDownload + reorganise features"],
    ["Plex", "configurable PLEX_DEEPLINK_BASE_URL для нативного iOS"],
    ["Plex", "fall back to title-only при series year-mismatch (Good Omens fix)"],
    ["Infra", "Web Station + Cloudflare Proxied + inadyn DDNS на роутере"],
  ];
  commits.forEach((c, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = 0.5 + col * 6.3;
    const y = 2.0 + row * 0.62;
    s.addShape(pres.ShapeType.roundRect, {
      x, y, w: 5.9, h: 0.55,
      fill: { color: CARD_BG },
      line: { color: CARD_BORDER, width: 1 },
      rectRadius: 0.04,
    });
    s.addShape(pres.ShapeType.roundRect, {
      x: x + 0.1, y: y + 0.12, w: 1.2, h: 0.3,
      fill: { color: ACCENT }, line: { type: "none" },
      rectRadius: 0.04,
    });
    s.addText(c[0], {
      x: x + 0.1, y: y + 0.12, w: 1.2, h: 0.3,
      fontSize: 10, bold: true, color: "FFFFFF",
      fontFace: FONT_HEAD, align: "center", valign: "middle",
    });
    s.addText(c[1], {
      x: x + 1.4, y: y + 0.05, w: 4.4, h: 0.5,
      fontSize: 11, color: TEXT_PRIMARY,
      fontFace: FONT_BODY, valign: "middle",
    });
  });

  addFootline(s, "Slide 12 · Build timeline · 13 commits + infra");
}

// === SLIDE 13: Tech stack & closing ===
{
  const s = pres.addSlide();
  s.background = { color: BG_DARK };

  s.addShape(pres.ShapeType.rect, {
    x: 0.5, y: 0.6, w: 0.4, h: 0.06,
    fill: { color: ACCENT }, line: { type: "none" },
  });
  s.addText("Tech stack", {
    x: 0.5, y: 0.75, w: 12.33, h: 0.7,
    fontSize: 32, bold: true, color: "FFFFFF",
    fontFace: FONT_HEAD,
  });
  s.addText("Технологии и зависимости которые держат всю систему", {
    x: 0.5, y: 1.45, w: 12.33, h: 0.4,
    fontSize: 16, color: "AAAAAA", italic: true,
    fontFace: FONT_BODY,
  });

  const stack = [
    ["Bot runtime", "Python 3.14, python-telegram-bot 22.x, asyncio, httpx"],
    ["Deployment", "Docker (Synology Container Manager), Alpine"],
    ["State", "Plain JSON-файлы с atomic write"],
    ["Tests", "pytest 9.x, unittest.mock, 645+ tests"],
    ["External APIs", "Telegram Bot, Cloudflare, Plex PMS, Kinopoisk, Jackett (Torznab)"],
    ["NAS OS", "DSM 7.3.2 (Synology)"],
    ["Router", "Keenetic + OPKG/entware, XKeen (Xray), inadyn 2.12"],
    ["DNS / CDN", "Cloudflare Free Plan (Proxied)"],
    ["Documentation", "ARCHITECTURE.md (Mermaid) + architecture.pptx"],
  ];
  stack.forEach((row, i) => {
    const y = 2.2 + i * 0.5;
    s.addText(row[0], {
      x: 0.7, y, w: 3.0, h: 0.4,
      fontSize: 14, bold: true, color: ACCENT,
      fontFace: FONT_HEAD,
    });
    s.addText(row[1], {
      x: 3.9, y, w: 8.8, h: 0.4,
      fontSize: 14, color: "DDDDDD",
      fontFace: FONT_BODY,
    });
  });

  s.addText("github.com/KiMorev/tg-torrent-bot", {
    x: 0.5, y: 6.8, w: 12.33, h: 0.4,
    fontSize: 12, color: TEXT_MUTED, italic: true,
    fontFace: "Consolas", align: "right",
  });
}

// === WRITE ===
const outPath = "C:/Users/morev/tg-torrent-bot/docs/architecture.pptx";
pres.writeFile({ fileName: outPath })
  .then(name => console.log(`✓ wrote ${name}`))
  .catch(err => { console.error("ERROR:", err); process.exit(1); });
