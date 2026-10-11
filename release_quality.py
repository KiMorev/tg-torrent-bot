"""Shared release-source exclusions and resolution ordering."""

import re


_QUALITY_ALIASES = {
    "4k": "2160p", "uhd": "2160p", "2160": "2160p", "2160p": "2160p",
    "1080": "1080p", "1080p": "1080p", "1080i": "1080p",
    "720": "720p", "720p": "720p", "576p": "576p", "480p": "480p",
}
_RESOLUTION_RE = re.compile(
    r"(?<![\w])(?:4k|uhd|2160p|1080p|1080i|720p|576p|480p)(?![\w])", re.I,
)
_SCREEN_SOURCE_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"hd[\s._-]*(?:cam(?:[\s._-]*rip)?|ts|tc)|"
    r"cam[\s._-]*rip|[t][sc][\s._-]*rip|"
    r"tele[\s._-]*(?:sync|cine)|dvd[\s._-]*(?:scr|screener)|"
    r"scr[\s._-]*rip|screener|work[\s._-]*print|экранка|экранная\s+копия"
    r")(?![a-z])", re.I,
)
_SHORT_SCREEN_SOURCE_RE = re.compile(r"(?<![a-z0-9])(?:cam|ts|tc|scr)(?![a-z])", re.I)


def normalize_quality(value: str | None) -> str:
    return _QUALITY_ALIASES.get(str(value or "").strip().lower(), "")


def detect_release_quality(title: str) -> str | None:
    qualities = [normalize_quality(match.group()) for match in _RESOLUTION_RE.finditer(title.replace("_", " "))]
    return max(qualities, key=lambda quality: int(quality[:-1]), default=None)


def result_quality(result: dict) -> str:
    return normalize_quality(result.get("quality")) or detect_release_quality(str(result.get("title") or "")) or ""


def quality_sort_key(
    result: dict,
    secondary_score: float = 0.0,
    *,
    preferred_quality: str | None = None,
) -> tuple[int, int, float]:
    quality = result_quality(result)
    preferred = normalize_quality(preferred_quality)
    return (
        int(bool(preferred) and quality == preferred),
        int(quality[:-1]) if quality else 0,
        secondary_score,
    )


def is_screen_release(title: str) -> bool:
    """Exclude marked cinema/pre-release copies, preserving TS containers."""
    if _SCREEN_SOURCE_RE.search(title):
        return True
    text = re.sub(r"\bmpeg[\s._-]*ts\b", "", title, flags=re.I)
    text = re.sub(r"\.ts(?=$|[\s\]\[()/])", "", text, flags=re.I)
    cam_title = re.match(r"^\s*cam(?:\s*/\s*cam)?\s*[\[(]?\s*(?:19|20)\d{2}\b", text, flags=re.I)
    for match in _SHORT_SCREEN_SOURCE_RE.finditer(text):
        # A movie named Cam followed by its year is a title, not a source tag.
        if cam_title and match.group().lower() == "cam" and match.end() <= cam_title.end():
            continue
        return True
    return False
