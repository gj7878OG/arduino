"""Steering tune: min-turn floor + lookahead, end to end.

Two complaints, two knobs, and this file pins the link between each knob and
the thing it is supposed to change:

  turn_floor_base  the rover is 10-20 cm off the line and drives straight. The
                   floor is the minimum wheel difference it may command while
                   correcting. At the shipped default it is 0, and pure pursuit
                   alone asks for only a few PWM, so the floor is the thing that
                   actually makes the wheels turn.
  lookahead_m      the rover corrects but still rides a few cm off-centre. That
                   is the lookahead, not the floor, and it has to reach *both*
                   the controller fallback and the vision sampling distance.

The clamping matters as much as the value: these arrive from a browser and from
a hand-editable control.json, so neither may be able to ask for more turn
authority than the fully-boosted case already allows, nor a lookahead short
enough to lose a usable line sample.
"""
import _env  # noqa: F401  (must be first)
import json
import os

import config
import control_state
import decision
import vision
import web
from vision import LineGeometry, ObstacleResult

assert control_state.CONTROL_PATH.startswith(_env.TMP), control_state.CONTROL_PATH


def _tmp(name):
    d = os.path.join(_env.TMP, name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'control.json')


def _drive(lc, off, dist, ticks=3, v_scale=1.0):
    """Feed a straight line `off` metres to one side, sampled at `dist`."""
    lc.speed_scale = v_scale
    lc.moving = True
    t = 0.0
    d = None
    for _ in range(ticks):
        lg = LineGeometry(found=True, offset_m=off, angle_deg=0.0,
                          lookahead_m=off, look_dist_m=dist, curvature=0.0)
        d = lc.decide(lg, True, ObstacleResult(), True, t)
        t += 0.05
    return d


# ── clamping ────────────────────────────────────────────────────────

def test_floor_clamped_at_both_ends():
    p = _tmp('floor')
    assert control_state.write_state(turn_floor_base=40, path=p)['turn_floor_base'] == 40.0
    assert control_state.write_state(turn_floor_base=999, path=p)['turn_floor_base'] == 40.0
    assert control_state.write_state(turn_floor_base=-50, path=p)['turn_floor_base'] == 0.0
    # The cap is TURN_FLOOR, the maximum the boost ramp already allows, so the
    # slider can only ever take headroom away from that ramp, never add to it.
    assert control_state.TURN_FLOOR_BASE_MAX == float(config.TURN_FLOOR)


def test_lookahead_clamped_at_both_ends():
    p = _tmp('look')
    assert control_state.write_state(lookahead_m=0.30, path=p)['lookahead_m'] == 0.30
    assert control_state.write_state(lookahead_m=1.00, path=p)['lookahead_m'] == 1.00
    assert control_state.write_state(lookahead_m=5.0, path=p)['lookahead_m'] == 1.00
    # Below the controller's 0.2 m "no usable sample" cut-off is never offered,
    # so a tuned value cannot put the fallback path in play.
    assert control_state.write_state(lookahead_m=0.05, path=p)['lookahead_m'] == \
        control_state.LOOKAHEAD_MIN_M > 0.2


def test_hand_edited_file_is_clamped_on_read_back():
    """A hand-edited control.json must not be trusted over the clamps."""
    p = _tmp('handedit')
    json.dump({'enabled': False, 'turn_floor_base': 9000, 'lookahead_m': 0.001},
              open(p, 'w'))
    st = control_state.read_state(p)
    assert st['turn_floor_base'] == control_state.TURN_FLOOR_BASE_MAX
    assert st['lookahead_m'] == control_state.LOOKAHEAD_MIN_M


def test_old_file_without_the_keys_defaults_to_shipped_values():
    """An older control.json has neither key: behaviour must not change."""
    p = _tmp('old')
    json.dump({'enabled': False, 'max_speed_pct': 50}, open(p, 'w'))
    st = control_state.read_state(p)
    assert st['turn_floor_base'] == float(config.TURN_FLOOR_BASE) == 0.0
    assert st['lookahead_m'] == float(config.LOOKAHEAD_M) == 1.0


def test_both_survive_a_restart():
    p = _tmp('persist')
    control_state.write_state(turn_floor_base=25, lookahead_m=0.6, path=p)
    st = control_state.read_state(p)
    assert (st['turn_floor_base'], st['lookahead_m']) == (25.0, 0.6)


