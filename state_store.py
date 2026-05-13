import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("tg_torrent_drop")

# Safety caps: prevent JSON state files from growing without bound
_MAX_NOTIFIED_TASKS = 500
_MAX_TRACKER_PROCESSED = 1000


class JsonStateStore:
    def __init__(
        self,
        approved_chat_ids_file: Path,
        tracker_processed_file: Path,
        task_owners_file: Path,
        notified_tasks_file: Path,
        auto_delete_tasks_file: Path,
        movie_discovery_cache_file: Path | None = None,
        topic_subscriptions_file: Path | None = None,
    ) -> None:
        self.approved_chat_ids_file = approved_chat_ids_file
        self.tracker_processed_file = tracker_processed_file
        self.task_owners_file = task_owners_file
        self.notified_tasks_file = notified_tasks_file
        self.auto_delete_tasks_file = auto_delete_tasks_file
        self.movie_discovery_cache_file = movie_discovery_cache_file
        self.topic_subscriptions_file = topic_subscriptions_file
        self.lock = threading.RLock()

    def load_json_file(self, path: Path, default: Any) -> Any:
        with self.lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return default

    def save_json_file(self, path: Path, payload: Any, label: str) -> None:
        tmp_path: Path | None = None
        with self.lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                tmp_path.write_text(text, encoding="utf-8")
                os.replace(tmp_path, path)
            except OSError:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                logger.warning("Failed to save %s", label, exc_info=True)

    def load_approved_users(self) -> dict[int, dict]:
        """Загружает одобренных пользователей с метаданными.
        Поддерживает старый формат (список int) и новый (dict с name/added_at)."""
        payload = self.load_json_file(self.approved_chat_ids_file, [])
        users: dict[int, dict] = {}

        if isinstance(payload, dict):
            for key, value in payload.items():
                try:
                    chat_id = int(key)
                    info = value if isinstance(value, dict) else {}
                    users[chat_id] = {
                        "name": str(info.get("name", "")),
                        "added_at": str(info.get("added_at", "")),
                    }
                except (TypeError, ValueError):
                    continue
            return users

        # Обратная совместимость: старый формат — список int
        if isinstance(payload, list):
            for value in payload:
                try:
                    users[int(value)] = {"name": "", "added_at": ""}
                except (TypeError, ValueError):
                    continue

        return users

    def save_approved_users(self, users: dict[int, dict]) -> None:
        payload = {
            str(chat_id): {
                "name": info.get("name", ""),
                "added_at": info.get("added_at", ""),
            }
            for chat_id, info in sorted(users.items())
        }
        self.save_json_file(self.approved_chat_ids_file, payload, "approved users")

    def load_approved_chat_ids(self) -> set[int]:
        return set(self.load_approved_users().keys())

    def save_approved_chat_ids(self, chat_ids: set[int]) -> None:
        """Совместимость: сохраняет set[int], сохраняя имена уже известных пользователей."""
        with self.lock:
            existing = self.load_approved_users()
            updated = {
                chat_id: existing.get(chat_id, {"name": "", "added_at": ""})
                for chat_id in chat_ids
            }
            self.save_approved_users(updated)

    def add_approved_user(self, chat_id: int, name: str = "") -> None:
        """Добавляет пользователя с именем и датой одобрения (атомарно)."""
        with self.lock:
            users = self.load_approved_users()
            users[chat_id] = {
                "name": name,
                "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
            self.save_approved_users(users)

    def remove_approved_user(self, chat_id: int) -> None:
        """Удаляет пользователя из одобренных (атомарно)."""
        with self.lock:
            users = self.load_approved_users()
            if chat_id in users:
                users.pop(chat_id)
                self.save_approved_users(users)

    def load_tracker_processed_ids(self) -> set[str]:
        payload = self.load_json_file(self.tracker_processed_file, [])
        if not isinstance(payload, list):
            return set()

        return {str(item) for item in payload if item}

    def save_tracker_processed_ids(self, task_ids: set[str]) -> None:
        if len(task_ids) > _MAX_TRACKER_PROCESSED:
            # Keep the last N entries alphabetically (deterministic trim)
            task_ids = set(sorted(task_ids)[-_MAX_TRACKER_PROCESSED:])
        self.save_json_file(self.tracker_processed_file, sorted(task_ids), "processed tracker task IDs")

    def add_tracker_processed_ids(self, task_ids: set[str]) -> None:
        task_ids = {str(task_id) for task_id in task_ids if task_id}
        if not task_ids:
            return

        with self.lock:
            processed_ids = self.load_tracker_processed_ids()
            updated_ids = processed_ids | task_ids
            if updated_ids != processed_ids:
                self.save_tracker_processed_ids(updated_ids)

    def load_task_owners(self) -> dict[str, int]:
        payload = self.load_json_file(self.task_owners_file, {})
        if not isinstance(payload, dict):
            return {}

        owners = {}
        for task_id, chat_id in payload.items():
            try:
                owners[str(task_id)] = int(chat_id)
            except (TypeError, ValueError):
                continue

        return owners

    def save_task_owners(self, owners: dict[str, int]) -> None:
        self.save_json_file(self.task_owners_file, owners, "task owners")

    def remember_task_owner(self, task_id: str, chat_id: int | None) -> None:
        if not task_id or chat_id is None:
            return

        with self.lock:
            owners = self.load_task_owners()
            if owners.get(task_id) == chat_id:
                return

            owners[task_id] = chat_id
            self.save_task_owners(owners)

    def load_notified_tasks(self) -> dict[str, object]:
        payload = self.load_json_file(self.notified_tasks_file, {})
        if not isinstance(payload, dict):
            return {}

        tasks: dict[str, object] = {}
        for task_id, value in payload.items():
            if not task_id or not value:
                continue

            if isinstance(value, dict):
                status = str(value.get("status", ""))
                sent = [str(chat_id) for chat_id in value.get("sent", []) if chat_id]
                raw_failures = value.get("failures", {})
                failures = {}
                if isinstance(raw_failures, dict):
                    for chat_id, count in raw_failures.items():
                        try:
                            failures[str(chat_id)] = max(0, int(count))
                        except (TypeError, ValueError):
                            continue
                subscribers = [
                    str(s) for s in value.get("subscribers", []) if s
                ]
                # Skip entries that carry no useful state at all.
                if not status and not subscribers:
                    continue
                entry: dict = {"status": status, "sent": sent, "failures": failures}
                if subscribers:
                    entry["subscribers"] = subscribers
                tasks[str(task_id)] = entry
            else:
                tasks[str(task_id)] = str(value)

        return tasks

    def save_notified_tasks(self, tasks: dict[str, object]) -> None:
        if len(tasks) > _MAX_NOTIFIED_TASKS:
            # dict preserves insertion order — drop the oldest entries
            tasks = dict(list(tasks.items())[-_MAX_NOTIFIED_TASKS:])
        self.save_json_file(self.notified_tasks_file, dict(sorted(tasks.items())), "notified tasks")

    def load_auto_delete_tasks(self) -> dict[str, float]:
        payload = self.load_json_file(self.auto_delete_tasks_file, {})
        if not isinstance(payload, dict):
            return {}

        tasks = {}
        for task_id, timestamp in payload.items():
            try:
                tasks[str(task_id)] = float(timestamp)
            except (TypeError, ValueError):
                continue

        return tasks

    def save_auto_delete_tasks(self, tasks: dict[str, float]) -> None:
        self.save_json_file(self.auto_delete_tasks_file, dict(sorted(tasks.items())), "auto-delete tasks")

    def prune_stale_task_state(self, active_ids: set[str]) -> None:
        """Remove state entries for task IDs that no longer exist in Download Station."""
        if not isinstance(active_ids, set):
            active_ids = set(active_ids)

        with self.lock:
            owners = self.load_task_owners()
            stale_owners = {k for k in owners if k not in active_ids}
            if stale_owners:
                for k in stale_owners:
                    owners.pop(k)
                self.save_task_owners(owners)

            notified = self.load_notified_tasks()
            stale_notified = {k for k in notified if k not in active_ids}
            if stale_notified:
                for k in stale_notified:
                    notified.pop(k)
                self.save_notified_tasks(notified)

            tracker_ids = self.load_tracker_processed_ids()
            stale_tracker = tracker_ids - active_ids
            if stale_tracker:
                self.save_tracker_processed_ids(tracker_ids - stale_tracker)

            auto_delete = self.load_auto_delete_tasks()
            stale_auto = {k for k in auto_delete if k not in active_ids}
            if stale_auto:
                for k in stale_auto:
                    auto_delete.pop(k)
                self.save_auto_delete_tasks(auto_delete)

    def forget_task_state(self, task_ids: list[str]) -> None:
        task_ids = [task_id for task_id in task_ids if task_id]
        if not task_ids:
            return

        with self.lock:
            task_id_set = set(task_ids)

            owners = self.load_task_owners()
            owners_changed = False
            for task_id in task_id_set:
                if task_id in owners:
                    owners.pop(task_id, None)
                    owners_changed = True
            if owners_changed:
                self.save_task_owners(owners)

            notified = self.load_notified_tasks()
            notified_changed = False
            for task_id in task_id_set:
                if task_id in notified:
                    notified.pop(task_id, None)
                    notified_changed = True
            if notified_changed:
                self.save_notified_tasks(notified)

            tracker_processed_ids = self.load_tracker_processed_ids()
            if tracker_processed_ids.intersection(task_id_set):
                self.save_tracker_processed_ids(tracker_processed_ids - task_id_set)

            auto_delete_tasks = self.load_auto_delete_tasks()
            auto_delete_changed = False
            for task_id in task_id_set:
                if task_id in auto_delete_tasks:
                    auto_delete_tasks.pop(task_id, None)
                    auto_delete_changed = True
            if auto_delete_changed:
                self.save_auto_delete_tasks(auto_delete_tasks)

    def load_topic_subscriptions(self) -> dict[str, dict]:
        """topic_id → {chat_id, title, last_episode_end, total_episodes, added_at}."""
        if not self.topic_subscriptions_file:
            return {}
        payload = self.load_json_file(self.topic_subscriptions_file, {})
        if not isinstance(payload, dict):
            return {}
        return {str(k): v for k, v in payload.items() if isinstance(v, dict)}

    def save_topic_subscriptions(self, subs: dict[str, dict]) -> None:
        if not self.topic_subscriptions_file:
            return
        self.save_json_file(self.topic_subscriptions_file, subs, "topic subscriptions")

    def load_movie_discovery_cache(self) -> dict:
        if not self.movie_discovery_cache_file:
            return {}
        payload = self.load_json_file(self.movie_discovery_cache_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_movie_discovery_cache(self, cache: dict) -> None:
        if not self.movie_discovery_cache_file:
            return
        self.save_json_file(self.movie_discovery_cache_file, cache, "movie discovery cache")
