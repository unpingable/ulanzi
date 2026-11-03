import io
from dataclasses import replace

from PIL import Image
import pytest

from ulanzi import metrics
from ulanzi import tiles
from ulanzi import weather_tiles
from experiments import weather_row as weather


def test_paired_rows_use_the_same_observations(monkeypatch):
    sampler, current, ncpu = weather.fixture('busy')
    monkeypatch.setattr(metrics, 'cpu_count', lambda: ncpu)
    sheet = Image.open(io.BytesIO(weather.comparison('busy')))
    for column, kind in enumerate(weather.KINDS):
        x = 24 + 216 * column
        ordinary = Image.open(io.BytesIO(tiles.render(tiles.WIDGETS[kind](sampler, current))))
        synth = Image.open(io.BytesIO(weather.render(kind, sampler, current, ncpu=ncpu)))
        assert ordinary.size == synth.size == (196, 196)
        assert sheet.crop((x, 106, x + 196, 302)).tobytes() == ordinary.tobytes()
        assert sheet.crop((x, 352, x + 196, 548)).tobytes() == synth.tobytes()


def test_unknown_is_not_zero_or_old_data():
    sampler, current, ncpu = weather.fixture('busy')
    empty = metrics.Sample(t=60)
    zero = metrics.Sample(t=60, load=(0, 0, 0), cpu=0, mem=0, disk=0, rx=0, tx=0)
    fresh = metrics.Sampler()
    for kind in weather.KINDS:
        unknown = weather.render(kind, sampler, empty, ncpu=ncpu)
        assert unknown == weather.render(kind, fresh, empty, ncpu=ncpu)
        assert unknown != weather.render(kind, fresh, zero, ncpu=ncpu)
    assert weather.render('net', sampler, replace(current, tx=None), ncpu=ncpu) == weather.render('net', fresh, empty, ncpu=ncpu)


def test_direction_and_load_normalization_are_semantic():
    sampler, current, _ = weather.fixture('busy')
    value, direction = weather.levels('load', sampler, current, 4)[0]
    assert value == pytest.approx(1.4) and direction == 0
    assert weather.levels('net', sampler, current, 4) == [(.75, -1), (.25, 1)]
    reversed_flow = replace(current, rx=current.tx, tx=current.rx)
    assert weather.render('net', sampler, current, ncpu=4) != weather.render('net', sampler, reversed_flow, ncpu=4)


def test_history_is_bounded_preserves_gaps_and_ages_and_does_not_drain_gauges():
    sampler, current, _ = weather.fixture('busy')
    sampler.history['cpu'] = [99] * 100 + [None, 20, None, 60]
    marks = weather.history_marks('cpu', sampler, 4)
    assert len(marks) == 28 and max(m.age for m in marks) == 29
    assert [(m.age, m.magnitude) for m in marks[:2]] == [(0, .6), (2, .2)]
    for kind in ('mem', 'disk'):
        before = weather.render(kind, sampler, current, ncpu=4)
        sampler.history[kind].clear()
        assert before == weather.render(kind, sampler, current, ncpu=4)


def test_repeatable_pixels_and_visible_saturation():
    sampler, current, _ = weather.fixture('busy')
    for kind in weather.KINDS:
        assert weather.render(kind, sampler, current, ncpu=4) == weather.render(kind, sampler, current, ncpu=4)
    load = Image.open(io.BytesIO(weather.render('load', sampler, current, ncpu=4)))
    assert tiles.WARN in {rgb for _, rgb in load.getcolors(196 * 196)}


def test_network_rx_is_right_anchored_and_tx_is_left_anchored():
    sampler = metrics.Sampler()
    current = metrics.Sample(t=1, rx=1, tx=1)
    im = Image.open(io.BytesIO(weather.render('net', sampler, current, ncpu=1)))
    assert im.getpixel((163, 65)) == weather_tiles.COLORS['net']
    assert im.getpixel((33, 119)) == weather_tiles.TX
    assert im.getpixel((19, 65)) == weather_tiles.BACKGROUND
    zero = Image.open(io.BytesIO(weather.render('net', sampler, replace(current, rx=0, tx=0), ncpu=1)))
    assert zero.getpixel((163, 65)) == zero.getpixel((33, 119)) == weather_tiles.BACKGROUND
