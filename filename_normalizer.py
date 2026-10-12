from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from torrent_utils import bdecode_torrent


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

NAMING_NO_ACTION = "no_action"
NAMING_PLEX_READY = "plex_ready"
NAMING_RENAMABLE_ARC = "renamable_arc"
NAMING_UNSAFE_ARC = "unsafe_arc"
NAMING_MIXED = "mixed"
NAMING_UNKNOWN_NON_PLEX = "unknown_non_plex"

PLEX_EPISODE_RE = re.compile(r"(?i)\bS\d{1,2}[\s._-]?E\d{1,3}\b")
DOTTED_EPISODE_RE = re.compile(r"(?i)\b(S\d{1,2})\.(E\d{1,3})\b")
ARC_EPISODE_RE = re.compile(
    r"^\s*(?P<arc>\d{1,3})\.\s+"
    r"(?P<title>.+?)\s*"
    r"\(\s*(?P<parts>\d{1,3}(?:\s*[,;+\-–—]\s*\d{1,3})*)\s*"
    r"(?:сер(?:\.|ия|ии|ий)?|эп(?:\.|изод)?)\s*\)"
    r"\s*(?:[-–—]\s*.*)?$",
    re.IGNORECASE,
)
SERIES_FILM_PART_RE = re.compile(
    r"^\s*(?P<prefix>.+?)[\s._-]+(?P<season>\d{1,2})[\s._-]+"
    r"(?:фильм|film)\s*0*(?P<arc>\d{1,3})[\s._-]+"
    r"(?P<title>.+?)[\s._-]*(?:часть|part)\s*0*(?P<part>\d{1,3})\s*$",
    re.IGNORECASE,
)
NUMBERED_EPISODE_RE = re.compile(r"^\s*0*(?P<episode>\d{1,3})\s*$")
NON_PLEX_EPISODE_LIKE_RE = re.compile(
    r"(?:"
    r"\b(?:episode|ep|e)\s*0*\d{1,3}\b|"
    r"\b0*\d{1,3}\s*(?:сер(?:\.|ия|ии|ий)?|эп(?:\.|изод)?)\b|"
    r"\b(?:сер(?:ия|ии)?|эп(?:изод)?)\s*0*\d{1,3}\b|"
    r"^\s*0*\d{1,3}(?:\s*$|[\s._-]+)"
    r")",
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
    episode_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class RenamePlan:
    show_title: str
    season: int
    source_root: Path
    target_dir: Path
    items: tuple[RenameItem, ...]
    confidence: str = "high"


@dataclass(frozen=True)
class FilenameInspection:
    status: str
    reason: str = ""
    files: tuple[Path, ...] = ()
    suspicious_files: tuple[Path, ...] = ()


class RenamePlanError(RuntimeError):
    pass


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def normalize_dotted_episode_name(name: str) -> str:
    return DOTTED_EPISODE_RE.sub(r"\1\2", name)


_IGNORE_BEGIN = "# PlexLoader torrent paths: begin"
_IGNORE_END = "# PlexLoader torrent paths: end"
_IGNORE_ALIASES = "# PlexLoader alias inodes: "
_MATCH_BEGIN = "# PlexLoader episode hints: begin"
_MATCH_END = "# PlexLoader episode hints: end"
_DOTTED_FILES_LOCK = threading.RLock()
_EPISODE_ASSIGNMENT_RE = re.compile(
    r"(?i)\bS(?P<season>\d{1,2})[\s._-]?E(?P<first>\d{1,3})"
    r"(?:-E(?P<last>\d{1,3}))?\b"
)
_HINT_CODE_RE = re.compile(r"(?i)S\d{1,2}E\d{1,3}(?:-E\d{1,3})?")
_DOTTED_MARKER_RE = re.compile(r"(?i)\bS\d+\.E\d+")


def episode_assignment_from_filename(path: Path) -> tuple[int, tuple[int, ...]] | None:
    name = path.stem
    if any(char in path.name for char in "\r\n"):
        return None
    matches = list(_EPISODE_ASSIGNMENT_RE.finditer(name))
    if len(matches) != 1:
        return None
    match = matches[0]
    first = int(match.group("first"))
    last = int(match.group("last") or first)
    remainder = name[:match.start()] + name[match.end():]
    if first <= 0 or last < first or re.search(r"(?i)\bE\d+\b", remainder):
        return None
    if re.match(r"-\d{1,3}\b", name[match.end():]):
        return None
    return int(match.group("season")), tuple(range(first, last + 1))


def _read_sidecar(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_symlink():
        raise RenamePlanError(f"sidecar is a symlink: {path}")
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except UnicodeError as exc:
        raise RenamePlanError(f"sidecar must use UTF-8: {path}") from exc


def _managed_block(text: str, begin: str, end: str, path: Path) -> tuple[str, str, str]:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == begin]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == end]
    if not starts and not ends:
        return text, "", ""
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise RenamePlanError(f"invalid PlexLoader block: {path}")
    return "".join(lines[:starts[0]]), "".join(lines[starts[0]+1:ends[0]]), "".join(lines[ends[0]+1:])


