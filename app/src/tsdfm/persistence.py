"""Room state that survives a restart.

Deliberately a small JSON file rather than a database or Redis: the payload is a few KB,
there is exactly one app instance, and nothing else needs to read it. A file also stays
inspectable with `cat` when something looks wrong.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("persistence")


def load_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable saved state at %s: %s", path, exc)
        return None


def save_state(path: Path, data: dict) -> None:
    # Written to a temp file and renamed, so a crash mid-write can't leave a truncated
    # file that then fails to load on the next start.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w") as tmp:
            json.dump(data, tmp)
        os.replace(tmp_name, path)
    except OSError as exc:
        logger.warning("Could not save room state to %s: %s", path, exc)
