"""``cam_faces.json``: who the camera sees, for the voice process.

The voice stack is a separate process and reads JSON, never project code
(``voice/ARCHITECTURE.md``), so this is the whole interface from the camera to
the rover's speech. Same shape as ``lidar/publisher.py``: a thread rewrites the
file atomically; readers decide for themselves whether it is fresh.

It carries only what speech needs: names, a stability flag and the greeting
text of people who opted in. No boxes, images, embeddings or notes.

``state`` is deliberately three-way. ``camera_offline`` means the rover is
blind; ``idle`` means nobody has asked the camera to look. Neither is "nobody
there", and the voice side must not say so.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from cam._atomic import write_json_private

log = logging.getLogger('agv.cam')

SCHEMA = 1
FRESH_CAMERA_MS = 2000      # same freshness bar the dashboard strip uses
ANALYSIS_STALE_S = 3.0
_WARN_EVERY_S = 10.0


class FacesFilePublisher:
    def __init__(self, analytics, camera, path: str, interval_s: float = 0.5,
                 clock=time.time) -> None:
        self._analytics = analytics
        self._camera = camera
        self._path = path
        self._interval_s = interval_s
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_warn = 0.0

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'FacesFilePublisher':
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name='cam-faces-file', daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # ── payload ──────────────────────────────────────────────────────
    def build_payload(self) -> dict:
        now = self._clock()
        snap = self._analytics.greet_snapshot()
        st = self._analytics.settings()
        cam = self._camera.status()
        age_ms = cam.get('age_ms')
        cam_ok = bool(cam.get('connected')) and age_ms is not None and age_ms < FRESH_CAMERA_MS
        analysed = snap['ts'] is not None and now - snap['ts'] <= ANALYSIS_STALE_S
        state = 'camera_offline' if not cam_ok else ('running' if analysed else 'idle')

        faces = []
        if state == 'running':
            profiles = getattr(self._analytics, 'profiles', None)
            for f in snap['faces']:
                if not f.get('name'):
                    continue
                prof = profiles.get(f['name']) if profiles is not None else {}
                faces.append({'track': f['track'], 'name': f['name'], 'sim': f['sim'],
                              'stable': bool(f.get('stable')),
                              'greet': bool(prof.get('greet')),
                              'greeting': prof.get('greeting', '')})
        return {
            'schema': SCHEMA, 'updated_at': now, 'pid': os.getpid(), 'state': state,
            'data_age_ms': int(1000 * (now - snap['ts'])) if snap['ts'] else None,
            'greeter': {'enabled': bool(st['greeter_enabled']), 'min_sim': st['greet_min_sim'],
                        'cooldown_s': st['greet_cooldown_s'],
                        'greet_unknown': bool(st['greet_unknown'])},
            'faces': faces,
            'unknown': snap['unknown'] if state == 'running' else 0,
            'roster': [p['name'] for p in self._analytics.store.people()],
        }

    def publish_once(self) -> dict:
        payload = self.build_payload()
        write_json_private(self._path, payload)
        return payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_once()
            except Exception as exc:                 # noqa: BLE001
                now = time.monotonic()
                if now - self._last_warn >= _WARN_EVERY_S:
                    self._last_warn = now
                    log.warning('cam_faces.json writer failed: %s', exc)
            self._stop.wait(self._interval_s)


__all__ = ['FacesFilePublisher', 'SCHEMA']
