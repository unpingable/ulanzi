import asyncio
import io
import json
import os
import subprocess
import time
import zipfile

import pytest

from ulanzi import deckproto
from ulanzi import daemon as ulanzid
from conftest import REPO
from ulanzi.daemon import ConfigError, DesiredState, load_config


def make_daemon(write_config, text):
    path = write_config(text)
    return ulanzid.D200Device(path, path.parent / 'sock')


class FakeWriter:
    """Stands in for deckproto.Writer; records every batch of frames."""

    def __init__(self):
        self.sent: list[list[bytes]] = []
        self.last_write = time.monotonic()

    async def send(self, packets):
        self.sent.append([packets] if isinstance(packets, bytes) else list(packets))
        self.last_write = time.monotonic()
        return 0.001

    def commands(self):
        return [batch[0][2:4] for batch in self.sent]

    def manifest(self, i=-1):
        batch = self.sent[i]
        length = int.from_bytes(batch[0][4:8], 'little')
        blob = (batch[0][8:] + b''.join(batch[1:]))[:length]
        zf = zipfile.ZipFile(io.BytesIO(blob))
        return json.loads(zf.read('manifest.json')), zf


def connect(d):
    d.writer = FakeWriter()
    d._lost = asyncio.Event()
    return d.writer


# --- config ------------------------------------------------------------------

def test_shipped_config_loads():
    config = load_config(REPO / 'buttons.toml')
    assert config.keys[0].run  # [keys.1] in the file
    assert config.widget_keys()


def test_config_rejects_bad_index_and_fields(write_config):
    for bad in ('0', '14', 'three'):
        with pytest.raises(ConfigError, match='is not 1..13'):
            load_config(write_config(f'[keys.{bad}]\nlabel = "x"\n'))
    with pytest.raises(ConfigError, match='unknown fields'):
        load_config(write_config('[keys.1]\nlable = "x"\n'))
    with pytest.raises(ConfigError, match='unknown fields'):
        load_config(write_config('[deck]\nheartbeet = 2\n'))


def test_widget_config(write_config):
    config = load_config(write_config('[keys.2]\nwidget = "load"\nrun = "uptime"\n'))
    assert config.widget_keys() == {1: 'load'} and config.keys[1].run == 'uptime'
    with pytest.raises(ConfigError, match='unknown widget'):
        load_config(write_config('[keys.2]\nwidget = "gpu"\n'))
    with pytest.raises(ConfigError, match='exclusive'):
        load_config(write_config('[keys.2]\nwidget = "cpu"\nlabel = "x"\n'))
    with pytest.raises(ConfigError, match='takes run but no label or widget'):
        load_config(write_config('[keys.window]\nwidget = "cpu"\n'))


def test_agent_show_active_config(write_config):
    config = load_config(write_config('[keys.13]\nwidget="agent_kimi"\nshow_active=false\n'))
    assert config.keys[12].show_active is False
    config = load_config(write_config('[keys.13]\nwidget="agent_kimi"\n'))
    assert config.keys[12].show_active is True
    with pytest.raises(ConfigError, match='show_active must be a boolean'):
        load_config(write_config('[keys.13]\nwidget="agent_kimi"\nshow_active="no"\n'))
    with pytest.raises(ConfigError, match='show_active requires an agent widget'):
        load_config(write_config('[keys.2]\nwidget="cpu"\nshow_active=false\n'))


