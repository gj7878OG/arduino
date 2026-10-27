"""Turn an interpretation into something Tarka says.

Pure: ``(Interpretation, RoverState) -> Plan``. It speaks; it never acts.

The one rule that shapes everything here: **never say something untrue**.

  * No battery sensor exists  -> "I can't measure the battery.", never a number.
  * No pose estimate exists   -> "I don't track my position."
  * Stale telemetry           -> "I can't reach the controller.", never old values.
  * A sensor that is offline or has stopped delivering is *offline*, not
    "clear": silence from a dead LiDAR must not be reported as an empty path.
  * A motion or stop command is answered with "Voice control isn't switched
    on" — never "Moving." or "Stopped.", because nothing moved or stopped.
    (That is ``dialogue`` / ``safety``'s job once they exist.)
"""

from __future__ import annotations

from typing import Optional

from .intents import Interpretation
from .rover_state import RoverState
from .speech_plan import EMPTY, Plan, distance_plan, event_plan


def motors_plan(s: RoverState) -> Plan:
    if s.enabled is None:
        return EMPTY
    return event_plan('status_motors_on' if s.enabled else 'motors_disabled')


def mode_plan(s: RoverState) -> Plan:
    if s.mode is None:
        return EMPTY
    return event_plan('status_mode_manual' if s.mode == 'manual' else 'status_mode_auto')


def obstacle_plan(s: RoverState) -> Plan:
    """What the LiDAR can honestly say about the way ahead."""
    if not s.fresh:
        return event_plan('controller_unreachable')
    if s.lidar_enabled is False:
        return event_plan('status_lidar_off')
    if not s.lidar_live:
        return event_plan('status_lidar_offline')
    if s.lidar_min_front_m is None:
        return event_plan('obstacle_none')
    dist = distance_plan(s.lidar_min_front_m)
    if dist is None:                                  # out of speakable range
        return event_plan('obstacle_none')
    lead = {'ESTOP': 'obstacle_stop', 'SLOW': 'obstacle_slow'}.get(s.lidar_action or '')
    return (event_plan(lead) + dist) if lead else dist


def status_report(s: RoverState) -> Plan:
    """Motors, mode, LiDAR, camera — or one honest line if we can't tell."""
    if not s.fresh:
        return event_plan('controller_unreachable')
    plan = motors_plan(s) + mode_plan(s)
    if s.lidar_enabled is False:
        plan += event_plan('status_lidar_off')
    elif not s.lidar_live:
        plan += event_plan('status_lidar_offline')
    elif s.lidar_min_front_m is None:
        plan += event_plan('status_lidar_clear')
    else:
        plan += obstacle_plan(s)
    if s.camera_connected is not None:
        plan += event_plan('status_camera_online' if s.camera_connected
                           else 'status_camera_offline')
    return plan


def respond(result: Interpretation, state: RoverState) -> Plan:
    """What to say for ``result``. Never empty for a recognised outcome."""
    if result.kind in ('clarify', 'refuse'):
        return event_plan(result.event)
    if result.kind != 'intent' or result.intent is None:
        return event_plan('clarify_default')

    action = result.intent.action
    if action == 'query_status':
        topic = dict(result.intent.args).get('topic', 'overall')
        if topic == 'battery':
            return event_plan('battery_unknown')
        if topic == 'obstacle':
            return obstacle_plan(state)
        if topic == 'help':
            return event_plan('intro') + event_plan('help_summary')
        if topic == 'mode':
            plan = (motors_plan(state) + mode_plan(state)) if state.fresh else EMPTY
            # Fresh telemetry but no control.json: we cannot say, and silence
            # would look like a hang.
            return plan or event_plan('controller_unreachable')
        return status_report(state)
    if action == 'query_position':
        return event_plan('position_unknown')
    if action in ('motion', 'stop'):
        return event_plan('voice_control_off')
    return event_plan('clarify_default')                 # 'unknown' and anything new


__all__ = ['respond', 'status_report', 'obstacle_plan']
