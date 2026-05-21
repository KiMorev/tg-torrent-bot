from dataclasses import dataclass

from config import AppSettings
from download_station import DownloadStationClient
from jackett import JackettClient
from kinopoisk import KinopoiskClient
from plex import PlexClient
from rutracker import RutrackerClient
from state_store import JsonStateStore


@dataclass(frozen=True)
class AppContext:
    settings: AppSettings
    ds_client: DownloadStationClient
    state_store: JsonStateStore
    rutracker_client: RutrackerClient | None
    jackett_client: JackettClient | None
    kinopoisk_client: KinopoiskClient | None
    plex_client: PlexClient | None


def build_app_context(settings: AppSettings) -> AppContext:
    ds_client = DownloadStationClient(
        settings.ds_url,
        settings.ds_account,
        settings.ds_password,
        destination=settings.ds_destination,
        verify_ssl=settings.ds_verify_ssl,
        retry_attempts=settings.ds_retry_attempts,
        retry_delay=settings.ds_retry_delay,
    )
    state_store = JsonStateStore(
        approved_chat_ids_file=settings.approved_chat_ids_file,
        tracker_processed_file=settings.trackers_processed_file,
        task_owners_file=settings.task_owners_file,
        notified_tasks_file=settings.notified_tasks_file,
        auto_delete_tasks_file=settings.auto_delete_tasks_file,
        movie_discovery_cache_file=settings.movie_discovery_cache_file,
        movie_discovery_settings_file=settings.movie_discovery_settings_file,
        topic_subscriptions_file=settings.topic_subscriptions_file,
        task_meta_file=settings.task_meta_file,
        pending_downloads_file=settings.pending_downloads_file,
        storage_history_file=settings.storage_history_file,
    )
    rutracker_client = (
        RutrackerClient(
            settings.rutracker_username,
            settings.rutracker_password,
            max_results=settings.rutracker_max_results,
        )
        if settings.rutracker_enabled
        else None
    )
    jackett_client = (
        JackettClient(
            settings.jackett_url,
            settings.jackett_api_key,
            max_results=settings.jackett_max_results,
            indexers=settings.jackett_indexers,
        )
        if settings.jackett_enabled
        else None
    )
    kinopoisk_client = (
        KinopoiskClient(settings.kinopoisk_api_key)
        if settings.kinopoisk_enabled
        else None
    )
    plex_client = (
        PlexClient(
            settings.plex_url,
            settings.plex_token,
            movie_section_id=settings.plex_movie_section or None,
        )
        if settings.plex_enabled
        else None
    )

    return AppContext(
        settings=settings,
        ds_client=ds_client,
        state_store=state_store,
        rutracker_client=rutracker_client,
        jackett_client=jackett_client,
        kinopoisk_client=kinopoisk_client,
        plex_client=plex_client,
    )
