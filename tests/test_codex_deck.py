import io
import json

from conftest import REPO

import pytest
from ulanzi.agent_deck import Adapter
from ulanzi import agent_deck
cd = Adapter('codex')
cd.attention = cd


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    (root / '.git').mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def isolate_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_deck.time, 'time', lambda: 1_000_000)
    monkeypatch.setattr(cd, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(cd, 'SOCKET', str(tmp_path / 'no-daemon'))
    monkeypatch.setattr(cd, 'TARGET', '')



def test_lifecycle_targeted_and_ack(tmp_path, monkeypatch, project):
    sent = []
    monkeypatch.setattr(cd.attention, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(cd, 'TARGET', 'matrix')
    monkeypatch.setattr(cd, 'original_send', lambda line: sent.append(line) or True)
    def hook(event, session='s1'):
        return cd.main(['codex-deck', 'hook'], io.StringIO(json.dumps({'session_id': session, 'hook_event_name': event, 'cwd': str(project)})))
    assert hook('PermissionRequest') == 0
    assert sent[-1] == 'target matrix attention 12 codex waiting 1 @1000000 ' + project.name
    hook('PostToolUse')
    assert sent[-1] == 'target matrix attention 12 codex working 1 @1000000 ' + project.name
    hook('Stop')
    assert sent[-1] == 'target matrix attention 12 codex done 1 @1000000 ' + project.name
    cd.main(['codex-deck', 'ack'])
    assert sent[-1] == 'target matrix reset 12'
    hook('Interrupt', 's2')
    assert sent[-1] == 'target matrix attention 12 codex interrupted 1 @1000000 ' + project.name
    hook('SessionEnd', 's2')
    assert sent[-1] == 'target matrix reset 12'


def test_notify_and_failure(tmp_path, monkeypatch, project):
    sent = []
    monkeypatch.setattr(cd.attention, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(cd, 'original_send', lambda line: sent.append(line) or True)
    cd.main(['codex-deck', 'notify', json.dumps({'type': 'agent-turn-complete', 'thread-id': 't1', 'cwd': str(project)})])
    assert 'codex done 1 @1000000 ' + project.name in sent[-1]
    before = sent[:]
    assert cd.main(['codex-deck', 'hook'], io.StringIO('invalid')) == 0
    assert cd.main(['codex-deck', 'hook'], io.StringIO('{"hook_event_name":"Unknown"}')) == 0
    assert sent == before
    monkeypatch.setattr(cd, 'TARGET', 'all')
    assert cd.send('reset 12') is False
    monkeypatch.setattr(cd, 'TARGET', 'bad\nname')
    assert cd.send('reset 12') is False


def test_hook_example():
    config = json.loads((REPO / 'examples/codex-hooks.json').read_text())
    assert set(config['hooks']) == {'UserPromptSubmit', 'PermissionRequest', 'PostToolUse', 'Stop', 'Interrupt', 'SessionEnd'}
    assert all('codex-deck hook' in row['hooks'][0]['command'] for rows in config['hooks'].values() for row in rows)
