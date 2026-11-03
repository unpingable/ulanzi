# CLAUDE.md — Instructions for Claude Code

## What This Is

ulanzi: a headless Linux daemon for the Ulanzi D200. Keys run shell commands, and
labels are set over a Unix socket, locally or from other hosts via ssh.

## What This Is Not

- Not a control plane. Keep it small; add machinery only when a real key needs it.
- Not a network service; ssh is the transport.
- Not a GUI or Ulanzi Studio replacement.

## Invariants

1. The daemon owns desired state (config labels plus runtime overrides); every connect re-sends all of it.
2. One request line, one reply line: `ok[ detail]` or `err <reason>`.
3. Key commands are daemon children, never detached, one run per key at a time.
4. Keys are 1–13 and `window` in every user-facing surface; 0-based indices stay internal.
5. The deck never goes longer than `deck.heartbeat` without a host write.
6. Unknown metrics render as unknown, never as a guess or zero.

## Quick Start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ulanzid
.venv/bin/python -m pytest -q
```

## Project Structure

- `ulanzi/daemon.py` — the daemon
- `ulanzi/deckproto.py` — wire frames, parsing, serialized writer
- `ulanzi/deckzip.py` — label tiles and the SET_BUTTONS zip
- `ulanzi/metrics.py`, `ulanzi/tiles.py` — sampler and the one tile renderer
- `qualify/`, `docs/qualification/` — hardware checks and their results
- `ulanzictl` — stdlib-only socket client
- `claude-deck` — Claude Code hooks → attention key 11; must stay stdlib-only, fast, and silent on failure
- `buttons.toml` — key config
- `udev/`, `systemd/` — host install pieces
- `tests/` — unit tests with a fake deck; no hardware

## Conventions

- License: Apache-2.0
- Python 3.11+, single quotes, type hints where they help
- pytest; tests never require the device
- Entry points: `ulanzid` (console script), `ulanzictl` (script)

## Status claims

Long-lived docs rot when they carry too many roles at once — design record, shipped-state log, pickup context, acceptance record.

If a doc claims `shipped`, `built`, `implemented`, `done`, or equivalent, treat that as a claim, not evidence. The claim should name its basis — paths, tests, version/commits, feature-log entry — or be treated as historical until rechecked.

Prefer role separation when authoring: design docs explain why/how work should exist; a separate log records what actually landed; tests, receipts, migrations provide evidence.

Use this repo's local vocabulary. Don't port field names or status taxonomies wholesale from elsewhere — recognition rule travels, vocabulary doesn't.

## Don't

- Select the device by interface number (`deckproto.select_vendor_path`), never by hidraw node or first match; the nodes renumber after a reboot.
- Don't bring strmdck back into the runtime; it is a test-only framing reference. `ulanzi/deckproto.py` owns the wire.
- Don't send LOCKSCREEN (`0x000f`); it rebooted the deck.
- Don't upload icons under unbounded fresh filenames (content hashes, UUIDs); use `deckzip.slot_name`. With real tile sizes, unbounded names exhaust the firmware.
- Don't claim hardware behavior from unit tests; record it in `docs/qualification/` from a real run.
- Don't hardcode host paths or hostnames outside of examples.
