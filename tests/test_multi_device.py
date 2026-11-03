"""Lifecycle integration tests use HID-like transports; no device access."""
import asyncio
import json
import threading
import time

import pytest

from ulanzi import deckproto
from ulanzi import daemon as ulanzid


def descriptor(serial, path=None, topology=''):
    return dict(serial=serial, path=(path or f'/fake/{serial}').encode(), topology=topology,
                vendor_id=deckproto.VENDOR_ID, product_id=deckproto.PRODUCT_ID,
                interface_number=0)


class Transport:
    def __init__(self, serial=''):
        self.serial = serial
        self.packets = []
        self.inputs = []
        self.closed = False
        self.fail = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def get_serial_number_string(self):
        return self.serial

    def write(self, packet):
        self.entered.set()
        self.release.wait(3)
        assert not self.closed
        if self.fail:
            raise OSError('deliberate device failure')
        self.packets.append(packet)
        return len(packet)

    def read(self, size):
        if self.fail:
            raise OSError('deliberate read failure')
        return self.inputs.pop(0) if self.inputs else []

    def close(self):
        self.closed = True


def fleet(write_config, text=''):
    path = write_config(text)
    transports = {}

    def opener(path):
        serial = next((d.descriptor['serial'] for d in daemon.registry.devices.values()
                       if d.descriptor and d.descriptor['path'] == path), '')
        transport = Transport(serial)
        transports.setdefault(path, []).append(transport)
        return transport

    daemon = ulanzid.Daemon(path, path.parent / 'sock', opener=opener)
    return daemon, transports


async def until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, 'timed out waiting for simulated device'
        await asyncio.sleep(.01)


