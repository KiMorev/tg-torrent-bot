"""Telegram InlineKeyboard builders and shared callback-data constants.

All keyboard functions are stateless — they depend only on their arguments.
`_task_keyboard` accepts an explicit `show_trackers` flag instead of reading
state directly, keeping this module free of project-level dependencies.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from formatters import _short_title, _tracker_abbr

# ---------------------------------------------------------------------------
# Callback prefixes & scope constants
# ---------------------------------------------------------------------------

TASK_CALLBACK_PREFIX = "task"
ACCESS_CALLBACK_PREFIX = "access"
ADMIN_CALLBACK_PREFIX = "admin"
SEARCH_CALLBACK_PREFIX = "srch"
JACKETT_SELECT_PREFIX = "jk"  # used inside srch: namespace
SUB_CALLBACK_PREFIX = "sub"

TASK_LIST_SCOPE_ALL = "all"
TASK_LIST_SCOPE_MY = "mine"
TASK_LIST_SCOPE_DEFAULT = "default"
TASK_LIST_PAGE_SIZE = 10

# ---------------------------------------------------------------------------
# Search-specific constants
# ---------------------------------------------------------------------------

_SRCH_QUALITY_OPTIONS: list[tuple[str, str]] = [
    ("🎬 4K", "4K"),
    ("📺 1080p", "1080p"),
    ("📺 720p", "720p"),
    ("🔍 Любое", "any"),
]
_SRCH_DEFAULT_SETTINGS: dict = {"quality": "1080p", "audio": False, "subs": False}

# ---------------------------------------------------------------------------
# Callback-data helpers
# ---------------------------------------------------------------------------


def _task_callback(action: str, task_id: str) -> str:
    return f"{TASK_CALLBACK_PREFIX}:{action}:{task_id}"


def _access_callback(action: str, chat_id: int) -> str:
    return f"{ACCESS_CALLBACK_PREFIX}:{action}:{chat_id}"


def _admin_callback(action: str) -> str:
    return f"{ADMIN_CALLBACK_PREFIX}:{action}"


# ---------------------------------------------------------------------------
# Pure helpers used by keyboard builders
# ---------------------------------------------------------------------------


def _finished_task_ids(tasks: list[dict]) -> list[str]:
    return [
        task["id"]
        for task in tasks
        if task.get("id") and (task.get("status") or "").lower() == "finished"
    ]


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def _access_approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Разрешить", callback_data=_access_callback("approve", chat_id)),
                InlineKeyboardButton("🚫 Отклонить", callback_data=_access_callback("deny", chat_id)),
            ]
        ]
    )


def _download_list_keyboard(scope: str = TASK_LIST_SCOPE_DEFAULT) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 К списку загрузок", callback_data=_task_callback("list", scope))]]
    )


def _admin_panel_keyboard(
    *,
    show_plex_unmatched: bool = False,
    plex_unmatched_count: int = 0,
    plex_unmatched_notify_enabled: bool = False,
    stuck_notifications_count: int = 0,
) -> InlineKeyboardMarkup:
    """Build the admin panel keyboard.

    ``show_plex_unmatched`` toggles the visibility of the Plex-unmatched
    radar row (only meaningful when PLEX_ENABLED). The toggle button label
    reflects ``plex_unmatched_notify_enabled``.

    ``stuck_notifications_count`` — if >0, surfaces a «Сбросить счётчики (N)»
    button. Hidden when 0 to avoid clutter; the admin sees the count in the
    status text either way and only needs the action when something's stuck.
    """
    rows = [
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=_admin_callback("home")),
            InlineKeyboardButton("🧭 Диагностика", callback_data=_admin_callback("diagnostics")),
        ],
        [
            InlineKeyboardButton("👥 Пользователи", callback_data=f"{ACCESS_CALLBACK_PREFIX}:users_refresh"),
            InlineKeyboardButton("📋 Загрузки", callback_data=_task_callback("list", TASK_LIST_SCOPE_ALL)),
        ],
        [
            InlineKeyboardButton("🔔 Подписки", callback_data=_admin_callback("subscriptions")),
        ],
        [
            # Two movie-related drill-downs share a row to save vertical space on
            # mobile. «🎬 Новинки» opens the discovery-status screen (filters,
            # sources, KP cache management); «🎬 Трекеры новинок» opens the
            # tracker enable/disable picker for movie ranking.
            InlineKeyboardButton("🎬 Новинки", callback_data=_admin_callback("movie_status")),
            InlineKeyboardButton("🎬 Трекеры новинок", callback_data=_admin_callback("movie_trackers")),
        ],
    ]

    if show_plex_unmatched:
        list_label = f"📋 Plex: без матча ({plex_unmatched_count})"
        toggle_label = (
            "🔔 Подписка: вкл"
            if plex_unmatched_notify_enabled
            else "🔕 Подписка: выкл"
        )
        rows.append([
            InlineKeyboardButton(list_label, callback_data=_admin_callback("plex_unmatched")),
            InlineKeyboardButton(toggle_label, callback_data=_admin_callback("plex_unmatched_toggle")),
        ])

    if stuck_notifications_count > 0:
        rows.append([InlineKeyboardButton(
            f"🔄 Сбросить счётчики ({stuck_notifications_count})",
            callback_data=_admin_callback("reset_notify_failures"),
        )])

    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=_admin_callback("close"))])
    return InlineKeyboardMarkup(rows)


def _admin_movie_status_keyboard(*, show_kp_buttons: bool) -> InlineKeyboardMarkup:
    """Drill-down keyboard for «🎬 Новинки» admin screen.

    ``show_kp_buttons`` toggles visibility of the KP cache management buttons
    (force-refresh / clear) — they're meaningful only when KINOPOISK_API_KEY
    is configured. Hidden when KP is disabled, keeping the screen to a single
    «⬅️ Назад» / «✖️ Закрыть» row.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if show_kp_buttons:
        rows.append([
            InlineKeyboardButton("🔄 Обновить KP кэш", callback_data=_admin_callback("force_kp_refresh")),
            InlineKeyboardButton("🗑 Очистить KP кеш", callback_data=_admin_callback("clear_kp_cache")),
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data=_admin_callback("home")),
        InlineKeyboardButton("✖️ Закрыть", callback_data=_admin_callback("close")),
    ])
    return InlineKeyboardMarkup(rows)


