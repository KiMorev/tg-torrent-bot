"""YouTube download helpers for PlexLoader.

This module is intentionally free of Telegram/Plex state. It parses supported
YouTube URLs, selects no-transcode MP4/H.264/AAC formats, plans Plex-friendly
paths, and runs yt-dlp with optional progress callbacks.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
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


class YouTubeDownloadError(Exception):
    """Base class for user-facing YouTube download errors."""


class YouTubeUnsupportedError(YouTubeDownloadError):
    """The URL/media is outside the MVP support boundary."""


class YouTubeToolMissingError(YouTubeDownloadError):
    """yt-dlp or ffmpeg is unavailable in the runtime image."""


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
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "webpage_url": canonical_url,
        "thumbnail": info.get("thumbnail"),
        "selected_quality": choice.label,
        "selected_format_id": choice.format_id,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _download_thumbnail_as_jpeg(url: str, poster_path: Path, fanart_path: Path) -> None:
    if not url:
        return
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "jpeg" in content_type or "jpg" in content_type:
        poster_path.write_bytes(response.content)
        fanart_path.write_bytes(response.content)
        return

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_path = Path(tmp_dir) / "thumb"
        raw_path.write_bytes(response.content)
        subprocess.run(
            [ffmpeg, "-y", "-i", str(raw_path), str(poster_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if poster_path.exists():
        fanart_path.write_bytes(poster_path.read_bytes())


def write_sidecars(info: dict[str, Any], choice: YouTubeFormatChoice, canonical_url: str, plan: YouTubePathPlan) -> None:
    _write_json(plan.info_path, safe_info_json(info, choice, canonical_url))
    try:
        _download_thumbnail_as_jpeg(str(info.get("thumbnail") or ""), plan.poster_path, plan.fanart_path)
    except Exception:
        # Artwork is nice-to-have; the downloaded video is the MVP result.
        pass


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


def _apply_audio_language(video_path: Path, audio_language: str | None) -> str | None:
    language = _normalize_audio_language(audio_language)
    if not language:
        return None
    tmp_path = video_path.with_name(f".{video_path.stem}.audio-lang.tmp{video_path.suffix}")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-metadata:s:a:0",
                f"language={language}",
                str(tmp_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp_path.replace(video_path)
    except subprocess.CalledProcessError as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise YouTubeDownloadError("Не удалось проставить язык аудиодорожки через ffmpeg.") from exc
    return language


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
    try:
        with yt_dlp.YoutubeDL({
            "format": choice.format_id,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "progress_hooks": [_hook],
            "overwrites": True,
        }) as ydl:
            ydl.download([canonical_url])
    except Exception as exc:
        raise YouTubeDownloadError(f"Не удалось скачать видео: {exc}") from exc

    final_path = _find_final_video(plan)
    if not final_path.exists():
        raise YouTubeDownloadError("yt-dlp завершился, но итоговый mp4 не найден.")

    if final_path != plan.video_path:
        final_path.replace(plan.video_path)
        final_path = plan.video_path

    applied_audio_language = _apply_audio_language(final_path, audio_language)
    write_sidecars(info, choice, canonical_url, plan)
    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration_seconds": info.get("duration"),
        "quality": choice.label,
        "format_id": choice.format_id,
        "canonical_url": canonical_url,
        "file_path": str(final_path),
        "file_size": final_path.stat().st_size,
        "item_dir": str(plan.item_dir),
        "audio_language": applied_audio_language,
    }
