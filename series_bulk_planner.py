"""Pure planning helpers for series bulk downloads.

This module intentionally has no Telegram or Download Station side effects.
It decides which season candidates are safe to auto-select and which ones need
an explicit user choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from formatters import _extract_season_from_query, _parse_episode_info
from movie_discovery import detect_quality, parse_size_gb
from series_continue import title_match_key


VOICE_ANY_FROM_REFERENCE = "any_from_reference"
VOICE_SINGLE_FROM_REFERENCE = "single_from_reference"
VOICE_ANY_RUSSIAN = "any_russian"
VOICE_REQUIRE_SELECTED = "require_selected"
VOICE_ORIGINAL_ONLY = "original_only"

STATUS_EXACT = "exact"
STATUS_GOOD = "good"
STATUS_NEEDS_DECISION = "needs_decision"
STATUS_MISSING = "missing"
STATUS_ALREADY_IN_PLEX = "already_in_plex"
STATUS_ALREADY_DOWNLOADING = "already_downloading"
STATUS_PARTIAL = "partial"


_AUDIO_ORIG_RE = re.compile(
    r"\b(orig(?:inal)?|dual|mvo|avo|dvo|nvo|"
    r"Лиценз|Дубляж)\b",
    re.IGNORECASE,
)
_SUBS_RE = re.compile(r"\b(subs?|forced|hardsub|softsub|субтит)\w*", re.IGNORECASE)
_RUSSIAN_AUDIO_RE = re.compile(
    r"\b(rus|ru|russian|dub|mvo|avo|dvo|nvo|vo)\b|"
    r"(лиценз|дубляж|озвуч|многоголос|двухголос)",
    re.IGNORECASE,
)
_PACK_RE = re.compile(
    r"(?:\bсезоны?[:\s]+0*(\d{1,2})\s*[-‑–—]\s*0*(\d{1,2})\b|"
    r"\bS0*(\d{1,2})\s*[-‑–—]\s*S?0*(\d{1,2})\b)",
    re.IGNORECASE,
)
_BRACKETS_RE = re.compile(r"[\[\(].*?[\]\)]")
_SPACE_RE = re.compile(r"\s+")

_RELEASE_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("BDRemux", ("bdremux",)),
    ("BluRay", ("blu-ray", "bluray", "blu ray")),
    ("WEB-DL", ("web-dl", "webdl")),
    ("WEBRip", ("webrip", "web-rip")),
    ("BDRip", ("bdrip",)),
    ("HDTV", ("hdtv",)),
    ("HDRip", ("hdrip",)),
    ("DVDRip", ("dvdrip",)),
)

_VOICE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("LostFilm", r"\blost\s*film\b|ло(?:ст)?фильм"),
    ("NewStudio", r"\bnew\s*studio\b|нью\s*студио"),
    ("Кравец", r"кравец"),
    ("Кубик", r"кубик|kubik"),
    ("HDRezka", r"hdrezka|резка"),
    ("Jaskier", r"jaskier|джаскер"),
    ("TVShows", r"tvshows"),
    ("Ultradox", r"ultradox"),
    ("ColdFilm", r"coldfilm|cold\s*film"),
    ("BaibaKo", r"baibako|байбако"),
    ("AlexFilm", r"alexfilm|alex\s*film"),
)
KNOWN_VOICE_LABELS: tuple[str, ...] = tuple(label for label, _pattern in _VOICE_PATTERNS)


@dataclass(frozen=True)
class ReleaseProfile:
    quality: str = ""
    release_type: str = ""
    voices: tuple[str, ...] = ()
    has_original: bool = False
    has_subs: bool = False
    has_russian_audio: bool = False
    release_group: str = ""
    size_gb: float = 0.0


@dataclass(frozen=True)
class SeriesBulkProfile:
    quality: str = "any"
    require_original: bool = False
    require_subs: bool = False
    voice_policy: str = VOICE_ANY_FROM_REFERENCE
    voices: tuple[str, ...] = ()
    preferred_voices: tuple[str, ...] = ()
    release_type: str = ""
    release_group: str = ""
    tracker: str = ""
    source: str = ""


@dataclass(frozen=True)
class CandidateEvaluation:
    result: dict
    season: int
    release: ReleaseProfile
    score: float
    confidence: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    hard_failures: tuple[str, ...] = ()
    episode_progress: tuple[int, int] | None = None
    gpt_hint: str = ""


@dataclass(frozen=True)
class SeasonPlan:
    season: int
    status: str
    selected: CandidateEvaluation | None = None
    candidates: tuple[CandidateEvaluation, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeriesBulkPlan:
    series_title: str
    seasons: tuple[SeasonPlan, ...]
    pack_candidates: tuple[dict, ...] = ()
    verified_season_range: bool = True


def release_profile_from_title(title: str, *, size: str = "") -> ReleaseProfile:
    """Extract the release traits needed for bulk-season planning."""
    return ReleaseProfile(
        quality=detect_quality(title) or "",
        release_type=_detect_release_type(title),
        voices=_extract_voices(title),
        has_original=bool(_AUDIO_ORIG_RE.search(title)),
        has_subs=bool(_SUBS_RE.search(title)),
        has_russian_audio=bool(_RUSSIAN_AUDIO_RE.search(title)),
        release_group=_extract_release_group(title),
        size_gb=parse_size_gb(size),
    )


def extract_voice_labels(text: str) -> tuple[str, ...]:
    """Return known voice/studio labels mentioned in ``text``."""
    return _extract_voices(text)


def season_pack_range_from_title(title: str) -> tuple[int, int] | None:
    """Return the season range for a pack title, if the title clearly has one."""
    return _detect_season_pack(title)


def build_series_bulk_plan(
    *,
    series_title: str,
    seasons: Iterable[int],
    results: Iterable[dict],
    profile: SeriesBulkProfile,
    plex_seasons: Iterable[int] = (),
    downloading_seasons: Iterable[int] = (),
    verified_season_range: bool = True,
) -> SeriesBulkPlan:
    """Build a season-by-season download plan from already fetched results."""
    wanted_seasons = tuple(sorted({int(s) for s in seasons if int(s) > 0}))
    plex_set = {int(s) for s in plex_seasons if int(s) > 0}
    downloading_set = {int(s) for s in downloading_seasons if int(s) > 0}

    result_list = [r for r in results if isinstance(r, dict)]
    profile = _profile_for_single_reference_voice(
        series_title=series_title,
        seasons=wanted_seasons,
        results=result_list,
        profile=profile,
        skipped_seasons=plex_set | downloading_set,
    )
    pack_candidates = tuple(
        r for r in result_list
        if not _result_is_non_downloadable_seed(r)
        and _title_matches_series(series_title, _result_title(r))
        and _detect_season_pack(_result_title(r)) is not None
    )

    plans: list[SeasonPlan] = []
    for season in wanted_seasons:
        if season in plex_set:
            plans.append(SeasonPlan(
                season=season,
                status=STATUS_ALREADY_IN_PLEX,
                reasons=("season already exists in Plex",),
            ))
            continue
        if season in downloading_set:
            plans.append(SeasonPlan(
                season=season,
                status=STATUS_ALREADY_DOWNLOADING,
                reasons=("season is already downloading",),
            ))
            continue

        evaluations = _evaluate_season_candidates(
            series_title=series_title,
            season=season,
            results=result_list,
            profile=profile,
        )
        plans.append(_choose_season_plan(season, evaluations))

    return SeriesBulkPlan(
        series_title=series_title,
        seasons=tuple(plans),
        pack_candidates=pack_candidates,
        verified_season_range=verified_season_range,
    )


def _evaluate_season_candidates(
    *,
    series_title: str,
    season: int,
    results: list[dict],
    profile: SeriesBulkProfile,
) -> tuple[CandidateEvaluation, ...]:
    season_candidates: list[tuple[dict, str, ReleaseProfile, tuple[int, int] | None]] = []
    for result in results:
        if _result_is_non_downloadable_seed(result):
            continue
        title = _result_title(result)
        if not title or not _title_matches_series(series_title, title):
            continue
        if _detect_season_pack(title) is not None:
            continue
        result_season = _extract_season_from_query(title)
        if result_season != season:
            continue

        release = release_profile_from_title(title, size=str(result.get("size") or ""))
        episode_progress = _parse_episode_info(title)
        season_candidates.append((result, title, release, episode_progress))

    preferred_quality_available = (
        bool(profile.quality)
        and profile.quality != "any"
        and any(release.quality == profile.quality for _result, _title, release, _progress in season_candidates)
    )

    evaluations: list[CandidateEvaluation] = []
    for result, _title, release, episode_progress in season_candidates:
        score, reasons, warnings, hard_failures = _score_candidate(
            result=result,
            release=release,
            profile=profile,
            preferred_quality_available=preferred_quality_available,
        )
        if episode_progress and episode_progress[0] < episode_progress[1]:
            warnings = (*warnings, "season is partial")

        confidence = _candidate_confidence(
            release=release,
            profile=profile,
            warnings=warnings,
            hard_failures=hard_failures,
            episode_progress=episode_progress,
        )
        evaluations.append(CandidateEvaluation(
            result=result,
            season=season,
            release=release,
            score=score,
            confidence=confidence,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            hard_failures=tuple(hard_failures),
            episode_progress=episode_progress,
        ))

    return tuple(sorted(evaluations, key=lambda item: item.score, reverse=True))


def _profile_for_single_reference_voice(
    *,
    series_title: str,
    seasons: tuple[int, ...],
    results: list[dict],
    profile: SeriesBulkProfile,
    skipped_seasons: set[int],
) -> SeriesBulkProfile:
    if profile.voice_policy != VOICE_SINGLE_FROM_REFERENCE or not profile.voices:
        return profile

    best_voice = ""
    best_ready_count = -1
    best_score = -1.0
    for voice in profile.voices:
        trial = _profile_copy(
            profile,
            voice_policy=VOICE_REQUIRE_SELECTED,
            voices=(voice,),
        )
        ready_count = 0
        score = 0.0
        for season in seasons:
            if season in skipped_seasons:
                continue
            plan = _choose_season_plan(
                season,
                _evaluate_season_candidates(
                    series_title=series_title,
                    season=season,
                    results=results,
                    profile=trial,
                ),
            )
            if plan.status in {STATUS_EXACT, STATUS_GOOD} and plan.selected is not None:
                ready_count += 1
                score += plan.selected.score
        if (ready_count, score) > (best_ready_count, best_score):
            best_voice = voice
            best_ready_count = ready_count
            best_score = score

    if not best_voice:
        return profile
    return _profile_copy(
        profile,
        voice_policy=VOICE_REQUIRE_SELECTED,
        voices=(best_voice,),
    )


def _profile_copy(profile: SeriesBulkProfile, **updates) -> SeriesBulkProfile:
    data = {
        "quality": profile.quality,
        "require_original": profile.require_original,
        "require_subs": profile.require_subs,
        "voice_policy": profile.voice_policy,
        "voices": profile.voices,
        "preferred_voices": profile.preferred_voices,
        "release_type": profile.release_type,
        "release_group": profile.release_group,
        "tracker": profile.tracker,
        "source": profile.source,
    }
    data.update(updates)
    return SeriesBulkProfile(**data)


def _choose_season_plan(season: int, evaluations: tuple[CandidateEvaluation, ...]) -> SeasonPlan:
    if not evaluations:
        return SeasonPlan(season=season, status=STATUS_MISSING)

    partials = tuple(e for e in evaluations if e.confidence == STATUS_PARTIAL)
    passing = tuple(
        e for e in evaluations
        if not e.hard_failures and e.confidence in {STATUS_EXACT, STATUS_GOOD}
    )

    if partials and not passing:
        return SeasonPlan(
            season=season,
            status=STATUS_PARTIAL,
            candidates=partials,
            reasons=("season is not complete yet",),
        )

    if not passing:
        return SeasonPlan(
            season=season,
            status=STATUS_NEEDS_DECISION,
            candidates=evaluations,
            reasons=("no candidate passed all hard filters",),
        )

    top = passing[0]
    if len(passing) > 1 and top.score - passing[1].score < 40:
        return SeasonPlan(
            season=season,
            status=STATUS_NEEDS_DECISION,
            candidates=passing[:3],
            reasons=("multiple candidates are too close to auto-select",),
        )

    return SeasonPlan(
        season=season,
        status=top.confidence,
        selected=top,
        candidates=passing[:3],
    )


def _score_candidate(
    *,
    result: dict,
    release: ReleaseProfile,
    profile: SeriesBulkProfile,
    preferred_quality_available: bool = True,
) -> tuple[float, list[str], list[str], list[str]]:
    score = 1000.0
    reasons: list[str] = ["season matches"]
    warnings: list[str] = []
    hard_failures: list[str] = []

    if profile.quality and profile.quality != "any":
        if release.quality == profile.quality:
            score += 260
            reasons.append(f"quality matches {profile.quality}")
        elif preferred_quality_available:
            hard_failures.append("quality does not match search preference")
        else:
            selected_quality = release.quality or "unknown"
            warnings.append(
                f"preferred quality unavailable: {profile.quality}; selected quality: {selected_quality}"
            )

    if profile.require_original:
        if release.has_original:
            score += 140
            reasons.append("original audio found")
        else:
            hard_failures.append("original audio not found")

    if profile.require_subs:
        if release.has_subs:
            score += 100
            reasons.append("subtitles found")
        else:
            hard_failures.append("subtitles not found")

    voice_score, voice_reason, voice_failure = _voice_policy_score(release, profile)
    score += voice_score
    if voice_reason:
        reasons.append(voice_reason)
    if voice_failure:
        hard_failures.append(voice_failure)

    if profile.release_type:
        if release.release_type == profile.release_type:
            score += 120
            reasons.append(f"release type matches {profile.release_type}")
        elif release.release_type:
            warnings.append(
                f"release type differs: {release.release_type} instead of {profile.release_type}"
            )

    if profile.release_group and release.release_group:
        if release.release_group.lower() == profile.release_group.lower():
            score += 70
            reasons.append("release group matches")
        else:
            warnings.append("release group differs")

    if _result_tracker(result).lower() == (profile.tracker or "").lower() and profile.tracker:
        score += 40
    if str(result.get("source") or "").lower() == (profile.source or "").lower() and profile.source:
        score += 30

    seeders = _safe_int(result.get("seeders"))
    score += min(seeders, 500) * 0.1
    if seeders <= 0:
        warnings.append("no seeders reported")

    if release.size_gb > 0:
        score += min(release.size_gb, 80) * 0.5

    return score, reasons, warnings, hard_failures


def _candidate_confidence(
    *,
    release: ReleaseProfile,
    profile: SeriesBulkProfile,
    warnings: tuple[str, ...],
    hard_failures: tuple[str, ...],
    episode_progress: tuple[int, int] | None,
) -> str:
    if episode_progress and episode_progress[0] < episode_progress[1]:
        return STATUS_PARTIAL
    if hard_failures:
        return STATUS_NEEDS_DECISION
    if warnings:
        return STATUS_GOOD
    if profile.release_type and release.release_type != profile.release_type:
        return STATUS_GOOD
    return STATUS_EXACT


def _voice_policy_score(release: ReleaseProfile, profile: SeriesBulkProfile) -> tuple[float, str, str]:
    policy = profile.voice_policy or VOICE_ANY_FROM_REFERENCE
    wanted = tuple(v for v in profile.voices if v)
    preferred = tuple(v for v in profile.preferred_voices if v)
    overlap = _voice_overlap(release.voices, wanted)
    preferred_overlap = _voice_overlap(release.voices, preferred)

    preferred_score = 0.0
    preferred_reason = ""
    if preferred_overlap:
        preferred_score = 80.0 + 10.0 * len(preferred_overlap)
        preferred_reason = f"preferred voice matched: {', '.join(preferred_overlap)}"

    if policy == VOICE_ORIGINAL_ONLY:
        if release.has_original:
            return 180.0, "original-only policy matched", ""
        return 0.0, "", "original audio not found"

    if policy == VOICE_REQUIRE_SELECTED:
        if not wanted:
            return 0.0, "no selected voice to require", ""
        if overlap:
            return 180.0 + 20.0 * len(overlap), f"voice matched: {', '.join(overlap)}", ""
        return 0.0, "", "selected voice not found"

    if policy == VOICE_ANY_RUSSIAN:
        if release.voices or release.has_russian_audio:
            reason = "russian audio looks present"
            if preferred_reason:
                reason = f"{reason}; {preferred_reason}"
            return 120.0 + preferred_score, reason, ""
        return 0.0, "", "russian audio not found"

    if wanted:
        if overlap:
            reason = f"voice from reference matched: {', '.join(overlap)}"
            if preferred_reason:
                reason = f"{reason}; {preferred_reason}"
            return 160.0 + 20.0 * len(overlap) + preferred_score, reason, ""
        return 0.0, "", "no voice from reference found"

    if preferred_reason:
        return preferred_score, preferred_reason, ""
    return 0.0, "voice preference is not constrained", ""


def _detect_release_type(title: str) -> str:
    lower = title.lower()
    for label, tokens in _RELEASE_TYPES:
        if any(token in lower for token in tokens):
            return label
    return ""


def _extract_voices(title: str) -> tuple[str, ...]:
    found: list[str] = []
    for label, pattern in _VOICE_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            found.append(label)
    return tuple(dict.fromkeys(found))


def _extract_release_group(title: str) -> str:
    m = re.search(r"-([A-Za-z0-9][A-Za-z0-9._-]{2,})\s*$", title)
    if m:
        return m.group(1).strip("._-")
    bracketed = re.findall(r"\[([A-Za-z0-9][A-Za-z0-9._-]{2,})\]", title)
    return bracketed[-1].strip("._-") if bracketed else ""


def _detect_season_pack(title: str) -> tuple[int, int] | None:
    m = _PACK_RE.search(title)
    if not m:
        return None
    nums = [int(g) for g in m.groups() if g]
    if len(nums) < 2:
        return None
    start, end = nums[0], nums[1]
    if start == end:
        return None
    return (min(start, end), max(start, end))


def _title_matches_series(series_title: str, title: str) -> bool:
    series_norm = _normalize_title(series_title)
    title_norm = _normalize_title(title)
    if series_norm and series_norm in title_norm:
        return True
    series_key = title_match_key(series_title)
    title_key = title_match_key(title)
    return bool(series_key and series_key in title_key)


def _normalize_title(value: str) -> str:
    value = _BRACKETS_RE.sub(" ", value.lower().replace("ё", "е"))
    value = re.sub(r"\b\d{3,4}p\b", " ", value)
    value = re.sub(r"\b(web-?dl|webrip|bdremux|bdrip|hdrip|hdtv)\b", " ", value)
    value = value.replace("/", " ")
    return _SPACE_RE.sub(" ", value).strip()


def _voice_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    right_by_norm = {_normalize_voice(v): v for v in right}
    out: list[str] = []
    for value in left:
        norm = _normalize_voice(value)
        if norm in right_by_norm:
            out.append(right_by_norm[norm])
    return tuple(dict.fromkeys(out))


def _normalize_voice(value: str) -> str:
    return re.sub(r"\s+", "", value.lower().replace("ё", "е"))


def _result_title(result: dict) -> str:
    return str(result.get("title") or "")


def _result_tracker(result: dict) -> str:
    return str(result.get("tracker_name") or result.get("category") or result.get("tracker") or "")


def _result_has_download_source(result: dict) -> bool:
    return any(
        str(result.get(field) or "").strip()
        for field in ("topic_id", "url", "torrent_url", "magnet_url")
    )


def _result_is_non_downloadable_seed(result: dict) -> bool:
    return (
        str(result.get("source") or "").lower() == "continue_missing"
        and not _result_has_download_source(result)
    )


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
