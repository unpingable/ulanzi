"""Status tiles: a title, a headline value, optional small text, and a sparkline.

Conventional widgets turn the latest sample and history into a TileSpec;
render() turns that into a 196x196 PNG. render_widget() also dispatches weather
projections of the same observations. Output is deterministic for the same input,
so byte equality means "nothing visible changed".
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ulanzi import metrics

TILE = 196
PAD = 10
BG = (0, 0, 0)
TITLE = (150, 150, 150)
VALUE = (255, 255, 255)
SUB = (170, 170, 170)
UNKNOWN = (120, 120, 120)
WARN = (255, 176, 32)
LINE = (80, 200, 255)
FILL = (20, 70, 95)
REF = (90, 90, 90)
GRAPH_TOP = 120
GRAPH_BOTTOM = TILE - PAD
FONTS = {
    'bold': ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
             '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf',
             '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'],
    'regular': ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf',
                '/usr/share/fonts/TTF/DejaVuSans.ttf'],
}


@lru_cache(maxsize=None)
def font(kind: str, size: int):
    for path in FONTS[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


@dataclass(frozen=True)
class TileSpec:
    title: str
    value: str | None           # None renders as N/A
    sub: str = ''
    series: tuple[float | None, ...] = ()
    lo: float = 0.0
    hi: float | None = None     # None: scale to the series max
    ref: float | None = None    # draw a faint horizontal line at this value
    warn: bool = False
    bar: float | None = None    # a 0..1 fill bar instead of a sparkline


def _fit(draw, text: str, kind: str, start: int, width: int):
    for size in range(start, 11, -2):
        f = font(kind, size)
        if draw.textlength(text, font=f) <= width:
            return f
    return font(kind, 12)


def render(spec: TileSpec) -> bytes:
    img = Image.new('RGB', (TILE, TILE), BG)
    d = ImageDraw.Draw(img)
    room = TILE - 2 * PAD

    d.text((PAD, PAD), spec.title.upper(), font=font('bold', 22), fill=TITLE)
    if spec.value is None:
        d.text((PAD, 40), 'N/A', font=font('bold', 50), fill=UNKNOWN)
    else:
        color = WARN if spec.warn else VALUE
        d.text((PAD, 38), spec.value, font=_fit(d, spec.value, 'bold', 54, room), fill=color)
    if spec.sub:
        d.text((PAD, 98), spec.sub, font=_fit(d, spec.sub, 'regular', 17, room), fill=SUB)

    if spec.bar is not None:
        _bar(d, spec)
    else:
        _sparkline(d, spec)
    out = io.BytesIO()
    img.save(out, 'PNG', optimize=False)
    return out.getvalue()


def _bar(d, spec: TileSpec):
    top, bottom = GRAPH_TOP + 20, GRAPH_BOTTOM - 10
    d.rectangle([PAD, top, TILE - PAD, bottom], outline=REF, width=2)
    fill = min(max(spec.bar, 0.0), 1.0)
    inner = TILE - 2 * PAD - 8
    if fill > 0:
        d.rectangle([PAD + 4, top + 4, PAD + 4 + round(inner * fill), bottom - 4],
                    fill=WARN if spec.warn else LINE)


def _sparkline(d, spec: TileSpec):
    known = [v for v in spec.series if v is not None]
    if not known:
        return
    hi = spec.hi if spec.hi is not None else max(known)
    if spec.ref is not None:
        hi = max(hi, spec.ref)
    span = max(hi - spec.lo, 1e-9)
    n = len(spec.series)
    left, right = PAD, TILE - PAD
    step = (right - left) / max(n - 1, 1)

    def y(v):
        v = min(max(v, spec.lo), hi)
        return round(GRAPH_BOTTOM - (v - spec.lo) / span * (GRAPH_BOTTOM - GRAPH_TOP))

    run: list[tuple[int, int]] = []
    runs = []
    for i, v in enumerate(spec.series):
        if v is None:
            if run:
                runs.append(run)
            run = []
            continue
        run.append((round(left + i * step), y(v)))
    if run:
        runs.append(run)
    for pts in runs:
        if len(pts) > 1:
            d.polygon(pts + [(pts[-1][0], GRAPH_BOTTOM), (pts[0][0], GRAPH_BOTTOM)], fill=FILL)
            d.line(pts, fill=LINE, width=3)
        else:
            x, py = pts[0]
            d.ellipse([x - 2, py - 2, x + 2, py + 2], fill=LINE)
    if spec.ref is not None:
        ry = y(spec.ref)
        for x in range(left, right, 8):
            d.line([(x, ry), (x + 3, ry)], fill=VALUE, width=2)
    d.line([(left, GRAPH_BOTTOM), (right, GRAPH_BOTTOM)], fill=REF)


# --- widgets -----------------------------------------------------------------

def human_rate(bps: float | None) -> str:
    if bps is None:
        return '?'
    for unit in ('', 'K', 'M', 'G'):
        if bps < 1000 or unit == 'G':
            return f'{bps:.0f}{unit}' if bps >= 10 or unit == '' else f'{bps:.1f}{unit}'
        bps /= 1000
    return '?'


def _pct(title: str, value: float | None, series, warn_at: float) -> TileSpec:
    return TileSpec(title=title, value=None if value is None else f'{value:.0f}%',
                    series=tuple(series), lo=0, hi=100,
                    warn=value is not None and value >= warn_at)


def load_widget(s: metrics.Sampler, cur: metrics.Sample) -> TileSpec:
    """Headline is load1 as reported. The graph is load1 / CPU count, with a faint
    line at 1.0 (as many runnable tasks as CPUs). Load is run-queue length, not CPU %."""
    ncpu = metrics.cpu_count()
    series = tuple(None if v is None else v / ncpu for v in s.history['load1'])
    if cur.load is None:
        return TileSpec('load', None, series=series, ref=1.0)
    l1, l5, l15 = cur.load
    return TileSpec('load', f'{l1:.2f}', sub=f'5m {l5:.1f}  15m {l15:.1f}',
                    series=series, lo=0, hi=None, ref=1.0, warn=l1 / ncpu >= 1.0)


def cpu_widget(s, cur):
    return _pct('cpu', cur.cpu, s.history['cpu'], 90)


def mem_widget(s, cur):
    return _pct('mem', cur.mem, s.history['mem'], 90)


def disk_widget(s, cur):
    # fullness moves too slowly for a minute of history to say anything; show a bar
    spec = _pct('disk /', cur.disk, (), 90)
    return TileSpec(spec.title, spec.value, warn=spec.warn,
                    bar=None if cur.disk is None else cur.disk / 100)


def net_widget(s, cur):
    total = tuple(None if rx is None or tx is None else rx + tx
                  for rx, tx in zip(s.history['rx'], s.history['tx']))
    value = None if cur.rx is None or cur.tx is None else human_rate(cur.rx + cur.tx) + 'B/s'
    return TileSpec('net', value, sub=f'rx {human_rate(cur.rx)} tx {human_rate(cur.tx)}',
                    series=total, lo=0, hi=None)


WIDGETS = {
    'load': load_widget,
    'cpu': cpu_widget,
    'mem': mem_widget,
    'disk': disk_widget,
    'net': net_widget,
}

WEATHER_WIDGETS = {f'weather_{kind}': kind for kind in WIDGETS}
AGENTS = {'claude': (240, 169, 127), 'codex': (120, 225, 175),
          'kimi': (112, 186, 255), 'agy': (189, 158, 255)}
AGENT_WIDGETS = {f'agent_{name}': name for name in AGENTS}
WIDGET_NAMES = (*WIDGETS, *WEATHER_WIDGETS, *AGENT_WIDGETS)


def render_attention(agent, state='idle', count=0, repos='', updated=None):
    """An honest attention state; a finished turn is never labeled successful."""
    img = Image.new('RGB', (TILE, TILE), (8, 12, 20))
    draw = ImageDraw.Draw(img)
    color = AGENTS[agent]
    stale = updated is not None and time.time() - updated > 30 * 60
    words = {'idle': 'QUIET', 'waiting': 'WAIT', 'done': 'DONE',
             'error': 'ERROR', 'interrupted': 'STOPPED', 'working': 'ACTIVE'}
    word = 'STALE' if stale else words[state]
    border = UNKNOWN if stale else WARN if state == 'waiting' else (255, 105, 105) if state == 'error' else color
    draw.rounded_rectangle((3, 3, TILE-4, TILE-4), radius=12, outline=border,
                           width=5 if state == 'waiting' and not stale else 2)
    draw.text((14, 13), agent.upper(), font=font('bold', 20), fill=color)
    draw.text((14, 48), word, font=_fit(draw, word, 'bold', 38, TILE-28), fill=border)
    if count:
        draw.text((14, 91), str(count), font=font('bold', 30), fill=VALUE)
        text = repos[:80]
        # Long single project names must fit instead of clipping at the edge.
        while text and draw.textlength(text, font=font('regular', 16)) > TILE-28:
            text = text[:-1]
        if text != repos:
            text = text[:-1] + '…'
        draw.text((14, 130), text, font=font('regular', 16), fill=VALUE)
    footer = ('LAST KNOWN STATE' if stale else 'CHECK AGENT' if state == 'waiting'
              else 'AGENT ACTIVE' if state == 'working' else 'PRESS TO ACK' if count else 'NO PENDING EVENTS')
    draw.text((14, 170), footer, font=font('bold', 12), fill=SUB)
    out = io.BytesIO()
    img.save(out, 'PNG')
    return out.getvalue()


def render_widget(name: str, sampler: metrics.Sampler, current: metrics.Sample) -> bytes:
    """Both styles consume the same sample and bounded history in one refresh."""
    if name in AGENT_WIDGETS:
        return render_attention(AGENT_WIDGETS[name])
    if name in WEATHER_WIDGETS:
        # Import after tiles initializes: the visual renderer reuses our fonts.
        from ulanzi import weather_tiles
        return weather_tiles.render(WEATHER_WIDGETS[name], sampler, current,
                                    ncpu=metrics.cpu_count())
    return render(WIDGETS[name](sampler, current))