# ── the floor actually changes the turn command ─────────────────────

def test_floor_raises_the_commanded_turn_when_line_is_off_centre():
    """The whole point: 15 cm off must not still command ~7 PWM."""
    base = _drive(decision.LineController(), 0.15, config.LOOKAHEAD_M).omega
    strong = decision.LineController()
    strong.turn_floor_base = 40.0
    hard = _drive(strong, 0.15, config.LOOKAHEAD_M).omega
    assert base < 10.0, f'pure pursuit alone should still be weak, got {base}'
    assert hard >= 40.0 - 0.5, f'floor should command at least its own value, got {hard}'
    assert hard > base


def test_floor_is_inert_on_a_centred_line():
    """A centred line must not be pushed off it just because the floor is up."""
    lc = decision.LineController()
    lc.turn_floor_base = 40.0
    assert abs(_drive(lc, 0.0, config.LOOKAHEAD_M).omega) < 1.0


def test_floor_survives_reset():
    """Re-arming the rover must not silently undo the operator's tuning."""
    lc = decision.LineController()
    lc.turn_floor_base = 30.0
    lc.lookahead_m = 0.55
    lc.reset()
    assert lc.turn_floor_base == 30.0
    assert lc.lookahead_m == 0.55


# ── the lookahead actually changes the geometry ─────────────────────

def test_shorter_lookahead_asks_for_a_stronger_turn():
    """Centring is the lookahead's job, so it must move the demand."""
    far = _drive(decision.LineController(), 0.15, 1.0).omega
    near = _drive(decision.LineController(), 0.15, 0.4).omega
    assert near > far, f'shorter lookahead should ask for more turn ({near} vs {far})'


def test_lookahead_defaults_to_the_config_constant():
    assert decision.LineController().lookahead_m == config.LOOKAHEAD_M
    assert vision.Vision().lookahead_m == config.LOOKAHEAD_M


def test_controller_uses_its_lookahead_when_the_line_sample_is_unusable():
    """look_dist_m <= 0.2 is the fallback path; it must follow the slider."""
    lc = decision.LineController()
    lc.lookahead_m = 0.4
    d = _drive(lc, 0.15, 0.1)          # too short to give a usable sample
    assert abs(d.omega) > 0.0


# ── /api/steer ──────────────────────────────────────────────────────

def _post(client, **body):
    return client.post('/api/steer', json=body)


def test_api_sets_both_and_returns_them():
    web.flask_app.config['TESTING'] = True
    with web.flask_app.test_client() as c:
        r = _post(c, turn_floor_base=30, lookahead_m=0.5)
        assert r.status_code == 200
        d = r.get_json()
        assert d['turn_floor_base'] == 30.0
        assert d['lookahead_m'] == 0.5
        # GET echoes the stored values back.
        assert c.get('/api/steer').get_json()['turn_floor_base'] == 30.0


def test_api_rejects_nonsense():
    web.flask_app.config['TESTING'] = True
    with web.flask_app.test_client() as c:
        assert _post(c, turn_floor_base='nonsense').status_code == 400
        assert _post(c, lookahead_m=[1]).status_code == 400


def test_api_clamps_out_of_range_requests():
    web.flask_app.config['TESTING'] = True
    with web.flask_app.test_client() as c:
        assert _post(c, turn_floor_base=9999).get_json()['turn_floor_base'] == 40.0
        assert _post(c, lookahead_m=99).get_json()['lookahead_m'] == 1.0


# ── dashboard UI ────────────────────────────────────────────────────

def _html():
    with web.flask_app.test_client() as c:
        return c.get('/').get_data(as_text=True)


def test_drive_tab_has_both_sliders():
    html = _html()
    for el in ('floor-range', 'floor-val', 'floor-applied',
               'look-range', 'look-val', 'look-applied'):
        assert f'id="{el}"' in html, f'missing slider element {el}'


def test_sliders_are_rendered_from_the_shipped_config():
    """Markup must not hard-code a range the server would clamp anyway."""
    html = _html()
    floor = html.split('id="floor-range"')[1].split('>')[0]
    assert 'max="40"' in floor and 'min="0"' in floor
    look = html.split('id="look-range"')[1].split('>')[0]
    assert 'value="1.0' in look


def test_js_posts_both_keys_to_the_steer_endpoint():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")).read()
    assert "'/api/steer'" in src
    assert "'turn_floor_base'" in src
    assert "'lookahead_m'" in src