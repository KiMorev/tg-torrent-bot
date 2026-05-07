def all_allowed_chat_ids(
    allowed_chat_ids: set[int],
    admin_chat_ids: set[int],
    approved_chat_ids: set[int],
) -> set[int]:
    return allowed_chat_ids | admin_chat_ids | approved_chat_ids


def is_admin_chat(chat_id: int | None, admin_chat_ids: set[int]) -> bool:
    return bool(chat_id is not None and chat_id in admin_chat_ids)


def is_allowed_chat(
    chat_id: int | None,
    allowed_chat_ids: set[int],
    admin_chat_ids: set[int],
    approved_chat_ids: set[int],
) -> bool:
    if chat_id is None:
        return False

    return chat_id in all_allowed_chat_ids(
        allowed_chat_ids,
        admin_chat_ids,
        approved_chat_ids,
    )


def access_request_user_label(full_name: str | None, username: str | None, user_id: int | None) -> str:
    parts = [part for part in [full_name, f"@{username}" if username else ""] if part]
    return " ".join(parts) or str(user_id or "неизвестно")
