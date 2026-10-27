"""The rover's view of the camera: read ``cam_faces.json``, ask the camera to look.

This package reads JSON and never imports the dashboard or ``cam`` (see
``ARCHITECTURE.md``): ``cam_faces.json`` is written by ``cam/facefile.py`` in the
web process, and ``cam_watch.json`` is how this side wakes the camera for a
moment. The file is treated as untrusted input: names are re-validated, text is
re-sanitised, and the thresholds it reports are clamped, so a corrupt or
hostile file cannot make the rover say something it was never meant to.

``FacesView.state`` is the honest part. ``camera_offline`` / ``no_data`` /
``idle`` all mean *blind*, which is not the same as "nobody is there".
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ._constants import (
    CAM_FACES_FILENAME,
    CAM_WATCH_FILENAME,
    FACES_FRESH_S,
    LOOKUP_SETTLE_S,
    LOOKUP_TIMEOUT_S,
    LOOKUP_WATCH_TTL_S,
)

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$')
_CTRL = re.compile(r'[\x00-\x1f\x7f-\x9f  ]+')
GREETING_MAX = 80
MIN_SIM_RANGE = (0.40, 0.80)
COOLDOWN_RANGE = (30.0, 3600.0)
_NUMBER_WORDS = ('no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
                 'nine', 'ten')


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def clean_text(value, cap: int = GREETING_MAX) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(_CTRL.sub(' ', value).split())[:cap]


def _num(value, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return max(lo, min(hi, float(value)))


@dataclass(frozen=True)
class FacesView:
    state: str = 'no_data'      # running | idle | camera_offline | no_data
    updated_at: float = 0.0
    faces: tuple = ()           # known people: name, sim, stable, greet, greeting
    unknown: int = 0
    roster: tuple = ()
    greeter: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """The camera is looking and the data is fresh: absence means absence."""
        return self.state == 'running'


def parse_faces(raw, now: float, fresh_s: float = FACES_FRESH_S) -> FacesView:
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return FacesView()
    ts = raw.get('updated_at')
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or abs(now - ts) > fresh_s:
        return FacesView()                          # the dashboard stopped publishing
    state = raw.get('state')
    if state not in ('running', 'idle', 'camera_offline'):
        return FacesView()
    age = raw.get('data_age_ms')
    if state == 'running' and (not isinstance(age, (int, float)) or age > 1000 * fresh_s):
        state = 'idle'                              # analysis itself has gone stale
    faces = []
    for f in raw.get('faces') or []:
        if not isinstance(f, dict) or not valid_name(f.get('name')):
            continue
        sim = f.get('sim')
        faces.append({'name': f['name'],
                      'sim': float(sim) if isinstance(sim, (int, float)) and not isinstance(sim, bool) else 0.0,
                      'stable': f.get('stable') is True,
                      'greet': f.get('greet') is True,
                      'greeting': clean_text(f.get('greeting'))})
    g = raw.get('greeter') if isinstance(raw.get('greeter'), dict) else {}
    unknown = raw.get('unknown')
    return FacesView(
        state=state, updated_at=float(ts), faces=tuple(faces),
        unknown=int(unknown) if isinstance(unknown, int) and not isinstance(unknown, bool) and unknown >= 0 else 0,
        roster=tuple(n for n in (raw.get('roster') or []) if valid_name(n)),
        greeter={'enabled': g.get('enabled') is True,
                 'min_sim': _num(g.get('min_sim'), 0.5, *MIN_SIM_RANGE),
                 'cooldown_s': _num(g.get('cooldown_s'), 300.0, *COOLDOWN_RANGE),
                 'greet_unknown': g.get('greet_unknown') is True})


def read_faces(telemetry_dir: str, now: float) -> FacesView:
    try:
        with open(os.path.join(telemetry_dir, CAM_FACES_FILENAME), encoding='utf-8') as fh:
            return parse_faces(json.load(fh), now)
    except (OSError, ValueError):
        return FacesView()


def _join(names) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def _people(n: int) -> str:
    if n == 1:
        return 'one person'
    return f'{_NUMBER_WORDS[n] if n < len(_NUMBER_WORDS) else n} people'


def describe(view: FacesView):
    """(persona event, slots) saying who is in view, honestly."""
    if not view.ok:
        return 'who_blind', {}
    names = sorted({f['name'] for f in view.faces if f['stable']})
    others = view.unknown + sum(1 for f in view.faces if not f['stable'])
    if not names and not others:
        return 'who_none', {}
    if not names:
        return 'who_strangers', {'others': _people(others)}
    if not others:
        return 'who_known', {'names': _join(names)}
    return 'who_mixed', {'names': _join(names), 'others': _people(others)}


class CamLookup:
    """Ask the camera to look, wait for a fresh answer, report what it saw."""

    def __init__(self, telemetry_dir: str, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout_s: float = LOOKUP_TIMEOUT_S, settle_s: float = LOOKUP_SETTLE_S,
                 poll_s: float = 0.25) -> None:
        self._dir = telemetry_dir
        self._clock, self._sleep = clock, sleep
        self._timeout_s, self._settle_s, self._poll_s = timeout_s, settle_s, poll_s

    def view(self) -> FacesView:
        return read_faces(self._dir, self._clock())

    def _ask(self, reason: str) -> None:
        path = os.path.join(self._dir, CAM_WATCH_FILENAME)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'id': secrets.token_hex(6), 'ts': self._clock(),
                       'ttl_s': LOOKUP_WATCH_TTL_S, 'reason': reason}, fh)
        os.replace(tmp, path)

    def look(self, reason: str, want_name: Optional[str] = None) -> FacesView:
        """Wake the camera, then return the first *settled* view after the request.

        Settled means analysed after we asked, at least ``settle_s`` elapsed (so
        "nobody" is not claimed from a single early frame), and every known face
        stable. Whatever the camera has when time runs out is returned as is,
        and the caller's wording reflects what is unsure.
        """
        try:
            self._ask(reason)
        except OSError as exc:
            log.warning('could not write %s: %s', CAM_WATCH_FILENAME, exc)
        start = self._clock()
        view = self.view()
        while self._clock() - start < self._timeout_s:
            self._sleep(self._poll_s)
            view = self.view()
            elapsed = self._clock() - start
            if view.state in ('camera_offline', 'no_data') and elapsed >= 1.0:
                return view                           # blind; waiting will not help
            if not view.ok or view.updated_at < start:
                continue
            if want_name is not None and any(
                    f['name'] == want_name and f['stable'] for f in view.faces):
                return view
            if elapsed >= self._settle_s and all(f['stable'] for f in view.faces) \
                    and want_name is None:
                return view
        return view


__all__ = ['CamLookup', 'FacesView', 'describe', 'parse_faces', 'read_faces',
           'valid_name', 'clean_text']
