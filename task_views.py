from formatters import (
    _format_eta,
    _format_progress,
    _format_size,
    _progress_bar,
    _progress_meter,
    _progress_percent,
    _status_icon,
    _status_label,
    _task_remaining_bytes,
)


ACTIVE_STATUSES = {"downloading", "waiting", "finishing", "hash_checking"}


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
) -> str:
    heading = "Все задачи Download Station" if scope == scope_all else "Мои загрузки"
    if not tasks:
        empty_text = "В Download Station нет задач." if scope == scope_all else "В ваших загрузках нет задач."
        return f"{heading}\nОбновлено: {updated_at}\n{empty_text}"

    lines = [heading, f"Обновлено: {updated_at}"]
    if total_count is not None and scope != scope_all and total_count != len(tasks):
        lines.append(f"Показано: {len(tasks)} из {total_count}")

    total_pages = max(1, (len(tasks) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    visible_tasks = tasks[start: start + page_size]

    for index, task in enumerate(visible_tasks, start=start + 1):
        title = task.get("title") or task.get("id") or "без названия"
        status = task.get("status", "unknown")
        transfer = task.get("additional", {}).get("transfer", {})
        downloaded = transfer.get("size_downloaded")
        total = task.get("size")
        percent = _progress_percent(downloaded, total)
        progress = _format_progress(downloaded, total)
        speed_bytes = transfer.get("speed_download")
        speed = _format_size(speed_bytes)
        eta = _format_eta(_task_remaining_bytes(task, transfer), speed_bytes)
        task_id = task.get("id")

        line = (
            f"{index}. {_status_icon(status)} {title}\n"
            f"   Статус: {_status_label(status)}\n"
            f"   Прогресс: {_progress_meter(percent)}\n"
            f"   Скачано: {progress}\n"
            f"   Скорость: {speed}/s | Осталось: {eta}"
        )
        if task_id:
            line += f"\n   ID: {task_id}"
            if scope == scope_all:
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


def format_task_card(task: dict) -> str:
    title = task.get("title") or "без названия"
    task_id = task.get("id") or "unknown"
    status = task.get("status", "unknown")
    transfer = task.get("additional", {}).get("transfer", {})
    downloaded = transfer.get("size_downloaded")
    total = task.get("size")
    percent = _progress_percent(downloaded, total)
    progress = _format_progress(downloaded, total)
    speed_bytes = transfer.get("speed_download")
    speed = _format_size(speed_bytes)
    eta = _format_eta(_task_remaining_bytes(task, transfer), speed_bytes)

    lines = [
        "Задача Download Station",
        f"Имя: {title}",
        f"ID: {task_id}",
        f"Статус: {_status_icon(status)} {_status_label(status)}",
        f"Прогресс: {_progress_bar(percent)}",
        f"Скачано: {progress}",
        f"Скорость: {speed}/s",
        f"Осталось: {eta}",
    ]

    return "\n".join(lines)


def has_active_tasks(tasks: list[dict], active_statuses: set[str] | None = None) -> bool:
    statuses = active_statuses or ACTIVE_STATUSES
    return any((task.get("status") or "").lower() in statuses for task in tasks)
