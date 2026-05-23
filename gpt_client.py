"""OpenAI Chat Completions API wrapper for precision-improvement features.

Used by movie_discovery (KP confidence check) and search (did-you-mean
suggestions on 0 results). Separate from voice_transcription.py because
the two use different OpenAI endpoints with different cost models:
voice → /v1/audio/transcriptions (per-minute), chat → /v1/chat/completions
(per-token).

Same OPENAI_API_KEY is reused for both. Cost tracking is also separated
into gpt_usage.json (vs voice_usage.json) so the operator can see at a
glance how much each feature spends.

Default model: gpt-4o-mini ($0.150/1M input, $0.600/1M output as of 2026).
A typical KP confidence check: ~200 input + 30 output ≈ $0.00005 per call.
A did-you-mean suggestion: ~50 input + 80 output ≈ $0.00005 per call.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger("tg_torrent_drop")

CHAT_API_URL = "https://api.openai.com/v1/chat/completions"

# Per-1M-tokens pricing per model (input, output). OpenAI doesn't expose
# pricing programmatically — keep this table updated when adding models.
# Prefix-match supported: any model id starting with one of these keys uses
# its pricing. Most-specific (longest) match wins.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.150, 0.600),
    "gpt-4o":      (2.500, 10.000),
}

# Back-compat for callers that imported the module-level constants directly.
_PRICE_INPUT_PER_1M = MODEL_PRICING["gpt-4o-mini"][0]
_PRICE_OUTPUT_PER_1M = MODEL_PRICING["gpt-4o-mini"][1]


def _lookup_pricing(model: str) -> tuple[float, float] | None:
    """Return (input_per_1M, output_per_1M) for ``model`` or None if unknown.

    Longest matching prefix wins so "gpt-4o-mini-2024-07-18" maps to the
    gpt-4o-mini row, not gpt-4o.
    """
    best_key: str | None = None
    for key in MODEL_PRICING:
        if model.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return MODEL_PRICING[best_key] if best_key else None


def estimate_chat_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model: str = "gpt-4o-mini",
) -> float | None:
    """Estimate USD cost of a chat completion given API-reported token counts.

    Returns ``None`` if ``model`` is not in MODEL_PRICING — callers should
    still record token counts but report cost as «unknown for model X».
    """
    pricing = _lookup_pricing(model)
    if pricing is None:
        return None
    in_per_1m, out_per_1m = pricing
    return (
        (max(0, input_tokens) / 1_000_000) * in_per_1m
        + (max(0, output_tokens) / 1_000_000) * out_per_1m
    )


def _classify_error(status_code: int | None, body: str = "") -> str:
    if status_code is None:
        return "network"
    if status_code == 401:
        return "auth"
    if status_code == 429:
        if "insufficient_quota" in body or "quota" in body.lower():
            return "quota_exceeded"
        return "rate_limit"
    if status_code == 400:
        return "bad_request"
    if 500 <= status_code < 600:
        return "server_error"
    return f"http_{status_code}"


def chat_completion(
    messages: list[dict],
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    max_tokens: int = 500,
    temperature: float = 0.0,
    timeout: int = 30,
    response_format: dict | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call OpenAI Chat Completions, returning (result_dict, error_label).

    Exactly one is non-None. ``result_dict`` shape:
        {
          "text": str,         # response.choices[0].message.content
          "input_tokens": int, # usage.prompt_tokens
          "output_tokens": int,# usage.completion_tokens
          "model": str,        # echoed from request
        }

    Error labels match those in voice_transcription.check_api_key: 'no_key',
    'timeout', 'network', 'auth', 'quota_exceeded', 'rate_limit',
    'bad_request', 'server_error', 'http_<code>', 'parse', 'empty'.

    Set ``response_format={"type": "json_object"}`` when expecting strict JSON
    from the model — OpenAI then guarantees valid JSON output.
    """
    if not api_key:
        return (None, "no_key")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    try:
        response = requests.post(
            CHAT_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        logger.warning("GPT chat timeout after %ss model=%s", timeout, model)
        return (None, "timeout")
    except requests.exceptions.RequestException as exc:
        logger.warning("GPT chat request failed: %s", exc)
        return (None, "network")

    if response.status_code != 200:
        label = _classify_error(response.status_code, response.text or "")
        logger.warning(
            "GPT chat HTTP %s (%s): %s",
            response.status_code, label, (response.text or "")[:200],
        )
        return (None, label)

    try:
        data = response.json()
    except ValueError:
        return (None, "parse")

    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
        usage = data.get("usage", {})
        result = {
            "text": text,
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "model": str(data.get("model") or model),
        }
    except (KeyError, IndexError, TypeError, ValueError):
        return (None, "parse")

    if not text:
        return (None, "empty")
    return (result, None)
