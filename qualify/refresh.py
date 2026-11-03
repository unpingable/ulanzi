#!/usr/bin/env python3
"""Benchmark live tile updates on the D200. Interactive: you watch and press keys.

Stop ulanzid first (it owns the device). Run from the repo:

    .venv/bin/python qualify/refresh.py [--seconds 20]

Each scenario sends a partial update of 1 or 5 metric tiles at a fixed interval.
While it runs, press keys now and then and watch the deck. The script records
upload size and write time per update, every key report, and the longest stall
of the input-polling loop (a host-side proxy for input responsiveness). After
each scenario it asks what you saw. Results land in a JSON receipt.
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
from ulanzi import metrics  # noqa: E402
from ulanzi import tiles  # noqa: E402

WIDGET_KEYS = {1: 'load', 2: 'cpu', 3: 'mem', 4: 'net', 5: 'disk'}  # keys 2..6
SCENARIOS = [(1, 2.0), (1, 1.0), (1, 0.5), (5, 2.0), (5, 1.0), (5, 0.5)]

LINES: asyncio.Queue = None


async def ask(prompt: str) -> str:
    while not LINES.empty():
        LINES.get_nowait()
    print(prompt, end='', flush=True)
    return (await LINES.get()).strip()


class Bench:
    def __init__(self, dev):
        self.dev = dev
        self.writer = deckproto.Writer(dev)
        self.sampler = metrics.Sampler()
        self.presses: list[float] = []
        self.poll_gaps: list[float] = []
        self.polling = False

    async def reader(self):
        last = time.monotonic()
        while True:
            inp = self.dev.read(1024)
            now = time.monotonic()
            if self.polling:
                self.poll_gaps.append(now - last)
            last = now
            parsed = deckproto.parse_report(inp) if inp else None
            if parsed and parsed[0] == 'button' and parsed[1][1]:
                self.presses.append(now)
                print(f'  [deck] press raw={parsed[1][0]}')
            if not inp:
                await asyncio.sleep(0.01)

    async def clock(self):
        while True:
            await self.writer.send(deckproto.clock_frame())
            await asyncio.sleep(1.0)

    def render(self, keys) -> dict[int, bytes]:
        cur = self.sampler.sample()
        return {n: tiles.render(tiles.WIDGETS[WIDGET_KEYS[n]](self.sampler, cur)) for n in keys}

    async def scenario(self, count: int, interval: float, seconds: float) -> dict:
        keys = list(WIDGET_KEYS)[:count]
        print(f'\n== {count} tile(s) every {interval} s for {seconds:g} s ==')
        print('  press a few keys while it runs; watch for flicker, lag, or the splash')
        uploads = []
        self.presses.clear()
        self.poll_gaps.clear()
        self.polling = True
        start = time.monotonic()
        tick = start
        while time.monotonic() - start < seconds:
            images = self.render(keys)
            render_done = time.monotonic()
            blob = deckzip.build_zip_images(images)
            took = await self.writer.send(deckproto.zip_frames(blob, partial=True))
            uploads.append({'bytes': len(blob), 'frames': -(-(len(blob) - 1016) // 1024) + 1,
                            'write_ms': round(took * 1000, 1),
                            'render_ms': round((render_done - tick) * 1000, 1)})
            tick += interval
            await asyncio.sleep(max(0.0, tick - time.monotonic()))
        self.polling = False
        writes = [u['write_ms'] for u in uploads]
        result = {
            'tiles': count, 'interval_s': interval, 'uploads': len(uploads),
            'bytes_median': int(statistics.median(u['bytes'] for u in uploads)),
            'write_ms_median': statistics.median(writes), 'write_ms_max': max(writes),
            'render_ms_median': statistics.median(u['render_ms'] for u in uploads),
            'presses_seen': len(self.presses),
            'max_input_poll_gap_ms': round(max(self.poll_gaps, default=0) * 1000, 1),
        }
        print(f'  uploads={result["uploads"]} ~{result["bytes_median"]} B, '
              f'write median {result["write_ms_median"]} ms max {result["write_ms_max"]} ms, '
              f'input poll gap max {result["max_input_poll_gap_ms"]} ms, presses {result["presses_seen"]}')
        result['flicker'] = await ask('  flicker or visible tearing? [y/n + note] ')
        result['input_felt'] = await ask('  did presses register promptly (see [deck] lines)? [y/n + note] ')
        result['firmware'] = await ask('  anything unhappy (splash, freeze, garbage, missing tiles)? [y/n + note] ')
        result['looks'] = await ask('  does this cadence look good for status tiles? [y/n + note] ')
        result['samples'] = uploads
        return result


async def main():
    global LINES
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seconds', type=float, default=20)
    ap.add_argument('--out', type=Path, default=None)
    args = ap.parse_args()

    loop = asyncio.get_running_loop()
    LINES = asyncio.Queue()
    loop.add_reader(sys.stdin, lambda: LINES.put_nowait(sys.stdin.readline()))

    dev = deckproto.open_device()
    if dev is None:
        sys.exit('no D200 found')
    b = Bench(dev)
    tasks = [asyncio.create_task(b.reader()), asyncio.create_task(b.clock())]
    results = []
    clock_check = {}
    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    try:
        await b.writer.send(deckproto.brightness_frame(60))
        full = {n: None for n in range(13)} | b.render(list(WIDGET_KEYS))
        full[0] = deckzip.render_tile('bench')
        await b.writer.send(deckproto.zip_frames(deckzip.build_zip_images(full)))
        await ask('Key 1 says "bench" and keys 2-6 show metric tiles? Enter to start. ')
        tasks.pop().cancel()  # stop clock writes only for the check, never while waiting on you
        print('\n== clock check: 12 s of brightness-only writes, no clock updates ==')
        for _ in range(6):
            await b.writer.send(deckproto.brightness_frame(60))
            await asyncio.sleep(2)
        clock_check['ticks_without_host'] = await ask(
            '  did the window clock keep counting seconds on its own? [y/n + note] ')
        tasks.append(asyncio.create_task(b.clock()))
        for count, interval in SCENARIOS:
            results.append(await b.scenario(count, interval, args.seconds))
    finally:
        for t in tasks:
            t.cancel()
        out = args.out or Path.home() / '.local/state/ulanzi' / f'refresh-{time.strftime("%Y%m%d-%H%M%S")}.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({'started': started, 'clock_check': clock_check,
                                   'results': results}, indent=2))
        print(f'\nreceipt: {out}')
        print(clock_check)
        for r in results:
            print({k: v for k, v in r.items() if k != 'samples'})


if __name__ == '__main__':
    asyncio.run(main())