def test_paired_weather_row_refreshes_and_uploads_one_shared_snapshot(monkeypatch):
    from experiments.weather_row import fixture
    sampler, current, ncpu = fixture('busy')
    d = ulanzid.D200Device(REPO / 'buttons.toml', REPO / 'unused-test-socket')
    d.sampler = sampler
    monkeypatch.setattr(sampler, 'sample', lambda: current)
    monkeypatch.setattr(ulanzid.metrics, 'cpu_count', lambda: ncpu)
    writer = connect(d)
    assert [d.state.config.keys[n].widget for n in range(10)] == [
        'load', 'cpu', 'mem', 'net', 'disk',
        'weather_load', 'weather_cpu', 'weather_mem', 'weather_net', 'weather_disk']
    assert d.state.image(6) == ulanzid.tiles.render_widget(
        'weather_cpu', ulanzid.metrics.Sampler(), ulanzid.metrics.Sample(t=0))
    sync = d.sync

    async def no_delay(_):
        pass

    async def one_upload(**kwargs):
        await sync(**kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(ulanzid.asyncio, 'sleep', no_delay)
    monkeypatch.setattr(d, 'sync', one_upload)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(d.tiles_loop())
    assert not d._sampler_failing
    manifest, archive = writer.manifest()
    for n in range(10):
        image = archive.read(manifest[f'{n % 5}_{n // 5}']['ViewParam'][0]['Icon'])
        assert image == ulanzid.tiles.render_widget(d.state.config.keys[n].widget, sampler, current)
        assert archive.getinfo(f'icons/{ulanzid.deckzip.slot_name(n, 0)}')


def test_window_key_takes_run_only(write_config):
    config = load_config(write_config('[keys.window]\nrun = "true"\n'))
    assert config.keys[ulanzid.WINDOW_INDEX].run == 'true'
    with pytest.raises(ConfigError, match='takes run but no label'):
        load_config(write_config('[keys.window]\nlabel = "x"\n'))
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n')
    assert d.handle('label window x').startswith('err ')
    assert d.handle('label 13 x') == 'ok'  # 13 is the last key, not the window


def test_heartbeat_default_stays_well_under_firmware_watchdog():
    # At least two heartbeats per watchdog window, so one late tick can't trip it.
    assert ulanzid.DEFAULT_HEARTBEAT * 2 <= ulanzid.FIRMWARE_WATCHDOG
    assert ulanzid.HEARTBEAT_RANGE[1] * 1.5 <= ulanzid.FIRMWARE_WATCHDOG
    assert ulanzid.Config().heartbeat == ulanzid.DEFAULT_HEARTBEAT
    assert load_config(REPO / 'buttons.toml').heartbeat * 2 <= ulanzid.FIRMWARE_WATCHDOG


def test_heartbeat_config_bounds(write_config):
    assert load_config(write_config('[deck]\n')).heartbeat == ulanzid.DEFAULT_HEARTBEAT
    assert load_config(write_config('[deck]\nheartbeat = 1\n')).heartbeat == 1.0
    for bad in ('10.0', '5', '4', '0.1', 'true', '"2"'):
        with pytest.raises(ConfigError, match='heartbeat'):
            load_config(write_config(f'[deck]\nheartbeat = {bad}\n'))


# --- desired state -----------------------------------------------------------

def test_precedence_override_then_widget_then_label(write_config):
    state = DesiredState(load_config(write_config(
        '[keys.1]\nlabel = "base"\n[keys.2]\nwidget = "cpu"\n')))
    state.widget_images[1] = b'cpu-tile'
    assert state.image(1) == b'cpu-tile' and state.describe(1) == '<cpu>'
    state.overrides[1] = 'DOWN'
    assert state.image(1) != b'cpu-tile' and state.describe(1) == "'DOWN'"
    del state.overrides[1]
    assert state.image(1) == b'cpu-tile'
    assert state.image(2) is None  # unconfigured key is blank
    # a widget with no sample yet shows an explicit N/A tile, not a blank key
    del state.widget_images[1]
    assert state.image(1) is not None


def test_overrides_survive_reload(write_config):
    path = write_config('[keys.3]\nlabel = "base"\n')
    state = DesiredState(load_config(path))
    state.overrides[2] = 'DOWN'
    state.config = load_config(write_config('[keys.3]\nlabel = "renamed"\n'))
    assert state.label(2) == 'DOWN'
    del state.overrides[2]
    assert state.label(2) == 'renamed'


def test_changed_images():
    assert ulanzid.changed_images({0: b'a', 1: None}, None) == {0: b'a', 1: None}
    assert ulanzid.changed_images({0: b'a', 1: None}, {0: b'a', 1: None}) == {}
    assert ulanzid.changed_images({0: b'b', 1: None}, {0: b'a', 1: None}) == {0: b'b'}
    assert ulanzid.changed_images({0: b'a', 1: None}, {0: b'a'}) == {1: None}


# --- syncing to the deck -----------------------------------------------------

def test_reconnect_restores_desired_state(write_config):
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n[keys.2]\nwidget = "cpu"\n')
    d.handle('label 1 REMOTE')
    w = connect(d)

    asyncio.run(d.restore_deck())
    assert w.commands() == [b'\x00\x0a', b'\x00\x06', b'\x00\x01']  # brightness, clock, full layout
    manifest, zf = w.manifest()
    assert len(manifest) == 13
    remote = zf.read(manifest['0_0']['ViewParam'][0]['Icon'])
    assert remote == d.state.image(0)
    assert manifest['1_0']['ViewParam'][0]['Icon']  # widget key shows its N/A tile
    assert manifest['2_0'] == {'State': 0, 'ViewParam': [{}]}


def test_sync_sends_one_partial_with_only_changed_keys(write_config):
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n[keys.2]\nwidget = "cpu"\n[keys.3]\nwidget = "mem"\n')
    w = connect(d)

    async def scenario():
        await d.sync(full=True)
        await d.sync()  # nothing changed: no write at all
        assert len(w.sent) == 1
        d.state.widget_images = {1: b'cpu 1', 2: d.state.image(2)}
        await d.sync()

    asyncio.run(scenario())
    assert len(w.sent) == 2 and w.commands()[-1] == b'\x00\x0d'
    manifest, _ = w.manifest()
    assert list(manifest) == ['1_0']


def test_heartbeat_fires_only_after_silence(write_config):
    d = make_daemon(write_config, '[deck]\nheartbeat = 0.5\n')
    w = connect(d)

    async def scenario():
        loop = asyncio.create_task(d.heartbeat_loop())
        await asyncio.sleep(0.3)
        quiet = len(w.sent)
        await asyncio.sleep(0.6)
        loop.cancel()
        return quiet

    assert asyncio.run(scenario()) == 0
    assert w.commands() and set(w.commands()) == {b'\x00\x0a'}
    assert len(w.sent) <= 2


def test_other_writes_count_as_liveness(write_config):
    d = make_daemon(write_config, '[deck]\nheartbeat = 0.5\n')
    w = connect(d)

    async def scenario():
        loop = asyncio.create_task(d.heartbeat_loop())
        for _ in range(6):  # something else writes every 0.2 s
            await asyncio.sleep(0.2)
            await w.send(deckproto.clock_frame())
        loop.cancel()

    asyncio.run(scenario())
    assert b'\x00\x0a' not in w.commands()  # no heartbeat was needed


def test_failed_write_drops_the_device(write_config):
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n')
    connect(d)

    async def boom(packets):
        raise OSError('gone')
    d.writer.send = boom
    assert asyncio.run(d.write(b'x')) is None
    assert d._lost.is_set()


def test_broken_sampler_does_not_stop_buttons(write_config):
    out = write_config('').parent / 'out'
    d = make_daemon(write_config, f'[deck]\nrefresh = 1\n[keys.2]\nwidget = "cpu"\n'
                                  f'[keys.1]\nrun = "echo ok > {out}"\n')

    def broken():
        raise RuntimeError('proc is on fire')
    d.sampler.sample = broken

    async def scenario():
        loop = asyncio.create_task(d.tiles_loop())
        await asyncio.sleep(1.1)
        assert d._sampler_failing and not loop.done()
        assert d.handle('press 1') == 'ok started'
        await d.running[0]
        loop.cancel()

    asyncio.run(scenario())
    assert out.read_text() == 'ok\n'


# --- socket and commands -----------------------------------------------------

def test_socket_replies(write_config):
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n[keys.2]\nwidget = "load"\n')
    assert d.handle('label 1 two words') == 'ok'
    assert d.state.label(0) == 'two words'
    assert d.handle('label 1') == 'ok' and d.state.label(0) == ''
    assert d.handle('reset all') == 'ok' and d.state.label(0) == 'a'
    assert d.handle('label 0 x') == "err key '0' is not 1..13"
    assert d.handle('label 14 x').startswith('err ')
    assert d.handle('status').split()[1:4] == ['connected=False', "1='a'", '2=<load>']
    assert d.handle('bogus').startswith('err unknown command')
    assert d.handle('') == 'err empty command'


def test_bad_reload_keeps_old_config(write_config):
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n')
    write_config('[keys.1\n')
    assert d.handle('reload').startswith('err ')
    assert d.state.label(0) == 'a'


def test_press_runs_command_once_at_a_time(write_config):
    out = write_config('').parent / 'out'
    d = make_daemon(write_config, f'[keys.4]\nrun = "sleep 0.2; echo $ULANZI_KEY >> {out}"\n'
                                  f'[keys.window]\nrun = "echo $ULANZI_KEY >> {out}"\n')

    async def scenario():
        assert d.handle('press 4') == 'ok started'
        assert d.handle('press 4') == 'ok already running'
        assert d.handle('press 5') == 'ok no command'
        assert d.handle('press window') == 'ok started'
        await d.running[13]
        await d.running[3]

    asyncio.run(scenario())
    assert out.read_text() == 'window\n4\n'
    assert not d.running


def test_ulanzictl_round_trip(write_config):
    d = make_daemon(write_config, '[keys.1]\nlabel = "a"\n')
    sock = d.socket_path
    env = dict(os.environ, ULANZI_SOCKET=str(sock))

    async def scenario():
        server = await asyncio.start_unix_server(d.serve_client, path=str(sock))
        async with server:
            ok = await asyncio.create_subprocess_exec(REPO / 'ulanzictl', 'label', '1', 'hi', env=env)
            bad = await asyncio.create_subprocess_exec(
                REPO / 'ulanzictl', 'label', '42', 'x', env=env, stderr=subprocess.DEVNULL)
            return await ok.wait(), await bad.wait()

    assert asyncio.run(scenario()) == (0, 1)
    assert d.state.label(0) == 'hi'
    missing = subprocess.run([REPO / 'ulanzictl', 'status'], capture_output=True,
                             env=dict(env, ULANZI_SOCKET=str(sock.parent / 'nope')))
    assert missing.returncode == 2


def test_key_command_can_call_ulanzictl(write_config, monkeypatch):
    monkeypatch.setenv('PATH', '/usr/bin:/bin')  # as in a bare foreground shell
    d = make_daemon(write_config, '[keys.5]\nlabel = "claude"\nrun = "ulanzictl reset 5"\n')

    async def scenario():
        server = await asyncio.start_unix_server(d.serve_client, path=str(d.socket_path))
        async with server:
            d.handle('label 5 CLAUDE WAITING')
            assert d.handle('press 5') == 'ok started'
            await d.running[4]

    asyncio.run(scenario())
    assert d.state.label(4) == 'claude'


def test_icon_names_stay_bounded_and_alternate(write_config):
    d = make_daemon(write_config, '[keys.2]\nwidget = "cpu"\n[keys.3]\nwidget = "mem"\n')
    w = connect(d)

    async def scenario():
        await d.sync(full=True)
        for i in range(50):
            d.state.widget_images = {1: f'cpu {i}'.encode(), 2: f'mem {i // 10}'.encode()}
            await d.sync()
        await d.restore_deck()  # as after a reconnect

    asyncio.run(scenario())
    uploads = [b for b in w.sent if b[0][2:4] in (b'\x00\x01', b'\x00\x0d')]
    per_key: dict[str, list[str]] = {}
    for i in range(len(uploads)):
        manifest, _ = w.manifest(w.sent.index(uploads[i]))
        for pos, entry in manifest.items():
            icon = entry['ViewParam'][0].get('Icon')
            if icon:
                per_key.setdefault(pos, []).append(icon)
    cpu = per_key['1_0']
    assert set(cpu) == {'icons/k02a.png', 'icons/k02b.png'}
    assert all(a != b for a, b in zip(cpu, cpu[1:]))  # never reuses the name on screen
    assert len(per_key['2_0']) < len(cpu)  # unchanged images are not re-sent
    assert len({icon for icons in per_key.values() for icon in icons}) <= 2 * 13
