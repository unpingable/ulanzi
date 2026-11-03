import asyncio
import io
import json
import threading

import pytest

from conftest import REPO

from ulanzi.agent_deck import Adapter, Store
cd = Adapter('claude')
cd.aggregate = __import__('ulanzi.agent_deck', fromlist=['*']).aggregate
cd.repo_name = __import__('ulanzi.agent_deck', fromlist=['*']).repo_name
cd.time = __import__('time')



@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.time, 'time', lambda: 1_000_000)
    """A Store in tmp and a fake ulanzid that records socket lines."""
    sent = []
    monkeypatch.setattr(cd, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(cd, 'SOCKET', str(tmp_path / 'no-daemon'))
    monkeypatch.setattr(cd, 'send', lambda line: sent.append(line) or True)
    return sent


def hook(event, session='s1', cwd='/fixture/ulanzi', **extra):
    payload = {'session_id': session, 'hook_event_name': event, 'cwd': cwd, **extra}
    return cd.main(['claude-deck', 'hook'], stdin=io.StringIO(json.dumps(payload)))


def test_repo_name(tmp_path):
    (tmp_path / 'proj' / '.git').mkdir(parents=True)
    (tmp_path / 'proj' / 'docs').mkdir()
    assert cd.repo_name(str(tmp_path / 'proj' / 'docs')) == 'proj'
    assert cd.repo_name('/') == '/'


def test_next_state():
    ns = cd.next_state
    assert ns('UserPromptSubmit', {}, None) == 'working'
    assert ns('Notification', {'message': 'Claude needs your permission to use Bash'}, 'working') == 'waiting'
    assert ns('PostToolUse', {}, 'waiting') == 'working'
    assert ns('Stop', {}, 'working') == 'done'
    # the idle reminder after a finished turn doesn't escalate it
    assert ns('Notification', {'message': 'Claude is waiting for your input'}, 'done') == 'done'
    assert ns('Notification', {'message': 'Claude is waiting for your input'}, 'seen') == 'seen'
    assert ns('SessionEnd', {}, 'done') is None


def test_aggregate_precedence():
    s = lambda state, repo: {'state': state, 'repo': repo, 't': 0}
    assert cd.aggregate([]) is None
    assert cd.aggregate([s('working', 'a'), s('seen', 'b')]) == '1 ACTIVE a'
    assert cd.aggregate([s('seen', 'b')]) is None
    assert cd.aggregate([s('done', 'ulanzi'), s('working', 'x')]) == '1 DONE ulanzi'
    both = [s('done', 'a'), s('waiting', 'civild'), s('waiting', 'ulanzi')]
    assert cd.aggregate(both) == '2 WAIT civild ulanzi'
    many = [s('done', r) for r in ('a', 'b', 'c', 'd')]
    assert cd.aggregate(many) == '4 DONE a b +2'


def test_flow_reconciles_even_when_unchanged(deck, tmp_path):
    civild = tmp_path / 'civild'
    (civild / '.git').mkdir(parents=True)
    active = 'attention 11 claude working 1 @1000000 ulanzi'
    hook('UserPromptSubmit')
    assert deck == [active]
    hook('PostToolUse')
    hook('UserPromptSubmit', session='s2', cwd=str(civild))
    assert deck == [active, active, 'attention 11 claude working 2 @1000000 civild ulanzi']
    hook('Notification', message='Claude needs your permission to use Bash')
    assert deck[-1] == 'attention 11 claude waiting 1 @1000000 ulanzi'
    hook('PostToolUse')
    hook('Stop')
    assert deck[-1] == 'attention 11 claude done 1 @1000000 ulanzi'
    hook('Stop', session='s2', cwd=str(civild))
    assert deck[-1] == 'attention 11 claude done 2 @1000000 civild ulanzi'
    cd.main(['claude-deck', 'ack'])
    assert deck[-1] == 'reset 11'
    hook('SessionEnd')
    assert deck[-2:] == ['reset 11', 'reset 11']



def test_stale_sessions_are_forgotten(deck, monkeypatch):
    hook('Stop')
    assert deck[-1] == 'attention 11 claude done 1 @1000000 ulanzi'
    later = cd.time.time() + cd.STALE_AFTER + 1
    monkeypatch.setattr(cd.time, 'time', lambda: later)
    hook('UserPromptSubmit', session='other')
    assert deck[-1] == 'attention 11 claude working 1 @1043201 ulanzi'
    assert Store(cd.STATE_DIR).get('s1') is None


def test_failures_are_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(cd, 'SOCKET', str(tmp_path / 'no-daemon'))
    assert hook('Stop') == 0  # daemon unreachable
    assert cd.main(['claude-deck', 'hook'], stdin=io.StringIO('not json')) == 0
    (tmp_path / 'state').chmod(0o500)
    try:
        assert hook('Stop', session='s9') == 0  # unwritable state
    finally:
        (tmp_path / 'state').chmod(0o700)
    # nothing was shown, so the next working hook still tries to reach the daemon
    assert (tmp_path / 'state' / '.shown').exists() is False


def test_concurrent_hooks_keep_every_session(deck):
    threads = [threading.Thread(target=hook, args=('Stop',), kwargs={'session': f's{i}', 'cwd': f'/tmp/r{i}'})
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert deck[-1].startswith('attention 11 claude done 20')


def test_against_real_daemon_socket(tmp_path, monkeypatch, write_config):
    from ulanzi import daemon as ulanzid
    d = ulanzid.Daemon(write_config('[keys.11]\nlabel = "claude"\n'), tmp_path / 'sock')
    monkeypatch.setattr(cd, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(cd, 'SOCKET', str(tmp_path / 'sock'))

    async def scenario():
        server = await asyncio.start_unix_server(d.serve_client, path=str(d.socket_path))
        async with server:
            await asyncio.to_thread(hook, 'Notification', message='needs your permission')
            waiting = d.targets()[0].state.describe(10)
            await asyncio.to_thread(hook, 'PostToolUse')
            return waiting, d.targets()[0].state.label(10)

    assert asyncio.run(scenario()) == ('<claude:waiting:1 ulanzi>', 'claude')
