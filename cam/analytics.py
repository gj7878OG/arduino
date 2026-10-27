"""Face recognition + object detection on the dashboard camera's frames.

Runs on its own thread, fed by ``CamPublisher.latest_frame()`` (depth-1: the
newest frame wins, older ones are dropped). A slow model therefore lowers the
analytics rate, never the video rate; ``/cam/mjpeg`` does not wait on any of
this.

CPU budget (measured on the Jetson): YuNet@416 ~22 ms, SFace embed ~74 ms,
YOLOv8n ~30 ms on the GPU. SFace is the expensive one, so a tracked face is
re-embedded only every ``EMBED_KNOWN_S`` / ``EMBED_UNKNOWN_S`` seconds.

The worker idles unless someone is watching (``touch()``, called by the
detections endpoint) or an enrolment is running, so the AGV's control loop is
not competing with an unwatched dashboard.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from cam.faces import FaceStore, make_thumb, valid_name

log = logging.getLogger('agv.cam')

DEFAULT_THRESHOLD = 0.363              # SFace's recommended cosine cut-off
LIMITS = {
    'threshold': {'min': 0.20, 'max': 0.80, 'step': 0.01, 'label': 'Match threshold'},
    'object_conf': {'min': 0.20, 'max': 0.90, 'step': 0.05, 'label': 'Object confidence'},
    # Saying a name aloud needs a much stricter bar than drawing a box: SFace's
    # 0.363 is its recommended operating point, not a safe one for greeting.
    'greet_min_sim': {'min': 0.40, 'max': 0.80, 'step': 0.01, 'label': 'Greeting similarity'},
    'greet_cooldown_s': {'min': 30, 'max': 3600, 'step': 10, 'label': 'Greeting cooldown (s)'},
}
DEFAULTS = {'faces_enabled': True, 'objects_enabled': False,
            'threshold': DEFAULT_THRESHOLD, 'object_conf': 0.40, 'classes': [],
            # The greeter is opt-in twice: this switch, then each person's own.
            'greeter_enabled': False, 'greet_min_sim': 0.50, 'greet_cooldown_s': 300,
            'greet_unknown': False}
GREETER_KEYS = ('greeter_enabled', 'greet_min_sim', 'greet_cooldown_s', 'greet_unknown')

WATCH_TTL_S = 10.0          # keep analysing this long after the last viewer poll
MIN_PERIOD_S = 0.10         # cap analytics at ~10 Hz so it leaves CPU for the AGV
EMBED_KNOWN_S = 2.0
EMBED_UNKNOWN_S = 0.8
MAX_EMBEDS_PER_FRAME = 2
TRACK_TTL_S = 1.0
TRACK_IOU = 0.3
EVENT_GAP_S = 8.0           # same person is logged again only after this long
EVENTS_KEPT = 100
OBJECTS_EVERY_S = 0.25
MODEL_RETRY_S = 30.0

GREETER_PERIOD_S = 0.33     # unwatched greeter: ~3 Hz, faces only (CPU shared with the AGV)
STABLE_AGREE = 2            # embeddings that must agree before a name counts as stable
WATCH_MAX_AGE_S = 15.0      # a wake request older than this is a stale file, not a request
WATCH_MAX_TTL_S = 30.0

ENROLL_SAMPLES = 8
ENROLL_MIN_SCORE = 0.80
ENROLL_MIN_FACE_PX = 90
ENROLL_SPACING_S = 0.7
ENROLL_DUP_COS = 0.98       # samples this alike add nothing: ask for a head turn
ENROLL_TIMEOUT_S = 60.0
ENROLL_RESULT_S = 6.0


def clamp_settings(raw: dict, base: Optional[dict] = None) -> dict:
    """Merge ``raw`` onto ``base`` (or defaults), dropping bad keys, clamping numbers."""
    out = dict(DEFAULTS if base is None else base)
    for key in ('faces_enabled', 'objects_enabled', 'greeter_enabled', 'greet_unknown'):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for key, lim in LIMITS.items():
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            out[key] = round(min(lim['max'], max(lim['min'], float(v))), 3)
    cls = raw.get('classes')
    if isinstance(cls, list) and all(isinstance(c, str) for c in cls):
        out['classes'] = sorted(set(cls))[:80]
    return out


def _iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = min(ax2, bx2) - max(a[0], b[0])
    ih = min(ay2, by2) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


class _Track:
    __slots__ = ('id', 'box', 'best', 'sim', 'embedded_at', 'seen_at', 'agree', 'agree_name')

    def __init__(self, tid: int, box, now: float) -> None:
        self.id, self.box, self.seen_at = tid, box, now
        self.best: Optional[str] = None
        self.sim = 0.0
        self.embedded_at = -1e9
        self.agree = 0
        self.agree_name: Optional[str] = None


class CamAnalytics:
    def __init__(self, publisher, store: Optional[FaceStore] = None,
                 settings_path: str = '', profiles=None, watch_path: str = '',
                 clock: Callable[[], float] = time.time,
                 detector_factory: Optional[Callable] = None,
                 recognizer_factory: Optional[Callable] = None,
                 objects_factory: Optional[Callable] = None) -> None:
        self._pub = publisher
        self.store = store if store is not None else FaceStore()
        self._settings_path = settings_path
        self.profiles = profiles
        self._watch_path = watch_path
        self._clock = clock
        self._detector_factory = detector_factory or self._default_detector
        self._recognizer_factory = recognizer_factory or self._default_recognizer
        self._objects_factory = objects_factory or self._default_objects

        self._lock = threading.Lock()
        self._settings = dict(DEFAULTS)
        self._load_settings()
        self._result: dict = self._blank('idle')
        self._events: collections.deque = collections.deque(maxlen=EVENTS_KEPT)
        self._enroll: dict = {'active': False}
        self._enroll_embs: List[np.ndarray] = []
        self._enroll_last_t = 0.0
        self._enroll_started = 0.0
        self._enroll_thumb: Optional[bytes] = None

        self._watch_until = 0.0
        self._ext_until = 0.0            # woken by the voice process (cam_watch.json)
        self._ext_seen: Optional[str] = None
        self._quiet = False
        self._greet: dict = {'ts': None, 'faces': [], 'unknown': 0}
        self._tracks: List[_Track] = []
        self._next_track = 1
        self._last_event_t: Dict[str, float] = {}
        self._detector = self._recognizer = self._objects = None
        self._model_err = {'faces': '', 'objects': ''}
        self._model_retry = {'faces': 0.0, 'objects': 0.0}
        self._ms = {'faces': 0.0, 'embed': 0.0, 'objects': 0.0}
        self._hz = 0.0
        self._last_obj_t = 0.0
        self._objects_out: List[dict] = []

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.profiles is not None:
            self.profiles.prune(p['name'] for p in self.store.people())

    # ── default model factories (late imports keep web.py start-up cheap) ──
    @staticmethod
    def _default_detector():
        from cam.faces import ensure_model
        from cam.yunet import YuNet
        return YuNet(ensure_model('yunet'))

    @staticmethod
    def _default_recognizer():
        from cam.faces import Recognizer
        return Recognizer()

    @staticmethod
    def _default_objects():
        from cam.objects import ObjectDetector
        return ObjectDetector()

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'CamAnalytics':
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name='cam-analytics', daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def touch(self) -> None:
        """A viewer is looking: keep analysing for the next WATCH_TTL_S seconds."""
        self._watch_until = time.monotonic() + WATCH_TTL_S

    # ── settings ─────────────────────────────────────────────────────
    def _load_settings(self) -> None:
        if not self._settings_path:
            return
        try:
            with open(self._settings_path) as f:
                self._settings = clamp_settings(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as exc:             # noqa: BLE001
            log.warning('cam: settings unreadable (%s); using defaults', exc)

    def settings(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def update_settings(self, raw: dict) -> dict:
        with self._lock:
            self._settings = clamp_settings(raw, self._settings)
            out = dict(self._settings)
        if self._settings_path:
            os.makedirs(os.path.dirname(self._settings_path) or '.', exist_ok=True)
            tmp = self._settings_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(out, f)
            os.replace(tmp, self._settings_path)
        return out

    # ── the rover's view of the camera ───────────────────────────────
    def greet_snapshot(self) -> dict:
        """Faces from the last analysed frame, whether or not the overlay shows them."""
        with self._lock:
            return {'ts': self._greet['ts'], 'unknown': self._greet['unknown'],
                    'faces': [dict(f) for f in self._greet['faces']]}

    def poll_watch(self) -> bool:
        """Wake for a request the voice process dropped in ``cam_watch.json``.

        Same protections as ``voice/command.py``: an id is honoured once, and a
        request older than ``WATCH_MAX_AGE_S`` is a leftover file, not a request.
        The ttl is clamped, so a bad writer cannot keep the camera busy.
        """
        if not self._watch_path:
            return False
        try:
            with open(self._watch_path, encoding='utf-8') as fh:
                req = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(req, dict):
            return False
        rid, ts, ttl = req.get('id'), req.get('ts'), req.get('ttl_s', 8.0)
        if not isinstance(rid, str) or not rid or rid == self._ext_seen:
            return False
        self._ext_seen = rid
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) \
                or abs(self._clock() - ts) > WATCH_MAX_AGE_S:
            return False
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl != ttl:
            ttl = 8.0
        self._ext_until = time.monotonic() + max(1.0, min(WATCH_MAX_TTL_S, float(ttl)))
        return True

    def remove_person(self, name: str) -> bool:
        """Forget a person: face samples, thumbnail, profile and live tracks."""
        ok = self.store.remove(name)
        if ok and self.profiles is not None:
            self.profiles.remove(name)
        self._tracks.clear()           # a deleted person must stop being named at once
        return ok

    def object_classes(self) -> List[str]:
        return list(getattr(self._objects, 'class_names', []) or [])

    # ── enrolment ────────────────────────────────────────────────────
    def start_enroll(self, name: str, samples: int = ENROLL_SAMPLES) -> dict:
        if not valid_name(name):
            raise ValueError('name must be 1-32 chars: letters, digits, space, _ . -')
        samples = max(3, min(20, int(samples)))
        with self._lock:
            self._enroll = {'active': True, 'name': name, 'want': samples, 'got': 0,
                            'hint': 'Look at the camera'}
            self._enroll_embs = []
            self._enroll_last_t = 0.0
            self._enroll_started = time.monotonic()
            self._enroll_thumb = None
            return dict(self._enroll)

    def cancel_enroll(self) -> None:
        with self._lock:
            if self._enroll.get('active'):
                self._enroll = {'active': False, 'result': 'cancelled',
                                'until': time.monotonic() + ENROLL_RESULT_S}
            self._enroll_embs = []

    def _enroll_public(self) -> dict:
        e = self._enroll
        if e.get('active'):
            return {k: e[k] for k in ('active', 'name', 'want', 'got', 'hint')}
        if e.get('result') and time.monotonic() < e.get('until', 0):
            return {'active': False, 'result': e['result'], 'name': e.get('name', ''),
                    'saved': e.get('saved', 0)}
        return {'active': False}

    # ── results ──────────────────────────────────────────────────────
    def _blank(self, state: str) -> dict:
        return {'state': state, 'seq': 0, 'ts': None, 'w': 0, 'h': 0,
                'faces': [], 'objects': []}

    def latest(self) -> dict:
        with self._lock:
            out = dict(self._result)
            out['age_ms'] = int(1000 * (time.time() - out['ts'])) if out['ts'] else None
            out['enroll'] = self._enroll_public()
            out['events'] = list(self._events)[-30:][::-1]
            out['settings'] = dict(self._settings)
            out['people'] = self.store.count()
            out['models'] = {
                'faces': self._model_err['faces'] or ('ok' if self._detector else 'off'),
                'objects': self._model_err['objects'] or
                           ('ok' if self._objects else ('off' if not self._settings['objects_enabled']
                                                         else 'loading'))}
            out['timing'] = {'faces_ms': round(self._ms['faces'], 1),
                             'embed_ms': round(self._ms['embed'], 1),
                             'objects_ms': round(self._ms['objects'], 1),
                             'hz': round(self._hz, 1)}
        return out

    # ── worker ───────────────────────────────────────────────────────
    def _ensure(self, which: str) -> bool:
        have = {'faces': self._detector is not None and self._recognizer is not None,
                'objects': self._objects is not None}[which]
        if have:
            return True
        if time.monotonic() < self._model_retry[which]:
            return False
        try:
            if which == 'faces':
                self._detector = self._detector_factory()
                self._recognizer = self._recognizer_factory()
            else:
                self._objects = self._objects_factory()
            self._model_err[which] = ''
            log.info('cam: %s model ready', which)
            return True
        except Exception as exc:             # noqa: BLE001
            self._model_err[which] = f'{type(exc).__name__}: {exc}'[:160]
            self._model_retry[which] = time.monotonic() + MODEL_RETRY_S
            log.warning('cam: %s model failed: %s', which, self._model_err[which])
            return False

    def _loop(self) -> None:
        last_seq, last_t, last_poll = 0, 0.0, 0.0
        while not self._stop.is_set():
            mono = time.monotonic()
            if mono - last_poll >= 0.5:
                last_poll = mono
                self.poll_watch()
            enrolling = bool(self._enroll.get('active'))
            watching = enrolling or mono < self._watch_until      # a person is at the dashboard
            asked = mono < self._ext_until                        # the rover asked who is there
            greeter = self.settings()['greeter_enabled']
            if not (watching or asked or greeter):
                with self._lock:
                    if self._result['state'] != 'idle':
                        self._result = self._blank('idle')
                    self._greet = {'ts': None, 'faces': [], 'unknown': 0}
                self._stop.wait(0.25)
                continue
            # Unwatched greeter: faces only, at ~3 Hz. The Jetson also runs the
            # AGV, and nobody is looking at the boxes.
            self._quiet = not watching
            period = MIN_PERIOD_S if (watching or asked) else GREETER_PERIOD_S
            seq, frame = self._pub.latest_frame()
            if frame is None or seq == last_seq:
                self._stop.wait(0.02)
                continue
            last_seq = seq
            t0 = time.monotonic()
            try:
                self._process(seq, frame)
            except Exception as exc:         # noqa: BLE001 - one bad frame must not kill the worker
                log.warning('cam: analytics frame failed: %s', exc)
            now = time.monotonic()
            if last_t:
                inst = 1.0 / max(now - last_t, 1e-3)
                self._hz = inst if not self._hz else 0.8 * self._hz + 0.2 * inst
            last_t = now
            self._stop.wait(max(0.0, period - (now - t0)))

    def _process(self, seq: int, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        st = self.settings()
        now = time.monotonic()
        faces: List[dict] = []
        enrolling = bool(self._enroll.get('active'))
        asked = time.monotonic() < self._ext_until
        want_faces = (st['faces_enabled'] or enrolling or st['greeter_enabled'] or asked)

        if want_faces and self._ensure('faces'):
            t = time.perf_counter()
            dets = self._detector.detect(frame)
            self._ms['faces'] = 0.8 * self._ms['faces'] + 0.2 * (time.perf_counter() - t) * 1000
            faces = self._track(frame, dets, st, now)
            self._log_events(faces, now)
            if enrolling:
                self._enroll_step(frame, dets, now)

        objects: List[dict] = []
        if st['objects_enabled'] and not self._quiet and self._ensure('objects'):
            if now - self._last_obj_t >= OBJECTS_EVERY_S:
                self._last_obj_t = now
                t = time.perf_counter()
                found = self._objects.detect(frame, st['object_conf'])
                self._ms['objects'] = 0.8 * self._ms['objects'] + 0.2 * (time.perf_counter() - t) * 1000
                self._objects_out = found
            keep = set(st['classes'])
            objects = [o for o in self._objects_out if not keep or o['name'] in keep]
        elif not st['objects_enabled'] or self._quiet:
            self._objects_out = []

        show = st['faces_enabled'] or enrolling          # the greeter can run with the overlay off
        with self._lock:
            self._greet = {'ts': time.time(), 'faces': faces,
                           'unknown': sum(1 for f in faces if not f['name'])}
            self._result = {
                'state': 'running', 'seq': seq, 'ts': time.time(), 'w': w, 'h': h,
                'faces': faces if show else [],
                'objects': [{'name': o['name'], 'conf': round(o['conf'], 2),
                             'box': [round(v) for v in o['box']]} for o in objects]}

    # ── faces: tracking + identity ───────────────────────────────────
    def _track(self, frame, dets: List[dict], st: dict, now: float) -> List[dict]:
        self._tracks = [t for t in self._tracks if now - t.seen_at <= TRACK_TTL_S]
        free = list(self._tracks)
        pairs = []
        for d in dets:
            best, best_iou = None, TRACK_IOU
            for t in free:
                v = _iou(d['box'], t.box)
                if v > best_iou:
                    best, best_iou = t, v
            if best is not None:
                free.remove(best)
            pairs.append((d, best))

        embeds = 0
        out = []
        have_db = self.store.count() > 0
        for d, t in pairs:
            if t is None:
                t = _Track(self._next_track, d['box'], now)
                self._next_track += 1
                self._tracks.append(t)
            t.box, t.seen_at = d['box'], now
            every = EMBED_KNOWN_S if self._stable(t, st) else EMBED_UNKNOWN_S
            if have_db and now - t.embedded_at >= every and embeds < MAX_EMBEDS_PER_FRAME \
                    and d['box'][2] >= 40:
                te = time.perf_counter()
                feat = self._recognizer.embed(frame, d)
                self._ms['embed'] = 0.8 * self._ms['embed'] + 0.2 * (time.perf_counter() - te) * 1000
                t.best, t.sim = self.store.match(feat)
                t.embedded_at = now
                embeds += 1
                if t.best and t.sim >= st['greet_min_sim'] and t.sim >= st['threshold']:
                    t.agree = t.agree + 1 if t.agree_name == t.best else 1
                    t.agree_name = t.best
                else:
                    t.agree, t.agree_name = 0, None
            known = bool(t.best) and t.sim >= st['threshold']   # live: the slider needs no re-embed
            out.append({'track': t.id, 'box': [round(v) for v in d['box']],
                        'score': round(d['score'], 2),
                        'name': t.best if known else None,
                        'sim': round(t.sim, 2) if have_db and t.embedded_at > 0 else None,
                        'known': known,
                        'stable': known and self._stable(t, st)})
        return out

    @staticmethod
    def _stable(t: _Track, st: dict) -> bool:
        """A name is stable once enough *separate* embeddings agree on it.

        One frame never names anyone: a single lucky match is exactly how a
        stranger gets greeted as someone else. Live against the sliders, like
        ``known``, so moving them needs no re-embed.
        """
        return (t.agree >= STABLE_AGREE and t.best is not None and t.best == t.agree_name
                and t.sim >= st['greet_min_sim'] and t.sim >= st['threshold'])

    def _log_events(self, faces: List[dict], now: float) -> None:
        for f in faces:
            if f['sim'] is None and self.store.count():
                continue                                # not identified yet: don't log "unknown" early
            key = f['name'] or 'unknown'
            if now - self._last_event_t.get(key, -1e9) >= EVENT_GAP_S:
                with self._lock:
                    self._events.append({'t': time.time(), 'name': f['name'],
                                         'sim': f['sim'], 'kind': 'seen' if f['name'] else 'unknown'})
            self._last_event_t[key] = now

    # ── enrolment step ───────────────────────────────────────────────
    def _enroll_step(self, frame, dets: List[dict], now: float) -> None:
        e = self._enroll
        if now - self._enroll_started > ENROLL_TIMEOUT_S:
            self.cancel_enroll()
            with self._lock:
                self._enroll['result'] = 'timed out'
            return
        if len(dets) != 1:
            hint = 'No face in view' if not dets else 'Only one person in frame, please'
        else:
            d = dets[0]
            if d['score'] < ENROLL_MIN_SCORE:
                hint = 'Face not clear - face the camera'
            elif d['box'][2] < ENROLL_MIN_FACE_PX:
                hint = 'Move closer'
            elif now - self._enroll_last_t < ENROLL_SPACING_S:
                hint = e['hint']
            else:
                feat = np.asarray(self._recognizer.embed(frame, d), np.float32).reshape(-1)
                prev = self._enroll_embs[-1].reshape(-1) if self._enroll_embs else None
                if prev is not None and float(
                        feat @ prev / ((np.linalg.norm(feat) * np.linalg.norm(prev)) or 1.0)) > ENROLL_DUP_COS:
                    hint = 'Turn your head slightly'
                else:
                    self._enroll_embs.append(feat.reshape(1, -1))
                    self._enroll_last_t = now
                    self._enroll_thumb = make_thumb(frame, d['box'])
                    hint = 'Captured - turn your head slightly'
                    if len(self._enroll_embs) >= e['want']:
                        self._finish_enroll()
                        return
        with self._lock:
            e['got'], e['hint'] = len(self._enroll_embs), hint

    def _finish_enroll(self) -> None:
        name = self._enroll['name']
        n = self.store.add(name, self._enroll_embs, self._enroll_thumb)
        log.info('[API] enrolled %s (%d samples total)', name, n)
        with self._lock:
            self._enroll = {'active': False, 'result': 'saved', 'name': name,
                            'saved': len(self._enroll_embs),
                            'until': time.monotonic() + ENROLL_RESULT_S}
            self._enroll_embs = []
        self._tracks.clear()           # re-identify everyone against the new db


__all__ = ['CamAnalytics', 'clamp_settings', 'LIMITS', 'DEFAULTS']
