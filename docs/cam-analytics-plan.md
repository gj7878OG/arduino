# Camera tab: live face recognition, enrolment and object detection

Status: implementing. Branch `feat/cam-dashboard-tab`.

## 1. Findings that shape the design (all measured on this Jetson)

| Finding | Evidence | Consequence |
|---|---|---|
| YuNet 2023mar cannot run in OpenCV 4.6.0 | `detect()` throws `-204 getLayerData`; the model needs OpenCV >= 4.8 | Run YuNet through **onnxruntime 1.30** (already installed), decode the 12 raw outputs ourselves. No venv, system cv2 untouched. |
| ORT is CPU only here | providers: `Azure`, `CPU` | Budget for CPU. |
| YuNet input can be shrunk | 640: ~43 ms, 416: ~22 ms, 320: ~26 ms; same faces found on a real frame (0.92 / 0.88 at 416) | Derive a 416 model once (`onnx` shape edit), cache next to the original. Fall back to 640 if `onnx` is missing. |
| SFace works in cv2 4.6 | `feature()` ~74 ms | Embed at most every ~1 s per tracked face, not every frame. |
| YOLOv8n on GPU | 28-32 ms | Optional, off by default, lazy-loaded. |
| uvcvideo has one opener | sidecar `VideoCapture` got 0 frames | All analytics read frames from `CamPublisher`, never a second `VideoCapture`. |
| C270 emits ~1.1 s (~25 frames) of black after open | mean brightness 2.0 for 25 frames, then 138 | `WARMUP_FRAMES=3` is wrong: the feed shows black on every (re)open. Replace with a brightness-aware warm-up. |
| Device numbers move | C270 was `video1`, is now `video0`; IMX219 is `video2` | Keep identity-based detection in `web.py`; CLI tools still default to `/dev/video0` (noted, not changed). |
| `/detections` already exists | AGV viewer page, `web.py:1136` | New API lives under `/cam/*` and `/api/cam/*`. |
| Overlay precedent | `detections.html:198` canvas over `<img>`; lidar tab sizes canvas by DPR | Client-side canvas overlay, no re-encode, no fps cost. |

## 2. Architecture

```
C270 -> CamPublisher thread (owns the device, ~12 fps)
          |-- JPEG -> /cam/mjpeg                       (unchanged, feed never waits on AI)
          '-- latest raw frame (seq, ndarray)
                   |
          CamAnalytics worker thread (newest frame only, drops the rest)
             YuNet(ORT 416) -> tracks -> SFace embed (throttled) -> FaceStore match
             [optional] YOLOv8n
             enrolment state machine, event log
                   |
          GET /cam/detections  (boxes/names/timings, polled ~4 Hz by the tab)
```

Detection runs on a second thread fed by a depth-1 "latest frame" slot, so a slow model lowers the
analytics rate but never the video rate.

## 3. Backend

- `cam/publisher.py`: `latest_frame()`, brightness-aware warm-up.
- `cam/yunet.py`: ORT YuNet wrapper + pure `decode()` (unit-testable without a model).
- `cam/faces.py`: `FaceStore` (pickle-compatible with `face_recognize.py`, atomic writes, thumbnails, name validation, vectorised cosine match).
- `cam/objects.py`: lazy YOLO wrapper reusing `face_recognition/models/yolov8n.pt`.
- `cam/analytics.py`: worker, tracks, enrolment, events, persisted settings (clamped, house `limits` pattern).
- `web.py` routes:
  - `GET /cam/detections`
  - `GET|POST /api/cam/settings` (open; display-only effects, values clamped)
  - `GET /api/cam/people`, `GET /api/cam/people/<name>/thumb`
  - `POST /api/cam/enroll`, `DELETE /api/cam/people/<name>` (auth required: they change who the system recognises)
  - `POST /api/cam/enroll/cancel`

## 4. UI (Camera tab)

1. Canvas overlay: face boxes with name and similarity, colour by known/unknown; object boxes.
2. Toggles: Faces, Objects. Threshold slider (live).
3. Faces-now list with confidence bar against the threshold line.
4. Enrol form: name, "Add face", progress `n/8`, live hint ("need exactly one face", "move closer").
5. Roster cards: thumbnail, sample count, delete.
6. Event log: seen / unknown, timestamped.
7. Perf line: detector ms, embed ms, analytics Hz, model state.

Polling follows the existing rule: the strip chip keeps polling when the tab is hidden; the heavy
`/cam/detections` poll only runs while the tab is visible.

## 5. Tests (house style: bare asserts, `_env`, one behaviour per test)

`test_cam_yunet`, `test_cam_faces`, `test_cam_analytics` (fake detector and recogniser), `test_cam_api`
(client, auth), `test_cam_publisher` (fake capture), plus a Playwright check that the overlay draws and
the roster renders from stubbed endpoints. Existing `test_dashboard_ui_cam` must keep passing.

## 6. Out of scope / risks

- ORT is CPU only: expect ~5 analytics Hz with one face, fewer with several. Embedding is throttled to keep this up.
- Face data is biometric: `faces_db.pkl`, thumbnails and `cam_settings.json` are gitignored; enrol and delete need a signed-in session.
- GPU JPEG encode and a TensorRT YOLO engine were not pursued: encode at 640x480 is cheap, and the bottleneck is the CPU face path.
- `web.py` / `dashboard.html` diffs mix unrelated lidar and voice WIP; nothing here is committed.
