from formatters import _format_hours, _format_progress, _format_size, _progress_percent, _status_icon, _status_label


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


def _has_specific_error_detail(value: object) -> bool:
    if not value:
        return False
    if not isinstance(value, dict):
        return True

    error_detail = value.get("error_detail")
    if error_detail is None:
        return False
    if isinstance(error_detail, str):
        return error_detail.strip().lower() not in {"", "unknown"}

    return True


def is_complete_despite_error(task: dict) -> bool:
    status = (task.get("status") or "").lower()
    task_type = (task.get("type") or "").lower()
    if status != "error" or task_type != "bt":
        return False

    if _has_specific_error_detail(task.get("status_extra")):
        return False

    additional = task.get("additional", {})
    additional = additional if isinstance(additional, dict) else {}
    detail = additional.get("detail", {})
    if _has_specific_error_detail(detail):
        return False

    total = task.get("size")
    transfer = additional.get("transfer", {})
    downloaded = transfer.get("size_downloaded") if isinstance(transfer, dict) else None
    percent = _progress_percent(downloaded, total)
    return percent is not None and percent >= 99.9


def user_task_status_icon(task: dict) -> str:
    if is_complete_despite_error(task):
        return "✅"

    return _status_icon(task.get("status"))


def user_task_status_label(task: dict, *, plex_polling_started: bool = False) -> str:
    if is_complete_despite_error(task):
        if plex_polling_started:
            return "скачано полностью, проверяем Plex"
        return "скачано полностью"

    return _status_label(task.get("status"))


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
    plex_polling_started: bool = False,
) -> str:
    title = task.get("title") or task.get("id") or "без названия"
    task_id = task.get("id") or "unknown"
    status = task.get("status", "unknown")
    transfer = task.get("additional", {}).get("transfer", {})
    progress = _format_progress(transfer.get("size_downloaded"), task.get("size"))
    speed = _format_size(transfer.get("speed_download"))

    complete_despite_error = is_complete_despite_error(task)
    status_label = user_task_status_label(task, plex_polling_started=plex_polling_started)

    if complete_despite_error:
        header = "✅ Загрузка дошла до 100%"
    elif (status or "").lower() == "finished":
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
        f"Статус: {status_label}",
        f"Скачано: {progress}",
        f"Скорость: {speed}/s",
    ]
    if complete_despite_error and plex_polling_started:
        lines.append(
            "Download Station показывает ошибку, но файл скачан полностью. "
            "Проверяем Plex и сообщим, когда он появится в библиотеке."
        )
    elif complete_despite_error:
        lines.append(
            "Download Station показывает ошибку, но файл скачан полностью. "
            "Скорее всего, всё в порядке."
        )

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
