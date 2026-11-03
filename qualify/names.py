#!/usr/bin/env python3
"""Find what makes repeated partial updates fail on the D200. Mostly unattended.

Stop ulanzid first (it owns the device). Run from the repo:

    .venv/bin/python qualify/names.py [--variants CADB] [--count 2000] [--interval 0.5]
                                      [--keys 4 --style metric]

--keys N --style metric sends N metric-sized tiles (sparkline, ~5 KB each) per
update, keys 1..N, which matches what the daemon uploads (~20 KB per update).

Each variant sends --count partial updates of key 1 and stops early if a write fails:

  A  stable filename, changing PNG     (the key shows "A" and the update number)
  B  unique filename, changing PNG
  C  stable filename, identical PNG    (the key shows a fixed "C")
  D  unique filename, identical PNG

Between variants the deck is reset: 15 s of silence (it falls back to the splash),
then a full layout upload, which cleared the failure once already.

Glance at the deck now and then. If key 1 goes blank, or its number stops
advancing while the script says it is still sending, press any OTHER deck key
(or Enter here). That marks the update number in the receipt. Write latency and
failures are recorded for every update either way.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ulanzi import deckproto  # noqa: E402
from ulanzi import deckzip  # noqa: E402
from ulanzi import tiles  # noqa: E402

KEY = 0
VARIANTS = {
    'A': ('stable', 'changing'),
    'B': ('unique', 'changing'),
    'C': ('stable', 'identical'),
    'D': ('unique', 'identical'),
}


class Run:
    def __init__(self, dev):
        self.dev = dev
        self.writer = deckproto.Writer(dev)
        self.marks: list[tuple[float, str]] = []
        self.reports: dict[str, int] = {}
        self.lines: asyncio.Queue = asyncio.Queue()

    async def reader(self):
        while True:
            try:
                inp = self.dev.read(1024)
            except OSError:
                return
            if not inp:
                await asyncio.sleep(0.01)
                continue
            head = bytes(inp[2:4]).hex()
            self.reports[head] = self.reports.get(head, 0) + 1
            parsed = deckproto.parse_report(inp)
            if parsed and parsed[0] == 'button' and parsed[1][1] and parsed[1][0] != KEY:
                self.marks.append((time.monotonic(), 'deck key'))
                print('  [marked from deck]')

    async def reset(self, label: str):
        print(f'  reset: 15 s silence, then a full upload ({label})')
        await asyncio.sleep(15)
        images = {n: None for n in range(13)}
        images[KEY] = deckzip.render_tile(label)
        images[5] = deckzip.render_tile('press a key here if 1 fails')
        await self.writer.send(deckproto.zip_frames(deckzip.build_zip_images(images)))
        await self.writer.send(deckproto.brightness_frame(60))

    @staticmethod
    def metric_tile(letter: str, k: int, i: int) -> bytes:
        # deterministic pseudo-metric: a 30-point series that shifts every update
        series = tuple(((i + j) * 37 + k * 11) % 100 for j in range(30))
        return tiles.render(tiles.TileSpec(f'{letter} key {k + 1}', str(i), sub=f'bytes test {k}',
                                           series=series, lo=0, hi=100))

    async def variant(self, letter: str, count: int, interval: float, keys: int = 1,
                      style: str = 'text') -> dict:
        naming, content = VARIANTS[letter]
        print(f'\n== {letter}: {naming} filename, {content} PNG, {keys} {style} tile(s), '
              f'{count} updates every {interval} s ==')
        await self.reset(f'{letter} ready')
        if style == 'metric':
            fixed_imgs = {k: self.metric_tile(letter, k, 0) for k in range(keys)}
        else:
            fixed_imgs = {k: deckzip.render_tile(f'{letter}\nfixed') for k in range(keys)}
        sizes = []
        writes, failure, start = [], None, time.monotonic()
        self.marks.clear()
        while not self.lines.empty():
            self.lines.get_nowait()
        tick = time.monotonic()
        for i in range(1, count + 1):
            if content == 'identical':
                images = dict(fixed_imgs)
            elif style == 'metric':
                images = {k: self.metric_tile(letter, k, i) for k in range(keys)}
            else:
                images = {k: deckzip.render_tile(f'{letter}\n{i}') for k in range(keys)}
            names = {k: f'q{letter}{k}.png' if naming == 'stable' else f'q{letter}{k}_{i:06d}.png'
                     for k in range(keys)}
            blob = deckzip.build_zip_images(images, names=names)
            sizes.append(len(blob))
            try:
                took = await self.writer.send(deckproto.zip_frames(blob, partial=True))
            except OSError as e:
                failure = {'update': i, 'error': str(e)}
                print(f'  write failed at update {i}: {e}')
                break
            writes.append(took)
            if not self.lines.empty():
                self.lines.get_nowait()
                self.marks.append((time.monotonic(), 'enter'))
                print('  [marked from terminal]')
            if i % 100 == 0:
                recent = writes[-100:]
                print(f'  {i}/{count}  write median {statistics.median(recent) * 1000:.1f} ms '
                      f'max {max(recent) * 1000:.1f} ms')
            tick += interval
            await asyncio.sleep(max(0.0, tick - time.monotonic()))
        per_100 = [round(statistics.median(writes[j:j + 100]) * 1000, 1) for j in range(0, len(writes), 100)]
        marks = [{'after_s': round(t - start, 1), 'update_approx': round((t - start) / interval),
                  'via': via} for t, via in self.marks]
        return {'variant': letter, 'naming': naming, 'content': content, 'interval_s': interval,
                'keys': keys, 'style': style, 'zip_bytes_median': int(statistics.median(sizes)),
                'bytes_sent_total': sum(sizes[:len(writes)]),
                'sent': len(writes), 'write_failure': failure, 'operator_marks': marks,
                'write_ms_median_per_100': per_100,
                'write_ms_max': round(max(writes, default=0) * 1000, 1)}


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--variants', default='CADB')
    ap.add_argument('--count', type=int, default=2000)
    ap.add_argument('--interval', type=float, default=0.5)
    ap.add_argument('--keys', type=int, default=1, help='tiles per update, keys 1..N')
    ap.add_argument('--style', choices=('text', 'metric'), default='text')
    ap.add_argument('--out', type=Path, default=None)
    args = ap.parse_args()

    dev = deckproto.open_device()
    if dev is None:
        sys.exit('no D200 found')
    run = Run(dev)
    loop = asyncio.get_running_loop()
    loop.add_reader(sys.stdin, lambda: run.lines.put_nowait(sys.stdin.readline()))
    reader = asyncio.create_task(run.reader())
    results, started = [], time.strftime('%Y-%m-%dT%H:%M:%S%z')
    out = args.out or Path.home() / '.local/state/ulanzi' / f'names-{time.strftime("%Y%m%d-%H%M%S")}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        for letter in args.variants.upper():
            results.append(await run.variant(letter, args.count, args.interval, args.keys, args.style))
            out.write_text(json.dumps({'started': started, 'results': results,
                                       'reports_by_command': run.reports}, indent=2))
            r = results[-1]
            print(f'  {letter}: sent {r["sent"]} ({r["bytes_sent_total"] / 1e6:.1f} MB), '
                  f'failure {r["write_failure"]}, marks {r["operator_marks"]}')
    finally:
        reader.cancel()
        out.write_text(json.dumps({'started': started, 'results': results,
                                   'reports_by_command': run.reports}, indent=2))
        print(f'\nreceipt: {out}')
        for r in results:
            print({k: v for k, v in r.items() if k != 'write_ms_median_per_100'})


if __name__ == '__main__':
    asyncio.run(main())
