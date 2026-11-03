"""Deterministic Synesthesia-inspired projections of Ulanzi metric snapshots.

No capture, event bus or animation loop; called at the normal widget cadence.
"""
from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw

from ulanzi import tiles

KINDS = ('load', 'cpu', 'mem', 'net', 'disk')
COLORS = {
    'load': (181, 150, 255), 'cpu': (255, 132, 107),
    'mem': (97, 226, 174), 'net': (98, 203, 255), 'disk': (240, 199, 112),
}
TX = (247, 155, 193)
BACKGROUND = (8, 12, 20)
NETWORK_SCALE = 10_000_000  # bytes/s per direction, explicit fixed display scale


@dataclass(frozen=True)
class Mark:
    """One observed level: category, magnitude, identity, age, direction.

    Age is position in the bounded history (0=newest), not invented event time.
    Identity places marks stably; age dims old marks, never the current gauge.
    """
    category: str
    magnitude: float
    identity: str
    age: int = 0
    direction: int = 0

    @property
    def level(self):
        return min(1.0, max(0.0, self.magnitude))

    @property
    def clipped(self):
        return self.magnitude > 1


def levels(kind, sampler, current, ncpu):
    """Project existing Ulanzi meanings, retaining unknowns and RX/TX separately."""
    if ncpu < 1:
        raise ValueError('ncpu must be positive')
    if kind == 'load':
        return [(None if current.load is None else current.load[0] / ncpu, 0)]
    if kind == 'net':
        return [(None if v is None else v / NETWORK_SCALE, direction)
                for v, direction in ((current.rx, -1), (current.tx, 1))]
    return [(None if (v := getattr(current, kind)) is None else v / 100, 0)]


def history_marks(kind, sampler, ncpu):
    names = ('rx', 'tx') if kind == 'net' else ('load1' if kind == 'load' else kind,)
    scale = ncpu if kind == 'load' else NETWORK_SCALE if kind == 'net' else 100
    result = []
    for name in names:
        direction = -1 if name == 'rx' else 1 if name == 'tx' else 0
        for age, value in enumerate(reversed(tuple(sampler.history[name])[-30:])):
            if value is not None:
                result.append(Mark(kind, value / scale, name, age, direction))
    return result


def tint(color, strength):
    return tuple(round(a * strength) for a in color)


def anchor(identity):
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:2], 'big')


def render(kind, sampler, current, *, ncpu):
    """Return one real-size RGB tile; deterministic and bounded to 30 samples."""
    im = Image.new('RGB', (tiles.TILE, tiles.TILE), BACKGROUND)
    d = ImageDraw.Draw(im)
    color = COLORS[kind]
    d.text((12, 9), kind.upper(), font=tiles.font('bold', 20), fill=color)
    observed = levels(kind, sampler, current, ncpu)
    # Current unknowns stay unknown even when older history is available.
    if any(v is None for v, _ in observed):
        d.text((12, 62), 'N/A', font=tiles.font('bold', 44), fill=tiles.UNKNOWN)
        d.text((12, 166), 'NO CURRENT DATA', font=tiles.font('regular', 13), fill=tiles.UNKNOWN)
        return _png(im)

    marks = [Mark(kind, value, kind, direction=direction) for value, direction in observed]
    level = marks[0].level
    # Recent observations leave dim traces behind the current field.
    # Capacity gauges hold their exact newest value instead of inventing motion.
    if kind in ('load', 'cpu', 'net'):
        for mark in history_marks(kind, sampler, ncpu):
            if mark.age == 0 or mark.level == 0:
                continue
            x = 14 + (29 - mark.age) * 5.7
            y = 143 - mark.level * 97
            trace_color = TX if mark.direction == 1 else color
            d.line((x, 147, x, y), fill=tint(trace_color, .08 + .24 * (1 - mark.age / 30)), width=3)

    if kind == 'load':
        # A woven queue: greater normalized load fills more crossing lanes.
        for i in range(math.ceil(level * 18)):
            y = 43 + i * 5.7
            points = [(x, y + math.sin(x / 23 + i * .6) * 6) for x in range(14, 184, 3)]
            d.line(points, fill=tint(color, .45 + .5 * i / 18), width=2)
        caption = f'{current.load[0]:.2f} / {ncpu} CPUs'
    elif kind == 'cpu':
        # A stable stipple field. Occupied cells encode CPU busy, not task count.
        order = sorted(range(120), key=lambda i: anchor(f'cpu:{i}'))
        for i in order[:round(level * 120)]:
            x, y = 17 + (i % 12) * 14.6, 47 + (i // 12) * 10
            d.rectangle((x, y, x + 5, y + 5), fill=color)
        caption = f'{current.cpu:.0f}% busy'
    elif kind == 'mem':
        # Sample-and-hold reservoir: height means in-use fraction, no decay.
        d.rounded_rectangle((14, 42, 182, 147), radius=8, outline=tint(color, .3), width=2)
        height = round(level * 98)
        if height:
            for y in range(144 - height, 144, 4):
                d.line((19, y, 177, y), fill=tint(color, .45 + .5 * (y - 43) / 104), width=2)
        caption = f'{current.mem:.0f}% in use'
    elif kind == 'net':
        for mark, y in zip(marks, (65, 119)):
            c = color if mark.direction == -1 else TX
            label = 'RX' if mark.direction == -1 else 'TX'
            label_font = tiles.font('bold', 12)
            label_x = 182 - d.textlength(label, font=label_font) if mark.direction == -1 else 12
            d.text((label_x, y - 24), label, font=label_font, fill=c)
            for i in range(math.ceil(mark.level * 9)):
                x = 172 - i * 18 if mark.direction == -1 else 24 + i * 18
                # RX points left, TX points right. Zero produces no streaks.
                tip = x + 9 * mark.direction
                d.line((x - 8 * mark.direction, y, tip, y), fill=c, width=3)
                d.line((tip - 4 * mark.direction, y - 4, tip, y,
                        tip - 4 * mark.direction, y + 4), fill=c, width=2)
        caption = f'{tiles.human_rate(current.rx)} / {tiles.human_rate(current.tx)} B/s'
    else:
        # Angular occupancy, not filesystem I/O activity.
        for radius in (29, 39, 49):
            box = (98 - radius, 95 - radius, 98 + radius, 95 + radius)
            d.arc(box, -90, 270, fill=tint(color, .16), width=5)
            if level:
                d.arc(box, -90, -90 + level * 360, fill=color, width=5)
        caption = f'/ {current.disk:.0f}% full'
    if any(mark.clipped for mark in marks):
        d.text((145, 13), 'CLIP', font=tiles.font('bold', 12), fill=tiles.WARN)
    d.text((12, 156), caption, font=tiles._fit(d, caption, 'bold', 17, tiles.TILE - 24), fill=tiles.VALUE)
    scale = '10 MB/s each way' if kind == 'net' else 'full = 1 load / CPU' if kind == 'load' else 'full = 100%'
    d.text((12, 178), scale, font=tiles.font('regular', 13), fill=(135, 146, 166))
    return _png(im)


def _png(im):
    output = io.BytesIO()
    im.save(output, format='PNG')
    return output.getvalue()
