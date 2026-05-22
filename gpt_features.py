"""High-level GPT-powered precision improvements.

Each function wraps `gpt_client.chat_completion` with a tailored prompt and
returns a clean Python value (not raw JSON). Failures degrade gracefully —
callers always handle None as "GPT couldn't help, fall back to non-GPT path".

Functions here are the ONLY entry points the rest of the bot should use
for GPT-driven decisions. Keeping prompts in one place makes it trivial
to A/B-test wording or swap models.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gpt_client import chat_completion

logger = logging.getLogger("tg_torrent_drop")


# Confidence threshold below which kp_confidence_check rejects a match.
# Tuned conservatively: 0.7 means GPT must be reasonably sure. Lower → more
# false-positive matches; higher → more cards left without KP enrichment.
KP_CONFIDENCE_THRESHOLD = 0.7


def kp_confidence_check(
    *,
    query: str,
    candidates: list[dict],
    api_key: str,
    model: str = "gpt-4o-mini",
) -> tuple[int | None, float, str | None]:
    """Ask GPT to pick the best Kinopoisk match for a torrent title.

    ``candidates`` is a list of dicts with keys: title_ru, title_en, year,
    rating, genres (list of str). The first matching item from the bot's
    existing KP search.

    Returns ``(best_index, confidence, error_label)``:
      - best_index: index into candidates, or None if no candidate is good enough
      - confidence: 0.0-1.0, GPT's estimated probability of correctness
      - error_label: None on success, else string from gpt_client.chat_completion

    Below KP_CONFIDENCE_THRESHOLD the function returns (None, conf, None) —
    the candidates exist but GPT thinks none fits. Caller should treat as
    "no KP match" rather than silently using candidates[0].
    """
    if not candidates:
        return (None, 0.0, "empty")

    # Single candidate — still ask GPT; if it disagrees we want to drop it.
    candidates_str = "\n".join(
        f"{i + 1}. {c.get('title_ru', '?')} / {c.get('title_en', '?')} "
        f"({c.get('year', '?')}) · КП {c.get('rating') or '—'} · "
        f"{', '.join(c.get('genres') or []) or '—'}"
        for i, c in enumerate(candidates)
    )

    system_prompt = (
        "You match torrent titles to Kinopoisk entries. Reply with strict JSON: "
        '{"pick": <1-based index or 0 if none fit>, "confidence": <0.0-1.0>, '
        '"reason": "<short Russian explanation>"}. '
        "Use 0 (and confidence ≤ 0.5) when none of the candidates is clearly the same film."
    )
    user_prompt = (
        f"Torrent query: «{query}»\n"
        f"Kinopoisk candidates:\n{candidates_str}\n\n"
        "Which candidate (if any) matches the query?"
    )

    result, error = chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key,
        model=model,
        max_tokens=150,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    if error or not result:
        return (None, 0.0, error)

    try:
        data = json.loads(result["text"])
        pick_raw = int(data.get("pick", 0))
        confidence = float(data.get("confidence", 0.0))
        reason = str(data.get("reason", ""))
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return (None, 0.0, "parse")

    pick_idx = pick_raw - 1 if 1 <= pick_raw <= len(candidates) else None
    if pick_idx is None or confidence < KP_CONFIDENCE_THRESHOLD:
        logger.info(
            "GPT KP-confidence rejected: query=%r pick=%s conf=%.2f reason=%r",
            query, pick_raw, confidence, reason,
        )
        return (None, confidence, None)

    logger.info(
        "GPT KP-confidence accepted: query=%r pick=%s conf=%.2f title=%s",
        query, pick_raw, confidence, candidates[pick_idx].get("title_ru"),
    )
    return (pick_idx, confidence, None)


def explain_movie_card(
    *,
    title: str,
    year: int | None,
    rating: float | None,
    genres: list[str],
    synopsis: str = "",
    api_key: str,
    model: str = "gpt-4o-mini",
) -> tuple[str | None, str | None]:
    """Generate a 1-sentence Russian «why this film» explanation for /new.

    Designed to help the user decide if a card is worth opening — gives
    a hook that's more specific than «КП 8.4 sci-fi».

    Returns ``(explanation, error_label)``:
      - explanation: short Russian sentence ≤120 chars, or None on failure
      - error_label: None on success, else gpt_client error vocabulary

    Caller is responsible for caching the result — explanations don't
    change (films are static).
    """
    if not title:
        return (None, "empty")

    genres_str = ", ".join(genres) if genres else "—"
    year_str = str(year) if year else "—"
    rating_str = f"{rating:.1f}" if rating else "—"

    # Synopsis is the biggest signal — when present, GPT has real material.
    # Without it, the model falls back to general knowledge which may be
    # weak or hallucinated for niche titles.
    synopsis_block = (
        f"Синопсис: {synopsis[:600]}"
        if synopsis else
        "Синопсис: (нет — опирайся только на жанр и название)"
    )

    system_prompt = (
        "Ты помогаешь пользователю быстро решить: смотреть фильм или нет. "
        "На основе предоставленных метаданных напиши **одно** короткое "
        "предложение (макс. 120 символов) на русском — что цепляет в этом "
        "фильме, кому он зайдёт, что отличает его от похожих.\n"
        "\n"
        "ПРАВИЛА:\n"
        "  - Не повторяй название и год — они уже видны юзеру.\n"
        "  - Не пиши «культовый шедевр», «обязательно к просмотру» — штампы.\n"
        "  - Не упоминай рейтинг КП — он тоже виден отдельно.\n"
        "  - Если синопсис есть — опирайся на сюжет/тему, не выдумывай факты.\n"
        "  - Если синопсиса нет — пиши общо («атмосферный sci-fi для…»), "
        "    не придумывай конкретные сюжетные детали.\n"
        "  - Тон: разговорный, помогающий, как у друга-киномана.\n"
        "\n"
        "Reply with strict JSON: {\"text\": \"одна короткая фраза\"}."
    )
    user_prompt = (
        f"Название: {title}\n"
        f"Год: {year_str}\n"
        f"Жанры: {genres_str}\n"
        f"Рейтинг КП: {rating_str}\n"
        f"{synopsis_block}"
    )

    result, error = chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key,
        model=model,
        max_tokens=120,
        temperature=0.5,  # small creativity for varied phrasing
        response_format={"type": "json_object"},
    )
    if error or not result:
        return (None, error)

    try:
        data = json.loads(result["text"])
        text = str(data.get("text", "")).strip()
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return (None, "parse")

    if not text:
        return (None, "empty")

    # Hard length cap (GPT sometimes ignores «макс. 120» — trim with «…»).
    if len(text) > 130:
        text = text[:127].rstrip() + "…"

    logger.info(
        "GPT explain_card: title=%r → %d chars: %s",
        title, len(text), text[:60],
    )
    return (text, None)


def did_you_mean(
    *,
    query: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    max_suggestions: int = 3,
) -> tuple[list[str], str | None]:
    """Generate alternative search queries when the original returned 0 results.

    Returns ``(suggestions, error_label)``. ``suggestions`` is a list of up to
    ``max_suggestions`` strings (typo fixes, original-language titles, year
    additions, etc.) — always a list (empty on failure), never None.

    Caller renders each as a button: tapping re-runs the search with that text.
    """
    system_prompt = (
        "You are a search assistant for a Russian-language movie torrent bot. "
        "The user's query returned 0 results — most likely they made a typo "
        "in the title, used the wrong language, or omitted the year.\n"
        "\n"
        "Your job: suggest up to 3 ACTUAL EXISTING movie or series titles "
        "the user might have intended. Prioritise in this order:\n"
        "  1. Typo fixes (1-2 character substitutions / phonetic matches) "
        "     mapped to a real movie title. Example: «Дюра» → «Дюна» "
        "     («у» and «р» look/sound similar; «Дюна» is a famous film).\n"
        "  2. Original-language equivalent of a Russian/transliterated query. "
        "     Example: «Аркейн» → «Arcane», «Дюна» → «Dune».\n"
        "  3. Adding a disambiguating year only when the title alone is "
        "     ambiguous between multiple films (e.g. «Дюна» → «Дюна 2024» "
        "     to distinguish from 1984 / 2000 versions).\n"
        "\n"
        "STRICT RULES — violating these makes the suggestion useless:\n"
        "  - Do NOT just append «1080p», «720p», «2160p», «4k», or any other "
        "    quality token to the original wrong word. We strip quality "
        "    suffixes before sending the query — fix the TITLE itself.\n"
        "  - Do NOT just append «2024», «часть 2», digits, or numerals to "
        "    the original wrong word. That's not a typo fix — it's a "
        "    variation that's still wrong.\n"
        "  - Do NOT suggest the original query unchanged.\n"
        "  - If the query is gibberish with no plausible interpretation, "
        "    return empty array. Better silent than misleading.\n"
        "\n"
        "Examples (input → output):\n"
        "  «Дюра»     → [\"Дюна\", \"Dune\", \"Дюна часть вторая\"]\n"
        "  «Аркаин»   → [\"Аркейн\", \"Arcane\", \"Arcane S2\"]\n"
        "  «Барпи»    → [\"Барби\", \"Barbie\", \"Барби 2023\"]\n"
        "  «Опенгеймр»→ [\"Оппенгеймер\", \"Oppenheimer\", \"Oppenheimer 2023\"]\n"
        "  «асдыфф»   → []   (gibberish, no plausible movie)\n"
        "\n"
        'Reply with strict JSON: {"suggestions": ["title1", "title2", "title3"]}'
    )
    user_prompt = f"Failed query: «{query}»"

    result, error = chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key,
        model=model,
        max_tokens=200,
        temperature=0.3,  # slight creativity for spelling variations
        response_format={"type": "json_object"},
    )
    if error or not result:
        return ([], error)

    try:
        data = json.loads(result["text"])
        raw = data.get("suggestions") or []
        suggestions = [
            str(s).strip() for s in raw
            if isinstance(s, str) and str(s).strip()
        ][:max_suggestions]
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return ([], "parse")

    logger.info(
        "GPT did-you-mean: query=%r → %d suggestions: %s",
        query, len(suggestions), suggestions,
    )
    return (suggestions, None)
