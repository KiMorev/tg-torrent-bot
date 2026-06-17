"""YouTube download helpers for PlexLoader.

This module is intentionally free of Telegram/Plex state. It parses supported
YouTube URLs, selects no-transcode MP4/H.264/AAC formats, plans Plex-friendly
paths, and runs yt-dlp with optional progress callbacks.
"""

from __future__ import annotations

import json
import re
import hashlib
import shutil
import struct
import subprocess
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import requests


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
MAX_FILENAME_CHARS = 140
DEFAULT_MIN_HEIGHT = 640
CHANNEL_POSTER_WIDTH = 1000
CHANNEL_POSTER_HEIGHT = 1500
CHANNEL_POSTER_AVATAR_SIZE = 760
CHANNEL_POSTER_MAT_SIZE = 820
CHANNEL_POSTER_PLATE_SIZE = 860
DOWNLOAD_RETRY_DELAYS_SECONDS = (5.0, 15.0)
TRANSIENT_DOWNLOAD_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed",
    "temporary failure",
    "network is unreachable",
    "name or service not known",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "server returned 500",
    "server returned 502",
    "server returned 503",
    "server returned 504",
)


class YouTubeDownloadError(Exception):
    """Base class for user-facing YouTube download errors."""


class YouTubeUnsupportedError(YouTubeDownloadError):
    """The URL/media is outside the MVP support boundary."""


class YouTubeToolMissingError(YouTubeDownloadError):
    """yt-dlp or ffmpeg is unavailable in the runtime image."""


def _is_transient_download_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in TRANSIENT_DOWNLOAD_ERROR_MARKERS)


def _download_retry_reason(exc: Exception) -> str:
    text = str(exc or "").lower()
    if "timed out" in text or "timeout" in text:
        return "сетевой таймаут YouTube"
    if "http error 5" in text or "server returned 5" in text:
        return "временная ошибка YouTube"
    return "временный сетевой сбой YouTube"


def _friendly_download_error(exc: Exception) -> str:
    reason = _download_retry_reason(exc)
    return f"Не удалось скачать видео: {reason}. Повторите позже."


