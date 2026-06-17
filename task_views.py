from datetime import datetime, timedelta, timezone, tzinfo

from formatters import (
    _format_eta,
    _format_hours,
    _format_progress,
    _format_size,
    _progress_bar,
    _progress_meter,
    _progress_percent,
    _task_remaining_bytes,
)
from task_policies import is_complete_despite_error, user_task_status_icon, user_task_status_label


ACTIVE_STATUSES = {"downloading", "waiting", "finishing", "hash_checking"}
FINISHED_STATUSES = {"finished", "seeding"}


def _task_status(task: dict) -> str:
    return (task.get("status") or "").lower()


def _is_finished_task(task: dict) -> bool:
    return _task_status(task) in FINISHED_STATUSES or is_complete_despite_error(task)


def _is_active_task(task: dict) -> bool:
    return _task_status(task) in ACTIVE_STATUSES


def _finished_status_label(task: dict) -> str:
    if is_complete_despite_error(task):
        return "Скачано полностью"

    return "Завершено"


def _display_status_icon(task: dict) -> str:
    if _is_finished_task(task):
        return "✅"

    return user_task_status_icon(task)


def _task_summary(tasks: list[dict]) -> str:
    active = sum(1 for task in tasks if _is_active_task(task))
    finished = sum(1 for task in tasks if _is_finished_task(task))
    errors = sum(
        1
        for task in tasks
        if _task_status(task) == "error" and not is_complete_despite_error(task)
    )
    return f"Всего: {len(tasks)} · Активно: {active} · Завершено: {finished} · С ошибкой: {errors}"


def _display_now(now: datetime | None, display_timezone: tzinfo) -> datetime:
    if now is None:
        return datetime.now(display_timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=display_timezone)

    return now.astimezone(display_timezone)