def _replace_managed_block(text: str, begin: str, end: str, body: str, path: Path) -> str:
    prefix, _old, suffix = _managed_block(text, begin, end, path)
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + begin + "\n" + body + end + "\n" + suffix


def _write_sidecar(path: Path, text: str) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _dotted_ignore_state(folder: Path) -> tuple[str, set[str], dict[str, int]]:
    ignore = folder / ".plexignore"
    text = _read_sidecar(ignore)
    _prefix, body, _suffix = _managed_block(text, _IGNORE_BEGIN, _IGNORE_END, ignore)
    lines = body.splitlines()
    inode_lines = [line[len(_IGNORE_ALIASES):] for line in lines if line.startswith(_IGNORE_ALIASES)]
    try:
        inodes = json.loads(inode_lines[0]) if inode_lines else {}
        if len(inode_lines) > 1 or not isinstance(inodes, dict):
            raise ValueError("invalid inode ledger")
    except ValueError as exc:
        raise RenamePlanError(f"invalid PlexLoader inode ledger: {ignore}") from exc
    return text, {line for line in lines if line and not line.startswith("#")}, inodes


def _write_dotted_ignore(folder: Path, text: str, names: set[str], inodes: dict[str, int]) -> None:
    ledger = _IGNORE_ALIASES + json.dumps(inodes, ensure_ascii=False, sort_keys=True)
    ignore = folder / ".plexignore"
    body = "\n".join(sorted(names)) + ("\n" if names else "") + ledger + "\n"
    updated = _replace_managed_block(text, _IGNORE_BEGIN, _IGNORE_END, body, ignore)
    if updated != text:
        _write_sidecar(ignore, updated)


def prepare_episode_match_hints(files: list[Path]) -> tuple[list[Path], int]:
    """Write exact Plex episode hints without changing media files or their names."""
    with _DOTTED_FILES_LOCK:
        return _prepare_episode_match_hints(files)


def _prepare_episode_match_hints(files: list[Path]) -> tuple[list[Path], int]:
    videos = list(dict.fromkeys(_video_files(files)))
    assignments: dict[Path, tuple[int, tuple[int, ...]]] = {}
    for path in videos:
        if not _DOTTED_MARKER_RE.search(path.stem):
            continue
        if any(char in path.name for char in "\r\n") or path.name != path.name.strip():
            raise RenamePlanError(f"unsafe filename for Plex hint: {path}")
        assignment = episode_assignment_from_filename(path)
        if assignment is None:
            raise RenamePlanError(f"ambiguous episode filename: {path}")
        if path.is_symlink() or not path.is_file():
            raise RenamePlanError(f"source file missing: {path}")
        assignments[path] = assignment

    ignore_states = {path.parent: _dotted_ignore_state(path.parent) for path in videos}
    match_writes: dict[Path, str] = {}
    changed = 0
    for folder in dict.fromkeys(path.parent for path in assignments):
        sidecar = folder / ".plexmatch"
        text = _read_sidecar(sidecar)
        prefix, body, suffix = _managed_block(text, _MATCH_BEGIN, _MATCH_END, sidecar)
        hints: dict[str, str] = {}
        for line in body.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"(?i)\s*(?:episode|ep)\s*:\s*([^:]+):\s*(.+)", line)
            if match is None or _HINT_CODE_RE.fullmatch(match[1].strip()) is None or episode_assignment_from_filename(Path(match[1].strip() + ".mkv")) is None:
                raise RenamePlanError(f"invalid PlexLoader episode hint: {sidecar}")
            name = match[2].strip()
            if name in hints:
                raise RenamePlanError(f"duplicate PlexLoader episode hint: {sidecar}")
            hints[name] = match[1].strip()
        for path, (season, episodes) in assignments.items():
            if path.parent != folder:
                continue
            code = f"S{season:02d}E{episodes[0]:02d}"
            if len(episodes) > 1:
                code += f"-E{episodes[-1]:02d}"
            for line in (prefix + suffix).splitlines():
                match = re.fullmatch(r"(?i)\s*(?:episode|ep)\s*:\s*([^:]+):\s*(.+)", line)
                if match and match[2].strip() == path.name:
                    existing = episode_assignment_from_filename(Path(match[1].strip() + ".mkv")) if _HINT_CODE_RE.fullmatch(match[1].strip()) else None
                    if existing != (season, episodes):
                        raise RenamePlanError(f"conflicting user episode hint: {sidecar}: {path.name}")
            changed += hints.get(path.name) != code
            hints[path.name] = code
        hint_body = "".join(f"ep: {hints[name]}: {name}\n" for name in sorted(hints))
        updated = _replace_managed_block(text, _MATCH_BEGIN, _MATCH_END, hint_body, sidecar)
        if updated != text:
            match_writes[sidecar] = updated

    ignore_updates = {}
    for folder, (text, managed, inodes) in ignore_states.items():
        names = set(managed)
        folder_sources = {path.name: path for path in assignments if path.parent == folder}
        folder_sources.update({name: folder / name for name in inodes if normalize_dotted_episode_name(name) != name})
        for raw_name, source in folder_sources.items():
            target = source.with_name(normalize_dotted_episode_name(raw_name))
            if raw_name not in managed and target.name not in managed:
                continue
            names.discard(raw_name)
            recorded = inodes.get(raw_name)
            proven = (
                source.is_file() and not source.is_symlink()
                and target.is_file() and not target.is_symlink()
                and (source.samefile(target) or (type(recorded) is int and recorded > 0 and target.stat().st_ino == recorded))
            )
            if proven:
                names.add(target.name)
            else:
                names.discard(target.name)
        if names != managed:
            ignore_updates[folder] = (text, names, inodes)

    # Publish hints before exposing the original names to the Plex scanner.
    for path, text in match_writes.items():
        _write_sidecar(path, text)
    for folder, (text, names, inodes) in ignore_updates.items():
        _write_dotted_ignore(folder, text, names, inodes)
    return [
        path for path in videos
        if not (
            path.name in ignore_updates.get(path.parent, ignore_states[path.parent])[1]
            and normalize_dotted_episode_name(path.name) == path.name
            and episode_assignment_from_filename(path) is not None
        )
    ], changed


