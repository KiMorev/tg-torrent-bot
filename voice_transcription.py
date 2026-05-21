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
MODELS_API_URL = "https://api.openai.com/v1/models"

# Per-minute price in USD. As of 2026 OpenAI charges $0.006/min for Whisper-1.
# If pricing changes, update here — used for the /admin usage estimate.
WHISPER_PRICE_USD_PER_MIN = 0.006


def estimate_cost_usd(duration_sec: float) -> float:
    """Estimate USD cost of a Whisper API call given audio duration in seconds.

    Whisper bills per-minute rounded up; we approximate with linear interpolation
    which is close enough for /admin display purposes.
    """
    return max(0.0, duration_sec) / 60.0 * WHISPER_PRICE_USD_PER_MIN


def _classify_error(status_code: int | None, body: str = "") -> str:
    """Map OpenAI HTTP response to a stable error label for state storage."""
    if status_code is None:
        return "network"
    if status_code == 401:
        return "auth"
    if status_code == 429:
        # 429 can be either rate-limit (transient) or insufficient-quota (terminal).
        # The body discriminator is the `error.type` field per OpenAI docs.
        if "insufficient_quota" in body or "quota" in body.lower():
            return "quota_exceeded"
        return "rate_limit"
    if status_code == 400:
        return "bad_request"
    if 500 <= status_code < 600:
        return "server_error"
    return f"http_{status_code}"


def transcribe_audio_detailed(
    audio_path: Path, api_key: str, timeout: int = 60,
) -> tuple[str | None, str | None]:
    """Transcribe audio via OpenAI Whisper, returning (text, error_label).

    Exactly one of the two is non-None:
      - (text, None) on success — `text` is the stripped transcription.
      - (None, error_label) on failure. Error labels are stable strings used
        for /admin state ('no_key', 'no_file', 'timeout', 'network',
        'auth', 'quota_exceeded', 'rate_limit', 'server_error',
        'bad_request', 'http_<code>', 'empty', 'parse').

    Caller is responsible for showing the user a friendly message; the labels
    are aimed at the operator (logs, admin diagnostics).
    """
    if not api_key:
        return (None, "no_key")
    if not audio_path or not audio_path.exists():
        return (None, "no_file")
    try:
        with audio_path.open("rb") as f:
            response = requests.post(
                WHISPER_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, f, "audio/ogg")},
                data={"model": WHISPER_MODEL},
                timeout=timeout,
            )
    except requests.exceptions.Timeout:
        logger.warning("Whisper API timeout after %ss", timeout)
        return (None, "timeout")
    except requests.exceptions.RequestException as exc:
        logger.warning("Whisper API request failed: %s", exc)
        return (None, "network")

    if response.status_code != 200:
        label = _classify_error(response.status_code, response.text or "")
        logger.warning(
            "Whisper API returned HTTP %s (%s): %s",
            response.status_code, label, (response.text or "")[:200],
        )
        return (None, label)

    try:
        text = (response.json().get("text") or "").strip()
    except ValueError:
        logger.warning("Whisper API returned non-JSON response")
        return (None, "parse")

    if not text:
        return (None, "empty")
    return (text, None)


def transcribe_audio(audio_path: Path, api_key: str, timeout: int = 60) -> str | None:
    """Thin wrapper over transcribe_audio_detailed for callers that only need text."""
    text, _err = transcribe_audio_detailed(audio_path, api_key, timeout)
    return text


def check_api_key(api_key: str, timeout: int = 5) -> tuple[bool, str | None]:
    """Verify an OpenAI API key by GET'ing /v1/models — free call, returns 401
    on invalid key.

    Returns (is_valid, error_label). is_valid is True only when HTTP 200.
    error_label uses the same vocabulary as _classify_error so callers can
    surface a stable status in /admin diagnostics.
    """
    if not api_key:
        return (False, "no_key")
    try:
        response = requests.get(
            MODELS_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return (False, "timeout")
    except requests.exceptions.RequestException:
        return (False, "network")

    if response.status_code == 200:
        return (True, None)
    return (False, _classify_error(response.status_code, response.text or ""))
