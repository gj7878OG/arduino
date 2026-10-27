"""Per-person info shown on the dashboard and used by the rover's greeting.

Kept in its own JSON file, never in ``faces_db.pkl``: that pickle is the format
``face_recognition/face_recognize.py`` shares, and a profile edit must not be
able to corrupt the embeddings.

Everything here is operator-typed text that ends up (a) rendered in the
dashboard and (b) for ``greeting`` only, spoken aloud. So it is capped and
stripped of control characters on the way in, and a person's profile is
private by default: ``greet`` is False until someone turns it on.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Dict

from cam._atomic import write_json_private

log = logging.getLogger('agv.cam')

CAPS = {'role': 40, 'greeting': 80, 'notes': 300}
_CTRL = re.compile(r'[\x00-\x1f\x7f-\x9f  ]+')


def clean_text(value, cap: int) -> str:
    """Single-line, control-free, length-capped text; '' for non-strings."""
    if not isinstance(value, str):
        return ''
    return ' '.join(_CTRL.sub(' ', value).split())[:cap]


def _blank() -> dict:
    return {'role': '', 'greeting': '', 'notes': '', 'greet': False, 'updated_at': None}


class ProfileStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding='utf-8') as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:                  # noqa: BLE001
            log.warning('cam: profiles unreadable (%s); starting empty', exc)
            return
        if not isinstance(raw, dict):
            return
        for name, prof in raw.items():
            if isinstance(name, str) and isinstance(prof, dict):
                self._data[name] = self._normalise(prof)

    @staticmethod
    def _normalise(prof: dict) -> dict:
        out = _blank()
        for key, cap in CAPS.items():
            out[key] = clean_text(prof.get(key), cap)
        out['greet'] = prof.get('greet') is True
        ts = prof.get('updated_at')
        out['updated_at'] = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
        return out

    def _save(self) -> None:
        write_json_private(self._path, self._data)       # 0600: notes about people

    def get(self, name: str) -> dict:
        with self._lock:
            return dict(self._data.get(name) or _blank())

    def set(self, name: str, fields: dict) -> dict:
        """Merge the known fields in ``fields`` onto the person's profile."""
        with self._lock:
            cur = dict(self._data.get(name) or _blank())
            for key, cap in CAPS.items():
                if key in fields:
                    cur[key] = clean_text(fields[key], cap)
            if 'greet' in fields:
                if not isinstance(fields['greet'], bool):
                    raise ValueError('greet must be true or false')
                cur['greet'] = fields['greet']
            cur['updated_at'] = time.time()
            self._data[name] = cur
            self._save()
            return dict(cur)

    def remove(self, name: str) -> None:
        with self._lock:
            if self._data.pop(name, None) is not None:
                self._save()

    def prune(self, keep) -> None:
        """Drop profiles whose person no longer exists (e.g. removed via the CLI)."""
        keep = set(keep)
        with self._lock:
            gone = [n for n in self._data if n not in keep]
            for n in gone:
                del self._data[n]
            if gone:
                self._save()


def public_view(prof: dict, signed_in: bool) -> dict:
    """What the API shows: notes are operator-only."""
    out = dict(prof)
    if not signed_in:
        out['notes'] = ''
    return out


__all__ = ['ProfileStore', 'clean_text', 'public_view', 'CAPS']
