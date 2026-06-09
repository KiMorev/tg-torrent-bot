from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
}

PLEX_EPISODE_RE = re.compile(r"(?i)\bS\d{1,2}E\d{1,3}\b")
ARC_EPISODE_RE = re.compile(
    r"^\s*(?P<arc>\d{1,3})\.\s+"
    r"(?P<title>.+?)\s*"
    r"\(\s*(?P<part>\d{1,3})\s*(?:сер(?:\.|ия|ии|ий)?|эп(?:\.|изод)?)\s*\)"
    r"\s*(?:[-–—]\s*.*)?$",
    re.IGNORECASE,
)
UNSAFE_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(frozen=True)
class RenameItem:
    source_path: Path
    target_path: Path
    episode_number: int
    episode_title: str
    arc_number: int
    part_number: int


@dataclass(frozen=True)
class RenamePlan:
    show_title: str
    season: int
    source_root: Path
    target_dir: Path
    items: tuple[RenameItem, ...]
    confidence: str = "high"


class RenamePlanError(RuntimeError):
    pass


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_plex_episode_filename(path: Path) -> bool:
    return bool(PLEX_EPISODE_RE.search(path.stem))


def has_arc_episode_filenames(files: list[Path]) -> bool:
    video_files = [path for path in files if is_video_file(path)]
    if len(video_files) < 2:
        return False
    if all(is_plex_episode_filename(path) for path in video_files):
        return False
    return all(_parse_arc_episode(path) is not None for path in video_files)


def sanitize_filename_part(value: str) -> str:
    cleaned = UNSAFE_FILENAME_CHARS_RE.sub(" ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Episode"


def _parse_arc_episode(path: Path) -> tuple[int, str, int] | None:
    match = ARC_EPISODE_RE.match(path.stem)
    if not match:
        return None
    try:
        arc = int(match.group("arc"))
        part = int(match.group("part"))
    except (TypeError, ValueError):
        return None
    title = sanitize_filename_part(match.group("title"))
    if arc <= 0 or part <= 0 or not title:
        return None
    return arc, title, part


def _single_parent(paths: list[Path]) -> Path | None:
    parents = {path.parent for path in paths}
    if len(parents) == 1:
        return next(iter(parents))
    return None


def build_arc_episode_rename_plan(
    *,
    show_title: str,
    season: int,
    files: list[Path],
    source_root: Path | None = None,
) -> RenamePlan | None:
    """Build a Plex rename plan for Russian arc/part episode names.

    Example source pattern:
    ``1. Мягкая лапа смерти (2 сер.) - hdtv1080p.mkv``.
    """
    title = sanitize_filename_part(show_title)
    if season <= 0 or not title:
        return None

    video_files = sorted(
        [path for path in files if is_video_file(path)],
        key=lambda path: str(path).casefold(),
    )
    if len(video_files) < 2:
        return None
    if all(is_plex_episode_filename(path) for path in video_files):
        return None

    parsed: list[tuple[int, str, int, Path]] = []
    for path in video_files:
        item = _parse_arc_episode(path)
        if item is None:
            return None
        arc, episode_title, part = item
        parsed.append((arc, episode_title, part, path))

    by_arc: dict[int, set[int]] = {}
    for arc, _episode_title, part, _path in parsed:
        by_arc.setdefault(arc, set()).add(part)
    for parts in by_arc.values():
        expected = set(range(1, len(parts) + 1))
        if parts != expected:
            return None

    parent = _single_parent(video_files)
    if parent is None:
        return None
    root = source_root or parent
    target_dir = root / f"Season {season:02d}"

    items: list[RenameItem] = []
    for episode_number, (arc, episode_title, part, source_path) in enumerate(
        sorted(parsed, key=lambda item: (item[0], item[2], str(item[3]).casefold())),
        start=1,
    ):
        target_name = (
            f"{title} - S{season:02d}E{episode_number:02d} - "
            f"{sanitize_filename_part(episode_title)}{source_path.suffix}"
        )
        items.append(
            RenameItem(
                source_path=source_path,
                target_path=target_dir / target_name,
                episode_number=episode_number,
                episode_title=episode_title,
                arc_number=arc,
                part_number=part,
            )
        )

    targets = [item.target_path for item in items]
    if len({os.path.normcase(str(path)) for path in targets}) != len(targets):
        return None

    return RenamePlan(
        show_title=title,
        season=season,
        source_root=root,
        target_dir=target_dir,
        items=tuple(items),
    )


def apply_rename_plan(plan: RenamePlan) -> None:
    if not plan.items:
        raise RenamePlanError("empty rename plan")

    seen_targets: set[str] = set()
    for item in plan.items:
        if not item.source_path.exists():
            raise RenamePlanError(f"source file missing: {item.source_path}")
        target_key = os.path.normcase(str(item.target_path))
        if target_key in seen_targets:
            raise RenamePlanError(f"duplicate target path: {item.target_path}")
        seen_targets.add(target_key)
        if item.target_path.exists() and item.target_path.resolve() != item.source_path.resolve():
            raise RenamePlanError(f"target already exists: {item.target_path}")

    plan.target_dir.mkdir(parents=True, exist_ok=True)
    for item in plan.items:
        if item.source_path.resolve() == item.target_path.resolve():
            continue
        item.source_path.rename(item.target_path)
