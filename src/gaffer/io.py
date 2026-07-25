"""Small IO helpers shared across the pipeline's artifact writers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON atomically: serialise to a temp file in the same directory,
    fsync, then os.replace() into place.

    A crash mid-write can't leave a half-written or empty artifact for the
    deployed site to serve — which matters now the refresh runs unattended on a
    schedule. os.replace is atomic on the same filesystem.
    """
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
