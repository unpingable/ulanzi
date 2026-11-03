import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture(autouse=True)
def isolate_deck_environment(monkeypatch, tmp_path):
    for name in ('ULANZI_DEVICE', 'ULANZI_IDENTITY', 'ULANZI_CLAUDE_DEVICE',
                 'ULANZI_CODEX_DEVICE', 'ULANZI_KIMI_DEVICE', 'ULANZI_AGY_DEVICE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ULANZI_SOCKET', str(tmp_path / 'isolated.sock'))
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'runtime'))


@pytest.fixture
def write_config(tmp_path):
    def write(text):
        p = tmp_path / 'buttons.toml'
        p.write_text(text)
        return p
    return write
