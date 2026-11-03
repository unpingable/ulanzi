#!/usr/bin/env python3
"""Check that reused icon filenames show new content (no stale firmware cache).

Stop ulanzid first (it owns the device). Run from the repo:

    .venv/bin/python qualify/stable.py [--updates 30] [--interval 1.0]

Keys 1-4 get daemon-sized metric tiles under the daemon's bounded names (two per
key, alternating), each showing the update number. So every name is reused about
--updates/2 times with different content. Watch that all four numbers advance by one
each step and never jump back. The run ends with a full upload that reuses the
names again with "FULL" tiles. Answers land in a JSON receipt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ulanzi import deckproto  # noqa: E402
from ulanzi import deckzip  # noqa: E402
from ulanzi import tiles  # noqa: E402

KEYS = range(4)


def tile(k: int, text: str, i: int) -> bytes:
    series = tuple(((i + j) * 37 + k * 11) % 100 for j in range(30))
    return tiles.render(tiles.TileSpec(f'key {k + 1}', text, series=series, lo=0, hi=100))


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--updates', type=int, default=30)
    ap.add_argument('--interval', type=float, default=1.0)
    args = ap.parse_args()

    lines: asyncio.Queue = asyncio.Queue()
    asyncio.get_running_loop().add_reader(sys.stdin, lambda: lines.put_nowait(sys.stdin.readline()))

    async def ask(prompt):
        while not lines.empty():
            lines.get_nowait()
        print(prompt, end='', flush=True)
        return (await lines.get()).strip()

    dev = deckproto.open_device()
    if dev is None:
        sys.exit('no D200 found')
    w = deckproto.Writer(dev)
    slots = {k: 0 for k in KEYS}
    writes = []

    async def keepalive():
        while True:
            await asyncio.sleep(1.0)
            if time.monotonic() - w.last_write > 1.0:
                await w.send(deckproto.brightness_frame(60))

    ka = asyncio.create_task(keepalive())
    images = {n: None for n in range(13)} | {k: tile(k, '0', 0) for k in KEYS}
    await w.send(deckproto.brightness_frame(60))
    await w.send(deckproto.zip_frames(deckzip.build_zip_images(
        images, names={k: deckzip.slot_name(k, 0) for k in KEYS})))
    await ask('Keys 1-4 show "0"? Enter to start, then watch them count up. ')

    for i in range(1, args.updates + 1):
        slots = {k: 1 - s for k, s in slots.items()}
        blob = deckzip.build_zip_images({k: tile(k, str(i), i) for k in KEYS},
                                        names={k: deckzip.slot_name(k, slots[k]) for k in KEYS})
        writes.append(round(await w.send(deckproto.zip_frames(blob, partial=True)) * 1000, 1))
        print(f'  sent {i}', end='\r', flush=True)
        await asyncio.sleep(args.interval)
    print()
    advanced = await ask(f'Did all four keys count 1..{args.updates}, one step at a time, never '
                         'freezing or jumping back? [y/n + note] ')

    slots = {k: 1 - s for k, s in slots.items()}
    full = {n: None for n in range(13)} | {k: tile(k, 'FULL', 0) for k in KEYS}
    await w.send(deckproto.zip_frames(deckzip.build_zip_images(
        full, names={k: deckzip.slot_name(k, slots[k]) for k in KEYS})))
    full_ok = await ask('Full upload sent. Do keys 1-4 now show "FULL"? [y/n + note] ')
    ka.cancel()

    receipt = {'started': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'updates': args.updates,
               'interval_s': args.interval, 'names_per_key': 2, 'write_ms': writes,
               'advanced_correctly': advanced, 'full_upload_reused_names_shown': full_ok}
    out = Path.home() / '.local/state/ulanzi' / f'stable-{time.strftime("%Y%m%d-%H%M%S")}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2))
    print(f'receipt: {out}')
    print({k: v for k, v in receipt.items() if k != 'write_ms'})


if __name__ == '__main__':
    asyncio.run(main())