def _admin_kp_cache_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmation dialog before clearing the KP results cache."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да, очистить", callback_data=_admin_callback("confirm_clear_kp_cache")),
                InlineKeyboardButton("⬅️ Назад", callback_data=_admin_callback("home")),
            ],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=_admin_callback("close"))],
        ]
    )


def _admin_kp_cache_cleared_keyboard() -> InlineKeyboardMarkup:
    """Shown after the KP cache has been successfully cleared."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Админ-панель", callback_data=_admin_callback("home"))],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=_admin_callback("close"))],
        ]
    )


def _admin_kp_force_refresh_keyboard(can_full: bool) -> InlineKeyboardMarkup:
    """Budget info screen: offers full (one-run) or gradual KP cache refresh."""
    rows = []
    if can_full:
        rows.append([
            InlineKeyboardButton(
                "✅ Обновить за один прогон",
                callback_data=_admin_callback("confirm_force_kp_refresh_full"),
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "🔄 Обновлять постепенно",
            callback_data=_admin_callback("confirm_force_kp_refresh_gradual"),
        )
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data=_admin_callback("home")),
        InlineKeyboardButton("✖️ Закрыть", callback_data=_admin_callback("close")),
    ])
    return InlineKeyboardMarkup(rows)


def _admin_diagnostics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Проверить снова", callback_data=_admin_callback("diagnostics")),
                InlineKeyboardButton("⬅️ Админ-панель", callback_data=_admin_callback("home")),
            ],
            [InlineKeyboardButton("✖️ Закрыть", callback_data=_admin_callback("close"))],
        ]
    )


def _tasks_keyboard(
    tasks: list[dict],
    scope: str = TASK_LIST_SCOPE_ALL,
    is_admin: bool = False,
    page: int = 0,
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Обновить", callback_data=_task_callback("list", scope))]]
    if is_admin:
        if scope == TASK_LIST_SCOPE_ALL:
            rows[0].append(
                InlineKeyboardButton(
                    "🙋 Мои загрузки", callback_data=_task_callback("list", TASK_LIST_SCOPE_MY)
                )
            )
        else:
            rows[0].append(
                InlineKeyboardButton(
                    "🌐 Все загрузки", callback_data=_task_callback("list", TASK_LIST_SCOPE_ALL)
                )
            )

    total_pages = max(1, (len(tasks) + TASK_LIST_PAGE_SIZE - 1) // TASK_LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * TASK_LIST_PAGE_SIZE
    visible_tasks = tasks[start : start + TASK_LIST_PAGE_SIZE]

    for index, task in enumerate(visible_tasks, start=start + 1):
        task_id = task.get("id")
        if not task_id:
            continue

        rows.append(
            [
                InlineKeyboardButton(
                    f"🔎 {index}. {_short_title(task)}",
                    callback_data=_task_callback("info", task_id),
                )
            ]
        )

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("◀ Назад", callback_data=_task_callback("page_prev", scope))
            )
        nav_row.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}", callback_data=_task_callback("list", scope)
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton("Вперёд ▶", callback_data=_task_callback("page_next", scope))
            )
        rows.append(nav_row)

    if _finished_task_ids(tasks):
        rows.append(
            [
                InlineKeyboardButton(
                    "🧹 Удалить завершенные",
                    callback_data=_task_callback("delete_finished_ask", scope),
                )
            ]
        )

    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _task_keyboard(
    task_id: str,
    status: str = "",
    task_type: str = "",
    *,
    show_trackers: bool = False,
) -> InlineKeyboardMarkup:
    status = status.lower()
    rows = [[InlineKeyboardButton("🔄 Обновить статус", callback_data=_task_callback("info", task_id))]]

    if status in {"downloading", "seeding", "finishing", "hash_checking"}:
        rows[0].append(
            InlineKeyboardButton("⏸️ Пауза", callback_data=_task_callback("pause", task_id))
        )
    elif status not in {"finished"}:
        rows[0].append(
            InlineKeyboardButton("▶️ Запустить", callback_data=_task_callback("resume", task_id))
        )

    if show_trackers:
        rows.append(
            [InlineKeyboardButton("➕ Добавить трекеры", callback_data=_task_callback("trackers", task_id))]
        )

    rows.append(
        [InlineKeyboardButton("🗑️ Удалить", callback_data=_task_callback("delete_ask", task_id))]
    )
    rows.append(
        [InlineKeyboardButton("📋 К списку загрузок", callback_data=_task_callback("list", task_id))]
    )
    rows.append(
        [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))]
    )

    return InlineKeyboardMarkup(rows)


def _plex_confirm_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for the Plex pre-download duplicate warning dialog."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ Скачать", callback_data="plex:confirm"),
            InlineKeyboardButton("✖️ Отмена", callback_data="plex:cancel"),
        ]
    ])


def _final_notification_keyboard(
    task_id: str,
    *,
    show_plex: bool = False,
    plex_url: str = "https://app.plex.tv/desktop",
) -> InlineKeyboardMarkup:
    """Final notification keyboard for a completed task.

    The default ``plex_url`` is the Plex Universal Link
    (``https://app.plex.tv/desktop``) — iOS/Android Plex apps intercept it via
    Universal Links / Intent filters, on desktop it opens Plex Web. We can't
    use the ``plex://`` scheme because Telegram rejects it in inline-button
    URLs since 2026 (BadRequest "unsupported url protocol").
    """
    rows = []
    if show_plex:
        rows.append([InlineKeyboardButton("▶️ Открыть Plex", url=plex_url)])
    rows.append([InlineKeyboardButton("🧹 Удалить из списка", callback_data=_task_callback("delete_ask", task_id))])
    rows.append([InlineKeyboardButton("📋 К списку загрузок", callback_data=_task_callback("list", task_id))])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def _new_task_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить статус", callback_data=_task_callback("info", task_id))],
            [InlineKeyboardButton("📋 К списку загрузок", callback_data=_task_callback("list", task_id))],
        ]
    )


def _task_reply_markup(task_id: str) -> InlineKeyboardMarkup | None:
    return _new_task_keyboard(task_id) if task_id else None


def _delete_confirm_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑️ Да, удалить", callback_data=_task_callback("delete", task_id))],
            [InlineKeyboardButton("↩️ Назад", callback_data=_task_callback("info", task_id))],
        ]
    )


def _delete_finished_confirm_keyboard(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🧹 Да, удалить завершенные",
                    callback_data=_task_callback("delete_finished", scope),
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ К списку загрузок", callback_data=_task_callback("list", scope)
                )
            ],
        ]
    )


def _no_results_keyboard(
    *,
    has_quality: bool,
    jackett_can_expand: bool,
    suggestions: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Shown on a 'no results' dead-end. Offers to relax filters and retry.

    Conditional rows — buttons appear only when they have something to do:

    - ``has_quality``: the query carried a quality suffix (e.g. ' 1080p') over
      the bare ``srch_query`` — offer to drop it.
    - ``jackett_can_expand``: Jackett is configured AND ``srch_jackett_selected``
      is a strict subset of available indexers — offer to broaden.
    - Both flags True → also offer the combined retry.
    - ``suggestions``: optional GPT-generated «did you mean …» variations.
      Each becomes a button that re-runs the search with that text.

    Always ends with Cancel so the screen never dead-ends.
    """
    rows: list[list[InlineKeyboardButton]] = []
    # GPT suggestions go FIRST — they're the most likely fix for typos.
    for suggestion in (suggestions or [])[:3]:
        if not suggestion:
            continue
        rows.append([InlineKeyboardButton(
            f"🔍 {suggestion}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:didmean:{suggestion}",
        )])
    if has_quality:
        rows.append([InlineKeyboardButton(
            "🔍 Без фильтра качества",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:no_quality",
        )])
    if jackett_can_expand:
        rows.append([InlineKeyboardButton(
            "🌐 На всех трекерах",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:expand_all_trackers",
        )])
    if has_quality and jackett_can_expand:
        rows.append([InlineKeyboardButton(
            "🔍🌐 Без качества + все трекеры",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:no_quality_all_trackers",
        )])
    rows.append([InlineKeyboardButton(
        "❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel",
    )])
    return InlineKeyboardMarkup(rows)


def _search_error_keyboard() -> InlineKeyboardMarkup:
    """Shown after a fatal search error (both sources unavailable).

    Gives the user a way to retry the same query or close the message.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"{SEARCH_CALLBACK_PREFIX}:retry")],
        [InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))],
    ])


def _download_error_keyboard(
    *,
    index: int,
    can_queue: bool = False,
    can_retry: bool = True,
) -> InlineKeyboardMarkup:
    """Shown on a torrent download failure.

    Offers actionable buttons depending on what's available:

    - **🔄 Повторить** — retry the same download (uses ``index`` into ``srch_results``).
      Default on; pass ``can_retry=False`` to hide (e.g. if the result is unrecoverable).
    - **⏳ Поставить в очередь** — only when ``can_queue=True``. The handler that
      processes this button is part of the pending-download-queue feature
      (gated by ``PENDING_DOWNLOADS_ENABLED``).
    - **✖️ Закрыть** — always present so the screen never dead-ends.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if can_retry:
        rows.append([InlineKeyboardButton(
            "🔄 Повторить",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:retry_dl:{index}",
        )])
    if can_queue:
        rows.append([InlineKeyboardButton(
            "⏳ Поставить в очередь",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:queue_dl:{index}",
        )])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def tracker_selection_label(indexers: list[dict], selected_ids: set[str]) -> str:
    """Human-readable label for the currently selected Jackett tracker set."""
    if not indexers:
        return "Rutracker"
    all_ids = {i["id"] for i in indexers}
    names = [i.get("name", i["id"]) for i in indexers if i["id"] in selected_ids]
    if not names:
        return "нет трекеров"
    if selected_ids >= all_ids:
        return "Все трекеры"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}, {names[1]}"
    return f"{names[0]} +{len(names) - 1}"