async def stop(daemon):
    tasks = [d.task for d in daemon.registry.devices.values() if d.task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('count', [0, 1, 2, 9])
def test_zero_one_two_n_duplicate_models(write_config, count):
    daemon, transports = fleet(write_config, '[keys.1]\nlabel="base"\n')

    async def scenario():
        await daemon.registry.reconcile([descriptor(str(i)) for i in range(count)])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            assert len(daemon.registry.devices) == count
            assert len(transports) == count
            assert len(json.loads(daemon.handle('devices')[3:])['devices']) == count
            if count > 1:
                assert daemon.handle('label 1 unsafe').startswith('err device target required')
            if count:
                assert daemon.handle('target all label 1 shared').startswith('ok')
                assert all(d.state.label(0) == 'shared' for d in daemon.registry.devices.values())
                devices = list(daemon.registry.devices.values())
                assert daemon.handle(f'target {devices[0].name} label 1 local') == 'ok'
                assert devices[0].state.label(0) == 'local'
                assert all(d.state.label(0) == 'shared' for d in devices[1:])
        finally:
            await stop(daemon)
    asyncio.run(scenario())


CONFIG = '''
[keys.1]
label = "base"
[profiles.ops.keys.1]
label = "OPS"
[profiles.studio.keys.1]
label = "STUDIO"
[devices.matrix]
match.serial = "A"
profile = "ops"
[devices.synth]
match.serial = "B"
profile = "studio"
'''


def test_identity_profiles_absent_roles_and_reload(write_config):
    daemon, _ = fleet(write_config, CONFIG)
    assert daemon.handle('target matrix label 2 offline') == 'ok'
    assert json.loads(daemon.handle('devices')[3:])['devices'][0]['state'] == 'absent'

    async def scenario():
        await daemon.registry.reconcile([descriptor('B'), descriptor('A')])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            a, b = daemon.registry.devices['serial:A'], daemon.registry.devices['serial:B']
            assert (a.name, b.name) == ('matrix', 'synth')
            assert (a.state.label(0), b.state.label(0)) == ('OPS', 'STUDIO')
            assert a.state.label(1) == 'offline'
            assert daemon.handle('target matrix profile studio') == 'ok'
            await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
            assert a.profile == 'studio'
            daemon.config_path.write_text(CONFIG.replace('"OPS"', '"NEW OPS"'))
            assert daemon.handle('reload') == 'ok'
            await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
            assert a.profile == 'ops' and a.state.label(0) == 'NEW OPS'
            assert a.state.label(1) == 'offline'
            assert a.generation == b.generation == 1
            daemon.config_path.write_text('[devices.bad]\nmatch.serial="A"\nprofile="missing"')
            assert daemon.handle('reload').startswith('err ')
            assert a.state.label(0) == 'NEW OPS'
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_stable_serial_reconnect_hot_removal_repeated_cycles(write_config):
    daemon, transports = fleet(write_config, CONFIG)

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            a, b = daemon.registry.devices['serial:A'], daemon.registry.devices['serial:B']
            daemon.handle('target matrix label 3 persistent')
            for generation in range(2, 5):
                await daemon.registry.reconcile([descriptor('B')])
                assert not a.connected and b.connected
                assert transports[a.descriptor['path']][-1].closed
                await daemon.registry.reconcile([descriptor('B'), descriptor('A', f'/fake/new{generation}')])
                await until(lambda: a.shown is not None)
                assert a.generation == generation and a.name == 'matrix'
                assert a.state.label(2) == 'persistent'
                assert b.generation == 1
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_duplicate_serial_and_fallback_matching(write_config):
    daemon, _ = fleet(write_config, '[devices.left]\nmatch.topology="port-left"\n')
    assignments = daemon.registry.assignments([
        descriptor('dup', '/fake/1', 'port-left'), descriptor('dup', '/fake/2', 'port-right')], daemon.config)
    assert set(assignments) == {'port:port-left', 'port:port-right'}
    assert assignments['port:port-left'][0] == 'left'
    assert set(daemon.registry.assignments([descriptor('', '/fake/1')], daemon.config)) == {'path:/fake/1'}
    bad, _ = fleet(write_config, '[devices.left]\nmatch.serial="dup"\n')
    with pytest.raises(ulanzid.ConfigError, match='ambiguous'):
        bad.registry.assignments([descriptor('dup', '/fake/1', 'p1'), descriptor('dup', '/fake/2', 'p2')], bad.config)


def test_input_routing_and_per_device_command_guard(write_config):
    daemon, transports = fleet(write_config, CONFIG)
    presses = []

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            for device in daemon.registry.devices.values():
                device.press = lambda n, source, d=device: presses.append((d.identity, n, source))
            for i in range(10):
                for serial in ('A', 'B'):
                    transports[f'/fake/{serial}'.encode()][0].inputs.append(
                        deckproto.frame(deckproto.Cmd.IN_BUTTON, bytes([0, i, 1, 1])))
            await until(lambda: len(presses) == 20)
            assert {(identity, n) for identity, n, _ in presses} == {
                (f'serial:{serial}', i) for serial in ('A', 'B') for i in range(10)}
            a, b = daemon.registry.devices.values()
            assert a.child_env(0)['ULANZI_DEVICE'] != b.child_env(0)['ULANZI_DEVICE']
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_concurrent_upload_failure_removal_during_work(write_config):
    daemon, transports = fleet(write_config, CONFIG)

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            a, b = daemon.registry.devices['serial:A'], daemon.registry.devices['serial:B']
            ta = transports[b'/fake/A'][0]
            ta.entered.clear()
            ta.release.clear()
            a.state.overrides[0] = 'blocked'
            upload = asyncio.create_task(a.sync())
            await until(ta.entered.is_set)
            b.state.overrides[0] = 'healthy'
            await asyncio.wait_for(b.sync(), 1)
            assert b.shown[0] == b.state.image(0)
            ta.fail = True
            ta.release.set()
            await upload
            await until(lambda: a.task.done())
            assert a.error and b.connected
            await daemon.registry.reconcile([descriptor('B'), descriptor('A', '/fake/replug')])
            await until(lambda: a.shown is not None)
            assert a.generation == 2
            assert a.shown[0] == a.state.image(0)
            ta = transports[b'/fake/replug'][0]
            ta.entered.clear()
            ta.release.clear()
            a.state.overrides[0] = 'pending'
            a.request_push()
            await until(ta.entered.is_set)
            removal = asyncio.create_task(daemon.registry.reconcile([descriptor('B')]))
            await asyncio.sleep(.05)
            assert not ta.closed
            b.state.overrides[0] = 'still busy'
            await asyncio.wait_for(b.sync(), 1)
            ta.release.set()
            await removal
            assert ta.closed and not a.connected and b.connected
        finally:
            for group in transports.values():
                for t in group:
                    t.release.set()
            await stop(daemon)
    asyncio.run(scenario())


def test_enumeration_failure_keeps_peers(write_config):
    daemon, _ = fleet(write_config)

    async def scenario():
        await daemon.registry.reconcile([descriptor('A')])
        try:
            await until(lambda: daemon.registry.devices['serial:A'].shown is not None)
            def broken():
                raise OSError('enumeration failed')
            daemon.registry.enumerate_fn = broken
            await daemon.registry.reconcile()
            assert daemon.registry.devices['serial:A'].connected
            assert daemon.registry.discovery_error == 'enumeration failed'
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_legacy_offline_overrides_adopted_for_one_device(write_config):
    daemon, _ = fleet(write_config, '[keys.1]\nlabel="base"')
    assert daemon.handle('label 1 offline') == 'ok'

    async def scenario():
        await daemon.registry.reconcile([descriptor('A')])
        try:
            assert daemon.handle('status').startswith('ok connected=')
            assert daemon.registry.devices['serial:A'].state.label(0) == 'offline'
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_role_retarget_reload_does_not_alias_absent_device(write_config):
    daemon, _ = fleet(write_config, CONFIG)

    async def scenario():
        await daemon.registry.reconcile([descriptor('A')])
        try:
            await until(lambda: daemon.registry.devices['serial:A'].shown is not None)
            await daemon.registry.reconcile([])
            daemon.config_path.write_text(CONFIG.replace('match.serial = "A"', 'match.serial = "C"'))
            assert daemon.handle('reload') == 'ok'
            assert daemon.registry.devices['serial:A'].name == '@serial:A'
            assert daemon.handle('target matrix label 2 new role') == 'ok'
            await daemon.registry.reconcile([descriptor('C')])
            await until(lambda: daemon.registry.devices['serial:C'].shown is not None)
            c = daemon.registry.devices['serial:C']
            assert c.name == 'matrix' and c.state.label(1) == 'new role'
            assert daemon.handle('target matrix reset 2') == 'ok'
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_commands_simultaneous_per_device_and_child_cli_routing(write_config, tmp_path):
    out = tmp_path / 'commands'
    daemon, _ = fleet(write_config, f'''[keys.1]
label = "base"
run = "sleep 0.05; echo $ULANZI_DEVICE >> {out}; ulanzictl label 1 $ULANZI_DEVICE"
''')

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        server = await asyncio.start_unix_server(daemon.serve_client, path=str(daemon.socket_path))
        try:
            async with server:
                devices = list(daemon.registry.devices.values())
                for d in devices:
                    assert daemon.handle(f'target {d.name} press 1') == 'ok started'
                    assert daemon.handle(f'target {d.name} press 1') == 'ok already running'
                await asyncio.gather(*(d.running[0] for d in devices))
                assert set(out.read_text().splitlines()) == {d.name for d in devices}
                assert all(d.state.label(0) == d.name for d in devices)
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_reload_during_upload_converges_without_cross_contamination(write_config):
    daemon, transports = fleet(write_config, CONFIG)

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            a, b = daemon.registry.devices['serial:A'], daemon.registry.devices['serial:B']
            ta = transports[b'/fake/A'][0]
            ta.entered.clear()
            ta.release.clear()
            a.state.overrides[1] = 'old upload'
            pending = asyncio.create_task(a.sync())
            await until(ta.entered.is_set)
            daemon.config_path.write_text(CONFIG.replace('"OPS"', '"RELOADED"'))
            assert daemon.handle('reload') == 'ok'
            await b.sync()
            assert b.shown[0] == b.state.image(0)
            assert b.state.label(0) == 'STUDIO'
            ta.release.set()
            await pending
            await a.sync()
            assert a.shown[0] == a.state.image(0)
            assert a.state.label(0) == 'RELOADED'
            assert a.state.label(1) == 'old upload'
        finally:
            for group in transports.values():
                for transport in group:
                    transport.release.set()
            await stop(daemon)
    asyncio.run(scenario())


def test_observed_duplicate_serial_never_reuses_other_units_state(write_config):
    daemon, _ = fleet(write_config, '[keys.1]\nlabel="base"')

    async def scenario():
        await daemon.registry.reconcile([descriptor('dup', '/fake/A', 'portA')])
        try:
            await until(lambda: daemon.registry.devices['serial:dup'].shown is not None)
            daemon.handle('label 1 only physical A')
            await daemon.registry.reconcile([descriptor('dup', '/fake/A', 'portA'),
                                             descriptor('dup', '/fake/B', 'portB')])
            await until(lambda: daemon.registry.devices['port:portB'].shown is not None)
            await daemon.registry.reconcile([descriptor('dup', '/fake/B', 'portB')])
            b = daemon.registry.devices['port:portB']
            assert b.state.label(0) == 'base'
            assert not daemon.registry.devices['serial:dup'].connected
            assert b.connected and b.identity == 'port:portB'
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_offline_legacy_child_identity_routes_without_device(write_config, tmp_path):
    out = tmp_path / 'result'
    daemon, _ = fleet(write_config, f'[keys.1]\nlabel="base"\nrun="ulanzictl label 1 offline; echo done > {out}"')

    async def scenario():
        server = await asyncio.start_unix_server(daemon.serve_client, path=str(daemon.socket_path))
        async with server:
            assert daemon.handle('press 1') == 'ok started'
            await daemon.legacy.running[0]
            assert daemon.legacy.state.label(0) == 'offline'
    asyncio.run(scenario())


def test_reopening_handle_keeps_bounded_icon_alternation(write_config):
    daemon, _ = fleet(write_config, '[keys.1]\nlabel="original"')

    async def scenario():
        await daemon.registry.reconcile([descriptor('A')])
        try:
            await until(lambda: daemon.registry.devices['serial:A'].shown is not None)
            a = daemon.registry.devices['serial:A']
            slot = a.slots[0]
            await daemon.registry.reconcile([])
            daemon.handle(f'target {a.identity} label 1 changed while absent')
            await daemon.registry.reconcile([descriptor('A')])
            await until(lambda: a.shown is not None)
            assert a.slots[0] == 1 - slot
            assert a.shown[0] == a.state.image(0)
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_reload_while_old_session_drains_uses_latest_assignments(write_config):
    daemon, transports = fleet(write_config, CONFIG)

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        try:
            await until(lambda: all(d.shown is not None for d in daemon.registry.devices.values()))
            a = daemon.registry.devices['serial:A']
            ta = transports[b'/fake/A'][0]
            ta.entered.clear()
            ta.release.clear()
            a.state.overrides[0] = 'draining'
            a.request_push()
            await until(ta.entered.is_set)
            change = asyncio.create_task(daemon.registry.reconcile([
                descriptor('A', '/fake/new'), descriptor('B')]))
            await asyncio.sleep(.05)
            daemon.config_path.write_text('[devices.matrix]\nmatch.serial="A"\n')
            assert daemon.handle('reload') == 'ok'
            ta.release.set()
            await change
            await until(lambda: a.shown is not None)
            assert a.profile == 'default' and a.generation == 2
            assert a.state.label(0) == 'draining'
            await daemon.registry.reconcile([descriptor('A', '/fake/new')])
            inventory = daemon.inventory()['devices']
            b = next(row for row in inventory if row['identity'] == 'serial:B')
            assert b['state'] == 'absent'
        finally:
            for group in transports.values():
                for t in group:
                    t.release.set()
            await stop(daemon)
    asyncio.run(scenario())


def test_empty_profile_argument_still_gets_exactly_one_socket_reply(write_config):
    daemon, _ = fleet(write_config)

    async def scenario():
        server = await asyncio.start_unix_server(daemon.serve_client, path=str(daemon.socket_path))
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
            writer.write(b'profile \n')
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 1) == b"err unknown profile ''\n"
            assert await reader.read() == b''
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_reused_hid_path_is_verified_against_opened_serial(write_config):
    daemon, transports = fleet(write_config)
    original = daemon.registry.opener
    def raced(path):
        transport = original(path)
        if path == b'/fake/A':
            transport.serial = 'different physical unit'
        return transport
    daemon.registry.opener = raced

    async def scenario():
        await daemon.registry.reconcile([descriptor('A'), descriptor('B')])
        try:
            a, b = daemon.registry.devices['serial:A'], daemon.registry.devices['serial:B']
            await until(lambda: a.task.done() and b.shown is not None)
            assert not a.connected and a.generation == 0
            assert 'refusing reused HID path' in a.error
            assert transports[b'/fake/A'][0].closed
            assert not transports[b'/fake/A'][0].packets
            assert b.connected and b.generation == 1
        finally:
            await stop(daemon)
    asyncio.run(scenario())


def test_busy_input_reader_yields_to_peer_activity(write_config):
    daemon, _ = fleet(write_config)
    events = []
    daemon.legacy.press = lambda *args: events.append('flood')
    packet = deckproto.frame(deckproto.Cmd.IN_BUTTON, bytes([0, 0, 1, 1]))
    class Flood:
        def read(self, size):
            return packet if events.count('flood') < 100 else []

    async def scenario():
        async def peer():
            events.append('peer')
        reader = asyncio.create_task(daemon.legacy.read_events(Flood()))
        try:
            await asyncio.create_task(peer())
            assert events.index('peer') < 10
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    asyncio.run(scenario())


def test_oversized_socket_request_gets_one_error_reply(write_config):
    daemon, _ = fleet(write_config)

    async def scenario():
        server = await asyncio.start_unix_server(daemon.serve_client, path=str(daemon.socket_path))
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
            writer.write(b'label 1 ' + b'x' * 70000 + b'\n')
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 1) == b'err request line too long\n'
            assert await reader.read() == b''
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())
