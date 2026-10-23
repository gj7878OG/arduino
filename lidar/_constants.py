"""Protocol and detector constants for the rotating LiDAR.

Split out of the safety module so the parser, the reader and the
dashboard publisher can share them without importing each other.
"""

from __future__ import annotations

# 7-byte SCAN response header on this "rtm" / Model 18 clone. Stock A1
# uses 5 bytes; misreading this offset shifts every subsequent field by
# the wrong number of bytes. Keep the constant here so the parser and
# the regression test agree.
SCAN_HEADER_LEN = 7
SCAN_HEADER = b"\xA5\x5A"
NODE_LEN = 5

# RPLIDAR SDK units: angle is 1/64 degree, distance is 0.25 mm (q2).
ANGLE_SCALE = 64.0
DIST_Q2_TO_M = 0.00025  # 1 / (4 * 1000)

# Stuck / desync detector thresholds.
#
# A frozen *front* reading means nothing on its own: a wall 4 m away
# genuinely reports 4.05, 4.06, 4.05, 4.07 m as the head sweeps, and an
# earlier "front value unchanged for 2 s" rule false-positived in any
# ordinary room within seconds of start-up (verified on the real sensor).
# What actually identifies a wedged or replaying sensor is that *every*
# sector agrees: real geometry always varies around the circle. So the
# detector watches the spread across all sectors instead.
STUCK_SPREAD_M = 0.05       # total spread across sectors that reads as "flat"
STUCK_HOLD_S = 2.0          # how long the flat sweep is held
STUCK_MIN_SAMPLES = 60      # too few points to judge a sweep
NOISY_FRACTION = 0.30       # >= 30 % bad points → noisy
MOTOR_SPAN_MIN_DEG = 60.0   # below this span over the window → stalled

# points.json writer. The dashboard polls the file every 250 ms and the
# writer republishes every 100 ms, so a slightly wider window than the
# poll interval keeps the trail continuous between ticks.
POINTS_WINDOW_S = 2.0
POINTS_MAX = 3000

# Reconnect policy. A read *exception* already triggers a reopen, but a head
# that is still spinning up — or one whose motor has stalled — keeps the USB
# handle perfectly healthy while producing no nodes, so no exception is ever
# raised and the sensor just sits there reporting `stale`. Rebuilding re-issues
# the SCAN command, which is what restarts a stalled head.
#
# Well above LIDAR_STALE_MS (200 ms) so ordinary gaps between sweeps never
# trip it, and rate-limited by its own backoff so a sensor that is genuinely
# gone costs one reopen attempt per interval rather than one per scan cycle.
REOPEN_QUIET_S = 5.0
REOPEN_BACKOFF_S = 10.0

__all__ = [
    'SCAN_HEADER_LEN', 'SCAN_HEADER', 'NODE_LEN',
    'ANGLE_SCALE', 'DIST_Q2_TO_M',
    'STUCK_SPREAD_M', 'STUCK_HOLD_S', 'STUCK_MIN_SAMPLES',
    'NOISY_FRACTION', 'MOTOR_SPAN_MIN_DEG',
    'POINTS_WINDOW_S', 'POINTS_MAX',
    'REOPEN_QUIET_S', 'REOPEN_BACKOFF_S',
]