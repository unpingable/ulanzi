#!/usr/bin/env python3
"""Measure the D200's host watchdog on this unit. Interactive: you watch the deck.

Stop ulanzid first (it owns the device). Run from the repo:

    .venv/bin/python qualify/watchdog.py [--hold 120] [--runs 3] [--lock]

Experiments:
  1. silence   upload a layout, then stop writing; press Enter when the splash shows (x3)
  2. clock     small-window clock write every 2 s for --hold seconds
  3. bright    SET_BRIGHTNESS(current) every 2 s for --hold seconds
  4. lock      LOCKSCREEN / UNLOCKSCREEN probe, only with --lock. On a tested unit
               (2026-09-25) LOCKSCREEN rebooted the deck; see docs/qualification.

Everything, including every report the deck sends, lands in a JSON receipt.
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

BRIGHTNESS = 60
T0 = time.monotonic()


def now() -> float:
    return round(time.monotonic() - T0, 3)


class Session:
    def __init__(self, dev):
        self.dev = dev
        self.writer = deckproto.Writer(dev)
        self.events: list[dict] = []
        self.results: dict = {}

    async def reader(self):
        while True:
            inp = self.dev.read(1024)
            if inp:
                parsed = deckproto.parse_report(inp)
                raw = bytes(inp[:12]).hex()
                self.events.append({'t': now(), 'parsed': repr(parsed), 'head': raw})
                if parsed and parsed[0] == 'button':
                    idx, pressed = parsed[1]
                    print(f'  [deck] key raw={idx} {"down" if pressed else "up"}')
                elif parsed and parsed[0] == 'info':
                    print(f'  [deck] info: {parsed[1]}')
            else:
                await asyncio.sleep(0.01)

    async def upload_layout(self, tag: str) -> float:
        labels = {n: f'{n + 1}\n{tag}' for n in range(13)}
        took = await self.writer.send(deckzip_frames(labels))
        await self.writer.send(deckproto.brightness_frame(BRIGHTNESS))
        await self.writer.send(deckproto.clock_frame())
        return took


def deckzip_frames(labels):
    return deckproto.zip_frames(deckzip.build_zip(labels))


LINES: asyncio.Queue[str] = None  # one stdin reader for the whole run; see main()


def start_stdin_reader(loop):
    global LINES
    LINES = asyncio.Queue()
    loop.add_reader(sys.stdin, lambda: LINES.put_nowait(sys.stdin.readline()))


def drain():
    while not LINES.empty():
        LINES.get_nowait()


async def ask(prompt: str) -> str:
    drain()
    print(prompt, end='', flush=True)
    return (await LINES.get()).strip().lower()


async def enter_or_timeout(seconds: float) -> float | None:
    """Seconds until Enter, or None if the timeout passes first."""
    drain()
    start = time.monotonic()
    try:
        await asyncio.wait_for(LINES.get(), seconds)
    except asyncio.TimeoutError:
        return None
    return time.monotonic() - start


async def silence(s: Session, runs: int):
    print('\n== 1. silence: time to firmware fallback with no host writes ==')
    out = []
    for i in range(runs):
        await s.upload_layout(f'run{i + 1}')
        await ask(f'Run {i + 1}/{runs}: the keys show "1..13 run{i + 1}"? Press Enter to start silence. ')
        await asyncio.sleep(0.2)  # let the clock frame settle
        start = s.writer.last_write
        print('  silence started; press Enter the moment the splash appears (60 s max)')
        waited = await enter_or_timeout(60)
        if waited is None:
            print('  no fallback within 60 s')
            out.append(None)
        else:
            fallback = round(time.monotonic() - start, 2)
            print(f'  fallback after {fallback} s (includes your reaction time)')
            out.append(fallback)
        if i == 0:
            await s.upload_layout('back')
            out_back = await ask('  after a fresh upload, is the host layout back ("back" on keys)? [y/n] ')
            s.results['silence_recovers_on_upload'] = out_back
    s.results['silence_fallback_s'] = out


async def hold(s: Session, name: str, make_frame, seconds: int, note: str):
    print(f'\n== {name}: one write every 2 s for {seconds} s ==')
    await s.upload_layout(name)
    print(f'  {note}')
    print('  press Enter if the splash appears at any point')
    drain()
    start = time.monotonic()
    fell_back_at = None
    while time.monotonic() - start < seconds:
        await s.writer.send(make_frame())
        try:
            await asyncio.wait_for(LINES.get(), 2.0)
            fell_back_at = round(time.monotonic() - start, 1)
            break
        except asyncio.TimeoutError:
            pass
    if fell_back_at is None:
        print('  time is up')
    visible = await ask('  did anything visible change on the keys or window during the hold (flicker, blank)? [y/n + note] ')
    s.results[name] = {'seconds': seconds, 'fell_back_at_s': fell_back_at, 'visible_change': visible}


async def lock_probe(s: Session):
    print('\n== 4. LOCKSCREEN / UNLOCKSCREEN (exploratory) ==')
    await s.upload_layout('lock')
    await ask('  layout shows "lock"? Enter to send LOCKSCREEN. ')
    await s.writer.send(deckproto.frame(deckproto.Cmd.LOCKSCREEN))
    r = {}
    r['what_shows'] = await ask('  what does the deck show now? (blank / splash / unchanged / other) ')
    before = len(s.events)
    await ask('  press two or three keys on the deck, then Enter here. ')
    r['button_reports_while_locked'] = sum(1 for e in s.events[before:] if "'button'" in e['parsed'])
    print(f'  {r["button_reports_while_locked"]} button reports arrived while locked')
    print('  now silent for 15 s (past the ~5 s watchdog); Enter if the splash appears')
    waited = await enter_or_timeout(15)
    r['splash_during_locked_silence_s'] = None if waited is None else round(waited, 1)
    await s.writer.send(deckproto.frame(deckproto.Cmd.UNLOCKSCREEN))
    r['after_unlock'] = await ask('  UNLOCKSCREEN sent. What shows? (layout / blank / splash / other) ')
    await s.upload_layout('relock')
    r['after_unlock_plus_upload'] = await ask('  layout re-sent. Keys show "relock"? [y/n] ')
    s.results['lock'] = r


def usb_identity() -> dict:
    ident = {}
    for dev in Path('/sys/bus/usb/devices').iterdir():
        try:
            if (dev / 'idVendor').read_text().strip() == '2207' and \
                    (dev / 'idProduct').read_text().strip() == '0019':
                for f in ('manufacturer', 'product', 'serial', 'bcdDevice', 'version', 'speed'):
                    p = dev / f
                    if p.exists():
                        ident[f] = p.read_text().strip()
        except OSError:
            continue
    return ident


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hold', type=int, default=120, help='seconds for the clock and brightness holds')
    ap.add_argument('--runs', type=int, default=3, help='silence runs')
    ap.add_argument('--lock', action='store_true', help='also probe LOCKSCREEN (rebooted our unit)')
    ap.add_argument('--out', type=Path, default=None)
    args = ap.parse_args()

    start_stdin_reader(asyncio.get_running_loop())
    dev = deckproto.open_device()
    if dev is None:
        sys.exit('no D200 found')
    s = Session(dev)
    reader = asyncio.create_task(s.reader())
    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    try:
        await silence(s, args.runs)
        await hold(s, 'clock', deckproto.clock_frame, args.hold,
                   'the window clock should keep ticking; keys should stay put')
        await hold(s, 'bright', lambda: deckproto.brightness_frame(BRIGHTNESS), args.hold,
                   'only brightness writes now; the window clock may stop updating')
        if args.lock:
            await lock_probe(s)
    finally:
        reader.cancel()
        receipt = {
            'started': started,
            'device': usb_identity(),
            'results': s.results,
            'reports': s.events,
        }
        out = args.out or Path.home() / '.local/state/ulanzi' / f'watchdog-{time.strftime("%Y%m%d-%H%M%S")}.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(receipt, indent=2))
        print(f'\nreceipt: {out}')
        print(json.dumps(s.results, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