def _download_with_retries(
    run_download: Callable[[], None],
    *,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> None:
    max_attempts = len(DOWNLOAD_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            run_download()
            return
        except Exception as exc:
            if not _is_transient_download_error(exc):
                raise YouTubeDownloadError(f"Не удалось скачать видео: {exc}") from exc
            if attempt >= max_attempts:
                raise YouTubeDownloadError(_friendly_download_error(exc)) from exc
            if progress_hook:
                progress_hook({
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "reason": _download_retry_reason(exc),
                })
            sleep_func(DOWNLOAD_RETRY_DELAYS_SECONDS[attempt - 1])


def _cleanup_failed_download(plan: "YouTubePathPlan", *, preserve_final: bool) -> None:
    if not plan.item_dir.exists():
        return
    removable_exact = {plan.video_path}
    if not preserve_final:
        removable_exact.update({plan.poster_path, plan.fanart_path, plan.info_path})
    stem = plan.video_path.stem
    for path in plan.item_dir.iterdir():
        if not path.is_file():
            continue
        if preserve_final and path == plan.video_path:
            continue
        if path in removable_exact or path.name.startswith(f"{stem}."):
            try:
                path.unlink()
            except OSError:
                pass
    try:
        plan.item_dir.rmdir()
    except OSError:
        pass


@dataclass(frozen=True)
class YouTubeFormatChoice:
    height: int
    format_id: str
    label: str
    filesize: int | None = None


@dataclass(frozen=True)
class YouTubePathPlan:
    item_dir: Path
    video_path: Path
    poster_path: Path
    fanart_path: Path
    info_path: Path


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ModuleNotFoundError as exc:
        raise YouTubeToolMissingError("yt-dlp не установлен в контейнере.") from exc
    return yt_dlp


def extract_youtube_video_id(url: str) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "youtube-nocookie.com"} or host.endswith(".youtube.com"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts[:1] == ["watch"]:
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif path_parts and path_parts[0] in {"shorts", "embed", "live"} and len(path_parts) >= 2:
            candidate = path_parts[1]

    return candidate if VIDEO_ID_RE.match(candidate or "") else None


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def find_youtube_url(text: str) -> str | None:
    for match in URL_RE.findall(str(text or "")):
        video_id = extract_youtube_video_id(match.rstrip(".,;)]"))
        if video_id:
            return canonical_watch_url(video_id)
    return None


def _has_playlist_only_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    query = parse_qs(parsed.query)
    return bool(query.get("list")) and not query.get("v")


def _is_h264(vcodec: object) -> bool:
    text = str(vcodec or "").lower()
    return text.startswith("avc1") or text.startswith("h264")


def _is_aac(acodec: object) -> bool:
    text = str(acodec or "").lower()
    return text.startswith("mp4a") or text.startswith("aac")


def _format_size_hint(fmt: dict[str, Any]) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _format_sort_key(fmt: dict[str, Any]) -> tuple[int, float, int]:
    height = int(fmt.get("height") or 0)
    bitrate = float(fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0)
    size = int(_format_size_hint(fmt) or 0)
    return height, bitrate, size


def display_quality_label(height: int) -> str:
    value = int(height or 0)
    if value >= 2160:
        return "2160p"
    if value >= 1440:
        return "1440p"
    if value >= 960:
        return "1080p"
    if value >= 640:
        return "720p"
    if value >= 426:
        return "480p"
    if value >= 320:
        return "360p"
    if value >= 214:
        return "240p"
    return "144p"


def _is_progressive_mp4(fmt: dict[str, Any]) -> bool:
    return (
        fmt.get("ext") == "mp4"
        and int(fmt.get("height") or 0) > 0
        and _is_h264(fmt.get("vcodec"))
        and _is_aac(fmt.get("acodec"))
    )


def _is_video_only_mp4(fmt: dict[str, Any]) -> bool:
    return (
        fmt.get("ext") == "mp4"
        and str(fmt.get("acodec") or "").lower() == "none"
        and int(fmt.get("height") or 0) > 0
        and _is_h264(fmt.get("vcodec"))
    )


def _is_audio_only_m4a(fmt: dict[str, Any]) -> bool:
    return (
        fmt.get("ext") in {"m4a", "mp4"}
        and str(fmt.get("vcodec") or "").lower() == "none"
        and _is_aac(fmt.get("acodec"))
    )


def select_format(info: dict[str, Any], max_height: int) -> YouTubeFormatChoice:
    formats = [fmt for fmt in info.get("formats") or [] if isinstance(fmt, dict)]
    target = max(1, int(max_height or 0))

    candidates: list[tuple[tuple[int, float, int], YouTubeFormatChoice]] = []
    for fmt in formats:
        if not (_is_progressive_mp4(fmt) and int(fmt.get("height") or 0) <= target):
            continue
        height = int(fmt.get("height") or 0)
        fmt_id = str(fmt.get("format_id") or "")
        if fmt_id:
            candidates.append((
                _format_sort_key(fmt),
                YouTubeFormatChoice(
                    height=height,
                    format_id=fmt_id,
                    label=display_quality_label(height),
                    filesize=_format_size_hint(fmt),
                ),
            ))

    videos = [
        fmt for fmt in formats
        if _is_video_only_mp4(fmt) and int(fmt.get("height") or 0) <= target
    ]
    audios = [fmt for fmt in formats if _is_audio_only_m4a(fmt)]
    if audios:
        audio = max(audios, key=_format_sort_key)
    else:
        audio = None
    if audio:
        audio_id = str(audio.get("format_id") or "")
    else:
        audio_id = ""
    if audio and audio_id:
        for video in videos:
            video_id = str(video.get("format_id") or "")
            if not video_id:
                continue
            height = int(video.get("height") or 0)
            total_size = None
            video_size = _format_size_hint(video)
            audio_size = _format_size_hint(audio)
            if video_size and audio_size:
                total_size = video_size + audio_size
            candidates.append((
                _format_sort_key(video),
                YouTubeFormatChoice(
                    height=height,
                    format_id=f"{video_id}+{audio_id}",
                    label=display_quality_label(height),
                    filesize=total_size,
                ),
            ))

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    if videos and audios:
        video = max(videos, key=_format_sort_key)
        audio = max(audios, key=_format_sort_key)
        video_id = str(video.get("format_id") or "")
        audio_id = str(audio.get("format_id") or "")
        if video_id and audio_id:
            height = int(video.get("height") or 0)
            total_size = None
            video_size = _format_size_hint(video)
            audio_size = _format_size_hint(audio)
            if video_size and audio_size:
                total_size = video_size + audio_size
            return YouTubeFormatChoice(
                height=height,
                format_id=f"{video_id}+{audio_id}",
                label=display_quality_label(height),
                filesize=total_size,
            )

    raise YouTubeUnsupportedError(
        "Не нашёл совместимый MP4/H.264/AAC формат без перекодирования."
    )


def compatible_quality_options(
    info: dict[str, Any],
    max_height: int = 1080,
    min_height: int = DEFAULT_MIN_HEIGHT,
) -> list[YouTubeFormatChoice]:
    formats = [fmt for fmt in info.get("formats") or [] if isinstance(fmt, dict)]
    heights = sorted(
        {
            int(fmt.get("height") or 0)
            for fmt in formats
            if int(fmt.get("height") or 0) >= int(min_height or 0)
            and int(fmt.get("height") or 0) <= max_height
        },
        reverse=True,
    )
    choices: dict[int, YouTubeFormatChoice] = {}
    seen_labels: set[str] = set()
    for height in heights:
        try:
            choice = select_format(info, height)
        except YouTubeUnsupportedError:
            continue
        if choice.height == height:
            if choice.label in seen_labels:
                continue
            seen_labels.add(choice.label)
            choices[height] = choice
    return [choices[h] for h in sorted(choices.keys(), reverse=True)]


def extract_metadata(url: str) -> dict[str, Any]:
    if _has_playlist_only_url(url):
        raise YouTubeUnsupportedError("Плейлисты в первой версии не поддерживаются.")
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise YouTubeUnsupportedError("Поддерживаются только одиночные YouTube-ссылки.")

    yt_dlp = _import_ytdlp()
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }) as ydl:
            info = ydl.extract_info(canonical_watch_url(video_id), download=False)
    except YouTubeDownloadError:
        raise
    except Exception as exc:
        raise YouTubeDownloadError(f"Не удалось получить данные YouTube: {exc}") from exc

    if not isinstance(info, dict):
        raise YouTubeDownloadError("YouTube вернул неожиданный ответ.")
    if info.get("_type") == "playlist":
        raise YouTubeUnsupportedError("Плейлисты в первой версии не поддерживаются.")
    if info.get("is_live") or str(info.get("live_status") or "").lower() in {"is_live", "is_upcoming"}:
        raise YouTubeUnsupportedError("Live-трансляции в первой версии не поддерживаются.")
    return info


