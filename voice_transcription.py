"""OpenAI Whisper API wrapper for voice search.

Telegram voice messages are OGG/Opus. We download them via Bot API, then
POST as `multipart/form-data` to the Whisper API. The API auto-detects
language (good for mixed Russian/English movie titles).

The feature is opt-in via OPENAI_API_KEY. When the key is empty or the
call fails, callers get None and degrade gracefully (no transcription —
voice messages are ignored as before).

Pricing: $0.006/min as of 2026. A typical 5-15s voice message costs
$0.0005–0.0015. Zero usage = zero charges (pay-per-use, no subscription).
Set a hard monthly limit at https://platform.openai.com/settings/limits
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

logger = logging.getLogger("tg_torrent_drop")

WHISPER_API_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"


def transcribe_audio(audio_path: Path, api_key: str, timeout: int = 60) -> str | None:
    """Transcribe an audio file via OpenAI Whisper API.

    Returns the transcribed text (stripped), or None on:
      - empty api_key (feature disabled)
      - HTTP error from OpenAI
      - empty transcription (Whisper returned nothing usable)
      - any exception (network, file IO, parsing)
    Callers must handle None as "transcription failed" and show a friendly
    message to the user.
    """
    if not api_key or not audio_path or not audio_path.exists():
        return None
    try:
        with audio_path.open("rb") as f:
            response = requests.post(
                WHISPER_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, f, "audio/ogg")},
                data={"model": WHISPER_MODEL},
                timeout=timeout,
            )
    except requests.exceptions.RequestException as exc:
        logger.warning("Whisper API request failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "Whisper API returned HTTP %s: %s",
            response.status_code, (response.text or "")[:200],
        )
        return None

    try:
        text = (response.json().get("text") or "").strip()
    except ValueError:
        logger.warning("Whisper API returned non-JSON response")
        return None

    return text or None
