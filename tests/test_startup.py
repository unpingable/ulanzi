import asyncio
import os
from pathlib import Path
import socket
import sys

import pytest
from ulanzi import daemon as ulanzid
from ulanzi import tiles, metrics


def test_config_init_and_check_without_device_access(tmp_path, monkeypatch, capsys):
    target = tmp_path / 'config/buttons.toml'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.delenv('ULANZI_CONFIG', raising=False)
    monkeypatch.setattr(ulanzid.deckproto, 'enumerate_devices', lambda: pytest.fail('validation opened HID'))
    assert ulanzid.main(['--config', str(target), '--init-config']) == 0
    original = target.read_bytes()
    assert ulanzid.main(['--config', str(target), '--init-config']) == 1
    assert 'already exists; not overwritten' in capsys.readouterr().err
    assert target.read_bytes() == original
    assert ulanzid.main(['--config', str(target), '--check']) == 0
    assert 'Configuration OK' in capsys.readouterr().out
    target.write_text('[keys.0]\nlabel="bad"')
    assert ulanzid.main(['--config', str(target), '--check']) == 1
    assert not (tmp_path / 'isolated.sock').exists()


def test_missing_config_has_initialization_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('ULANZI_CONFIG', raising=False)
    monkeypatch.setattr(ulanzid, 'DEFAULT_CONFIG', tmp_path / 'missing-source.toml')
    assert ulanzid.main(['--check']) == 1
    assert 'ulanzid --init-config' in capsys.readouterr().err


def test_user_config_selection_and_installed_helpers(tmp_path, monkeypatch, write_config):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('ULANZI_CONFIG', raising=False)
    user = tmp_path / 'ulanzi/buttons.toml'
    user.parent.mkdir()
    user.write_text('[keys.1]\nlabel="user"')
    assert ulanzid.default_config_path() == user
    monkeypatch.setenv('ULANZI_CONFIG', '/fixture/explicit.toml')
    assert ulanzid.default_config_path() == Path('/fixture/explicit.toml')
    d = ulanzid.D200Device(write_config(''), tmp_path / 's')
    assert d.child_env(0)['PATH'].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert ulanzid.default_socket_path() == Path(os.environ['ULANZI_SOCKET'])


def test_second_daemon_refuses_existing_listener_and_preserves_path(tmp_path, write_config):
    path = tmp_path / 's'
    d = ulanzid.Daemon(write_config(''), path, enumerate_fn=lambda: [])

    async def run():
        def close(reader, writer):
            writer.close()
        server = await asyncio.start_unix_server(close, path=str(path))
        inode = path.stat().st_ino
        async with server:
            with pytest.raises(ulanzid.ConfigError, match='already listening'):
                await d.run()
            assert path.stat().st_ino == inode
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_daemon_lock_stale_socket_recovery_and_path_ownership(tmp_path, write_config):
    path = tmp_path / 's'
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    d = ulanzid.Daemon(write_config(''), path, enumerate_fn=lambda: [])
    second = ulanzid.Daemon(d.config_path, path, enumerate_fn=lambda: [])

    async def run():
        task = asyncio.create_task(d.run())
        try:
            for _ in range(100):
                if path.exists() and path.stat().st_ino != stale_inode:
                    break
                await asyncio.sleep(.01)
            assert not task.done()
            with pytest.raises(ulanzid.ConfigError, match='already owns'):
                await second.run()
            # A replacement pathname is not the first daemon's file to remove.
            path.unlink()
            path.write_text('replacement')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert path.read_text() == 'replacement'
    stale_inode = path.stat().st_ino
    asyncio.run(run())


def test_cancelled_key_command_reaps_its_shell(tmp_path, write_config):
    pidfile = tmp_path / 'child.pid'
    d = ulanzid.D200Device(write_config(f'[keys.1]\nrun="echo $$ > {pidfile}; exec sleep 60"'), tmp_path / 's')

    async def run():
        d.press(0, 'test')
        task = d.running[0]
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(.01)
        pid = int(pidfile.read_text())
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not d.running
    asyncio.run(run())


def test_reload_keeps_observed_widgets(write_config, tmp_path):
    d = ulanzid.D200Device(write_config('[keys.1]\nwidget="cpu"\n[keys.6]\nwidget="weather_cpu"'), tmp_path / 's')
    d.last_sample = metrics.Sample(t=1, cpu=37)
    d.apply_config(d.state.config)
    before = d.state.images()
    d.reload()
    assert d.state.images() == before
    assert d.state.image(0) != tiles.render(tiles.TileSpec('cpu', None))


def test_repeated_open_error_is_logged_once(write_config, tmp_path, capsys):
    d = ulanzid.D200Device(write_config(''), tmp_path / 's')
    def unavailable(_):
        raise OSError('fixture access denied')
    async def run():
        for _ in range(4):
            await d.session({'path': b'/fixture/device'}, unavailable)
    asyncio.run(run())
    errors = capsys.readouterr().err
    assert errors.count('event=session_failed') == 1
    assert 'event=disconnected' not in errors


def test_real_sigterm_stops_daemon_and_key_child(tmp_path):
    import subprocess
    import json
    from conftest import REPO
    config = tmp_path / 'buttons.toml'
    pidfile = tmp_path / 'key.pid'
    socket_path = tmp_path / 'daemon.sock'
    config.write_text(f'[keys.1]\nrun="echo $$ > {pidfile}; exec sleep 60"')
    script = '''from unittest.mock import patch
from ulanzi.daemon import main
with patch('ulanzi.daemon.deckproto.enumerate_devices', return_value=[]):
    raise SystemExit(main(['--config', CONFIG, '--socket', SOCKET]))
'''.replace('CONFIG', repr(str(config))).replace('SOCKET', repr(str(socket_path)))
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.Popen([sys.executable, '-c', script], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(200):
            if socket_path.exists():
                break
            if proc.poll() is not None:
                pytest.fail(proc.communicate()[1])
            __import__('time').sleep(.01)
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            client.sendall(b'press 1\n')
            assert client.recv(1024).startswith(b'ok started')
        for _ in range(100):
            if pidfile.exists():
                break
            __import__('time').sleep(.01)
        child_pid = int(pidfile.read_text())
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 0, stderr
        assert not socket_path.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