def _search_options_keyboard(tracker_label: str = "") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 Искать", callback_data=f"{SEARCH_CALLBACK_PREFIX}:quick", style="success")],
        [InlineKeyboardButton("⚙️ Доп. параметры", callback_data=f"{SEARCH_CALLBACK_PREFIX}:adv")],
    ]
    if tracker_label:
        rows.append([InlineKeyboardButton(
            f"🌐 Трекер: {tracker_label}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:pick_tracker:options",
        )])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")])
    return InlineKeyboardMarkup(rows)


def _search_advanced_keyboard(settings: dict, tracker_label: str = "") -> InlineKeyboardMarkup:
    quality = settings.get("quality", "1080p")
    audio = settings.get("audio", False)
    subs = settings.get("subs", False)

    def q_btn(label: str, value: str) -> InlineKeyboardButton:
        prefix = "✅ " if quality == value else ""
        return InlineKeyboardButton(
            f"{prefix}{label}", callback_data=f"{SEARCH_CALLBACK_PREFIX}:quality:{value}"
        )

    def toggle_btn(label: str, key: str, active: bool) -> InlineKeyboardButton:
        prefix = "✅ " if active else ""
        return InlineKeyboardButton(
            f"{prefix}{label}", callback_data=f"{SEARCH_CALLBACK_PREFIX}:toggle:{key}"
        )

    rows = [
        [q_btn(label, val) for label, val in _SRCH_QUALITY_OPTIONS],
        [toggle_btn("🎵 Оригинальная дорожка", "audio", audio)],
        [toggle_btn("💬 Субтитры", "subs", subs)],
    ]
    if tracker_label:
        rows.append([InlineKeyboardButton(
            f"🌐 Трекер: {tracker_label}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:pick_tracker:advanced",
        )])
    rows.append([InlineKeyboardButton("🔍 Искать", callback_data=f"{SEARCH_CALLBACK_PREFIX}:do_search", style="success")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")])
    return InlineKeyboardMarkup(rows)


SEARCH_PAGE_SIZE = 5


def _search_results_keyboard(
    results: list[dict],
    page: int = 0,
    show_switch_trackers: bool = False,   # source=jackett  → "🔄 Сменить трекеры"
    show_retry_jackett: bool = False,     # source=rutracker → "↩️ Повторить через Jackett"
    show_direct_rutracker: bool = False,  # source=jackett  → "🔗 Rutracker напрямую"
    show_back_to_discovery: bool = False,
    # Legacy aliases kept for backwards-compat during transition
    show_jackett_expand: bool = False,
    show_jackett_direct: bool = False,
) -> InlineKeyboardMarkup:
    total = len(results)
    total_pages = max(1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * SEARCH_PAGE_SIZE
    visible = results[start : start + SEARCH_PAGE_SIZE]

    rows = []

    # Single row of numbered download buttons for all visible results.
    dl_row = [
        InlineKeyboardButton(
            f"⬇️ {start + i + 1}",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:dl:{start + i}",
        )
        for i in range(len(visible))
    ]
    rows.append(dl_row)

    # One row per partial result with two subscribe buttons — let the user pick
    # their notification mode at subscribe time:
    #   📺 «Серии» (per_episode) — push on each new episode batch (current behavior).
    #   🎯 «Сезон» (season_complete) — silent until the whole season is released,
    #     then one consolidated push. Useful for marathon viewers.
    # Both buttons trigger the same download + subscribe flow; they differ only
    # in the `notify_mode` field stored on the subscription.
    for i, result in enumerate(visible):
        if result.get("partial"):
            index = start + i
            rows.append([
                InlineKeyboardButton(
                    f"⬇️📺 Серии {index + 1}",
                    callback_data=f"{SEARCH_CALLBACK_PREFIX}:sub:{index}",
                ),
                InlineKeyboardButton(
                    f"⬇️🎯 Сезон {index + 1}",
                    callback_data=f"{SEARCH_CALLBACK_PREFIX}:sub_season:{index}",
                ),
            ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("◀", callback_data=f"{SEARCH_CALLBACK_PREFIX}:res_page:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:res_page:{page}",
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton("▶", callback_data=f"{SEARCH_CALLBACK_PREFIX}:res_page:{page + 1}")
            )
        rows.append(nav_row)

    if show_switch_trackers or show_jackett_expand or show_jackett_direct:
        rows.append([
            InlineKeyboardButton(
                "🔄 Сменить трекеры",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:switch_trackers",
            )
        ])
    if show_retry_jackett:
        rows.append([
            InlineKeyboardButton(
                "↩️ Повторить через Jackett",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:switch_trackers",
            )
        ])
    if show_direct_rutracker:
        rows.append([
            InlineKeyboardButton(
                "🔗 Rutracker напрямую",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:direct_rt",
            )
        ])
    if show_back_to_discovery:
        rows.append([InlineKeyboardButton("🎬 ← Новинки", callback_data="new:back")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")])
    return InlineKeyboardMarkup(rows)


_RUTRACKER_TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t={topic_id}"


def _search_after_add_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown in the success message when a series torrent was added.

    Includes a shortcut to search for another season of the same show,
    plus the standard task-management buttons.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔎 Другой сезон",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:series_base",
        )],
        [
            InlineKeyboardButton("🔄 Обновить статус", callback_data=_task_callback("info", task_id)),
            InlineKeyboardButton("📋 К загрузкам", callback_data=_task_callback("list", task_id)),
        ],
    ])


def _season_back_to_picker_keyboard() -> InlineKeyboardMarkup:
    """Shown after a season-specific search returned 0 hits but the tracker
    has other seasons. Lets the user step back into the picker instead of
    having to restart the whole flow."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⬅️ К выбору сезона",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:season_back_to_picker",
        )],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])


def _season_select_keyboard(
    total_seasons: int | None,
    plex_seasons: set[int] | None = None,
) -> InlineKeyboardMarkup:
    """Keyboard for choosing which season to search for.

    When *total_seasons* is known (2–20) numbered buttons are shown 5 per row,
    so the user can tap a season directly. Always includes 'Enter manually',
    'No filter' (search the whole series), 'Back' and 'Cancel'.

    When *plex_seasons* is provided, season buttons that already exist in the
    user's Plex library are prefixed with a '✅ ' marker — the callback_data
    is unchanged, so the user can still tap to re-download.
    """
    in_plex = plex_seasons or set()
    rows: list[list[InlineKeyboardButton]] = []

    if total_seasons and 1 < total_seasons <= 20:
        nums = list(range(1, total_seasons + 1))
        for i in range(0, len(nums), 5):
            rows.append([
                InlineKeyboardButton(
                    f"✅ {n}" if n in in_plex else str(n),
                    callback_data=f"{SEARCH_CALLBACK_PREFIX}:season:{n}",
                )
                for n in nums[i : i + 5]
            ])

    rows.append([
        InlineKeyboardButton(
            "✏️ Свой номер",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:season_input",
        ),
        InlineKeyboardButton(
            "🔎 Без сезона",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:season_skip",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ Назад",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:season_back",
        ),
        InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _jackett_select_keyboard(
    indexers: list[dict],   # [{"id": "rutracker", "name": "RuTracker.org"}, ...]
    selected_ids: set[str],
    *,
    confirm_label: str = "🔍 Искать",
    show_back: bool = False,
) -> InlineKeyboardMarkup:
    """Keyboard for choosing which Jackett indexers to search.

    confirm_label: text for the confirm button ("🔍 Искать" when searching immediately,
                   "✅ Применить" when returning to the options/advanced screen).
    show_back:     show a «← Назад» button (used when opened from options/advanced).
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for idx in indexers:
        id_ = idx.get("id", "")
        if not id_:
            continue
        check = "✅ " if id_ in selected_ids else ""
        abbr = _tracker_abbr(id_)
        row.append(
            InlineKeyboardButton(
                f"{check}{abbr}",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:{JACKETT_SELECT_PREFIX}_toggle:{id_}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    bottom: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            confirm_label,
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:{JACKETT_SELECT_PREFIX}_search",
        ),
    ]
    if show_back:
        bottom.append(InlineKeyboardButton(
            "⬅️ Назад",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:jk_back",
        ))
    else:
        bottom.append(InlineKeyboardButton(
            "❌ Отмена",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel",
        ))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def users_keyboard(
    approved_users: dict,
    *,
    back_to_admin: bool = True,
) -> InlineKeyboardMarkup:
    """Keyboard for the users management panel."""
    rows = [
        [InlineKeyboardButton(
            f"🚫 {info.get('name', '') or uid}",
            callback_data=f"{ACCESS_CALLBACK_PREFIX}:remove:{uid}",
        )]
        for uid, info in approved_users.items()
    ]
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"{ACCESS_CALLBACK_PREFIX}:users_refresh")])
    if back_to_admin:
        rows.append([InlineKeyboardButton("⬅️ Админ-панель", callback_data=f"{ADMIN_CALLBACK_PREFIX}:home")])
    else:
        rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=_task_callback("close", ""))])
    return InlineKeyboardMarkup(rows)


def movie_trackers_keyboard(
    all_trackers: list[dict],
    enabled_ids: set[str] | None,
) -> InlineKeyboardMarkup:
    """Keyboard for selecting which Jackett trackers participate in /new rating."""
    def _sort_key(idx: dict) -> tuple:
        is_off = enabled_ids is not None and idx.get("id", "") not in enabled_ids
        return (1 if is_off else 0, idx.get("name", idx.get("id", "")))

    rows = []
    for idx in sorted(all_trackers, key=_sort_key):
        id_ = idx.get("id", "")
        name = idx.get("name", id_)
        is_on = enabled_ids is None or id_ in enabled_ids
        check = "✅ " if is_on else "☐ "
        rows.append([InlineKeyboardButton(
            f"{check}{name}",
            callback_data=_admin_callback(f"tracker_toggle:{id_}"),
        )])
    some_disabled = enabled_ids is not None and any(
        idx.get("id", "") not in enabled_ids for idx in all_trackers
    )
    if some_disabled:
        rows.append([InlineKeyboardButton("✅ Включить все", callback_data=_admin_callback("tracker_enable_all"))])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=_admin_callback("home"))])
    return InlineKeyboardMarkup(rows)
