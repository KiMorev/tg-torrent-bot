from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix=".pytest-runtime-", dir=_PROJECT_ROOT))

# bot.py reads settings at import time. Keep test state out of /tmp so local
# permission issues or stale files cannot affect the suite.
os.environ["TMP_DIR"] = str(_TEST_RUNTIME_DIR / "tmp")
os.environ["STATE_DIR"] = str(_TEST_RUNTIME_DIR / "state")


def pytest_sessionfinish(session, exitstatus) -> None:
    shutil.rmtree(_TEST_RUNTIME_DIR, ignore_errors=True)