def restore_dotted_episode_torrent_paths(torrent_path: Path, storage_root: Path) -> int:
    """Restore exact torrent names from legacy Plex names before DSM verifies pieces."""
    with _DOTTED_FILES_LOCK:
        return _restore_dotted_episode_torrent_paths(torrent_path, storage_root)


def _restore_dotted_episode_torrent_paths(torrent_path: Path, storage_root: Path) -> int:
    if not storage_root.is_dir():
        return 0
    try:
        _, info = bdecode_torrent(torrent_path.read_bytes())
        if not isinstance(info, dict):
            raise ValueError("missing info dictionary")

        def component(value: bytes) -> str:
            name = value.decode("utf-8")
            if not name or name in {".", ".."} or any(c in name for c in "/\\:") or any(ord(c) < 32 for c in name):
                raise ValueError("unsafe torrent path")
            return name

        root_name = component(info.get(b"name.utf-8", info.get(b"name", b"")))
        root = storage_root.resolve()
        entries = info.get(b"files")
        paths = [storage_root / root_name] if entries is None else [
            storage_root / root_name / Path(*(component(part) for part in entry.get(b"path.utf-8", entry.get(b"path", []))))
            for entry in entries
        ]
        pairs = []
        for source in paths:
            target = source.with_name(normalize_dotted_episode_name(source.name))
            if not source.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root):
                raise ValueError("torrent path escapes storage")
            if source == target or not is_video_file(source) or not target.exists():
                continue
            if source.is_symlink() or target.is_symlink() or not target.is_file():
                raise ValueError("unsafe torrent file")
            if source.exists() and not source.samefile(target):
                continue
            pairs.append((source, target, not source.exists()))
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
        raise RenamePlanError(f"invalid torrent paths: {exc}") from exc
    ignore_states = {source.parent: _dotted_ignore_state(source.parent) for source, _target, _new in pairs}
    for source, target, new in pairs:
        if new:
            os.link(target, source)
    for folder, (text, managed, inodes) in ignore_states.items():
        folder_aliases = {source.name: target.stat().st_ino for source, target, _new in pairs if source.parent == folder}
        visible_raw = {source.name for source, target, _new in pairs if source.parent == folder and target.name in managed}
        _write_dotted_ignore(folder, text, (managed | folder_aliases.keys()) - visible_raw, inodes | folder_aliases)
    return sum(new for _source, _target, new in pairs)


def _video_files(files: list[Path]) -> list[Path]:
    return sorted(
        [path for path in files if is_video_file(path)],
        key=lambda path: str(path).casefold(),
    )


