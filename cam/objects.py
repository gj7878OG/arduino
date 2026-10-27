"""YOLOv8n object detection for the Camera tab, loaded only when first enabled.

ultralytics pulls in torch (seconds to import, hundreds of MB resident), so
nothing here is imported until an operator switches "Objects" on.
"""
from __future__ import annotations

import os
from typing import List

import numpy as np

from cam.faces import MODEL_DIR

YOLO_WEIGHTS = os.path.join(MODEL_DIR, 'yolov8n.pt')
MIN_WEIGHTS_BYTES = 5_000_000          # the real file is ~6.5 MB


class ObjectDetector:
    def __init__(self, weights: str = YOLO_WEIGHTS) -> None:
        if not os.path.exists(weights) or os.path.getsize(weights) < MIN_WEIGHTS_BYTES:
            raise FileNotFoundError(
                f'{weights} missing - run face_recognition/detect_objects.py smoke once to fetch it')
        import torch
        from ultralytics import YOLO
        self._model = YOLO(weights)
        self._device = '0' if torch.cuda.is_available() else 'cpu'
        self.names = dict(self._model.names)
        self.class_names = sorted(self.names.values())

    def detect(self, bgr: np.ndarray, conf: float) -> List[dict]:
        res = self._model.predict(source=bgr, imgsz=480, device=self._device,
                                  conf=conf, verbose=False)
        if not res or res[0].boxes is None or len(res[0].boxes) == 0:
            return []
        b = res[0].boxes
        xyxy = b.xyxy.cpu().numpy()
        out = []
        for (x1, y1, x2, y2), c, k in zip(xyxy, b.conf.cpu().numpy(), b.cls.cpu().numpy()):
            out.append({'name': self.names[int(k)], 'conf': float(c),
                        'box': [float(x1), float(y1), float(x2 - x1), float(y2 - y1)]})
        return out


__all__ = ['ObjectDetector']
