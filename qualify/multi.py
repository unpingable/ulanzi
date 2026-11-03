#!/usr/bin/env python3
"""Bounded two-device campaign. Requires exclusive access: stop ulanzid first.

  .venv/bin/python qualify/multi.py --live --output /tmp/d200-multi.json
  Add --interactive for display, input and cable checks with an operator.

Uses a temporary config/socket. Does not change buttons.toml or system services.
Successful writes and controlled session restarts are distinct from display and
physical unplug acceptance. Partial/error receipts are saved too.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ulanzi import deckproto
from ulanzi import daemon as ulanzid


class RecordedTransport:
    def __init__(self, dev, identity, writes):
        self.dev, self.identity, self.writes = dev, identity, writes

    def write(self, packet):
        result = self.dev.write(packet)
        self.writes.append(dict(time=time.time(), identity=self.identity, bytes=len(packet),
                                sha256=hashlib.sha256(packet).hexdigest(), result=result))
        return result

    def get_serial_number_string(self):
        return self.dev.get_serial_number_string()

    def read(self, size):
        return self.dev.read(size)

    def close(self):
        self.dev.close()


async def wait_for(predicate, seconds=15):
    end = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= end:
            raise TimeoutError('qualification condition not reached')
        await asyncio.sleep(.05)


async def prompt(text, timeout=1800):
    # A cancellable stdin reader avoids an input thread wedging executor shutdown.
    loop = asyncio.get_running_loop()
    answer = loop.create_future()
    def ready():
        line = sys.stdin.readline()
        if not answer.done():
            if line:
                answer.set_result(line.strip())
            else:
                answer.set_exception(EOFError('operator input closed'))
    print(text, end=' ', flush=True)
    loop.add_reader(sys.stdin.fileno(), ready)
    try:
        return await asyncio.wait_for(answer, timeout)
    finally:
        loop.remove_reader(sys.stdin.fileno())


def save_receipt(args, receipt):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(args.output.suffix + '.tmp')
    temp.write_text(json.dumps(receipt, indent=2,
                          default=lambda v: v.decode() if isinstance(v, bytes) else str(v)) + '\n')
    temp.replace(args.output)


async def checkpoints(args, receipt):
    while True:
        save_receipt(args, receipt)
        await asyncio.sleep(1)


async def campaign(args, receipt):
    descriptors = deckproto.enumerate_devices()
    receipt['initial_enumeration'] = descriptors
    if len(descriptors) != 2:
        raise RuntimeError(f'two-device campaign needs exactly two currently attached units; found {len(descriptors)}')
    serials = [d['serial'] for d in descriptors]
    if not all(serials) or len(set(serials)) != 2:
        raise RuntimeError('this acceptance campaign needs two unique serials; fallback identity is covered by simulations')
    writes, inputs = receipt['writes'], receipt['inputs']
    def opener(path):
        current = next((d for d in deckproto.enumerate_devices() if d['path'] == path), {})
        return RecordedTransport(deckproto.open_device(path), current.get('serial') or path.decode(), writes)

    with tempfile.TemporaryDirectory(prefix='d200-multi-') as temp:
        path = Path(temp) / 'buttons.toml'
        config = '''[deck]
clock = 1
heartbeat = 1
[profiles.ops.keys.1]
label = 'MATRIX OPS'
[profiles.studio.keys.1]
label = 'SYNTH STUDIO'
'''
        if args.physical_only:
            config += "[profiles.ops.keys.2]\nlabel = 'MATRIX 4'\n[profiles.studio.keys.2]\nlabel = 'SYNTH 4'\n"
        for name, serial, profile in zip(('matrix', 'synth'), serials, ('ops', 'studio')):
            config += f'[devices.{name}]\nmatch.serial = {json.dumps(serial)}\nprofile = "{profile}"\n'
        path.write_text(config)
        daemon = ulanzid.Daemon(path, Path(temp) / 'sock', opener=opener)
        task = asyncio.create_task(daemon.run())
        try:
            await wait_for(lambda: len(daemon.registry.devices) == 2 and
                           all(d.shown is not None for d in daemon.registry.devices.values()))
            devices = list(daemon.registry.devices.values())
            for d in devices:
                original = d.press
                def record(n, source, device=d, original=original):
                    inputs.append(dict(time=time.time(), identity=device.identity,
                                       name=device.name, generation=device.generation,
                                       key=ulanzid.key_name(n), source=source))
                    return original(n, source)
                d.press = record
            receipt['checks']['distinct_discovery'] = daemon.inventory()
            receipt['checks']['different_layout_write_state'] = {
                d.name: dict(profile=d.profile, label=d.state.label(0),
                             image_sha256=hashlib.sha256(d.shown[0]).hexdigest()) for d in devices}
            if not args.physical_only:
                for i in range(5):
                    for d in devices:
                        daemon.handle(f'target {d.name} label 2 {d.name.upper()} {i}')
                    await asyncio.gather(*(d.sync() for d in devices))
                    assert all(d.shown[1] == d.state.image(1) for d in devices)
                    await asyncio.sleep(.25)
                receipt['checks']['simultaneous_update_write_state'] = 'passed: 5 rounds, device-local shown/desired bytes agree'
                for cycle in range(2):
                    for d in devices:
                        peer = next(p for p in devices if p is not d)
                        generation, peer_generation = d.generation, peer.generation
                        d._lose('qualification controlled session restart (not a USB unplug)')
                        await wait_for(lambda: d.generation > generation and d.shown is not None)
                        assert peer.connected and peer.generation == peer_generation
                receipt['checks']['controlled_session_restarts'] = daemon.inventory()
                # This checks live single-session compatibility without claiming physical unplug.
                peer = devices[1]
                peer.task.cancel()
                await asyncio.gather(peer.task, return_exceptions=True)
                assert devices[0].connected
                await devices[0].sync(full=True)
                await daemon.registry.reconcile()
                await wait_for(lambda: peer.connected and peer.shown is not None)
                receipt['checks']['single_active_session_upload'] = 'passed; peer transport was closed, USB remained attached'
            if args.interactive:
                if not args.physical_only:
                    receipt['stage'] = 'waiting for display and input confirmation'
                    start = len(inputs)
                    reply = await prompt('Check key 1: MATRIX OPS / SYNTH STUDIO; key 2: MATRIX 4 / SYNTH 4. '
                                         'Press key 1 on EACH deck, then confirm all labels [yes/no].')
                    receipt['checks']['visible_layouts_and_updates'] = reply
                    await wait_for(lambda: all(any(e['identity'] == d.identity and e['source'] == 'deck'
                                                   and e['key'] == '1' for e in inputs[start:])
                                              for d in devices), 10)
                    receipt['checks']['physical_input_routing'] = 'passed; see input records'
                for cycle in range(2):
                    for d in devices:
                        peer = next(p for p in devices if p is not d)
                        generation, peer_generation = d.generation, peer.generation
                        async def observe():
                            await wait_for(lambda: not d.present, 1800)
                            assert peer.connected
                            peer.state.overrides[2] = f'PEER ALIVE {cycle}'
                            await peer.sync()
                            removal_inventory = daemon.inventory()
                            await wait_for(lambda: d.generation > generation and d.shown is not None, 1800)
                            assert peer.connected and peer.generation == peer_generation
                            return removal_inventory
                        observer = asyncio.create_task(observe())
                        receipt['stage'] = f'physical unplug/replug {d.name} cycle {cycle + 1}'
                        try:
                            reply = await prompt(f'Unplug {d.name}; leave it unplugged until {peer.name} '
                                                 f'shows PEER ALIVE {cycle} on key 3. Replug {d.name}, '
                                                 'check its key 1 layout returns, then confirm BOTH displays [yes/no].')
                            removal_inventory = await observer
                        finally:
                            if not observer.done():
                                observer.cancel()
                                await asyncio.gather(observer, return_exceptions=True)
                        receipt['checks'].setdefault('physical_replug_cycles', []).append(
                            dict(device=d.name, identity=d.identity, generation=d.generation,
                                 both_displays_correct=reply, removal_inventory=removal_inventory,
                                 restored_inventory=daemon.inventory()))
                        save_receipt(args, receipt)
            else:
                receipt['checks']['physical_checks'] = 'pending: no operator; no visible rendering, key press, USB unplug/reboot claims'
            receipt['final_inventory'] = daemon.inventory()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--physical-only', action='store_true', help='skip transport/update/input checks already recorded in a separate receipt')
    args = parser.parse_args()
    # Never compete with the installed daemon. This tool does not stop it itself.
    for node in Path('/proc').iterdir():
        if node.name.isdecimal():
            try:
                if (node / 'comm').read_text().strip() == 'ulanzid':
                    parser.error('ulanzid is running; arrange exclusive access before --live')
            except OSError:
                pass
    receipt = dict(started=time.time(), checks={}, writes=[], inputs=[], result='incomplete',
                   source_sha256={name: hashlib.sha256((ulanzid.REPO / name).read_bytes()).hexdigest()
                                  for name in ('ulanzi/daemon.py', 'ulanzi/deckproto.py', 'ulanzi/deckzip.py',
                                               'ulanzi/tiles.py', 'ulanzi/metrics.py',
                                               'ulanzi/weather_tiles.py', 'ulanzictl', 'qualify/multi.py')})
    async def run():
        checkpoint_task = asyncio.create_task(checkpoints(args, receipt))
        try:
            await campaign(args, receipt)
        finally:
            checkpoint_task.cancel()
            await asyncio.gather(checkpoint_task, return_exceptions=True)
    try:
        asyncio.run(run())
        checks = receipt['checks']
        if args.interactive:
            passed = ((args.physical_only or checks.get('visible_layouts_and_updates', '').lower() == 'yes') and
                      all(c['both_displays_correct'].lower() == 'yes'
                          for c in checks.get('physical_replug_cycles', [])))
            receipt['result'] = 'passed' if passed else 'completed with negative operator observations'
        else:
            receipt['result'] = 'automated transport checks passed; physical checks pending'
    except BaseException as e:
        receipt['error'] = repr(e)
        raise
    finally:
        receipt['finished'] = time.time()
        save_receipt(args, receipt)


if __name__ == '__main__':
    main()
