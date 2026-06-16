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
        movie_discovery_settings_file: Path | None = None,
        topic_subscriptions_file: Path | None = None,
        task_meta_file: Path | None = None,
        pending_downloads_file: Path | None = None,
        series_bulk_jobs_file: Path | None = None,
        series_continue_totals_file: Path | None = None,
        series_continue_hidden_file: Path | None = None,
        storage_history_file: Path | None = None,
        voice_usage_file: Path | None = None,
        user_search_defaults_file: Path | None = None,
        gpt_usage_file: Path | None = None,
        torrent_titles_cache_file: Path | None = None,
        download_history_file: Path | None = None,
        jackett_guard_file: Path | None = None,
    ) -> None:
        self.approved_chat_ids_file = approved_chat_ids_file
        self.tracker_processed_file = tracker_processed_file
        self.task_owners_file = task_owners_file
        self.notified_tasks_file = notified_tasks_file
        self.auto_delete_tasks_file = auto_delete_tasks_file
        self.movie_discovery_cache_file = movie_discovery_cache_file
        self.movie_discovery_settings_file = movie_discovery_settings_file
        self.topic_subscriptions_file = topic_subscriptions_file
        self.task_meta_file = task_meta_file
        self.pending_downloads_file = pending_downloads_file
        self.series_bulk_jobs_file = series_bulk_jobs_file
        self.series_continue_totals_file = series_continue_totals_file
        self.series_continue_hidden_file = series_continue_hidden_file
        self.storage_history_file = storage_history_file
        self.voice_usage_file = voice_usage_file
        self.user_search_defaults_file = user_search_defaults_file
        self.gpt_usage_file = gpt_usage_file
        self.torrent_titles_cache_file = torrent_titles_cache_file
        self.download_history_file = download_history_file
        self.jackett_guard_file = jackett_guard_file
        self.lock = threading.RLock()

    def load_json_file(self, path: Path, default: Any) -> Any:
        with self.lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return default
            except json.JSONDecodeError:
                logger.warning("Malformed JSON in %s; using default", path, exc_info=True)
                return default
            except OSError:
                logger.warning("Failed to load %s; using default", path, exc_info=True)
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
            except (OSError, TypeError, ValueError):
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                logger.warning("Failed to save %s", label, exc_info=True)

    def append_jsonl_file(self, path: Path, payload: dict, label: str) -> None:
        with self.lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except (OSError, TypeError, ValueError):
                logger.warning("Failed to append %s", label, exc_info=True)

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

    def load_task_meta(self) -> dict[str, dict]:
        """task_id → {kind, title, year, quality, series_query, season_num, source}.

        Provides canonical metadata captured at task-creation time, so Plex
        polling can match by (title, year) or (series_query, season_num) rather
        than guessing from the raw DS task title.
        """
        if not self.task_meta_file:
            return {}
        payload = self.load_json_file(self.task_meta_file, {})
        if not isinstance(payload, dict):
            return {}
        out: dict[str, dict] = {}
        for task_id, entry in payload.items():
            if not task_id or not isinstance(entry, dict):
                continue
            out[str(task_id)] = entry
        return out

    def save_task_meta(self, meta: dict[str, dict]) -> None:
        if not self.task_meta_file:
            return
        self.save_json_file(self.task_meta_file, dict(sorted(meta.items())), "task meta")

    def remember_task_meta(self, task_id: str, entry: dict) -> None:
        if not task_id or not isinstance(entry, dict) or not self.task_meta_file:
            return

        with self.lock:
            meta = self.load_task_meta()
            if meta.get(task_id) == entry:
                return
            meta[str(task_id)] = entry
            self.save_task_meta(meta)

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
                plex_done: bool = bool(value.get("plex_done"))
                raw_plex_poll = value.get("plex_poll", {})
                plex_poll: dict[str, list[str]] = {}
                if isinstance(raw_plex_poll, dict):
                    for name, sent_ids in raw_plex_poll.items():
                        if not isinstance(sent_ids, list):
                            continue
                        sent = sorted({str(chat_id) for chat_id in sent_ids if chat_id})
                        if sent:
                            plex_poll[str(name)] = sent
                # Skip entries that carry no useful state at all.
                if not status and not subscribers and not plex_done and not plex_poll:
                    continue
                entry: dict = {"status": status, "sent": sent, "failures": failures}
                if subscribers:
                    entry["subscribers"] = subscribers
                if plex_done:
                    entry["plex_done"] = True
                if plex_poll:
                    entry["plex_poll"] = plex_poll
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

            if self.task_meta_file:
                meta = self.load_task_meta()
                stale_meta = {k for k in meta if k not in active_ids}
                if stale_meta:
                    for k in stale_meta:
                        meta.pop(k)
                    self.save_task_meta(meta)

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

            if self.task_meta_file:
                meta = self.load_task_meta()
                meta_changed = False
                for task_id in task_id_set:
                    if task_id in meta:
                        meta.pop(task_id, None)
                        meta_changed = True
                if meta_changed:
                    self.save_task_meta(meta)

    def load_topic_subscriptions(self) -> dict[str, dict]:
        """topic_id → {chat_id, title, last_episode_end, total_episodes, added_at, ...}.

        Runtime subscriptions use explicit ``notify_policy`` and
        ``download_policy`` fields; missing/invalid policy values are handled by
        subscription_policy helpers at read time.
        """
        if not self.topic_subscriptions_file:
            return {}
        payload = self.load_json_file(self.topic_subscriptions_file, {})
        if not isinstance(payload, dict):
            return {}
        result: dict[str, dict] = {}
        for k, v in payload.items():
            if isinstance(v, dict):
                result[str(k)] = v
        return result

    def save_topic_subscriptions(self, subs: dict[str, dict]) -> None:
        if not self.topic_subscriptions_file:
            return
        self.save_json_file(self.topic_subscriptions_file, subs, "topic subscriptions")

    def load_pending_downloads(self) -> dict[str, dict]:
        """Load the deferred download queue.

        Returns ``{entry_id: entry_dict}`` or an empty dict on missing/invalid file.
        """
        if not self.pending_downloads_file:
            return {}
        payload = self.load_json_file(self.pending_downloads_file, {})
        if not isinstance(payload, dict):
            return {}
        out: dict[str, dict] = {}
        for entry_id, entry in payload.items():
            if isinstance(entry, dict) and entry_id:
                out[str(entry_id)] = entry
        return out

    def save_pending_downloads(self, entries: dict[str, dict]) -> None:
        if not self.pending_downloads_file:
            return
        # Stable order for diff-friendly JSON.
        ordered = {k: entries[k] for k in sorted(entries.keys())}
        self.save_json_file(self.pending_downloads_file, ordered, "pending downloads")

    def load_series_bulk_jobs(self) -> dict[str, dict]:
        """Load persistent series bulk plans.

        Shape: ``{job_id: job_dict}``. Invalid top-level payloads and non-dict
        entries are ignored so one malformed item doesn't break the whole file.
        """
        if not self.series_bulk_jobs_file:
            return {}
        payload = self.load_json_file(self.series_bulk_jobs_file, {})
        if not isinstance(payload, dict):
            return {}
        out: dict[str, dict] = {}
        for job_id, job in payload.items():
            if isinstance(job, dict) and job_id:
                out[str(job_id)] = job
        return out

    def save_series_bulk_jobs(self, jobs: dict[str, dict]) -> None:
        if not self.series_bulk_jobs_file:
            return
        ordered = {
            str(job_id): jobs[job_id]
            for job_id in sorted(jobs.keys())
            if isinstance(jobs[job_id], dict) and job_id
        }
        self.save_json_file(self.series_bulk_jobs_file, ordered, "series bulk jobs")

    def load_series_continue_totals(self) -> dict:
        if not self.series_continue_totals_file:
            return {}
        payload = self.load_json_file(self.series_continue_totals_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_series_continue_totals(self, payload: dict) -> None:
        if not self.series_continue_totals_file:
            return
        self.save_json_file(self.series_continue_totals_file, payload, "series continue totals")

    def load_series_continue_hidden(self) -> dict[str, list[str]]:
        if not self.series_continue_hidden_file:
            return {}
        payload = self.load_json_file(self.series_continue_hidden_file, {})
        if not isinstance(payload, dict):
            return {}
        hidden: dict[str, list[str]] = {}
        for chat_id, keys in payload.items():
            if not isinstance(keys, list):
                continue
            clean = [str(key) for key in keys if str(key or "").strip()]
            if clean:
                hidden[str(chat_id)] = sorted(set(clean))
        return hidden

    def save_series_continue_hidden(self, payload: dict[str, list[str]]) -> None:
        if not self.series_continue_hidden_file:
            return
        clean = {
            str(chat_id): sorted({str(key) for key in keys if str(key or "").strip()})
            for chat_id, keys in payload.items()
            if isinstance(keys, list)
        }
        self.save_json_file(self.series_continue_hidden_file, clean, "series continue hidden")

    def load_movie_discovery_cache(self) -> dict:
        if not self.movie_discovery_cache_file:
            return {}
        payload = self.load_json_file(self.movie_discovery_cache_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_movie_discovery_cache(self, cache: dict) -> None:
        if not self.movie_discovery_cache_file:
            return
        self.save_json_file(self.movie_discovery_cache_file, cache, "movie discovery cache")

    def load_movie_discovery_settings(self) -> dict:
        if not self.movie_discovery_settings_file:
            return {}
        payload = self.load_json_file(self.movie_discovery_settings_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_movie_discovery_settings(self, settings: dict) -> None:
        if not self.movie_discovery_settings_file:
            return
        self.save_json_file(self.movie_discovery_settings_file, settings, "movie discovery settings")

    def load_jackett_guard(self) -> dict:
        if not self.jackett_guard_file:
            return {}
        payload = self.load_json_file(self.jackett_guard_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_jackett_guard(self, payload: dict) -> None:
        if not self.jackett_guard_file:
            return
        self.save_json_file(self.jackett_guard_file, payload, "Jackett guard")

    # ---- Storage history (for /admin «📀 Хранилище» forecast) ----

    def load_storage_history(self) -> list[dict]:
        """Return the rolling list of disk-usage snapshots (oldest first).

        Each entry: {"ts": ISO8601 str, "used_bytes": int, "free_bytes": int}.
        Returns [] when the file doesn't exist yet or is malformed.
        """
        if not self.storage_history_file:
            return []
        payload = self.load_json_file(self.storage_history_file, [])
        if not isinstance(payload, list):
            return []
        # Filter to well-formed entries — defence against partial corruption.
        return [
            e for e in payload
            if isinstance(e, dict)
            and isinstance(e.get("ts"), str)
            and isinstance(e.get("used_bytes"), int)
        ]

    # ---- Voice search usage stats (for /admin → 🧭 Диагностика block) ----

    def load_voice_usage(self) -> dict:
        """Return the voice-search usage record, or an empty dict.

        Shape:
            {
              "month": "YYYY-MM",
              "request_count": int,
              "total_seconds": float,
              "estimated_cost_usd": float,
              "last_request": {"ts", "duration_sec", "text_preview", "outcome"},
              "last_error": {"ts", "type", "raw"} | None,
            }
        Counters reset on month rollover (handled by record_voice_usage).
        """
        if not self.voice_usage_file:
            return {}
        payload = self.load_json_file(self.voice_usage_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_voice_usage(self, payload: dict) -> None:
        if not self.voice_usage_file:
            return
        self.save_json_file(self.voice_usage_file, payload, "voice usage")

    def load_user_search_defaults(self, chat_id: int) -> dict | None:
        if not self.user_search_defaults_file:
            return None
        payload = self.load_json_file(self.user_search_defaults_file, {})
        if not isinstance(payload, dict):
            return None
        raw = payload.get(str(chat_id))
        if not isinstance(raw, dict):
            return None
        quality = str(raw.get("quality") or "1080p")
        if quality not in {"4K", "1080p", "720p", "any"}:
            quality = "1080p"
        voices_raw = raw.get("preferred_voices") or []
        if isinstance(voices_raw, str):
            voices = [voices_raw]
        elif isinstance(voices_raw, list):
            voices = [str(v) for v in voices_raw if v]
        else:
            voices = []
        return {
            "quality": quality,
            "audio": bool(raw.get("audio", False)),
            "subs": bool(raw.get("subs", False)),
            "preferred_voices": voices[:2],
            "updated_at": str(raw.get("updated_at") or ""),
        }

    def save_user_search_defaults(self, chat_id: int, defaults: dict) -> None:
        if not self.user_search_defaults_file:
            return
        payload = self.load_json_file(self.user_search_defaults_file, {})
        if not isinstance(payload, dict):
            payload = {}
        quality = str(defaults.get("quality") or "1080p")
        if quality not in {"4K", "1080p", "720p", "any"}:
            quality = "1080p"
        voices_raw = defaults.get("preferred_voices") or []
        voices = [str(v) for v in voices_raw if v] if isinstance(voices_raw, list) else []
        payload[str(chat_id)] = {
            "quality": quality,
            "audio": bool(defaults.get("audio", False)),
            "subs": bool(defaults.get("subs", False)),
            "preferred_voices": voices[:2],
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.save_json_file(self.user_search_defaults_file, payload, "user search defaults")

    def reset_user_search_defaults(self, chat_id: int) -> None:
        if not self.user_search_defaults_file:
            return
        payload = self.load_json_file(self.user_search_defaults_file, {})
        if not isinstance(payload, dict):
            return
        payload.pop(str(chat_id), None)
        self.save_json_file(self.user_search_defaults_file, payload, "user search defaults")

    # ---- GPT chat usage stats (per-feature: kp_confidence, did_you_mean, ...) ----

    def load_gpt_usage(self) -> dict:
        """Return GPT chat usage record, or empty dict.

        Shape mirrors voice_usage but adds per-feature breakdown:
            {
              "month": "YYYY-MM",
              "features": {
                "kp_confidence": {"calls": int, "input_tokens": int,
                                  "output_tokens": int, "estimated_cost_usd": float},
                "did_you_mean":  {...same...},
                "explain_card":  {...same...},   # PR2
                "quality_parse": {...same...},   # PR3
                "plex_unmatched":{...same...},   # PR4
              },
              "last_error": {"ts", "feature", "type"} | None,
            }
        Per-feature buckets reset monthly together with the top-level counter.
        """
        if not self.gpt_usage_file:
            return {}
        payload = self.load_json_file(self.gpt_usage_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_gpt_usage(self, payload: dict) -> None:
        if not self.gpt_usage_file:
            return
        self.save_json_file(self.gpt_usage_file, payload, "gpt usage")

    # ---- Download history (append-only memory for future "same as last time") ----

    def append_download_history(self, entry: dict) -> None:
        if not self.download_history_file or not isinstance(entry, dict):
            return
        self.append_jsonl_file(self.download_history_file, entry, "download history")

    def load_download_history(
        self,
        *,
        chat_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if not self.download_history_file:
            return []
        with self.lock:
            try:
                lines = self.download_history_file.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
            except OSError:
                logger.warning("Failed to load download history", exc_info=True)
                return []

        entries: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Malformed JSONL line in download history; skipping", exc_info=True)
                continue
            if not isinstance(entry, dict):
                continue
            if chat_id is not None:
                chat_ids = entry.get("chat_ids")
                if entry.get("chat_id") != chat_id and not (
                    isinstance(chat_ids, list) and chat_id in chat_ids
                ):
                    continue
            entries.append(entry)

        if limit is not None and limit > 0:
            return entries[-limit:]
        return entries

    def find_latest_download_history(
        self,
        chat_id: int,
        *,
        kind: str | None = None,
        title: str | None = None,
        series_query: str | None = None,
    ) -> dict | None:
        wanted_kind = (kind or "").strip().lower()
        wanted_title = (title or "").strip().casefold()
        wanted_series = (series_query or "").strip().casefold()

        for entry in reversed(self.load_download_history(chat_id=chat_id)):
            if wanted_kind and str(entry.get("kind") or "").lower() != wanted_kind:
                continue
            if wanted_series and str(entry.get("series_query") or "").strip().casefold() != wanted_series:
                continue
            if wanted_title:
                candidates = [
                    str(entry.get("title") or ""),
                    str(entry.get("canonical_title") or ""),
                ]
                if wanted_title not in {candidate.strip().casefold() for candidate in candidates}:
                    continue
            return entry

        return None

    # ---- Torrent-title parsed-metadata cache (PR3) ----

    def load_torrent_titles_cache(self) -> dict:
        """Return the GPT-parsed-torrent-titles cache.

        Shape: {title_hash_hex: parsed_meta_dict}. Torrent titles never
        change (they're file-naming snapshots), so cached parses are valid
        forever — no TTL needed. Capped via LRU eviction in the caller.
        """
        if not self.torrent_titles_cache_file:
            return {}
        payload = self.load_json_file(self.torrent_titles_cache_file, {})
        return payload if isinstance(payload, dict) else {}

    def save_torrent_titles_cache(self, payload: dict) -> None:
        if not self.torrent_titles_cache_file:
            return
        self.save_json_file(self.torrent_titles_cache_file, payload, "torrent titles cache")

    def append_storage_snapshot(self, snapshot: dict, max_age_days: int = 30) -> None:
        """Append a snapshot, prune entries older than `max_age_days`, save atomically.

        Pruning is by ISO timestamp — entries with `ts` lexicographically less
        than (now - max_age_days) get dropped. Lex compare works because we
        always write `isoformat(timespec='seconds')` which is sortable.
        """
        if not self.storage_history_file:
            return
        from datetime import datetime, timedelta, timezone
        history = self.load_storage_history()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        # Normalize cutoff to be lex-comparable with naive ISO strings.
        cutoff_naive = cutoff.split("+")[0]
        history = [e for e in history if e["ts"] >= cutoff_naive]
        history.append(snapshot)
        self.save_json_file(self.storage_history_file, history, "storage history")
