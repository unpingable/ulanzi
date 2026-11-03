import io
import json
import time

import pytest
from ulanzi import agent_deck, daemon, tiles


@pytest.mark.parametrize('agent', ['claude', 'codex', 'kimi'])
def test_hook_and_physical_ack_resolve_same_store(agent, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setenv('ULANZI_SOCKET', str(tmp_path / 's'))
    inventory = 'ok ' + json.dumps({'devices': [{'name': '@serial:EXAMPLE', 'identity': 'serial:EXAMPLE'}]})
    sent = []
    monkeypatch.setattr(agent_deck.Adapter, '_request', lambda self, line: inventory if line == 'devices' else sent.append(line) or 'ok')
    hook = agent_deck.Adapter(agent)
    payload = {'session_id': 's', 'hook_event_name': 'Stop', 'cwd': '/fixture/project'}
    assert hook.main(['hook', 'hook'], io.StringIO(json.dumps(payload))) == 0
    monkeypatch.setenv('ULANZI_IDENTITY', 'serial:EXAMPLE')
    ack = agent_deck.Adapter(agent)
    ack.main(['ack', 'ack'])
    assert ack.STATE_DIR == hook.STATE_DIR
    assert agent_deck.Store(ack.STATE_DIR).get('s')['state'] == 'seen'
    assert sent[-1] == f'target serial:EXAMPLE reset {ack.KEY}'


def test_explicit_identity_scope_survives_role_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('ULANZI_CLAUDE_DEVICE', 'serial:EXAMPLE')
    reply = 'ok ' + json.dumps({'devices': [{'name': 'desk', 'identity': 'serial:EXAMPLE'}]})
    monkeypatch.setattr(agent_deck.Adapter, '_request', lambda *args: reply)
    a = agent_deck.Adapter('claude')
    a.resolve_target()
    monkeypatch.setattr(agent_deck.Adapter, '_request', lambda *args: reply.replace('desk', 'studio'))
    monkeypatch.setenv('ULANZI_IDENTITY', 'serial:EXAMPLE')
    b = agent_deck.Adapter('claude')
    b.resolve_target()
    assert a.STATE_DIR == b.STATE_DIR and a.TARGET == 'desk' and b.TARGET == 'studio'


def test_untargeted_scope_survives_daemon_unavailability(tmp_path, monkeypatch):
    monkeypatch.setenv('ULANZI_SOCKET', str(tmp_path / 's'))
    def unavailable(*args):
        raise FileNotFoundError('fixture offline')
    monkeypatch.setattr(agent_deck.Adapter, '_request', unavailable)
    offline = agent_deck.Adapter('kimi')
    offline.main(['kimi-deck', 'hook'], io.StringIO(json.dumps(
        {'session_id': 's', 'hook_event_name': 'Stop', 'cwd': '/fixture/project'})))
    monkeypatch.setattr(agent_deck.Adapter, '_request', lambda *args: 'ok ' + json.dumps(
        {'devices': [{'name': '@serial:EXAMPLE', 'identity': 'serial:EXAMPLE'}]}))
    online = agent_deck.Adapter('kimi')
    online.resolve_target()
    assert online.STATE_DIR == offline.STATE_DIR
    assert agent_deck.Store(online.STATE_DIR).get('s')['state'] == 'done'


def test_kimi_and_agy_contract_events_are_not_guessed():
    ns = agent_deck.next_state
    assert ns('kimi', 'PermissionRequest', {}, 'working') == 'waiting'
    assert ns('kimi', 'PermissionResult', {}, 'waiting') == 'working'
    assert ns('kimi', 'StopFailure', {}, 'working') == 'error'
    assert ns('kimi', 'Notification', {'notification_type': 'task.completed'}, 'working') == 'working'
    assert ns('agy', 'PreToolUse', {'toolCall': {'name': 'run_command'}}, 'working') == 'working'
    assert ns('agy', 'PreToolUse', {'toolCall': {'name': 'ask_question'}}, 'working') == 'waiting'
    assert ns('agy', 'Stop', {}, 'working') == 'done'


@pytest.mark.parametrize('agent,key', [('claude', '11'), ('codex', '12'), ('kimi', '13'), ('agy', '13')])
def test_active_sessions_are_visible_for_every_agent(agent, key, tmp_path, monkeypatch):
    a = agent_deck.Adapter(agent)
    a.STATE_DIR = tmp_path
    sent = []
    monkeypatch.setattr(a, 'original_send', lambda line: sent.append(line) or True)
    with agent_deck.Store(tmp_path) as store:
        store.put('active', {'state': 'working', 'repo': 'project', 't': 100})
    a.redraw(store, 100)
    assert sent == [f'attention {key} {agent} working 1 @100 project']


def test_active_yields_to_pending_attention():
    active = {'state': 'working', 'repo': 'active', 't': 200}
    done = {'state': 'done', 'repo': 'pending', 't': 100}
    assert agent_deck.aggregate([active, done]) == '1 DONE pending'
    assert agent_deck.aggregate([active]) == '1 ACTIVE active'


def test_active_attention_reaches_tile(write_config, tmp_path):
    d = daemon.D200Device(write_config('[keys.11]\nwidget="agent_claude"'), tmp_path / 's')
    assert d.handle('attention 11 claude working 1 @100 project') == 'ok'
    assert d.state.attention[10] == ('claude', 'working', 1, 'project', 100)
    assert d.state.image(10) == tiles.render_attention('claude', 'working', 1, 'project', 100)


def test_agent_key_can_hide_only_active_attention(write_config, tmp_path):
    d = daemon.D200Device(write_config(
        '[keys.11]\nwidget="agent_claude"\nshow_active=false'), tmp_path / 's')
    assert d.handle('attention 11 claude working 1 @100 project') == 'ok'
    assert d.state.describe(10) == '<agent_claude>'
    assert d.state.image(10) == tiles.render_attention('claude')
    assert d.handle('attention 11 claude done 1 @100 project') == 'ok'
    assert d.state.describe(10) == '<claude:done:1 project>'
    assert d.state.image(10) == tiles.render_attention('claude', 'done', 1, 'project', 100)


def test_unchanged_wait_is_resent_after_daemon_state_loss(tmp_path, monkeypatch):
    a = agent_deck.Adapter('kimi')
    a.STATE_DIR = tmp_path
    monkeypatch.setattr(a, '_request', lambda line: 'ok {"devices":[]}')
    sent = []
    monkeypatch.setattr(a, 'original_send', lambda line: sent.append(line) or True)
    payload = json.dumps({'session_id': 's', 'hook_event_name': 'PermissionRequest', 'cwd': '/fixture/repo'})
    a.main(['kimi-deck', 'hook'], io.StringIO(payload))
    a.main(['kimi-deck', 'hook'], io.StringIO(payload))
    assert len(sent) == 2 and all('attention 13 kimi waiting 1' in line for line in sent)


def test_irrelevant_notifications_do_not_refresh_wait_age(tmp_path):
    a = agent_deck.Adapter('claude')
    with agent_deck.Store(tmp_path) as store:
        a.on_hook({'session_id': 's', 'hook_event_name': 'PermissionRequest'}, store, 100)
        assert not a.on_hook({'session_id': 's', 'hook_event_name': 'Notification', 'notification_type': 'auth_success'}, store, 900)
        assert store.get('s')['t'] == 100


def test_store_bounds_names_and_malformed_records(tmp_path):
    with agent_deck.Store(tmp_path) as store:
        for n in range(agent_deck.MAX_SESSIONS + 4):
            store.put(str(n), {'state': 'done', 'repo': 'x', 't': n})
        (tmp_path / 'bad.json').write_text('[]')
        assert len(store.sessions(1000)) == agent_deck.MAX_SESSIONS
        assert len(list(tmp_path.glob('*.json'))) == agent_deck.MAX_SESSIONS
        store.put('a/b', {'state': 'done', 'repo': 'x', 't': 1})
        store.put('ab', {'state': 'waiting', 'repo': 'y', 't': 2})
        assert store.get('a/b')['repo'] == 'x' and store.get('ab')['repo'] == 'y'


def test_attention_wire_and_stale_rendering(write_config, tmp_path, monkeypatch):
    d = daemon.D200Device(write_config('[keys.11]\nwidget="agent_claude"'), tmp_path / 's')
    assert d.handle('attention 11 claude waiting 2 @100 first second') == 'ok'
    assert d.state.attention[10] == ('claude', 'waiting', 2, 'first second', 100)
    monkeypatch.setattr(tiles.time, 'time', lambda: 200)
    fresh = d.state.image(10)
    monkeypatch.setattr(tiles.time, 'time', lambda: 2000)
    assert d.state.image(10) != fresh
    before = dict(d.state.attention)
    for bad in ('attention 11 claude waiting 1 @nan nope', 'attention 11 unknown done 1', 'attention 11 claude done 999'):
        assert d.handle(bad).startswith('err ')
        assert d.state.attention == before
    assert d.handle('reset 11') == 'ok' and not d.state.attention
    assert d.state.image(10) == tiles.render_attention('claude')


def test_agy_explicit_event_and_no_approval_output(tmp_path, monkeypatch, capsys):
    a = agent_deck.Adapter('agy')
    a.STATE_DIR = tmp_path
    monkeypatch.setattr(a, '_request', lambda line: 'ok {"devices":[]}')
    sent = []
    monkeypatch.setattr(a, 'original_send', lambda line: sent.append(line) or True)
    payload = {'conversationId': 'example', 'workspacePaths': ['/fixture/project']}
    assert a.main(['agy-deck', 'hook', 'Stop'], io.StringIO(json.dumps(payload))) == 0
    assert 'agy done 1' in sent[-1]
    assert capsys.readouterr().out == ''


def test_agy_published_pretool_fixture(tmp_path):
    from conftest import REPO
    payload = json.loads((REPO / 'tests/fixtures/agy-pre-tool-use.json').read_text())
    a = agent_deck.Adapter('agy')
    with agent_deck.Store(tmp_path) as store:
        assert not a.on_hook(payload, store, 100, 'PreToolUse')
        payload['toolCall'] = {'name': 'ask_question', 'args': {'questions': []}}
        assert a.on_hook(payload, store, 101, 'PreToolUse')
        record = store.get(payload['conversationId'])
        assert record['state'] == 'waiting' and record['repo'] == 'project'


def test_new_wait_is_not_dimmed_by_an_older_wait(tmp_path, monkeypatch):
    a = agent_deck.Adapter('kimi')
    a.STATE_DIR = tmp_path
    sent = []
    monkeypatch.setattr(a, 'original_send', lambda line: sent.append(line) or True)
    with agent_deck.Store(tmp_path) as store:
        store.put('old', {'state': 'waiting', 'repo': 'old', 't': 0})
        store.put('new', {'state': 'waiting', 'repo': 'new', 't': 2000})
    a.redraw(store, 2000)
    assert '@2000' in sent[-1]


def test_explicit_scope_survives_offline_hook_and_online_ack(tmp_path, monkeypatch):
    monkeypatch.setenv('ULANZI_CLAUDE_DEVICE', 'serial:EXAMPLE')
    monkeypatch.setenv('ULANZI_SOCKET', str(tmp_path / 's'))
    def offline(*args):
        raise FileNotFoundError('fixture offline')
    monkeypatch.setattr(agent_deck.Adapter, '_request', offline)
    a = agent_deck.Adapter('claude')
    a.main(['claude-deck', 'hook'], io.StringIO(json.dumps(
        {'session_id': 's', 'hook_event_name': 'Stop', 'cwd': '/fixture/project'})))
    reply = 'ok ' + json.dumps({'devices': [{'name': 'desk', 'identity': 'serial:EXAMPLE'}]})
    monkeypatch.setattr(agent_deck.Adapter, '_request', lambda self, line: reply if line == 'devices' else 'ok')
    b = agent_deck.Adapter('claude')
    b.main(['claude-deck', 'ack'])
    assert a.STATE_DIR == b.STATE_DIR
    assert agent_deck.Store(b.STATE_DIR).get('s')['state'] == 'seen'


def test_slow_delivery_does_not_drop_concurrent_hook_state(tmp_path, monkeypatch):
    import threading
    a = agent_deck.Adapter('kimi')
    a.STATE_DIR = tmp_path
    monkeypatch.setattr(a, '_request', lambda line: 'ok {"devices":[]}')
    sending = threading.Event()
    def slow_send(line):
        sending.set()
        time.sleep(.25)
        return True
    monkeypatch.setattr(a, 'original_send', slow_send)
    def hook(session):
        a.main(['kimi-deck', 'hook'], io.StringIO(json.dumps(
            {'session_id': session, 'hook_event_name': 'Stop', 'cwd': '/fixture/project'})))
    first = threading.Thread(target=hook, args=('a',))
    second = threading.Thread(target=hook, args=('b',))
    first.start()
    assert sending.wait(2)
    second.start()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    store = agent_deck.Store(tmp_path)
    assert store.get('a')['state'] == store.get('b')['state'] == 'done'