def _coerce_timestamp(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            timestamp = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None

    if timestamp > 1_000_000_000_000:
        timestamp /= 1000
    if timestamp <= 0:
        return None

    return timestamp


def _task_finished_timestamp(task: dict, auto_delete_tasks: dict[str, float] | None) -> float | None:
    additional = task.get("additional", {})
    additional = additional if isinstance(additional, dict) else {}
    detail = additional.get("detail", {})
    detail = detail if isinstance(detail, dict) else {}

    for source in (task, detail):
        for key in (
            "finished_at",
            "completed_at",
            "finish_time",
            "finished_time",
            "completed_time",
            "complete_time",
        ):
            timestamp = _coerce_timestamp(source.get(key))
            if timestamp is not None:
                return timestamp

    task_id = task.get("id")
    if task_id and auto_delete_tasks:
        return _coerce_timestamp(auto_delete_tasks.get(str(task_id)))

    return None


def _format_finished_at(
    task: dict,
    *,
    auto_delete_tasks: dict[str, float] | None,
    now: datetime | None,
    display_timezone: tzinfo,
) -> str:
    timestamp = _task_finished_timestamp(task, auto_delete_tasks)
    if timestamp is None:
        return ""

    current = _display_now(now, display_timezone)
    finished_at = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(display_timezone)
    if finished_at.date() == current.date():
        return f"сегодня {finished_at:%H:%M}"
    if finished_at.date() == (current - timedelta(days=1)).date():
        return f"вчера {finished_at:%H:%M}"
    if finished_at.year == current.year:
        return f"{finished_at:%d.%m %H:%M}"

    return f"{finished_at:%d.%m.%y %H:%M}"


def _finished_size(task: dict, transfer: dict) -> str:
    return _format_size(transfer.get("size_downloaded") or task.get("size"))


def _task_source_label(task: dict) -> str:
    task_type = (task.get("type") or "").lower()
    if task_type == "youtube":
        return "▶️ YouTube"
    if task_type == "bt":
        return "🧲 Download Station"
    return "📥 Download Station"


def _is_youtube_task(task: dict) -> bool:
    return (task.get("type") or "").lower() == "youtube"


def _auto_delete_line(
    task: dict,
    *,
    auto_delete_tasks: dict[str, float] | None,
    auto_delete_enabled: bool,
    auto_delete_statuses: set[str] | None,
    auto_delete_after_hours: float,
    now: datetime | None,
    display_timezone: tzinfo,
) -> str:
    if _is_youtube_task(task):
        return ""
    if not auto_delete_enabled or auto_delete_after_hours <= 0:
        return ""
    if _task_status(task) not in (auto_delete_statuses or set()):
        return ""

    timestamp = _task_finished_timestamp(task, auto_delete_tasks)
    if timestamp is None:
        return f"Автоочистка: через {_format_hours(auto_delete_after_hours)}"

    current = _display_now(now, display_timezone)
    delete_at = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(display_timezone) + timedelta(hours=auto_delete_after_hours)
    remaining_hours = (delete_at - current).total_seconds() / 3600
    if remaining_hours <= 0:
        return "Автоочистка: скоро"

    return f"Автоочистка: через {_format_hours(remaining_hours)}"


def _format_task_lines(
    task: dict,
    *,
    transfer: dict,
    percent: float | None,
    progress: str,
    speed: str,
    eta: str,
    auto_delete_tasks: dict[str, float] | None,
    auto_delete_enabled: bool,
    auto_delete_statuses: set[str] | None,
    auto_delete_after_hours: float,
    now: datetime | None,
    display_timezone: tzinfo,
    indent: str,
) -> list[str]:
    lines = [f"{indent}Источник: {_task_source_label(task)}"]
    if _is_finished_task(task):
        parts = [_finished_status_label(task)]
        finished_at = _format_finished_at(
            task,
            auto_delete_tasks=auto_delete_tasks,
            now=now,
            display_timezone=display_timezone,
        )
        if finished_at:
            parts.append(finished_at)
        parts.append(_finished_size(task, transfer))

        lines.append(f"{indent}{' · '.join(parts)}")
        auto_delete = _auto_delete_line(
            task,
            auto_delete_tasks=auto_delete_tasks,
            auto_delete_enabled=auto_delete_enabled,
            auto_delete_statuses=auto_delete_statuses,
            auto_delete_after_hours=auto_delete_after_hours,
            now=now,
            display_timezone=display_timezone,
        )
        if auto_delete:
            lines.append(f"{indent}{auto_delete}")
        return lines

    lines.extend([
        f"{indent}Статус: {user_task_status_label(task)}",
        f"{indent}Прогресс: {_progress_meter(percent)}",
        f"{indent}Скачано: {progress}",
    ])
    if _is_active_task(task):
        lines.append(f"{indent}Скорость: {speed}/s | Осталось: {eta}")

    return lines


def default_list_scope(is_admin: bool, *, scope_all: str, scope_my: str) -> str:
    return scope_all if is_admin else scope_my


def normalize_list_scope(
    scope: str | None,
    is_admin: bool,
    *,
    scope_all: str,
    scope_my: str,
    scope_default: str,
) -> str:
    if scope == scope_my:
        return scope_my
    if scope == scope_default or scope not in {scope_all, scope_my}:
        return default_list_scope(is_admin, scope_all=scope_all, scope_my=scope_my)
    if scope == scope_all and is_admin:
        return scope_all

    return scope_my


def filter_tasks_for_scope(
    tasks: list[dict],
    chat_id: int | None,
    scope: str,
    *,
    owners: dict[str, int],
    is_admin: bool,
    scope_all: str,
) -> list[dict]:
    if scope == scope_all and is_admin:
        return tasks

    return [
        task
        for task in tasks
        if task.get("id") and owners.get(str(task["id"])) == chat_id
    ]


def format_tasks(
    tasks: list[dict],
    *,
    scope: str,
    updated_at: str,
    owners: dict[str, int],
    owner_labels: dict[int, str] | None = None,
    total_count: int | None = None,
    page: int = 0,
    page_size: int = 5,
    scope_all: str,
    auto_delete_tasks: dict[str, float] | None = None,
    auto_delete_enabled: bool = False,
    auto_delete_statuses: set[str] | None = None,
    auto_delete_after_hours: float = 0.0,
    now: datetime | None = None,
    display_timezone: tzinfo = timezone.utc,
) -> str:
    heading = "Все загрузки" if scope == scope_all else "Мои загрузки"
    if not tasks:
        if scope == scope_all:
            empty_text = (
                "Задач сейчас нет.\n\n"
                "Здесь появятся загрузки, которые бот отправил в очередь скачивания, "
                "задачи из Download Station и YouTube-очереди.\n"
                "Если задача только что добавлена, нажмите «Обновить»."
            )
        else:
            empty_text = (
                "В ваших загрузках сейчас пусто.\n\n"
                "Здесь отображаются задачи, которые вы запустили через бот.\n"
                "Если загрузка только что добавлена, нажмите «Обновить»."
            )
        return f"{heading}\nОбновлено: {updated_at}\n{empty_text}"

    lines = [heading, f"Обновлено: {updated_at}", _task_summary(tasks)]
    if total_count is not None and scope != scope_all and total_count != len(tasks):
        lines.append(f"Показано: {len(tasks)} из {total_count}")

    total_pages = max(1, (len(tasks) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    visible_tasks = tasks[start: start + page_size]

    for index, task in enumerate(visible_tasks, start=start + 1):
        title = task.get("title") or (
            "YouTube" if _is_youtube_task(task) else task.get("id") or "без названия"
        )
        transfer = task.get("additional", {}).get("transfer", {})
        downloaded = transfer.get("size_downloaded")
        total = task.get("size")
        percent = _progress_percent(downloaded, total)
        progress = _format_progress(downloaded, total)
        speed_bytes = transfer.get("speed_download")
        speed = _format_size(speed_bytes)
        eta = _format_eta(_task_remaining_bytes(task, transfer), speed_bytes)
        task_id = task.get("id")

        task_lines = _format_task_lines(
            task,
            transfer=transfer,
            percent=percent,
            progress=progress,
            speed=speed,
            eta=eta,
            auto_delete_tasks=auto_delete_tasks,
            auto_delete_enabled=auto_delete_enabled,
            auto_delete_statuses=auto_delete_statuses,
            auto_delete_after_hours=auto_delete_after_hours,
            now=now,
            display_timezone=display_timezone,
            indent="   ",
        )
        line = f"{index}. {_display_status_icon(task)} {title}\n" + "\n".join(task_lines)
        if task_id and scope == scope_all:
            if not _is_youtube_task(task):
                line += f"\n   ID: {task_id}"
            owner = owners.get(str(task_id))
            label = (owner_labels or {}).get(owner, str(owner)) if owner else ""
            line += f"\n   Владелец: {label}" if label else "\n   Владелец: неизвестно"
        lines.append(line)
        if index - start < len(visible_tasks):
            lines.append("────────────")

    if total_pages > 1:
        lines.append(f"\nСтраница {page + 1} из {total_pages} (всего задач: {len(tasks)}).")

    return "\n".join(lines)


def find_task(tasks: list[dict], task_id: str) -> dict | None:
    for task in tasks:
        if task.get("id") == task_id:
            return task

    return None


def format_task_card(
    task: dict,
    *,
    is_admin: bool = False,
    auto_delete_tasks: dict[str, float] | None = None,
    auto_delete_enabled: bool = False,
    auto_delete_statuses: set[str] | None = None,
    auto_delete_after_hours: float = 0.0,
    now: datetime | None = None,
    display_timezone: tzinfo = timezone.utc,
) -> str:
    title = task.get("title") or "без названия"
    task_id = task.get("id") or "unknown"
    transfer = task.get("additional", {}).get("transfer", {})
    downloaded = transfer.get("size_downloaded")
    total = task.get("size")
    percent = _progress_percent(downloaded, total)
    progress = _format_progress(downloaded, total)
    speed_bytes = transfer.get("speed_download")
    speed = _format_size(speed_bytes)
    eta = _format_eta(_task_remaining_bytes(task, transfer), speed_bytes)
    complete_despite_error = is_complete_despite_error(task)

    if is_admin:
        lines = [
            "Задача",
            f"Имя: {title}",
            f"Источник: {_task_source_label(task)}",
        ]
        if not _is_youtube_task(task):
            lines.insert(2, f"ID: {task_id}")
    else:
        lines = [
            "Загрузка",
            f"Файл: {title}",
            f"Источник: {_task_source_label(task)}",
        ]
    if _is_finished_task(task):
        parts = [_finished_status_label(task)]
        finished_at = _format_finished_at(
            task,
            auto_delete_tasks=auto_delete_tasks,
            now=now,
            display_timezone=display_timezone,
        )
        if finished_at:
            parts.append(finished_at)
        lines.append(f"Статус: ✅ {' · '.join(parts)}")
        lines.append(f"Скачано: {_finished_size(task, transfer)}")
        auto_delete = _auto_delete_line(
            task,
            auto_delete_tasks=auto_delete_tasks,
            auto_delete_enabled=auto_delete_enabled,
            auto_delete_statuses=auto_delete_statuses,
            auto_delete_after_hours=auto_delete_after_hours,
            now=now,
            display_timezone=display_timezone,
        )
        if auto_delete:
            lines.append(auto_delete)
    else:
        lines.extend([
            f"Статус: {user_task_status_icon(task)} {user_task_status_label(task)}",
            f"Прогресс: {_progress_bar(percent)}",
            f"Скачано: {progress}",
        ])
        if _is_active_task(task):
            lines.extend([
                f"Скорость: {speed}/s",
                f"Осталось: {eta}",
            ])
    if complete_despite_error:
        if is_admin:
            lines.append("Download Station показывает ошибку, но файл скачан полностью.")
        else:
            lines.append("Сервис загрузок показывает ошибку, но файл скачан полностью.")

    return "\n".join(lines)


def has_active_tasks(tasks: list[dict], active_statuses: set[str] | None = None) -> bool:
    statuses = active_statuses or ACTIVE_STATUSES
    return any((task.get("status") or "").lower() in statuses for task in tasks)
