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

    return InlineKeyboardMarkup(rows)


def _final_notification_keyboard(task_id: str, *, show_plex: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if show_plex:
        rows.append([InlineKeyboardButton("▶️ Открыть Plex", url="https://app.plex.tv")])
    rows.append([InlineKeyboardButton("🧹 Удалить из списка", callback_data=_task_callback("delete_ask", task_id))])
    rows.append([InlineKeyboardButton("📋 К списку загрузок", callback_data=_task_callback("list", task_id))])
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


def _no_quality_keyboard(base_query: str) -> InlineKeyboardMarkup:
    """Shown when a quality-filtered search returns no results.

    Offers to repeat the search with the bare base query (no quality suffix)
    or cancel altogether.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔍 Искать без фильтра качества",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:no_quality",
        )],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
    ])


def _search_options_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Искать", callback_data=f"{SEARCH_CALLBACK_PREFIX}:quick")],
            [InlineKeyboardButton("⚙️ Доп. параметры", callback_data=f"{SEARCH_CALLBACK_PREFIX}:adv")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
        ]
    )


def _search_advanced_keyboard(settings: dict) -> InlineKeyboardMarkup:
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

    return InlineKeyboardMarkup(
        [
            [q_btn(label, val) for label, val in _SRCH_QUALITY_OPTIONS],
            [toggle_btn("🎵 Оригинальная дорожка", "audio", audio)],
            [toggle_btn("💬 Субтитры", "subs", subs)],
            [InlineKeyboardButton("🔍 Искать", callback_data=f"{SEARCH_CALLBACK_PREFIX}:do_search")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel")],
        ]
    )


SEARCH_PAGE_SIZE = 5


def _search_results_keyboard(
    results: list[dict],
    page: int = 0,
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

    # One row per partial result with a combined download+subscribe button.
    for i, result in enumerate(visible):
        if result.get("partial"):
            index = start + i
            rows.append([
                InlineKeyboardButton(
                    f"⬇️+🔔 Подписка {index + 1}",
                    callback_data=f"{SEARCH_CALLBACK_PREFIX}:sub:{index}",
                )
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

    if show_jackett_expand:
        rows.append([
            InlineKeyboardButton(
                "🌐 Расширить поиск (Jackett)",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:expand_jackett",
            )
        ])
    if show_jackett_direct:
        rows.append([
            InlineKeyboardButton(
                "🔍 Поиск через Jackett",
                callback_data=f"{SEARCH_CALLBACK_PREFIX}:jackett_direct",
            )
        ])
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


def _season_select_keyboard(total_seasons: int | None) -> InlineKeyboardMarkup:
    """Keyboard for choosing which season to search for.

    When *total_seasons* is known (2–20) numbered buttons are shown 5 per row,
    so the user can tap a season directly. Always includes 'Enter manually',
    'No filter' (search the whole series), and 'Cancel'.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if total_seasons and 1 < total_seasons <= 20:
        nums = list(range(1, total_seasons + 1))
        for i in range(0, len(nums), 5):
            rows.append([
                InlineKeyboardButton(
                    str(n),
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
        InlineKeyboardButton("❌ Отмена", callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _jackett_select_keyboard(
    indexers: list[dict],   # [{"id": "rutracker", "name": "RuTracker.org"}, ...]
    selected_ids: set[str],
) -> InlineKeyboardMarkup:
    """Keyboard for choosing which Jackett indexers to search."""
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

    rows.append([
        InlineKeyboardButton(
            "🔍 Искать",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:{JACKETT_SELECT_PREFIX}_search",
        ),
        InlineKeyboardButton(
            "❌ Отмена",
            callback_data=f"{SEARCH_CALLBACK_PREFIX}:cancel",
        ),
    ])
    return InlineKeyboardMarkup(rows)


