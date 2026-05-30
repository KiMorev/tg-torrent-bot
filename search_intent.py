"""Parse natural-language search intent into existing bot search settings."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Any

from gpt_client import chat_completion
from series_bulk_planner import KNOWN_VOICE_LABELS, extract_voice_labels


INTENT_ONE_RELEASE = "one_release"
INTENT_SERIES_MASTER = "series_master"
INTENT_UNKNOWN = "unknown"

QUALITY_VALUES = {"4K", "1080p", "720p", "any"}
CONFIDENCE_VALUES = {"high", "medium", "low"}


@dataclass(frozen=True)
class SearchIntentDraft:
    base_query: str
    intent: str = INTENT_UNKNOWN
    quality: str | None = None
    audio_original: bool | None = None
    subs: bool | None = None
    season: int | None = None
    whole_series: bool = False
    partial_policy_hint: str | None = None
    tracker_hint: str | None = None
    voice_hints: tuple[str, ...] = ()
    voice_required: bool = False
    confidence: str = "medium"
    conflicts: tuple[str, ...] = ()

    @property
    def has_explicit_settings(self) -> bool:
        return any((
            self.quality is not None,
            self.audio_original is not None,
            self.subs is not None,
            self.season is not None,
            bool(self.voice_hints),
            self.intent == INTENT_SERIES_MASTER,
            self.partial_policy_hint is not None,
        ))


_QUALITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("4K", r"(?<!\w)(?:4\s*[кk]|2160\s*[pр]?|uhd|ультра\s*hd|ultra\s*hd)(?!\w)"),
    ("1080p", r"(?<!\w)(?:1080\s*[pр]?|full\s*hd|фулл?\s*hd)(?!\w)"),
    ("720p", r"(?<!\w)720\s*[pр]?(?!\w)"),
    ("any", r"(?<!\w)(?:любое\s+качество|без\s+качества|неважно\s+качество)(?!\w)"),
)
_SEASON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\w)s0?(\d{1,2})(?:e\d{1,3})?(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)(\d{1,2})\s*(?:й|ый|ой)?\s+сезон(?:а)?(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)сезон(?:\s*:)?\s*0?(\d{1,2})(?!\w)", re.IGNORECASE),
)
_WHOLE_SERIES_RE = re.compile(
    r"(?:"
    r"все\s+сезоны|весь\s+сериал|сериал\s+целиком|целиком|полностью|"
    r"all\s+seasons|whole\s+series"
    r")",
    re.IGNORECASE,
)
_SUBS_POSITIVE_RE = re.compile(
    r"(?<!\w)(?:с\s+субтитрами|субтитры|сабы|subs?|subtitles)(?!\w)",
    re.IGNORECASE,
)
_SUBS_NEGATIVE_RE = re.compile(
    r"(?<!\w)(?:без\s+субтитров|без\s+сабов|без\s+subs?|no\s+subs?)(?!\w)",
    re.IGNORECASE,
)
_AUDIO_ORIGINAL_POSITIVE_RE = re.compile(
    r"(?<!\w)(?:в\s+оригинале|оригинал(?:ьная)?\s+дорожка|original|orig)(?!\w)",
    re.IGNORECASE,
)
_AUDIO_ORIGINAL_NEGATIVE_RE = re.compile(
    r"(?<!\w)(?:без\s+оригинала|не\s+оригинал|дубляж|русская\s+озвучка|на\s+русском)(?!\w)",
    re.IGNORECASE,
)
_VOICE_REQUIRED_PREFIX_RE = re.compile(
    r"(?<!\w)(?:в|с)\s+озвучк\w*\s+|(?<!\w)перевод\s+|(?<!\w)только\s+",
    re.IGNORECASE,
)
_WITHOUT_PREFIX_RE = re.compile(r"(?<!\w)(?:без|кроме)\s+", re.IGNORECASE)
_COMMAND_WORDS_RE = re.compile(
    r"(?<!\w)(?:найди|найти|ищи|поиск|скачай|скачать|покажи|хочу|нужен|нужна)(?!\w)",
    re.IGNORECASE,
)
_MEDIA_WORDS_RE = re.compile(
    r"(?<!\w)(?:фильм|фильма|кино|мультфильм|мультик|сериал|сериала)(?!\w)",
    re.IGNORECASE,
)
_SPACES_RE = re.compile(r"\s+")


def _clean_query(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text)
    text = _COMMAND_WORDS_RE.sub(" ", text)
    text = _MEDIA_WORDS_RE.sub(" ", text)
    text = re.sub(r"\s+[-–—:;,]\s+", " ", text)
    return _SPACES_RE.sub(" ", text).strip(" -–—:;,")


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    pieces: list[str] = []
    last = 0
    for start, end in sorted(spans):
        if start < last:
            continue
        pieces.append(text[last:start])
        pieces.append(" ")
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def _normalise_quality_hits(text: str) -> tuple[str | None, tuple[str, ...], list[tuple[int, int]]]:
    hits: list[str] = []
    spans: list[tuple[int, int]] = []
    for quality, pattern in _QUALITY_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            hits.append(quality)
            spans.append(match.span())
    unique = tuple(dict.fromkeys(hits))
    return (unique[0] if len(unique) == 1 else None, unique, spans)


def _find_season(text: str) -> tuple[int | None, list[tuple[int, int]], bool]:
    seasons: list[int] = []
    spans: list[tuple[int, int]] = []
    for pattern in _SEASON_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 99:
                seasons.append(value)
                spans.append(match.span())
    unique = tuple(dict.fromkeys(seasons))
    return (unique[0] if len(unique) == 1 else None, spans, len(unique) > 1)


def _voice_spans(text: str, voices: tuple[str, ...]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    lowered = text.lower()
    for voice in voices:
        voice_lower = voice.lower()
        start = lowered.find(voice_lower)
        if start >= 0:
            spans.append((start, start + len(voice)))
    return spans


def _is_voice_negative(text: str, voice: str) -> bool:
    lowered = text.lower()
    voice_lower = voice.lower()
    pos = lowered.find(voice_lower)
    if pos < 0:
        return False
    prefix = lowered[max(0, pos - 20):pos]
    return bool(_WITHOUT_PREFIX_RE.search(prefix))


def _voice_has_explicit_context(text: str, voice: str) -> bool:
    lowered = text.lower()
    voice_lower = voice.lower()
    pos = lowered.find(voice_lower)
    if pos < 0:
        return False
    prefix = lowered[max(0, pos - 28):pos]
    if _VOICE_REQUIRED_PREFIX_RE.search(prefix) or _WITHOUT_PREFIX_RE.search(prefix):
        return True
    # Bare "Клиника LostFilm" is a common compact way to ask for a studio.
    # Treat it as voice only when there is a real title before the studio.
    # "LostFilm документальный фильм" stays a title query.
    full_prefix = text[:pos]
    full_prefix = _COMMAND_WORDS_RE.sub(" ", full_prefix)
    full_prefix = _MEDIA_WORDS_RE.sub(" ", full_prefix)
    return bool(re.search(r"[A-Za-zА-Яа-я0-9]", full_prefix))


def parse_search_intent(text: str) -> SearchIntentDraft:
    raw = (text or "").strip()
    if not raw:
        return SearchIntentDraft(base_query="", confidence="low", conflicts=("empty",))

    spans_to_remove: list[tuple[int, int]] = []
    conflicts: list[str] = []

    quality, quality_hits, quality_spans = _normalise_quality_hits(raw)
    spans_to_remove.extend(quality_spans)
    if len(quality_hits) > 1:
        conflicts.append("quality")

    season, season_spans, season_conflict = _find_season(raw)
    spans_to_remove.extend(season_spans)
    if season_conflict:
        conflicts.append("season")

    whole_match = _WHOLE_SERIES_RE.search(raw)
    whole_series = bool(whole_match)
    if whole_match:
        spans_to_remove.append(whole_match.span())
    if whole_series and season is not None:
        conflicts.append("series_scope")

    subs_positive = bool(_SUBS_POSITIVE_RE.search(raw))
    subs_negative = bool(_SUBS_NEGATIVE_RE.search(raw))
    subs: bool | None = None
    if subs_positive and subs_negative:
        conflicts.append("subs")
    elif subs_positive:
        subs = True
    elif subs_negative:
        subs = False
    spans_to_remove.extend(match.span() for match in _SUBS_POSITIVE_RE.finditer(raw))
    spans_to_remove.extend(match.span() for match in _SUBS_NEGATIVE_RE.finditer(raw))

    audio_positive = bool(_AUDIO_ORIGINAL_POSITIVE_RE.search(raw))
    audio_negative = bool(_AUDIO_ORIGINAL_NEGATIVE_RE.search(raw))
    audio_original: bool | None = None
    if audio_positive and audio_negative:
        conflicts.append("audio")
    elif audio_positive:
        audio_original = True
    elif audio_negative:
        audio_original = False
    spans_to_remove.extend(match.span() for match in _AUDIO_ORIGINAL_POSITIVE_RE.finditer(raw))
    spans_to_remove.extend(match.span() for match in _AUDIO_ORIGINAL_NEGATIVE_RE.finditer(raw))

    detected_voices = extract_voice_labels(raw)
    # Avoid corrupting title queries where a known studio label is part of the
    # movie/series name or an unrelated phrase. We only treat it as an explicit
    # voice hint when the user wrote a nearby cue like "в озвучке", "перевод",
    # "только" or "без". Plain "LostFilm ..." stays in the title.
    all_voices = tuple(v for v in detected_voices if _voice_has_explicit_context(raw, v))
    voices = tuple(v for v in all_voices if not _is_voice_negative(raw, v))
    spans_to_remove.extend(_voice_spans(raw, all_voices))
    if all_voices:
        spans_to_remove.extend(match.span() for match in _VOICE_REQUIRED_PREFIX_RE.finditer(raw))
        spans_to_remove.extend(match.span() for match in _WITHOUT_PREFIX_RE.finditer(raw))
    voice_required = bool(voices and _VOICE_REQUIRED_PREFIX_RE.search(raw))

    partial_policy_hint = None
    lower = raw.lower()
    if "след" in lower and ("доступ" in lower or "нов" in lower):
        partial_policy_hint = "download_each_update"
    elif "когда сезон" in lower and ("заверш" in lower or "полностью" in lower):
        partial_policy_hint = "download_when_complete"

    stripped = _remove_spans(raw, spans_to_remove)
    base_query = _clean_query(stripped)

    intent = INTENT_SERIES_MASTER if whole_series else INTENT_ONE_RELEASE if season is not None else INTENT_UNKNOWN
    confidence = "low" if conflicts else "medium"
    if base_query and not conflicts and (
        quality is not None
        or season is not None
        or whole_series
        or audio_original is not None
        or subs is not None
        or voices
        or partial_policy_hint
    ):
        confidence = "high"
    if not base_query:
        confidence = "low"
        conflicts.append("empty")

    return SearchIntentDraft(
        base_query=base_query,
        intent=intent,
        quality=quality,
        audio_original=audio_original,
        subs=subs,
        season=season,
        whole_series=whole_series,
        partial_policy_hint=partial_policy_hint,
        voice_hints=voices,
        voice_required=voice_required,
        confidence=confidence,
        conflicts=tuple(dict.fromkeys(conflicts)),
    )


def _validate_voice_hints(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw_values = [raw]
    elif isinstance(raw, list):
        raw_values = raw
    else:
        return ()
    allowed = {label.lower(): label for label in KNOWN_VOICE_LABELS}
    result: list[str] = []
    for value in raw_values:
        label = allowed.get(str(value).strip().lower())
        if label and label not in result:
            result.append(label)
    return tuple(result[:2])


def validate_gpt_intent_payload(payload: dict[str, Any], fallback: SearchIntentDraft) -> SearchIntentDraft:
    base_query = str(payload.get("base_query") or fallback.base_query).strip()
    intent = str(payload.get("intent") or fallback.intent or INTENT_UNKNOWN)
    if intent not in {INTENT_ONE_RELEASE, INTENT_SERIES_MASTER, INTENT_UNKNOWN}:
        intent = fallback.intent

    quality_raw = payload.get("quality")
    quality = str(quality_raw).strip() if quality_raw is not None else fallback.quality
    if quality not in QUALITY_VALUES:
        quality = fallback.quality

    def bool_or_none(key: str, default: bool | None) -> bool | None:
        value = payload.get(key)
        return value if isinstance(value, bool) else default

    season = fallback.season
    try:
        season_raw = payload.get("season")
        if season_raw is not None:
            season_int = int(season_raw)
            season = season_int if 1 <= season_int <= 99 else fallback.season
    except (TypeError, ValueError):
        season = fallback.season

    confidence = str(payload.get("confidence") or fallback.confidence)
    if confidence not in CONFIDENCE_VALUES:
        confidence = fallback.confidence

    voices = _validate_voice_hints(payload.get("voice_hints")) or fallback.voice_hints
    voice_required = bool_or_none("voice_required", fallback.voice_required) or False
    if voice_required and not voices:
        voice_required = False

    partial_policy = payload.get("partial_policy_hint") or fallback.partial_policy_hint
    if partial_policy not in {None, "download_each_update", "download_when_complete", "notify_each_update"}:
        partial_policy = fallback.partial_policy_hint

    if not base_query:
        return replace(fallback, confidence="low")

    return SearchIntentDraft(
        base_query=base_query,
        intent=intent,
        quality=quality,
        audio_original=bool_or_none("audio_original", fallback.audio_original),
        subs=bool_or_none("subs", fallback.subs),
        season=season,
        whole_series=bool_or_none("whole_series", fallback.whole_series) or intent == INTENT_SERIES_MASTER,
        partial_policy_hint=partial_policy,
        tracker_hint=None,
        voice_hints=voices,
        voice_required=voice_required,
        confidence=confidence,
        conflicts=fallback.conflicts,
    )


def parse_search_intent_with_gpt(
    text: str,
    fallback: SearchIntentDraft,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    usage_sink: list | None = None,
) -> tuple[SearchIntentDraft | None, str | None]:
    if not text.strip():
        return (None, "empty")

    known_voices = ", ".join(KNOWN_VOICE_LABELS)
    system_prompt = (
        "Ты разбираешь поисковый запрос для русскоязычного Telegram-бота, "
        "который ищет фильмы и сериалы на торрент-трекерах. Ответь строгим JSON "
        "без пояснений. Не придумывай названия: base_query должен быть только "
        "очищенным названием из пользовательского текста.\n\n"
        "Схема: {"
        '"base_query": string, '
        '"intent": "one_release"|"series_master"|"unknown", '
        '"quality": "4K"|"1080p"|"720p"|"any"|null, '
        '"audio_original": boolean|null, '
        '"subs": boolean|null, '
        '"season": integer|null, '
        '"whole_series": boolean, '
        '"partial_policy_hint": "download_each_update"|"download_when_complete"|"notify_each_update"|null, '
        '"voice_hints": [known voice labels], '
        '"voice_required": boolean, '
        '"confidence": "high"|"medium"|"low"'
        "}.\n\n"
    "Качество 2160p/4к/4k/UHD нормализуй в 4K. Учитывай русскую букву р "
    "в 1080р/720р/2160р. Фраза «сериал Клиника» не означает весь сериал; "
    "series_master ставь только для «все сезоны», «весь сериал», «целиком». "
    "Если пользователь явно указал озвучку через «в озвучке», «перевод», "
    "«только», voice_required=true. Если известная студия просто входит в "
    "название без такого маркера, не вырезай её из base_query и не ставь voice_hints. "
    f"Известные озвучки: {known_voices}."
    )
    result, error = chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:500]},
        ],
        api_key=api_key,
        model=model,
        max_tokens=220,
        temperature=0.0,
        timeout=10,
        response_format={"type": "json_object"},
    )
    if usage_sink is not None and result is not None:
        usage_sink.append({
            "input_tokens": int(result.get("input_tokens") or 0),
            "output_tokens": int(result.get("output_tokens") or 0),
            "model": str(result.get("model") or model),
        })
    if error or not result:
        return (None, error)
    try:
        payload = json.loads(result["text"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return (None, "parse")
    if not isinstance(payload, dict):
        return (None, "parse")
    return (validate_gpt_intent_payload(payload, fallback), None)
