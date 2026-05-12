from formatters import _format_hours, _format_progress, _format_size, _status_icon, _status_label


def notification_recipients(
    task_id: str,
    *,
    explicit_chat_ids: set[int],
    task_owners: dict[str, int],
    notify_external_tasks: bool,
    fallback_chat_ids: set[int],
    allowed_chat_ids: set[int] | None = None,
) -> set[int]:
    if explicit_chat_ids:
        return explicit_chat_ids

    owner_chat_id = task_owners.get(task_id)
    if owner_chat_id and (allowed_chat_ids is None or owner_chat_id in allowed_chat_ids):
        return {owner_chat_id}

    if notify_external_tasks:
        return fallback_chat_ids

    return set()


def notification_status_key(status: str) -> str:
    if status in {"finished", "seeding"}:
        return "done"
    if status == "error":
        return "error"

    return status


def auto_delete_notice(
    status: str,
    *,
    enabled: bool,
    finished_statuses: set[str],
    delete_after_hours: float,
) -> str:
    if not enabled:
        return ""
    if (status or "").lower() not in finished_statuses:
        return ""

    return f"Автоочистка: через {_format_hours(delete_after_hours)}."


def format_task_notification(
    task: dict,
    *,
    auto_delete_enabled: bool,
    auto_delete_statuses: set[str],
    auto_delete_after_hours: float,
) -> str:
    title = task.get("title") or task.get("id") or "без названия"
    task_id = task.get("id") or "unknown"
    status = task.get("status", "unknown")
    transfer = task.get("additional", {}).get("transfer", {})
    progress = _format_progress(transfer.get("size_downloaded"), task.get("size"))
    speed = _format_size(transfer.get("speed_download"))

    if (status or "").lower() == "finished":
        header = "✅ Загрузка завершена"
    elif (status or "").lower() == "seeding":
        header = "✅ Загрузка завершена, идет раздача"
    elif (status or "").lower() == "error":
        header = "⚠️ Загрузка остановилась с ошибкой"
    else:
        header = f"{_status_icon(status)} Статус загрузки изменился"

    lines = [
        header,
        f"Имя: {title}",
        f"ID: {task_id}",
        f"Статус: {_status_label(status)}",
        f"Скачано: {progress}",
        f"Скорость: {speed}/s",
    ]

    notice = auto_delete_notice(
        status,
        enabled=auto_delete_enabled,
        finished_statuses=auto_delete_statuses,
        delete_after_hours=auto_delete_after_hours,
    )
    if notice:
        lines.append(notice)

    return "\n".join(lines)


def is_auto_delete_candidate(task: dict, finished_statuses: set[str]) -> bool:
    task_id = task.get("id")
    status = (task.get("status") or "").lower()
    return bool(task_id and status in finished_statuses)
