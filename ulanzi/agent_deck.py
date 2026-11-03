"""Small stdlib-only attention adapters. Hooks are observers and emit no output.

No transcripts, credentials, tool arguments or prompt content are read or saved.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import time

WORKING, WAITING, DONE, SEEN, ERROR, INTERRUPTED = 'working', 'waiting', 'done', 'seen', 'error', 'interrupted'
STALE_AFTER = 12 * 3600
MAX_SESSIONS = 256
MAX_INPUT = 65536
AGENTS = {'claude': '11', 'codex': '12', 'kimi': '13', 'agy': '13'}


def repo_name(cwd):
    path = Path(cwd or '/')
    for parent in (path, *path.parents):
        if (parent / '.git').exists():
            path = parent
            break
    return ''.join(c if c.isprintable() and not c.isspace() else '_' for c in (path.name or '/'))[:64]


def next_state(agent, event, payload, current):
    if event == 'SessionEnd':
        return None
    if event in ('UserPromptSubmit', 'PostToolUse', 'PostToolUseFailure', 'PermissionResult', 'TurnStarted', 'PreInvocation'):
        return WORKING
    if event == 'PermissionRequest':
        return WAITING
    if event == 'StopFailure':
        return ERROR
    if event == 'Interrupt':
        return INTERRUPTED
    if event == 'Stop':
        return DONE
    if agent == 'agy' and event == 'PreToolUse':
        tool = payload.get('toolCall') or {}
        # A question call really does request input. Generic tool calls do not
        # establish whether a permission prompt was shown.
        return WAITING if tool.get('name') == 'ask_question' else current
    if agent == 'claude' and event == 'Notification':
        kind = payload.get('notification_type', '')
        message = str(payload.get('message', '')).lower()
        if kind in ('permission_prompt', 'elicitation_dialog') or (not kind and 'permission' in message):
            return WAITING
    return current


def aggregate(sessions):
    for state, word in ((WAITING, 'WAIT'), (ERROR, 'ERROR'), (INTERRUPTED, 'STOPPED'), (DONE, 'DONE'), (WORKING, 'ACTIVE')):
        selected = [s for s in sessions if s['state'] == state]
        if selected:
            repos = sorted({s['repo'] for s in selected})
            names = ' '.join(repos[:2]) + (f' +{len(repos)-2}' if len(repos) > 2 else '')
            return f'{len(selected)} {word} {names}'
    return None


class Store:
    def __init__(self, root, lock_name='.lock'):
        self.root = root
        self.lock_name = lock_name

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / self.lock_name).open('a')
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.lock.close()
                    raise TimeoutError('attention store busy')
                time.sleep(.005)

    def __exit__(self, *exc):
        self.lock.close()

    def _path(self, session_id):
        return self.root / (hashlib.sha256(session_id.encode()).hexdigest() + '.json')

    def get(self, session_id):
        try:
            record = json.loads(self._path(session_id).read_text())
            return record if self.valid(record) else None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def valid(record):
        return (isinstance(record, dict) and record.get('state') in
                (WORKING, WAITING, DONE, SEEN, ERROR, INTERRUPTED) and
                isinstance(record.get('repo'), str) and
                isinstance(record.get('t'), (float, int)))

    def write(self, path, record):
        if record is None:
            path.unlink(missing_ok=True)
        else:
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(record))
            tmp.replace(path)

    def put(self, session_id, record):
        self.write(self._path(session_id), record)

    def records(self, now):
        found = []
        for path in self.root.glob('*.json'):
            try:
                record = json.loads(path.read_text())
                if not self.valid(record) or now - record['t'] > STALE_AFTER:
                    path.unlink(missing_ok=True)
                    continue
                found.append((path, record))
            except (OSError, ValueError, TypeError):
                path.unlink(missing_ok=True)
        found.sort(key=lambda item: item[1]['t'], reverse=True)
        for path, _ in found[MAX_SESSIONS:]:
            path.unlink(missing_ok=True)
        return found[:MAX_SESSIONS]

    def sessions(self, now):
        return [record for _, record in self.records(now)]


class Adapter:
    STALE_AFTER = STALE_AFTER
    WORKING, WAITING, DONE, SEEN = WORKING, WAITING, DONE, SEEN

    def __init__(self, agent):
        self.agent = agent
        self.KEY = os.environ.get(f'ULANZI_{agent.upper()}_KEY', AGENTS[agent])
        self.TARGET = os.environ.get(f'ULANZI_{agent.upper()}_DEVICE', '')
        self.scope_target = self.TARGET
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
        self.SOCKET = os.environ.get('ULANZI_SOCKET', str(runtime / 'ulanzi.sock'))
        self.base = runtime / 'ulanzi' / agent
        self.STATE_DIR = None
        self.last_error = ''
        self.original_send = self._socket_send

    def _request(self, line):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(.3)
            s.connect(self.SOCKET)
            s.sendall(line.encode() + b'\n')
            return s.makefile(encoding='utf-8').readline(MAX_INPUT).strip()

    def _socket_send(self, line):
        try:
            reply = self._request(line)
            if reply == 'ok' or reply.startswith('ok '):
                self.last_error = ''
                return True
            self.last_error = reply or 'empty daemon reply'
        except OSError as e:
            self.last_error = str(e)
        return False

    def resolve_target(self):
        target = self.TARGET or os.environ.get('ULANZI_IDENTITY') or os.environ.get('ULANZI_DEVICE', '')
        try:
            reply = self._request('devices')
            rows = json.loads(reply[3:])['devices'] if reply.startswith('ok ') else []
            matches = [d for d in rows if target in (d.get('name'), d.get('identity'))] if target else rows
            if len(matches) == 1:
                device = matches[0]
                name = device.get('name', '')
                target = name if name and not name.startswith('@') else device.get('identity') or name
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.TARGET = target
        if self.STATE_DIR is None:
            # Untargeted single-deck hooks and physical acknowledgments share a
            # scope even when the daemon was unavailable during an earlier hook.
            scope = self.scope_target
            destination = hashlib.sha256(f'{self.SOCKET}\0{scope}\0{self.KEY}'.encode()).hexdigest()[:24]
            self.STATE_DIR = self.base / destination

    def send(self, line):
        if not self.KEY.isdecimal() or not 1 <= int(self.KEY) <= 13:
            self.last_error = 'key must be 1..13'
            return False
        if self.TARGET:
            if self.TARGET == 'all' or any(c.isspace() for c in self.TARGET):
                self.last_error = 'attention requires one valid device target'
                return False
            line = f'target {self.TARGET} {line}'
        return self.original_send(line)

    def next_state(self, event, payload, current):
        return next_state(self.agent, event, payload, current)

    def on_hook(self, payload, store, now, explicit_event=None):
        event = explicit_event or payload.get('hook_event_name', '')
        allowed = {'UserPromptSubmit', 'PostToolUse', 'PostToolUseFailure', 'PermissionRequest', 'Stop', 'StopFailure', 'SessionEnd', 'Interrupt'}
        if self.agent == 'claude':
            allowed.add('Notification')
        if self.agent == 'kimi':
            allowed.update(('TurnStarted', 'PermissionResult'))
        if self.agent == 'agy':
            allowed = {'PreInvocation', 'PostToolUse', 'PreToolUse', 'Stop'}
            payload = dict(payload, session_id=payload.get('conversationId'),
                           cwd=(payload.get('workspacePaths') or [''])[0])
        session = payload.get('session_id')
        if event not in allowed or not isinstance(session, str) or not session:
            return False
        current = store.get(session)
        if event == 'Notification' and self.next_state(event, payload, None) != WAITING:
            return False
        if self.agent == 'agy' and event == 'PreToolUse' and self.next_state(event, payload, None) != WAITING:
            return False
        state = self.next_state(event, payload, current and current['state'])
        if state is None:
            store.put(session, None)
        else:
            store.put(session, {'state': state, 'repo': repo_name(payload.get('cwd') or (current or {}).get('cwd', '')),
                                'cwd': payload.get('cwd') or (current or {}).get('cwd', ''), 't': now})
        return True

    def redraw(self, store, now):
        # Serialize delivery separately from state mutation. Slow replies cannot
        # block another hook from recording a new transition. Recompute after
        # acquiring delivery ownership so delayed senders do not replay old state.
        with Store(store.root, '.delivery'):
            with Store(store.root) as current:
                sessions = current.sessions(now)
            self._deliver(store.root, sessions)

    def _deliver(self, root, sessions):
        text = aggregate(sessions)
        if text:
            count, word, repos = text.split(' ', 2)
            state = {'WAIT': WAITING, 'DONE': DONE, 'ERROR': ERROR, 'STOPPED': INTERRUPTED, 'ACTIVE': WORKING}[word]
            updated = max(s['t'] for s in sessions if s['state'] == state)
            line = f'attention {self.KEY} {self.agent} {state} {count} @{int(updated)} {repos}'
        else:
            line = f'reset {self.KEY}'
        # Always reconcile with the daemon. It already suppresses unchanged HID
        # images, and a local "already shown" cache hides state after restarts.
        ok = self.send(line)
        if not ok and text and self.last_error.startswith('err unknown command'):
            ok = self.send(f'label {self.KEY} {text}')  # older daemon compatibility
        (root / '.last_error').write_text('' if ok else self.last_error or 'delivery failed')

    def main(self, argv, stdin=sys.stdin):
        mode = argv[1] if len(argv) > 1 else 'hook'
        try:
            self.resolve_target()
            now = time.time()
            with Store(self.STATE_DIR) as store:
                if mode == 'hook':
                    raw = stdin.read(MAX_INPUT + 1)
                    if len(raw) > MAX_INPUT:
                        return 0
                    payload = json.loads(raw or '{}')
                    if not isinstance(payload, dict) or not self.on_hook(payload, store, now, argv[2] if len(argv) > 2 else None):
                        return 0
                elif mode == 'notify' and self.agent == 'codex':
                    payload = json.loads(argv[2])
                    if payload.get('type') != 'agent-turn-complete':
                        return 0
                    payload.update(session_id=payload.get('thread-id'), hook_event_name='Stop')
                    if not self.on_hook(payload, store, now):
                        return 0
                elif mode == 'ack':
                    for path, record in store.records(now):
                        if record['state'] in (DONE, ERROR, INTERRUPTED):
                            store.write(path, dict(record, state=SEEN))
                elif mode in ('show', 'sync'):
                    if mode == 'show':
                        print(aggregate(store.sessions(now)) or '(nothing known needs you)')
                        for record in store.sessions(now):
                            print(f"  {record['state']:12} {record['repo']}")
                        err = store.root / '.last_error'
                        if err.exists() and err.read_text():
                            print('Last delivery error:', err.read_text())
                        return 0
                else:
                    return 0
            self.redraw(store, now)
        except Exception:
            pass  # observer failures cannot interrupt an agent or grant approval
        return 0


def run(agent):
    return Adapter(agent).main(sys.argv)
