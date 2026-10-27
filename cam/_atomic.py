"""Atomic, owner-only JSON writes for files that name people."""
from __future__ import annotations

import json
import os


def write_json_private(path: str, obj) -> None:
    """Write-then-rename so a reader never sees half a file, at mode 0600.

    ``/tmp/agv-dashboard`` is group-readable, and these files carry names, so the
    mode is set at creation (not chmod'ed afterwards, which leaves a window).
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)
