"""Enrolled-face storage and SFace embedding for the Camera tab.

``FaceStore`` reads and writes the same ``faces_db.pkl`` that
``face_recognition/face_recognize.py`` uses (``{name: [ (1,128) float32, ... ]}``),
so people enrolled from the CLI show up in the dashboard and vice versa.
"""
from __future__ import annotations

import logging
import os
import pickle
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import cv2
import numpy as np

log = logging.getLogger('agv.cam')

FACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'face_recognition')
MODEL_DIR = os.path.join(FACE_DIR, 'models')
DB_PATH = os.path.join(FACE_DIR, 'faces_db.pkl')
THUMB_DIR = os.path.join(FACE_DIR, 'faces')

_ZOO = 'https://github.com/opencv/opencv_zoo/raw/main/models'
MODELS = {
    'yunet': ('face_detection_yunet_2023mar.onnx', 100_000,
              f'{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx'),
    'sface': ('face_recognition_sface_2021dec.onnx', 30_000_000,
              f'{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx'),
}

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$')
THUMB_PX = 96


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name))


def ensure_model(key: str) -> str:
    """Path to a model, fetching it with curl if missing or truncated.

    curl, not urllib: ``urlretrieve`` silently kept an 850 KB stub of the 38 MB
    SFace file after GitHub's redirect, which later failed as an opaque cv2
    error. The size floor catches that case.
    """
    filename, min_bytes, url = MODELS[key]
    os.makedirs(MODEL_DIR, exist_ok=True)
    path = os.path.join(MODEL_DIR, filename)
    if os.path.exists(path) and os.path.getsize(path) >= min_bytes:
        return path
    log.info('cam: fetching %s', filename)
    subprocess.run(['curl', '-fL', '--retry', '2', '-o', path, url], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.getsize(path) < min_bytes:
        os.remove(path)
        raise RuntimeError(f'{filename} download looks truncated')
    return path


class Recognizer:
    """SFace: aligned face -> 128-d embedding (cv2 4.6 runs this one fine)."""

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._sface = cv2.FaceRecognizerSF.create(model_path or ensure_model('sface'), '')

    def embed(self, bgr: np.ndarray, face: dict) -> np.ndarray:
        row = np.array([*face['box'], *face['lm'], face['score']], np.float32)
        return self._sface.feature(self._sface.alignCrop(bgr, row))


def _unit(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, np.float32).reshape(-1)
    return v / (np.linalg.norm(v) or 1.0)


class FaceStore:
    def __init__(self, db_path: str = DB_PATH, thumb_dir: str = THUMB_DIR) -> None:
        self._db_path = db_path
        self._thumb_dir = thumb_dir
        self._lock = threading.Lock()
        self._db: Dict[str, List[np.ndarray]] = {}
        self._enrolled_at: Dict[str, float] = {}
        self._names: List[str] = []
        self._matrix = np.zeros((0, 128), np.float32)
        self._owner: List[int] = []
        self._load()

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._db_path, 'rb') as f:
                self._db = pickle.load(f)
        except FileNotFoundError:
            self._db = {}
        except Exception as exc:             # noqa: BLE001
            log.warning('cam: face db unreadable (%s); starting empty', exc)
            self._db = {}
        for name in self._db:
            self._enrolled_at[name] = self._mtime(self._thumb_path(name)) or 0.0
        self._rebuild()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        tmp = self._db_path + '.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump(self._db, f)
        os.replace(tmp, self._db_path)       # never leave a half-written db

    def _rebuild(self) -> None:
        self._names = list(self._db)
        rows, owner = [], []
        for i, name in enumerate(self._names):
            for emb in self._db[name]:
                rows.append(_unit(emb))
                owner.append(i)
        self._matrix = np.stack(rows) if rows else np.zeros((0, 128), np.float32)
        self._owner = owner

    @staticmethod
    def _mtime(path: str) -> Optional[float]:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _thumb_path(self, name: str) -> str:
        return os.path.join(self._thumb_dir, quote(name, safe='') + '.jpg')

    # ── queries ──────────────────────────────────────────────────────
    def people(self) -> List[dict]:
        with self._lock:
            return [{'name': n, 'samples': len(self._db[n]),
                     'has_thumb': os.path.exists(self._thumb_path(n)),
                     'enrolled_at': self._enrolled_at.get(n) or None}
                    for n in sorted(self._db)]

    def count(self) -> int:
        with self._lock:
            return len(self._db)

    def thumb(self, name: str) -> Optional[bytes]:
        if not valid_name(name):
            return None
        try:
            with open(self._thumb_path(name), 'rb') as f:
                return f.read()
        except OSError:
            return None

    def match(self, feature: np.ndarray) -> Tuple[Optional[str], float]:
        """Best (name, cosine) over every stored sample; name None if empty."""
        with self._lock:
            if not len(self._owner):
                return None, 0.0
            sims = self._matrix @ _unit(feature)
            j = int(np.argmax(sims))
            return self._names[self._owner[j]], float(sims[j])

    # ── mutations ────────────────────────────────────────────────────
    def add(self, name: str, embeddings: List[np.ndarray],
            thumb_jpeg: Optional[bytes] = None) -> int:
        if not valid_name(name):
            raise ValueError('invalid name')
        if not embeddings:
            raise ValueError('no samples')
        with self._lock:
            self._db.setdefault(name, []).extend(
                np.asarray(e, np.float32).reshape(1, -1) for e in embeddings)
            if thumb_jpeg:
                os.makedirs(self._thumb_dir, exist_ok=True)
                with open(self._thumb_path(name), 'wb') as f:
                    f.write(thumb_jpeg)
            self._enrolled_at[name] = time.time()
            self._save()
            self._rebuild()
            return len(self._db[name])

    def remove(self, name: str) -> bool:
        if not valid_name(name):
            return False
        with self._lock:
            if name not in self._db:
                return False
            del self._db[name]
            self._enrolled_at.pop(name, None)
            try:
                os.remove(self._thumb_path(name))
            except OSError:
                pass
            self._save()
            self._rebuild()
            return True


def make_thumb(bgr: np.ndarray, box) -> Optional[bytes]:
    """Square, slightly padded face crop as a small JPEG."""
    h, w = bgr.shape[:2]
    x, y, bw, bh = box
    side = max(bw, bh) * 1.35
    cx, cy = x + bw / 2, y + bh / 2
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = cv2.resize(bgr[y0:y1, x0:x1], (THUMB_PX, THUMB_PX), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else None


__all__ = ['FaceStore', 'Recognizer', 'ensure_model', 'make_thumb', 'valid_name']
