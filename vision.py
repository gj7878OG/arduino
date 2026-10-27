"""Camera-only perception: bird's-eye warp, line geometry, floor obstacles.

The camera looks forward and down (height ``CAM_HEIGHT_M``, tilt
``CAM_TILT_DEG``). A ground-plane homography built from those numbers maps
the frame onto a metric top-down view (BEV), so everything below is in metres
on the floor with the camera at the origin: X to the right, Y forward.

Line: yellow mask in the BEV -> main blob -> centroid per horizontal strip ->
jump rejection -> polynomial fit -> offset / heading / lookahead / curvature.

Obstacle: colour model of the floor inside a corridor as wide as the rover.
Anything that differs from the floor (and is not the yellow line) is an
obstacle; its distance is the floor position of its nearest edge, which is
exact for the base of an object standing on the floor.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from camera import _gray_world_white_balance
from config import (
    BEV_PX_PER_M, BEV_X_HALF_M, BEV_Y_FAR_M, BEV_Y_NEAR_M,
    CAM_HEIGHT_M, CAM_HFOV_DEG, CAM_TILT_DEG,
    LINE_MAX_JUMP_M, LINE_MAX_WIDTH_M, LINE_MIN_WIDTH_M, LINE_REF_Y_M,
    LINE_HUE_DEG, LINE_HUE_MARGIN_DEG, LINE_HUE_TOL_DEG, LINE_MIN_CHROMA,
    LINE_MAX_COVERAGE, LINE_MAX_WIDTH_RATIO, LINE_MIN_ELONGATION, LINE_MIN_L, LINE_MIN_LENGTH_M, LINE_TAPE_DELTA_E,
    TAPE_MODEL_TTL_S,
    LINE_STRIPS, LOOKAHEAD_M, MIN_CONTOUR_AREA, MORPHOLOGY_KERNEL,
    OBST_DEBOUNCE, OBST_DELTA_E, OBST_ENABLED, OBST_MARGIN_M,
    OBST_MIN_AREA_M2, OBST_SLOW_M, OBST_STOP_M, ROVER_WIDTH_M,
    TIER0_GREEN_GUARD_RATIO, VISION_WIDTH, WHITE_BALANCE,
)


def yellow_mask_bgr(img: np.ndarray, valid: Optional[np.ndarray] = None) -> np.ndarray:
    """Binary mask (0/255) of yellow-tape pixels in a BGR image.

    A pixel counts as tape when, in CIELAB:
      * it has enough chroma (not grey / white glare / dark noise),
      * its hue is within LINE_HUE_TOL_DEG of yellow,
      * it is at least LINE_HUE_MARGIN_DEG *closer to yellow than the median
        floor pixel*. That is what separates pale tape from orange wood or a
        cream wall, and it follows lighting casts because the floor shifts
        with the tape.
    """
    return yellow_mask_lab(cv2.cvtColor(img, cv2.COLOR_BGR2LAB), valid)


def yellow_mask_lab(lab: np.ndarray, valid: Optional[np.ndarray] = None,
                    sample: Optional[np.ndarray] = None) -> np.ndarray:
    """``sample``: optional flat indices of floor pixels used for the median."""
    l_ch = lab[..., 0]
    a = lab[..., 1].astype(np.float32) - 128.0
    b = lab[..., 2].astype(np.float32) - 128.0
    chroma, hue = cv2.cartToPolar(a, b, angleInDegrees=True)
    dist = np.abs((hue - LINE_HUE_DEG + 180.0) % 360.0 - 180.0)    # 0 = yellow

    if sample is None:
        sample = np.arange(0, l_ch.size, 5)
    # Neutral pixels (no hue) count as "not yellow"; otherwise on a grey floor
    # the only coloured samples would be the tape itself.
    c_s = chroma.ravel()[sample]
    d_s = np.where(c_s > 4.0, dist.ravel()[sample], 180.0)
    floor_dist = float(np.median(d_s)) if d_s.size else 180.0
    limit = min(LINE_HUE_TOL_DEG, floor_dist - LINE_HUE_MARGIN_DEG)
    mask = (chroma >= LINE_MIN_CHROMA) & (dist <= limit) & (l_ch >= LINE_MIN_L)
    if valid is not None:
        mask &= valid
    return mask.astype(np.uint8) * 255


@dataclass(frozen=True)
class LineGeometry:
    found: bool = False
    offset_m: float = 0.0          # line X at LINE_REF_Y_M; + = line is to the right
    angle_deg: float = 0.0         # heading of the line; + = leans right going away
    lookahead_m: float = 0.0       # line X at the lookahead distance
    look_dist_m: float = 0.0       # that distance (forward, metres)
    curvature: float = 0.0         # 1/m, + = bending right
    strips_used: int = 0
    reason: str = ''
    # (X, Y) floor points of the fitted curve, for the HUD.
    curve: tuple = ()
    look_point: Optional[tuple] = None


@dataclass(frozen=True)
class ObstacleResult:
    state: str = 'CLEAR'           # CLEAR | SLOW | STOP
    distance_m: float = 0.0        # nearest edge, 0 when nothing seen
    raw_state: str = 'CLEAR'


@dataclass
class VisionResult:
    line: LineGeometry = field(default_factory=LineGeometry)
    obstacle: ObstacleResult = field(default_factory=ObstacleResult)
    bev_debug: Optional[np.ndarray] = None
    ms: float = 0.0


def build_homography(width: int, height: int) -> np.ndarray:
    """Image (pixels of a width x height frame) -> BEV pixels."""
    f = (width / 2.0) / math.tan(math.radians(CAM_HFOV_DEG) / 2.0)
    k_inv = np.array([[1 / f, 0, -(width / 2.0) / f],
                      [0, 1 / f, -(height / 2.0) / f],
                      [0, 0, 1]], dtype=np.float64)
    th = math.radians(CAM_TILT_DEG)
    h = CAM_HEIGHT_M
    ground = np.array([[h, 0, 0],
                       [0, -h * math.sin(th), h * math.cos(th)],
                       [0, math.cos(th), math.sin(th)]], dtype=np.float64)
    ppm = BEV_PX_PER_M
    to_bev = np.array([[ppm, 0, BEV_X_HALF_M * ppm],
                       [0, -ppm, BEV_Y_FAR_M * ppm],
                       [0, 0, 1]], dtype=np.float64)
    return to_bev @ ground @ k_inv


class Vision:
    def __init__(self) -> None:
        # How far ahead of the rover the line is sampled (metres). This is
        # the pure-pursuit lookahead: a short one tracks the centre tightly but
        # is noisier, a long one is smooth but rides off-centre. Pushed from the
        # dashboard each tick; the config constant is the default.
        self.lookahead_m = LOOKAHEAD_M
        self.bev_w = int(round(2 * BEV_X_HALF_M * BEV_PX_PER_M))
        self.bev_h = int(round((BEV_Y_FAR_M - BEV_Y_NEAR_M) * BEV_PX_PER_M))
        self._size: Optional[tuple] = None
        self._h = None
        self._h_inv = None
        self._valid = None
        self._k = np.ones(MORPHOLOGY_KERNEL, np.uint8)
        self._k_close = np.ones((15, 3), np.uint8)
        # Joins pieces separated by a thin seam / crease (up to ~5 cm).
        self._k_merge = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        self._merged: Optional[np.ndarray] = None
        self._tape_off: Optional[np.ndarray] = None
        self._dbg: dict = {}
        self._tape_t = 0.0
        self._prev_off: Optional[float] = None    # last good line X (tracking)
        self._prev_t = 0.0
        # Obstacle state
        self._floor_lab: Optional[np.ndarray] = None
        self._obs_state = 'CLEAR'
        self._obs_pending = 'CLEAR'
        self._obs_count = 0
        half = ROVER_WIDTH_M / 2.0 + OBST_MARGIN_M
        self._corr_x0 = max(0, int((BEV_X_HALF_M - half) * BEV_PX_PER_M))
        self._corr_x1 = min(self.bev_w, int((BEV_X_HALF_M + half) * BEV_PX_PER_M))

    def debug_image(self) -> Optional[np.ndarray]:
        """BEV | yellow mask | obstacle-test region | obstacle mask, for diagnosis."""
        d = self._dbg
        if not d:
            return None
        h, w = d['bev'].shape[:2]
        def tile(m):
            m = cv2.resize(m.astype(np.uint8) * (255 if m.dtype == bool else 1), (w, h),
                           interpolation=cv2.INTER_NEAREST)
            return cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
        dmap = np.clip(d['d'] * 8, 0, 255).astype(np.uint8)
        return np.hstack([d['bev'], tile(d['yellow']), tile(d['region']),
                          tile(d['obs']), tile(dmap)])

    def reset_floor(self) -> None:
        """Forget the floor colour model (call when the operator re-arms)."""
        self._floor_lab = None
        self._obs_state, self._obs_pending, self._obs_count = 'CLEAR', 'CLEAR', 0

    # ── coordinate helpers ───────────────────────────────────────────
    def _bev_px(self, x: float, y: float) -> tuple:
        return ((x + BEV_X_HALF_M) * BEV_PX_PER_M,
                (BEV_Y_FAR_M - y) * BEV_PX_PER_M)

    def world_to_frame(self, pts, frame_w: int, frame_h: int):
        """Floor (X, Y) points -> pixel coordinates in the full frame."""
        if self._h_inv is None or not pts:
            return []
        src = np.array([self._bev_px(x, y) for x, y in pts],
                       dtype=np.float32).reshape(-1, 1, 2)
        small = cv2.perspectiveTransform(src, self._h_inv).reshape(-1, 2)
        sx = frame_w / float(self._size[0])
        sy = frame_h / float(self._size[1])
        return [(int(p[0] * sx), int(p[1] * sy)) for p in small]

    def _prepare(self, w: int, h: int) -> None:
        self._size = (w, h)
        self._h = build_homography(w, h)
        self._h_inv = np.linalg.inv(self._h)
        ones = np.full((h, w), 255, np.uint8)
        valid = cv2.warpPerspective(ones, self._h, (self.bev_w, self.bev_h),
                                    flags=cv2.INTER_NEAREST)
        self._valid = cv2.erode(valid, np.ones((5, 5), np.uint8)) > 0
        self._sample = np.flatnonzero(self._valid.ravel())[::5]
        corr = np.zeros(self._valid.shape, np.uint8)
        corr[:, self._corr_x0:self._corr_x1] = self._valid[:, self._corr_x0:self._corr_x1]
        self._corr_half = cv2.resize(
            corr, (self.bev_w // 2, self.bev_h // 2), interpolation=cv2.INTER_NEAREST) > 0

    # ── main entry ───────────────────────────────────────────────────
    def analyze(self, frame: np.ndarray, debug: bool = True) -> VisionResult:
        t0 = time.monotonic()
        fh, fw = frame.shape[:2]
        sw = VISION_WIDTH
        sh = int(round(fh * sw / float(fw)))
        small = (cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
                 if sw != fw else frame)
        if self._size != (sw, sh):
            self._prepare(sw, sh)

        m = cv2.mean(small)
        b, g, r = float(m[0]), float(m[1]), float(m[2])
        green_cast = (g > TIER0_GREEN_GUARD_RATIO * r
                      and g > TIER0_GREEN_GUARD_RATIO * b)

        work = _gray_world_white_balance(small) if WHITE_BALANCE else small
        bev = cv2.warpPerspective(work, self._h, (self.bev_w, self.bev_h),
                                  flags=cv2.INTER_LINEAR)
        bev = cv2.GaussianBlur(bev, (3, 3), 0)

        lab = cv2.cvtColor(bev, cv2.COLOR_BGR2LAB)
        yellow = self._yellow_mask(lab)
        if green_cast:
            line = LineGeometry(reason='green cast')
        else:
            line = self._analyze_line(yellow)
            if line.found:
                self._learn_tape(lab, yellow)
            else:
                model = self._tape_model_mask(lab)
                if model is not None:
                    alt = self._analyze_line(model)
                    if alt.found:
                        line, yellow = alt, model
        seam_free = self._merged if (self._merged is not None and line.found) else yellow
        obstacle = (self._analyze_obstacle(bev, seam_free)
                    if OBST_ENABLED else ObstacleResult())

        result = VisionResult(line=line, obstacle=obstacle)
        if debug:
            result.bev_debug = self._render_debug(bev, yellow, line, obstacle)
        result.ms = (time.monotonic() - t0) * 1000.0
        return result

    # ── line ─────────────────────────────────────────────────────────
    def _finish_mask(self, mask: np.ndarray) -> np.ndarray:
        # Bridge small gaps along the line (glare, tape creases) with a tall
        # kernel, then drop speckle.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._k_close)
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._k)

    def _yellow_mask(self, lab: np.ndarray) -> np.ndarray:
        return self._finish_mask(yellow_mask_lab(lab, self._valid, self._sample))

    # Colour tracking. A strong cast (pink fluorescent, green ISP tint)
    # rotates every hue so the yellow-hue test can fail. Once the strict test
    # has found the line we remember how the tape differs from the floor in
    # Lab (an *offset*, so it survives a cast that shifts both) and, if the
    # strict test later finds nothing, follow that. The model fades after
    # TAPE_MODEL_TTL_S without a strict confirmation.
    def _floor_median(self, lab: np.ndarray, exclude: Optional[np.ndarray] = None):
        flat = lab.reshape(-1, 3)
        if exclude is None:
            idx = self._sample
        else:
            idx = self._sample[~exclude.ravel()[self._sample]]
        return np.median(flat[idx].astype(np.float32), axis=0)

    def _learn_tape(self, lab: np.ndarray, mask: np.ndarray) -> None:
        sel = mask > 0
        if int(sel.sum()) < 200:
            return
        tape = np.median(lab[sel].astype(np.float32), axis=0)
        offset = tape - self._floor_median(lab, cv2.dilate(mask, self._k) > 0)
        self._tape_off = (offset if self._tape_off is None
                          else 0.9 * self._tape_off + 0.1 * offset)
        self._tape_t = time.monotonic()

    def _tape_model_mask(self, lab: np.ndarray) -> Optional[np.ndarray]:
        if (self._tape_off is None
                or time.monotonic() - self._tape_t > TAPE_MODEL_TTL_S):
            return None
        off = self._tape_off
        if np.linalg.norm([0.4 * off[0], off[1], off[2]]) < 1.3 * LINE_TAPE_DELTA_E:
            return None                       # tape too close to floor colour
        rel = lab.astype(np.float32) - self._floor_median(lab)
        d = np.sqrt((0.4 * (rel[..., 0] - off[0])) ** 2
                    + (rel[..., 1] - off[1]) ** 2 + (rel[..., 2] - off[2]) ** 2)
        return self._finish_mask(((d < LINE_TAPE_DELTA_E) & self._valid).astype(np.uint8) * 255)

    def _pick_component(self, labels, stats):
        """Choose the blob that looks most like the line.

        Candidates must be long enough and elongated (rejects round yellow
        objects). Among them the score is area, boosted for length and for
        being near where the line was a moment ago.
        """
        ppm = BEV_PX_PER_M
        min_area = max(MIN_CONTOUR_AREA * 0.3, LINE_MIN_WIDTH_M * ppm * 0.25 * ppm)
        order = np.argsort(stats[1:, cv2.CC_STAT_AREA])[::-1][:6] + 1
        tracking = (self._prev_off is not None
                    and time.monotonic() - self._prev_t < 1.0)
        best, best_score, best_info = None, 0.0, None
        for i in order:
            area = float(stats[i, cv2.CC_STAT_AREA])
            if area < min_area:
                break
            ys, xs = np.nonzero(labels[stats[i, cv2.CC_STAT_TOP]:
                                       stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]]
                                == i)
            if xs.size < 10:
                continue
            area = float(xs.size)                  # true yellow pixels, not the merged blob
            ys = ys + stats[i, cv2.CC_STAT_TOP]
            cov = np.cov(np.vstack([xs, ys]).astype(np.float32))
            ev, evec = np.linalg.eigh(cov)
            minor, major = math.sqrt(max(ev[0], 1e-3)), math.sqrt(max(ev[1], 1e-3))
            length_m = 4.0 * major / ppm
            if length_m < LINE_MIN_LENGTH_M or major / minor < LINE_MIN_ELONGATION:
                continue
            # True (perpendicular) width: area / length, so a diagonal piece is
            # not mistaken for a wide one.
            width_m = (area / (math.sqrt(12.0) * major)) / ppm
            if width_m > LINE_MAX_WIDTH_M:
                continue
            score = area * (0.5 + min(length_m / 0.8, 1.0))
            if tracking:
                dx = xs.mean() / ppm - BEV_X_HALF_M - self._prev_off
                score /= 1.0 + (dx / 0.35) ** 2
            if score > best_score:
                # Principal axis in floor coordinates (X right, Y forward),
                # pointing away from the rover.
                ax, ay = float(evec[0, 1]), -float(evec[1, 1])    # image y is -Y
                if ay < 0 or (abs(ay) < 1e-6 and ax < 0):
                    ax, ay = -ax, -ay
                X = xs / ppm - BEV_X_HALF_M
                Y = BEV_Y_FAR_M - ys / ppm
                best, best_score = int(i), score
                best_info = dict(cx=float(X.mean()), cy=float(Y.mean()), ax=ax, ay=ay,
                                 y_max=float(Y.max()), y_min=float(Y.min()),
                                 width_m=width_m, length_m=length_m)
        return best, best_info

    def _analyze_line(self, mask: np.ndarray) -> LineGeometry:
        ppm = BEV_PX_PER_M
        merged = cv2.dilate(mask, self._k_merge)
        self._merged = merged
        n, labels, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
        if n <= 1:
            return LineGeometry(reason='no yellow')
        labels = np.where(mask > 0, labels, 0)      # real yellow pixels only
        best, info = self._pick_component(labels, stats)
        if best is None:
            self._prev_off = None
            return LineGeometry(reason='no line-shaped blob')

        strips = max(2, LINE_STRIPS)
        sh = self.bev_h // strips
        min_w = LINE_MIN_WIDTH_M * ppm
        # A diagonal piece is wider per image row than it really is.
        max_w = LINE_MAX_WIDTH_M * ppm / max(0.35, abs(info['ay']))
        coverage = cv2.countNonZero(mask) / float(max(1, int(self._valid.sum())))
        if coverage > LINE_MAX_COVERAGE:
            return LineGeometry(reason=f'too much yellow ({coverage:.0%})')
        pts = []                                  # near -> far
        widths = []
        for k in range(strips):
            r1 = self.bev_h - k * sh
            r0 = r1 - sh
            rows, cols = np.nonzero(labels[r0:r1] == best)
            if cols.size == 0:
                continue
            width = cols.size / float(sh)         # mean px width per row
            if width < min_w or width > max_w:
                continue
            x_m = cols.mean() / ppm - BEV_X_HALF_M
            y_m = BEV_Y_FAR_M - ((r0 + r1) / 2.0) / ppm
            if pts:
                gap = (y_m - pts[-1][1]) / (sh / float(ppm))
                if abs(x_m - pts[-1][0]) > LINE_MAX_JUMP_M * max(1.0, gap):
                    continue
            pts.append((x_m, y_m))
            widths.append(width)

        # Tape has the same width everywhere in the metric view; a wall,
        # floor patch or glare has strips much wider than the typical one.
        # (Narrow strips are tolerated: an object standing on the line hides
        # part of it.)
        if len(widths) >= 3 and max(widths) > LINE_MAX_WIDTH_RATIO * float(np.median(widths)):
            return LineGeometry(reason='inconsistent width')

        # Heading per half of the shape. The near half gives offset/heading at
        # the rover; the change between near and far half gives the curvature.
        # (Per-strip centroids are biased on diagonal, tapered or clipped
        # shapes and made straight runs look curved.)
        py, px = np.nonzero(labels == best)
        Xp = px / float(ppm) - BEV_X_HALF_M
        Yp = BEV_Y_FAR_M - py / float(ppm)

        def axis(sel):
            if int(sel.sum()) < 120:
                return None
            x, y = Xp[sel], Yp[sel]
            ev, evec = np.linalg.eigh(np.cov(np.vstack([x, y])))
            ax, ay = float(evec[0, 1]), float(evec[1, 1])
            if ay < 0 or (abs(ay) < 1e-6 and ax < 0):
                ax, ay = -ax, -ay
            return float(x.mean()), float(y.mean()), ax, ay, math.sqrt(max(ev[1], 1e-9))

        ymid = 0.5 * (float(Yp.min()) + float(Yp.max()))
        near = axis(Yp <= ymid + 0.10)
        far = axis(Yp >= ymid - 0.10)
        curvature = 0.0
        if near is not None:
            cx, cy, ax, ay, _ = near
        else:
            cx, cy, ax, ay = info['cx'], info['cy'], info['ax'], info['ay']
        slope = max(-6.7, min(6.7, ax / max(ay, 1e-3)))
        if near is not None and far is not None and info['length_m'] > 0.6:
            th_n = math.atan2(near[2], near[3])
            th_f = math.atan2(far[2], far[3])
            ds = max(far[1] - near[1], 0.2) / max(math.cos(th_n), 0.3)
            curvature = (th_f - th_n) / ds
            if abs(curvature) < 0.15:           # indistinguishable from straight
                curvature = 0.0
        if curvature == 0.0:
            # Straight: the whole shape's axis is the most accurate estimate.
            cx, cy, ax, ay = info['cx'], info['cy'], info['ax'], info['ay']
            slope = max(-6.7, min(6.7, ax / max(ay, 1e-3)))
        half = 0.5 * curvature
        fit = lambda y: cx + slope * (y - cy) + half * (y - cy) ** 2
        look_y = min(self.lookahead_m, info['y_max'])
        y_lo = info['y_min']
        y_ref = LINE_REF_Y_M
        self._prev_off, self._prev_t = float(fit(y_ref)), time.monotonic()
        sample = np.linspace(min(y_ref, y_lo, look_y), look_y, 8)
        return LineGeometry(
            found=True,
            offset_m=float(fit(y_ref)),
            angle_deg=math.degrees(math.atan(slope)) if abs(ay) > 0.05 else math.degrees(math.atan2(ax, ay)),
            lookahead_m=float(fit(look_y)),
            look_dist_m=float(look_y),
            curvature=float(curvature),
            strips_used=len(pts),
            curve=tuple((float(fit(y)), float(y)) for y in sample),
            look_point=(float(fit(look_y)), float(look_y)),
        )

    # ── obstacle ─────────────────────────────────────────────────────
    def _analyze_obstacle(self, bev: np.ndarray, yellow: np.ndarray) -> ObstacleResult:
        # Half resolution: 4x cheaper and the AREA resize doubles as a blur.
        hw, hh = self.bev_w // 2, self.bev_h // 2
        ppm = BEV_PX_PER_M / 2.0
        small = cv2.resize(bev, (hw, hh), interpolation=cv2.INTER_AREA)
        lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
        ycov = cv2.resize(cv2.dilate(yellow, np.ones((9, 9), np.uint8)), (hw, hh),
                          interpolation=cv2.INTER_NEAREST) > 0
        region = self._corr_half & ~ycov
        if int(region.sum()) < 200:
            return self._debounce('CLEAR', 0.0)

        floor = np.median(lab[region][::2], axis=0)
        if self._floor_lab is None:
            self._floor_lab = floor
        diff = lab - self._floor_lab
        # A shadow only darkens: count lightness loss at a fraction of a
        # lightness gain or a colour change.
        wl = np.where(diff[..., 0] < 0, 0.15, 0.5)
        d = np.sqrt((wl * diff[..., 0]) ** 2 + diff[..., 1] ** 2 + diff[..., 2] ** 2)
        obs = ((d > OBST_DELTA_E) & region).astype(np.uint8) * 255
        obs = cv2.morphologyEx(obs, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        obs = cv2.morphologyEx(obs, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(obs, connectivity=8)

        self._dbg = {'bev': bev, 'yellow': yellow, 'obs': obs, 'region': region,
                     'd': d}
        raw, dist = 'CLEAR', 0.0
        min_area = OBST_MIN_AREA_M2 * ppm * ppm
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                continue
            bottom = stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]
            y_m = BEV_Y_FAR_M - bottom / float(ppm)       # nearest edge
            if dist == 0.0 or y_m < dist:
                dist = y_m
        if dist > 0.0:
            raw = ('STOP' if dist < OBST_STOP_M
                   else 'SLOW' if dist < OBST_SLOW_M else 'CLEAR')
        # Follow lighting changes, but never learn an obstacle as "floor".
        if raw == 'CLEAR' and self._obs_state == 'CLEAR' and dist == 0.0:
            self._floor_lab = 0.98 * self._floor_lab + 0.02 * floor
        return self._debounce(raw, dist)

    def _debounce(self, raw: str, dist: float) -> ObstacleResult:
        if raw == self._obs_state:
            self._obs_count = 0
        else:
            if raw == self._obs_pending:
                self._obs_count += 1
            else:
                self._obs_pending, self._obs_count = raw, 1
            if self._obs_count >= OBST_DEBOUNCE:
                self._obs_state, self._obs_count = raw, 0
        return ObstacleResult(self._obs_state, dist, raw)

    # ── debug view ───────────────────────────────────────────────────
    def _render_debug(self, bev, yellow, line, obstacle) -> np.ndarray:
        img = bev.copy()
        img[yellow > 0] = (0, 230, 255)
        cv2.rectangle(img, (self._corr_x0, 0), (self._corr_x1, self.bev_h - 1),
                      (255, 200, 0) if obstacle.state == 'CLEAR' else (0, 0, 255), 1)
        for name, dist, col in (('stop', OBST_STOP_M, (0, 0, 255)),
                                ('slow', OBST_SLOW_M, (0, 200, 255))):
            y = int(self._bev_px(0, dist)[1])
            if 0 <= y < self.bev_h:
                cv2.line(img, (0, y), (self.bev_w - 1, y), col, 1)
        if line.found:
            poly = np.array([self._bev_px(x, y) for x, y in line.curve], np.int32)
            cv2.polylines(img, [poly], False, (0, 255, 0), 2)
            if line.look_point:
                cv2.circle(img, tuple(int(v) for v in self._bev_px(*line.look_point)),
                           6, (0, 0, 255), -1)
        cv2.line(img, (self.bev_w // 2, 0), (self.bev_w // 2, self.bev_h), (255, 200, 0), 1)
        img[~self._valid] = (30, 30, 30)
        return img
