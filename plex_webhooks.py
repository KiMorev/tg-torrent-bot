"""Small aiohttp server for Plex webhook callbacks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("tg_torrent_drop")

WebhookTrigger = Callable[[dict], Awaitable[None]]


@dataclass
class PlexWebhookState:
    enabled: bool = False
    listening: bool = False
    host: str = ""
    port: int = 0
    last_received_at: str = ""
    last_accepted_at: str = ""
    last_event: str = ""
    invalid_token_count: int = 0
    trigger_count: int = 0
    debounced_count: int = 0
    last_error: str = ""
    _last_trigger_monotonic: float = field(default=0.0, repr=False)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "listening": self.listening,
            "host": self.host,
            "port": self.port,
            "last_received_at": self.last_received_at,
            "last_accepted_at": self.last_accepted_at,
            "last_event": self.last_event,
            "invalid_token_count": self.invalid_token_count,
            "trigger_count": self.trigger_count,
            "debounced_count": self.debounced_count,
            "last_error": self.last_error,
        }


class PlexWebhookServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        debounce_seconds: float,
        trigger: WebhookTrigger,
        state: PlexWebhookState | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.debounce_seconds = max(0.0, debounce_seconds)
        self.trigger = trigger
        self.state = state or PlexWebhookState()
        self.state.enabled = True
        self.state.host = host
        self.state.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if not self.token:
            self.state.last_error = "PLEX_WEBHOOK_TOKEN is empty"
            logger.error("Plex webhook server disabled: PLEX_WEBHOOK_TOKEN is empty")
            return

        app = web.Application()
        app.router.add_post("/plex/webhook", self.handle_webhook)
        app.router.add_get("/plex/webhook/health", self.handle_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self.state.listening = True
        self.state.last_error = ""
        logger.info("Plex webhook server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        self.state.listening = False
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._valid_token(request):
            self.state.invalid_token_count += 1
            return web.json_response({"ok": False}, status=401)
        return web.json_response({"ok": True})

    async def handle_webhook(self, request: web.Request) -> web.Response:
        self.state.last_received_at = _utc_now()
        if not self._valid_token(request):
            self.state.invalid_token_count += 1
            return web.json_response({"ok": False}, status=401)

        try:
            payload = await parse_plex_payload(request)
        except Exception as exc:
            self.state.last_error = f"payload parse failed: {exc}"
            logger.warning("Plex webhook payload parse failed", exc_info=True)
            return web.json_response({"ok": False}, status=400)

        event = str(payload.get("event") or "")
        metadata = payload.get("Metadata") if isinstance(payload.get("Metadata"), dict) else {}
        self.state.last_event = event
        self.state.last_accepted_at = _utc_now()

        now = time.monotonic()
        if (
            self.debounce_seconds > 0
            and self.state._last_trigger_monotonic
            and now - self.state._last_trigger_monotonic < self.debounce_seconds
        ):
            self.state.debounced_count += 1
            logger.info("Plex webhook debounced event=%s", event or "-")
            return web.json_response({"ok": True, "debounced": True})

        self.state._last_trigger_monotonic = now
        self.state.trigger_count += 1
        asyncio.create_task(self.trigger({
            "event": event,
            "type": metadata.get("type") or "",
            "title": metadata.get("title") or metadata.get("grandparentTitle") or "",
            "rating_key": metadata.get("ratingKey") or "",
        }))
        logger.info(
            "Plex webhook accepted event=%s type=%s title=%r",
            event or "-",
            metadata.get("type") or "-",
            metadata.get("title") or metadata.get("grandparentTitle") or "",
        )
        return web.json_response({"ok": True})

    def _valid_token(self, request: web.Request) -> bool:
        query_token = request.query.get("token", "")
        header_token = request.headers.get("X-PlexLoader-Token", "")
        return bool(self.token) and self.token in {query_token, header_token}


async def parse_plex_payload(request: web.Request) -> dict:
    content_type = (request.content_type or "").lower()
    if content_type == "application/json":
        data = await request.json()
        return data if isinstance(data, dict) else {}

    if content_type == "multipart/form-data":
        reader = await request.multipart()
        async for part in reader:
            if part.name == "payload":
                raw = await part.text()
                return _loads_payload(raw)
            await part.release()
        return {}

    post = await request.post()
    raw = post.get("payload")
    return _loads_payload(str(raw or ""))


def _loads_payload(raw: str) -> dict:
    if not raw:
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