def is_plex_episode_filename(path: Path) -> bool:
    return bool(PLEX_EPISODE_RE.search(path.stem))


def is_non_plex_episode_like_filename(
    path: Path,
    *,
    series_context: bool = False,
    show_title: str = "",
) -> bool:
    if is_plex_episode_filename(path):
        return False
    return (
        _parse_arc_episode(path, series_context=series_context, show_title=show_title) is not None
        or bool(NON_PLEX_EPISODE_LIKE_RE.search(path.stem))
    )


def has_arc_episode_filenames(
    files: list[Path],
    *,
    series_context: bool = False,
    show_title: str = "",
) -> bool:
    video_files = _video_files(files)
    if len(video_files) < 2:
        return False
    if all(is_plex_episode_filename(path) for path in video_files):
        return False
    return all(
        _parse_arc_episode(path, series_context=series_context, show_title=show_title) is not None
        for path in video_files
    )


def sanitize_filename_part(value: str) -> str:
    cleaned = UNSAFE_FILENAME_CHARS_RE.sub(" ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Episode"


def _title_key(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value or "", flags=re.UNICODE).casefold().strip()


def _parse_arc_parts(value: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for raw in re.split(r"\s*[,;+\-–—]\s*", value or ""):
        if not raw:
            return None
        try:
            part = int(raw)
        except ValueError:
            return None
        if part <= 0 or part in parts:
            return None
        parts.append(part)
    return tuple(parts) if parts else None


def _parse_series_film_part(
    path: Path,
    *,
    show_title: str = "",
) -> tuple[int, int, str, tuple[int, ...]] | None:
    match = SERIES_FILM_PART_RE.match(path.stem)
    if not match:
        return None
    if show_title and _title_key(match.group("prefix") or "") != _title_key(show_title):
        return None
    try:
        season = int(match.group("season"))
        arc = int(match.group("arc"))
        part = int(match.group("part"))
    except (TypeError, ValueError):
        return None
    title = sanitize_filename_part(re.sub(r"[_]+", " ", match.group("title") or ""))
    if season <= 0 or arc <= 0 or part <= 0 or not title:
        return None
    return season, arc, title, (part,)


def _parse_numbered_episode(path: Path) -> tuple[int, str, tuple[int, ...]] | None:
    match = NUMBERED_EPISODE_RE.match(path.stem)
    if not match:
        return None
    try:
        episode = int(match.group("episode"))
    except (TypeError, ValueError):
        return None
    if episode <= 0:
        return None
    return 1, "", (episode,)


def infer_series_season_from_filenames(files: list[Path], show_title: str = "") -> int | None:
    video_files = _video_files(files)
    if len(video_files) < 2:
        return None
    parsed = [
        _parse_series_film_part(path, show_title=show_title)
        for path in video_files
    ]
    if any(item is None for item in parsed):
        return None
    seasons = {item[0] for item in parsed if item is not None}
    if len(seasons) != 1:
        return None
    return next(iter(seasons))


def _parse_arc_episode(
    path: Path,
    *,
    series_context: bool = False,
    show_title: str = "",
) -> tuple[int, str, tuple[int, ...]] | None:
    match = ARC_EPISODE_RE.match(path.stem)
    if match:
        try:
            arc = int(match.group("arc"))
        except (TypeError, ValueError):
            return None
        parts = _parse_arc_parts(match.group("parts") or "")
        if parts is None:
            return None
        title = sanitize_filename_part(match.group("title"))
        if arc <= 0 or not title:
            return None
        return arc, title, parts

    if series_context:
        item = _parse_series_film_part(path, show_title=show_title)
        if item is not None:
            _season, arc, title, parts = item
            return arc, title, parts
        numbered = _parse_numbered_episode(path)
        if numbered is not None:
            return numbered
    return None


def _single_parent(paths: list[Path]) -> Path | None:
    parents = {path.parent for path in paths}
    if len(parents) == 1:
        return next(iter(parents))
    return None


def _arc_parts_are_contiguous(parsed: list[tuple[int, str, tuple[int, ...], Path]]) -> bool:
    by_arc: dict[int, set[int]] = {}
    for arc, _episode_title, parts, _path in parsed:
        seen = by_arc.setdefault(arc, set())
        for part in parts:
            if part in seen:
                return False
            seen.add(part)
    for parts in by_arc.values():
        expected = set(range(1, max(parts) + 1))
        if parts != expected:
            return False
    return True


def inspect_series_filenames(
    files: list[Path],
    *,
    series_context: bool = False,
    show_title: str = "",
) -> FilenameInspection:
    video_files = _video_files(files)
    if not video_files:
        return FilenameInspection(NAMING_NO_ACTION)

    if all(is_plex_episode_filename(path) for path in video_files):
        return FilenameInspection(NAMING_PLEX_READY, files=tuple(video_files))

    suspicious = tuple(
        path for path in video_files
        if is_non_plex_episode_like_filename(path, series_context=series_context, show_title=show_title)
    )
    if not suspicious:
        return FilenameInspection(NAMING_NO_ACTION, files=tuple(video_files))

    plex_ready = [path for path in video_files if is_plex_episode_filename(path)]
    if plex_ready:
        return FilenameInspection(
            NAMING_MIXED,
            "часть файлов уже в формате Plex, часть требует переименования; смешанный набор не переименовываю автоматически",
            files=tuple(video_files),
            suspicious_files=suspicious,
        )

    parsed: list[tuple[int, str, tuple[int, ...], Path]] = []
    for path in video_files:
        item = _parse_arc_episode(path, series_context=series_context, show_title=show_title)
        if item is None:
            if any(
                _parse_arc_episode(candidate, series_context=series_context, show_title=show_title) is not None
                for candidate in video_files
            ):
                return FilenameInspection(
                    NAMING_MIXED,
                    "часть файлов распознана как серии, часть нет; набор неоднородный",
                    files=tuple(video_files),
                    suspicious_files=suspicious,
                )
            return FilenameInspection(
                NAMING_UNKNOWN_NON_PLEX,
                "не удалось однозначно определить номера серий для автопереименования",
                files=tuple(video_files),
                suspicious_files=suspicious,
            )
        arc, episode_title, parts = item
        parsed.append((arc, episode_title, parts, path))

    if len(video_files) < 2:
        return FilenameInspection(
            NAMING_UNSAFE_ARC,
            "одного файла недостаточно, чтобы безопасно сопоставить его с номером серии сезона",
            files=tuple(video_files),
            suspicious_files=suspicious,
        )
    if not _arc_parts_are_contiguous(parsed):
        return FilenameInspection(
            NAMING_UNSAFE_ARC,
            "в найденной нумерации серий есть пропуски или повторы",
            files=tuple(video_files),
            suspicious_files=suspicious,
        )
    return FilenameInspection(
        NAMING_RENAMABLE_ARC,
        files=tuple(video_files),
        suspicious_files=suspicious,
    )


def build_arc_episode_rename_plan(
    *,
    show_title: str,
    season: int,
    files: list[Path],
    source_root: Path | None = None,
    series_context: bool = False,
) -> RenamePlan | None:
    """Build a Plex rename plan for Russian arc/part episode names.

    Example source pattern:
    ``1. Мягкая лапа смерти (2 сер.) - hdtv1080p.mkv``.
    """
    title = sanitize_filename_part(show_title)
    if season <= 0 or not title:
        return None

    video_files = _video_files(files)
    if len(video_files) < 2:
        return None
    if all(is_plex_episode_filename(path) for path in video_files):
        return None

    parsed: list[tuple[int, str, tuple[int, ...], Path]] = []
    for path in video_files:
        item = _parse_arc_episode(path, series_context=series_context, show_title=title)
        if item is None:
            return None
        arc, episode_title, parts = item
        parsed.append((arc, episode_title, parts, path))

    if not _arc_parts_are_contiguous(parsed):
        return None

    parent = _single_parent(video_files)
    if parent is None:
        return None
    root = source_root or parent
    target_dir = root / f"Season {season:02d}"

    items: list[RenameItem] = []
    episode_number = 1
    for arc, episode_title, parts, source_path in sorted(
        parsed,
        key=lambda item: (item[0], min(item[2]), str(item[3]).casefold()),
    ):
        episode_numbers = tuple(range(episode_number, episode_number + len(parts)))
        episode_number += len(parts)
        first_episode = episode_numbers[0]
        last_episode = episode_numbers[-1]
        episode_marker = (
            f"S{season:02d}E{first_episode:02d}"
            if first_episode == last_episode
            else f"S{season:02d}E{first_episode:02d}-E{last_episode:02d}"
        )
        episode_title_suffix = (
            f" - {sanitize_filename_part(episode_title)}"
            if episode_title
            else ""
        )
        target_name = f"{title} - {episode_marker}{episode_title_suffix}{source_path.suffix}"
        items.append(
            RenameItem(
                source_path=source_path,
                target_path=target_dir / target_name,
                episode_number=first_episode,
                episode_title=episode_title,
                arc_number=arc,
                part_number=parts[0],
                episode_numbers=episode_numbers,
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
