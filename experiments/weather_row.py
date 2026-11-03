"""Offline fixture comparisons for the live weather widgets.

Run from the checkout with python -m experiments.weather_row. This tool never
samples metrics, opens sockets or writes to hardware; fixtures are examples.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path

from PIL import Image, ImageDraw

from ulanzi import metrics
from ulanzi import tiles
from ulanzi.weather_tiles import KINDS, Mark, levels, history_marks, render, _png


def fixture(name):
    """Explicit synthetic examples with a shared 4-CPU normalization."""
    sampler = metrics.Sampler()
    if name == 'unknown':
        return sampler, metrics.Sample(t=60), 4
    busy = name == 'busy'
    for i in range(30):
        fraction = i / 29
        sampler.history['load1'].append(.4 + fraction * (5.2 if busy else .6))
        sampler.history['cpu'].append(8 + fraction * (85 if busy else 10))
        sampler.history['mem'].append(40 + fraction * (45 if busy else 2))
        sampler.history['disk'].append(68 + fraction * 2)
        sampler.history['rx'].append(fraction * (7_500_000 if busy else 25_000))
        sampler.history['tx'].append(fraction * (2_500_000 if busy else 8_000))
    h = sampler.history
    current = metrics.Sample(t=60, load=(h['load1'][-1], 1.2, .8),
                             **{k: h[k][-1] for k in ('cpu', 'mem', 'disk', 'rx', 'tx')})
    return sampler, current, 4


def comparison(name):
    sampler, current, ncpu = fixture(name)
    canvas = Image.new('RGB', (1108, 580), (17, 22, 31))
    d = ImageDraw.Draw(canvas)
    d.text((24, 13), f'ULANZI / {name.upper()} FIXTURE', font=tiles.font('bold', 24), fill=tiles.VALUE)
    d.text((24, 47), 'Same observations. Normal above, weather below. 196 x 196 pixels per key.',
           font=tiles.font('regular', 17), fill=(174, 185, 204))
    # The real widget functions normalize load using this dependency. Pass an
    # already normalized load spec here to keep fixture CPU count host-independent.
    load = tiles.TileSpec('load', None if current.load is None else f'{current.load[0]:.2f}',
                         sub='' if current.load is None else '5m 1.2  15m 0.8',
                         series=tuple(v / ncpu for v in sampler.history['load1']),
                         ref=1.0, warn=current.load is not None and current.load[0] / ncpu >= 1)
    for column, kind in enumerate(KINDS):
        x = 24 + column * 216
        spec = load if kind == 'load' else tiles.WIDGETS[kind](sampler, current)
        for row, png in enumerate((tiles.render(spec), render(kind, sampler, current, ncpu=ncpu))):
            y = 106 + row * 246
            d.text((x, y - 24), f'KEY {column + 1 + row * 5}', font=tiles.font('bold', 13), fill=(157, 169, 189))
            canvas.paste(Image.open(io.BytesIO(png)), (x, y))
    d.text((24, 558), 'Offline design study / fixture data / lower row is not installed / no animation or device writes',
           font=tiles.font('regular', 14), fill=(157, 169, 189))
    return _png(canvas)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/ulanzi-weather-row'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ('quiet', 'busy', 'unknown'):
        (args.output / f'{name}.png').write_bytes(comparison(name))
    print(args.output)


if __name__ == '__main__':
    main()