def _safe_component(value: object, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if len(text) > MAX_FILENAME_CHARS:
        text = text[:MAX_FILENAME_CHARS].rstrip(" .")
    return text or fallback


def _upload_date(info: dict[str, Any]) -> str:
    raw = str(info.get("upload_date") or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_path_plan(info: dict[str, Any], output_root: Path) -> YouTubePathPlan:
    video_id = str(info.get("id") or extract_youtube_video_id(str(info.get("webpage_url") or "")) or "").strip()
    if not VIDEO_ID_RE.match(video_id):
        raise YouTubeDownloadError("Не удалось определить YouTube video_id.")

    title = _safe_component(info.get("title"), video_id)
    channel = _safe_component(info.get("channel") or info.get("uploader"), "YouTube")
    item_dir = Path(output_root) / channel / title
    video_path = item_dir / f"{title}.mp4"
    return YouTubePathPlan(
        item_dir=item_dir,
        video_path=video_path,
        poster_path=item_dir / "poster.jpg",
        fanart_path=item_dir / "fanart.jpg",
        info_path=item_dir / "info.json",
    )


def safe_info_json(info: dict[str, Any], choice: YouTubeFormatChoice, canonical_url: str) -> dict[str, Any]:
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id") or info.get("uploader_id"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "webpage_url": canonical_url,
        "thumbnail": info.get("thumbnail"),
        "channel_thumbnail": _direct_channel_thumbnail(info),
        "selected_quality": choice.label,
        "selected_format_id": choice.format_id,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _download_image_as_jpeg(url: str, target_path: Path) -> None:
    if not url:
        return
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "jpeg" in content_type or "jpg" in content_type:
        target_path.write_bytes(response.content)
        return

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_path = Path(tmp_dir) / "thumb"
        raw_path.write_bytes(response.content)
        subprocess.run(
            [ffmpeg, "-y", "-i", str(raw_path), str(target_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _download_thumbnail_as_jpeg(url: str, poster_path: Path, fanart_path: Path) -> None:
    _download_image_as_jpeg(url, poster_path)
    if poster_path.exists():
        fanart_path.write_bytes(poster_path.read_bytes())


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = result.stdout.strip().splitlines()[0]
        width, height = raw.split("x", 1)
        return int(width), int(height)
    except Exception:
        return None


def _image_is_portrait_poster(path: Path) -> bool:
    dimensions = _image_dimensions(path)
    if dimensions is None:
        return True
    width, height = dimensions
    if width <= 0 or height <= 0:
        return False
    ratio = width / height
    return height > width and abs(ratio - (2 / 3)) <= 0.08


def _image_average_rgb(path: Path) -> tuple[int, int, int] | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                "scale=1:1",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return None
    if len(result.stdout) < 3:
        return None
    return result.stdout[0], result.stdout[1], result.stdout[2]


def _channel_poster_contrast_colors(path: Path) -> tuple[str, str, str]:
    rgb = _image_average_rgb(path)
    if rgb is None:
        return "0x111820", "white", "-0.34"

    red, green, blue = rgb
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    if luminance >= 128:
        return "0x111820", "white", "-0.38"
    return "white", "0x111820", "-0.22"


def _write_avatar_portrait_poster(raw_path: Path, target_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")

    mat_color, border_color, background_brightness = _channel_poster_contrast_colors(raw_path)
    filter_complex = (
        f"[0:v]scale={CHANNEL_POSTER_WIDTH}:{CHANNEL_POSTER_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={CHANNEL_POSTER_WIDTH}:{CHANNEL_POSTER_HEIGHT},"
        f"boxblur=40:1,eq=brightness={background_brightness}:saturation=0.65[bg];"
        f"[0:v]scale={CHANNEL_POSTER_AVATAR_SIZE}:{CHANNEL_POSTER_AVATAR_SIZE}:force_original_aspect_ratio=decrease,"
        f"pad={CHANNEL_POSTER_MAT_SIZE}:{CHANNEL_POSTER_MAT_SIZE}:(ow-iw)/2:(oh-ih)/2:color={mat_color},"
        f"pad={CHANNEL_POSTER_PLATE_SIZE}:{CHANNEL_POSTER_PLATE_SIZE}:(ow-iw)/2:(oh-ih)/2:color={border_color}[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(raw_path),
            "-filter_complex",
            filter_complex,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(target_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def _download_channel_avatar_poster(url: str, target_path: Path) -> bool:
    if not url:
        return False
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_path = Path(tmp_dir) / "channel-avatar"
        raw_path.write_bytes(response.content)
        _write_avatar_portrait_poster(raw_path, target_path)
    return target_path.exists()


_CYRILLIC_TO_LATIN = str.maketrans({
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "ZH", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "TS", "Ч": "CH", "Ш": "SH", "Щ": "SCH",
    "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "YU", "Я": "YA",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})

_FONT_5X7 = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
}


def _direct_channel_thumbnail(info: dict[str, Any]) -> str:
    for key in ("channel_thumbnail", "uploader_thumbnail", "channel_avatar", "uploader_avatar", "avatar"):
        value = str(info.get(key) or "").strip()
        if value.startswith("http"):
            return value
    return ""


def _channel_page_url(info: dict[str, Any]) -> str:
    for key in ("channel_url", "uploader_url"):
        value = str(info.get(key) or "").strip()
        if value.startswith("http"):
            return value
    channel_id = str(info.get("channel_id") or info.get("uploader_id") or "").strip()
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}"
    return ""


def _decode_jsonish_url(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace("\\u0026", "&").replace("\\/", "/")


def _extract_channel_avatar_from_page(channel_url: str) -> str:
    if not channel_url:
        return ""
    response = requests.get(
        channel_url,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    html = response.text
    for meta_tag in re.findall(r"<meta\b[^>]*>", html, flags=re.IGNORECASE):
        if not re.search(r'(?:property|name)=["\'](?:og:image|twitter:image)["\']', meta_tag, flags=re.IGNORECASE):
            continue
        content_match = re.search(r'content=["\']([^"\']+)["\']', meta_tag, flags=re.IGNORECASE)
        if content_match:
            return content_match.group(1).replace("&amp;", "&")

    for avatar_block in re.findall(r'"avatar"\s*:\s*\{\s*"thumbnails"\s*:\s*\[(.*?)\]', html):
        urls = re.findall(r'"url"\s*:\s*"([^"]+)"', avatar_block)
        if urls:
            return _decode_jsonish_url(urls[-1])
    return ""


def _channel_poster_paths(channel_dir: Path) -> tuple[Path, Path]:
    return channel_dir / "channel-poster.jpg", channel_dir / "channel-poster.png"


def _transliterated_ascii(value: str) -> str:
    value = value.translate(_CYRILLIC_TO_LATIN)
    cleaned = []
    for char in value.upper():
        cleaned.append(char if char in _FONT_5X7 else " ")
    text = re.sub(r"\s+", " ", "".join(cleaned)).strip()
    return text or "YOUTUBE"


def _wrap_poster_text(text: str, max_chars: int, max_lines: int = 4) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > max_chars:
            lines.append(word[:max_chars])
            word = word[max_chars:]
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines] or ["YOUTUBE"]


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * ratio) for i in range(3))


def _draw_rect(pixels: bytearray, width: int, height: int, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(color) * (x1 - x0)
    for yy in range(y0, y1):
        start = (yy * width + x0) * 3
        pixels[start:start + len(row)] = row


def _draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    text: str,
    x: int,
    y: int,
    scale: int,
    color: tuple[int, int, int],
) -> int:
    cursor = x
    for char in text:
        glyph = _FONT_5X7.get(char, _FONT_5X7[" "])
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit == "1":
                    _draw_rect(
                        pixels,
                        width,
                        height,
                        cursor + col_index * scale,
                        y + row_index * scale,
                        scale,
                        scale,
                        color,
                    )
        cursor += 6 * scale
    return cursor - x


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    rows = []
    stride = width * 3
    for y in range(height):
        rows.append(b"\x00" + bytes(pixels[y * stride:(y + 1) * stride]))
    raw = b"".join(rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _write_fallback_channel_poster(channel: str, output_path: Path) -> None:
    width, height = 1000, 1500
    digest = hashlib.sha256(channel.encode("utf-8", errors="ignore")).digest()
    base = (35 + digest[0] % 80, 35 + digest[1] % 80, 45 + digest[2] % 80)
    accent = (120 + digest[3] % 100, 120 + digest[4] % 100, 120 + digest[5] % 100)
    dark = (12, 16, 22)
    pixels = bytearray(width * height * 3)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = _blend(base, dark, ratio * 0.8)
        row = bytes(color) * width
        start = y * width * 3
        pixels[start:start + len(row)] = row
    _draw_rect(pixels, width, height, 0, 0, width, 18, accent)
    _draw_rect(pixels, width, height, 0, height - 18, width, 18, accent)

    text = _transliterated_ascii(channel)
    scale = 18
    lines = _wrap_poster_text(text, max_chars=8, max_lines=4)
    if max(len(line) for line in lines) > 8:
        scale = 14
        lines = _wrap_poster_text(text, max_chars=11, max_lines=4)
    line_height = 9 * scale
    block_height = len(lines) * line_height
    y = height // 2 - block_height // 2
    for line in lines:
        text_width = len(line) * 6 * scale - scale
        _draw_text(pixels, width, height, line, (width - text_width) // 2, y, scale, (245, 248, 250))
        y += line_height

    label = "YOUTUBE"
    small_scale = 8
    label_width = len(label) * 6 * small_scale - small_scale
    _draw_text(pixels, width, height, label, (width - label_width) // 2, height - 170, small_scale, (210, 220, 230))
    _write_png(output_path, width, height, pixels)


def write_channel_poster(info: dict[str, Any], plan: YouTubePathPlan) -> Path | None:
    channel = str(info.get("channel") or info.get("uploader") or "YouTube").strip() or "YouTube"
    jpg_path, png_path = _channel_poster_paths(plan.item_dir.parent)
    if jpg_path.exists() and _image_is_portrait_poster(jpg_path):
        return jpg_path
    if png_path.exists() and _image_is_portrait_poster(png_path):
        return png_path

    avatar_url = _direct_channel_thumbnail(info)
    if not avatar_url:
        try:
            avatar_url = _extract_channel_avatar_from_page(_channel_page_url(info))
        except Exception:
            avatar_url = ""
    if avatar_url:
        try:
            _download_channel_avatar_poster(avatar_url, jpg_path)
            if jpg_path.exists():
                return jpg_path
        except Exception:
            pass

    try:
        _write_fallback_channel_poster(channel, png_path)
        return png_path if png_path.exists() else None
    except Exception:
        return None


def write_sidecars(info: dict[str, Any], choice: YouTubeFormatChoice, canonical_url: str, plan: YouTubePathPlan) -> Path | None:
    _write_json(plan.info_path, safe_info_json(info, choice, canonical_url))
    try:
        _download_thumbnail_as_jpeg(str(info.get("thumbnail") or ""), plan.poster_path, plan.fanart_path)
    except Exception:
        # Artwork is nice-to-have; the downloaded video is the MVP result.
        pass
    return write_channel_poster(info, plan)



def _find_final_video(plan: YouTubePathPlan) -> Path:
    if plan.video_path.exists():
        return plan.video_path
    candidates = sorted(plan.item_dir.glob(f"{plan.video_path.stem}.*"))
    for candidate in candidates:
        if candidate.suffix.lower() == ".mp4":
            return candidate
    return plan.video_path


def _normalize_audio_language(audio_language: str | None) -> str | None:
    value = str(audio_language or "und").strip().lower()
    if value in {"", "auto"}:
        return None
    if not re.fullmatch(r"[a-z]{3}", value):
        raise YouTubeDownloadError(
            "YOUTUBE_AUDIO_LANGUAGE должен быть ISO-639-2 кодом из 3 букв, например und или rus."
        )
    return value


def _mp4_metadata_fields(info: dict[str, Any], canonical_url: str) -> dict[str, str]:
    title = str(info.get("title") or "").strip()
    channel = str(info.get("channel") or info.get("uploader") or "").strip()
    fields: dict[str, str] = {}
    if title:
        fields["title"] = title
    if channel:
        fields["artist"] = channel
        fields["album"] = channel
    if info.get("upload_date") or info.get("timestamp") or info.get("release_timestamp"):
        fields["date"] = _upload_date(info)
    if canonical_url:
        fields["comment"] = canonical_url
    return fields


def _apply_mp4_metadata(
    video_path: Path,
    *,
    info: dict[str, Any],
    canonical_url: str,
    audio_language: str | None,
) -> str | None:
    language = _normalize_audio_language(audio_language)
    metadata = _mp4_metadata_fields(info, canonical_url)
    if not language and not metadata:
        return None
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0",
        "-c",
        "copy",
    ]
    for key, value in metadata.items():
        command.extend(["-metadata", f"{key}={value}"])
    if language:
        command.extend(["-metadata:s:a:0", f"language={language}"])
    tmp_path = video_path.with_name(f".{video_path.stem}.metadata.tmp{video_path.suffix}")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        subprocess.run(
            [*command, str(tmp_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp_path.replace(video_path)
    except subprocess.CalledProcessError as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise YouTubeDownloadError("Не удалось проставить MP4 metadata через ffmpeg.") from exc
    return language


def _apply_audio_language(video_path: Path, audio_language: str | None) -> str | None:
    """Compatibility wrapper for tests and callers that only need audio language."""
    language = _normalize_audio_language(audio_language)
    if not language:
        return None
    return _apply_mp4_metadata(
        video_path,
        info={},
        canonical_url="",
        audio_language=audio_language,
    )


def download_video(
    url: str,
    *,
    output_root: Path,
    max_height: int,
    audio_language: str | None = "und",
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not shutil.which("ffmpeg"):
        raise YouTubeToolMissingError("ffmpeg не установлен в контейнере.")
    info = extract_metadata(url)
    video_id = str(info.get("id") or extract_youtube_video_id(url) or "")
    if not VIDEO_ID_RE.match(video_id):
        raise YouTubeDownloadError("Не удалось определить YouTube video_id.")
    canonical_url = canonical_watch_url(video_id)
    choice = select_format(info, max_height)
    plan = build_path_plan(info, Path(output_root))
    plan.item_dir.mkdir(parents=True, exist_ok=True)

    yt_dlp = _import_ytdlp()

    def _hook(payload: dict[str, Any]) -> None:
        if progress_hook:
            progress_hook(payload)

    outtmpl = str(plan.video_path.with_suffix(".%(ext)s"))
    ydl_options = {
        "format": choice.format_id,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "progress_hooks": [_hook],
        "overwrites": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "extractor_retries": 3,
    }

    def _run_download() -> None:
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            ydl.download([canonical_url])

    had_existing_video = plan.video_path.exists()
    try:
        _download_with_retries(_run_download, progress_hook=_hook)
    except YouTubeDownloadError:
        _cleanup_failed_download(plan, preserve_final=had_existing_video)
        raise

    final_path = _find_final_video(plan)
    if not final_path.exists():
        raise YouTubeDownloadError("yt-dlp завершился, но итоговый mp4 не найден.")

    if final_path != plan.video_path:
        final_path.replace(plan.video_path)
        final_path = plan.video_path

    applied_audio_language = _apply_mp4_metadata(
        final_path,
        info=info,
        canonical_url=canonical_url,
        audio_language=audio_language,
    )
    channel_poster_path = write_sidecars(info, choice, canonical_url, plan)
    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id") or info.get("uploader_id"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration_seconds": info.get("duration"),
        "quality": choice.label,
        "format_id": choice.format_id,
        "canonical_url": canonical_url,
        "file_path": str(final_path),
        "file_size": final_path.stat().st_size,
        "item_dir": str(plan.item_dir),
        "channel_poster_path": str(channel_poster_path) if channel_poster_path else "",
        "audio_language": applied_audio_language,
    }
