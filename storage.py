"""Disk-usage helper for the «📀 Хранилище» block in /admin.

The bot runs inside a Docker container that has no filesystem visibility
into the NAS video volume by default. To enable this feature the admin
bind-mounts `/volume1/video` (or wherever movies live) read-only into the
container at `/storage`. We use `shutil.disk_usage()` on that path.

The feature gracefully degrades: if the `/storage` mount isn't present
(`Path.exists()` is False), `get_storage_info()` returns None and the
admin panel simply omits the section. No alerts fire either.

Why a hardcoded path instead of an env var:
- The mount destination inside the container is purely an internal
  convention. The admin only needs to point the LEFT side of the bind-mount
  at whatever NAS path holds movies; the right side (`/storage`) never
  needs to change.
- Avoids confusion with `DS_DESTINATION`, which is a DSM-relative path
  passed to Download Station API — a completely different layer.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


# Conventional mount point inside the container. The admin bind-mounts the
# NAS movie volume here in compose.yaml.
STORAGE_MOUNT_PATH = "/storage"


@dataclass
class StorageInfo:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float


def get_storage_info(path: str = STORAGE_MOUNT_PATH) -> StorageInfo | None:
    """Return disk usage for the given mount, or None if the path is missing
    or unreadable. None is the canonical "feature disabled" signal.
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        total, used, free = shutil.disk_usage(p)
    except (OSError, ValueError):
        return None
    pct = (used / total * 100) if total > 0 else 0.0
    return StorageInfo(total_bytes=total, used_bytes=used, free_bytes=free, used_percent=pct)


def format_bytes(num: int) -> str:
    """Human-readable size: 1.4 TB, 256 GB, 800 MB, 4.5 KB, 320 B.

    Uses decimal (1000-based) units to match how NAS vendors report storage
    on the box — DSM Storage Manager shows TB, not TiB.
    """
    if num is None or num < 0:
        return "?"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000 or unit == "PB":
            if unit == "B":
                return f"{int(n)} {unit}"
            # 2 значащих знака для < 10, иначе 1
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.0f} PB"  # unreachable but mypy-friendly
