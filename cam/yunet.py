"""YuNet face detector run through onnxruntime instead of cv2.FaceDetectorYN.

Why not cv2: the 2023mar YuNet model needs OpenCV >= 4.8; this Jetson's apt
OpenCV is 4.6.0 and ``detect()`` throws ``-204 getLayerData`` on it. Replacing
the system cv2 would risk the CUDA build the rest of the stack uses, so the
network runs under onnxruntime and ``decode()`` below turns its 12 raw outputs
into boxes.

The shipped model is fixed at 640x640 (~43 ms on this CPU). Re-shaping it to
416x416 costs nothing in accuracy on a 640x480 frame (same faces, scores
0.92/0.88 vs 0.91/0.86) and halves the time, so ``YuNet`` derives and caches
that variant next to the original.
"""
from __future__ import annotations

import logging
import os
from typing import List

import cv2
import numpy as np

log = logging.getLogger('agv.cam')

STRIDES = (8, 16, 32)
DEFAULT_SIZE = 416
SCORE_TH = 0.6
NMS_TH = 0.3
TOP_K = 20
THREADS = 2


def decode(outs, size: int, frame_w: int, frame_h: int,
           score_th: float = SCORE_TH, nms_th: float = NMS_TH,
           top_k: int = TOP_K) -> List[dict]:
    """Raw network outputs -> faces in frame pixels.

    ``outs`` is the model's output list: cls_{8,16,32}, obj_{8,16,32},
    bbox_{8,16,32}, kps_{8,16,32}, each shaped (1, cells, C). Each face is
    ``{'box': [x, y, w, h], 'lm': [10 floats: re, le, nose, rm, lm as x,y],
    'score': float}``.
    """
    cls, obj, bbox, kps = outs[0:3], outs[3:6], outs[6:9], outs[9:12]
    boxes, scores, marks = [], [], []
    for i, stride in enumerate(STRIDES):
        score = np.sqrt(np.clip(cls[i][0, :, 0], 0, 1) * np.clip(obj[i][0, :, 0], 0, 1))
        keep = score > score_th
        if not keep.any():
            continue
        cells = size // stride
        idx = np.arange(score.shape[0])
        gx = (idx % cells).astype(np.float32)[keep]
        gy = (idx // cells).astype(np.float32)[keep]
        b, k = bbox[i][0][keep], kps[i][0][keep]
        cx, cy = (gx + b[:, 0]) * stride, (gy + b[:, 1]) * stride
        bw, bh = np.exp(b[:, 2]) * stride, np.exp(b[:, 3]) * stride
        boxes.append(np.stack([cx - bw / 2, cy - bh / 2, bw, bh], 1))
        scores.append(score[keep])
        pts = np.empty((len(gx), 10), np.float32)
        pts[:, 0::2] = (k[:, 0::2] + gx[:, None]) * stride
        pts[:, 1::2] = (k[:, 1::2] + gy[:, None]) * stride
        marks.append(pts)
    if not boxes:
        return []
    boxes, scores, marks = np.concatenate(boxes), np.concatenate(scores), np.concatenate(marks)
    kept = np.array(cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), score_th, nms_th))
    sx, sy = frame_w / size, frame_h / size
    scale = np.array([sx, sy] * 5, np.float32)
    faces = []
    for j in kept.reshape(-1)[:top_k]:
        x, y, w, h = boxes[j]
        faces.append({'box': [float(x * sx), float(y * sy), float(w * sx), float(h * sy)],
                      'lm': (marks[j] * scale).tolist(),
                      'score': float(scores[j])})
    return faces


def _derive_resized(src: str, size: int) -> str:
    """Write a ``size``x``size`` copy of the 640 model beside it; '' on failure."""
    dst = os.path.join(os.path.dirname(src), f'yunet_2023mar_{size}.onnx')
    if os.path.exists(dst) and os.path.getsize(dst) > 100_000:
        return dst
    try:
        import onnx
        model = onnx.load(src)
        dims = model.graph.input[0].type.tensor_type.shape.dim
        dims[2].dim_value = dims[3].dim_value = size
        del model.graph.value_info[:]
        for out in model.graph.output:       # stale 640-derived output shapes
            for d in out.type.tensor_type.shape.dim:
                d.Clear()
        tmp = dst + '.tmp'
        onnx.save(model, tmp)
        os.replace(tmp, dst)
        return dst
    except Exception as exc:                 # noqa: BLE001 - fall back to 640
        log.warning('cam: could not derive %dx%d YuNet (%s); using 640', size, size, exc)
        return ''


class YuNet:
    def __init__(self, model_path: str, size: int = DEFAULT_SIZE) -> None:
        import onnxruntime as ort
        path = _derive_resized(model_path, size) if size != 640 else ''
        self.size = size if path else 640
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        # Measured on this 6-core Jetson at 3 Hz: the default pool spins on every
        # core and costs 82% of a core; 2 threads with spinning off costs 14% for
        # the same ~21 ms inference. The AGV's control loop shares these cores.
        opts.intra_op_num_threads = THREADS
        opts.inter_op_num_threads = 1
        opts.add_session_config_entry('session.intra_op.allow_spinning', '0')
        self._sess = ort.InferenceSession(
            path or model_path, sess_options=opts, providers=['CPUExecutionProvider'])
        self._input = self._sess.get_inputs()[0].name

    def detect(self, bgr: np.ndarray) -> List[dict]:
        h, w = bgr.shape[:2]
        blob = cv2.resize(bgr, (self.size, self.size)).astype(np.float32)
        blob = blob.transpose(2, 0, 1)[None]          # BGR, 0..255, no mean shift
        return decode(self._sess.run(None, {self._input: blob}), self.size, w, h)


__all__ = ['YuNet', 'decode']
