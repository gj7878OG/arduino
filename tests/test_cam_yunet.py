"""Tests for cam/yunet.py — the onnxruntime YuNet wrapper.

OpenCV 4.6's FaceDetectorYN cannot run the 2023mar model, so the network runs
under onnxruntime and ``decode()`` turns its 12 raw outputs into faces. The
decode tests feed it hand-built outputs, so they need no model file.
"""
import _env  # noqa: F401  (must be first)
import os

import numpy as np

from cam import yunet
from cam.faces import MODEL_DIR

SIZE = 416


def _outputs(hits):
    """12 zeroed tensors with ``hits`` = [(stride, cell_x, cell_y, w, h, score)] set.

    bbox is (dx, dy, log w/stride, log h/stride); kps are x,y offsets in cells.
    """
    outs = []
    for kind, ch in (('cls', 1), ('obj', 1), ('bbox', 4), ('kps', 10)):
        for stride in yunet.STRIDES:
            cells = (SIZE // stride) ** 2
            outs.append(np.zeros((1, cells, ch), np.float32))
    idx = {(k, s): 3 * ki + si for ki, k in enumerate(('cls', 'obj', 'bbox', 'kps'))
           for si, s in enumerate(yunet.STRIDES)}
    for stride, cx, cy, w, h, score in hits:
        cell = cy * (SIZE // stride) + cx
        outs[idx['cls', stride]][0, cell, 0] = score
        outs[idx['obj', stride]][0, cell, 0] = score
        outs[idx['bbox', stride]][0, cell] = [0.5, 0.5, np.log(w / stride), np.log(h / stride)]
        outs[idx['kps', stride]][0, cell] = [0.25, 0.25] * 5
    return outs


def test_decode_maps_a_hit_to_frame_pixels():
    """Stride 16, cell (10, 8): centre (168, 136) in network space, 32x48 box."""
    faces = yunet.decode(_outputs([(16, 10, 8, 32, 48, 0.9)]), SIZE, 640, 480)
    assert len(faces) == 1, faces
    x, y, w, h = faces[0]['box']
    sx, sy = 640 / SIZE, 480 / SIZE
    assert abs(x - 152 * sx) < 0.5 and abs(y - 112 * sy) < 0.5, (x, y)
    assert abs(w - 32 * sx) < 0.5 and abs(h - 48 * sy) < 0.5, (w, h)
    assert abs(faces[0]['score'] - 0.9) < 1e-3


def test_decode_scales_landmarks_with_the_frame():
    """Landmark 0 is (cell + 0.25) * stride = (10.25, 8.25) * 16 in network space."""
    f = yunet.decode(_outputs([(16, 10, 8, 32, 48, 0.9)]), SIZE, 640, 480)[0]
    assert len(f['lm']) == 10
    assert abs(f['lm'][0] - 10.25 * 16 * 640 / SIZE) < 0.5
    assert abs(f['lm'][1] - 8.25 * 16 * 480 / SIZE) < 0.5


def test_decode_ignores_scores_below_threshold():
    assert yunet.decode(_outputs([(16, 10, 8, 32, 48, 0.3)]), SIZE, 640, 480) == []


def test_decode_empty_output_is_an_empty_list():
    assert yunet.decode(_outputs([]), SIZE, 640, 480) == []


def test_decode_merges_overlapping_duplicates():
    """Neighbouring cells vote for the same face; NMS must leave one."""
    hits = [(16, 10, 8, 32, 48, 0.9), (16, 11, 8, 32, 48, 0.8)]
    assert len(yunet.decode(_outputs(hits), SIZE, 640, 480)) == 1


def test_decode_keeps_separate_faces():
    hits = [(16, 4, 4, 32, 48, 0.9), (16, 20, 20, 32, 48, 0.8)]
    faces = yunet.decode(_outputs(hits), SIZE, 640, 480)
    assert len(faces) == 2, faces


def test_real_model_runs_on_a_blank_frame():
    """The derived 416 model loads under onnxruntime and finds nothing in a grey frame."""
    path = os.path.join(MODEL_DIR, 'face_detection_yunet_2023mar.onnx')
    if not os.path.exists(path):
        print('  (skipped: YuNet model not downloaded)')
        return
    det = yunet.YuNet(path)
    assert det.size in (416, 640), det.size
    assert det.detect(np.full((480, 640, 3), 128, np.uint8)) == []


def test_session_is_limited_to_two_threads_with_spinning_off():
    """Default onnxruntime threading cost 82% of a core at 3 Hz on the Jetson (vs 14%)."""
    path = os.path.join(MODEL_DIR, 'face_detection_yunet_2023mar.onnx')
    if not os.path.exists(path):
        print('  (skipped: YuNet model not downloaded)')
        return
    det = yunet.YuNet(path)
    opts = det._sess.get_session_options()
    assert opts.intra_op_num_threads == yunet.THREADS == 2
    assert opts.inter_op_num_threads == 1


if __name__ == '__main__':
    _env.run_tests(globals())
