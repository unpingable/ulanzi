# AGENTS.md — Working in this repo

This file is a **travel guide**, not a law.
If anything here conflicts with the user's explicit instructions, the user wins.

> Instruction files shape behavior; the user determines direction.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ulanzid
.venv/bin/python -m pytest -q
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Always run tests before proposing commits. Never claim tests pass without running them.

Tests never touch the device. Hardware behavior (labels showing on keys, key
numbering, replug recovery) is checked by hand and should be reported that way.

---

## Safety and irreversibility

### Do not do these without explicit user confirmation
- Push to remote, create or close PRs, or close issues
- Delete or rewrite git history
- Modify dependency files in ways that change the lock file
- Install or change udev rules, systemd units, or anything else outside the repo
- Send commands to a live daemon or deck someone is using

### Preferred workflow
- Make changes in small, reviewable steps
- Run tests locally before proposing commits
- For external-state changes not covered by standing owner authorization, require explicit user confirmation.

---

## Repository layout

```
ulanzi/daemon.py        the daemon: loops for input, heartbeat, clock, tiles, label pushes
ulanzi/deckproto.py      wire frames, report parsing, device selection, serialized writer
ulanzi/deckzip.py        renders labels as PNG tiles and builds the SET_BUTTONS zip
ulanzi/metrics.py        /proc and statvfs sampler with bounded in-memory history
ulanzi/tiles.py          the one status-tile renderer and the widget definitions
ulanzictl         stdlib-only socket client (also used over ssh)
claude-deck       Claude Code hook -> attention key (stdlib-only, never fails loudly)
buttons.toml      key labels, widgets, commands, deck settings
qualify/          interactive hardware checks (watchdog, refresh) -> JSON receipts
docs/qualification/  what was measured on real hardware, and what wasn't
udev/             hidraw access rule
systemd/          user unit template (@REPO@ substituted at install)
tests/            unit tests with fake /proc and a fake writer; no hardware needed
```

---

## Coding conventions

- Python 3.11+ (uses `tomllib`), type hints where they help, single quotes.
- pytest. Test the pure parts (config, desired state, socket replies) and fake the deck.
- `ulanzictl` stays standard-library only so it runs anywhere without the venv.
- Keep wire framing in `ulanzi/deckproto.py` and archive quirks in `ulanzi/deckzip.py`.
  strmdck is an optional test reference, not a runtime dependency.

---

## Invariants

Non-negotiable rules that must hold at all times. If these break, something
is wrong — not "could be improved," wrong.

1. The daemon owns desired state (config labels plus runtime overrides). The deck is
   re-sent the full state after every connect.
2. Every socket request gets exactly one reply line: `ok[ detail]` or `err <reason>`.
3. Key commands are daemon children, one run at a time per key. Nothing is detached.
4. No network listener. Remote access goes through ssh and `ulanzictl`.
5. Keys are 1–13 and `window` everywhere people see them: config, CLI, logs, env.
   Protocol indices (0–13) stay internal. No toggle for 0-based numbering.
6. The deck never goes more than `deck.heartbeat` seconds without a host write, and
   the heartbeat limits stay well under the firmware watchdog.
7. Unknown metrics render as unknown. Never substitute a guess or a zero on a tile.
8. Never send LOCKSCREEN (`0x000f`); it rebooted the deck.
9. Icon filenames stay bounded (`deckzip.slot_name`: two per key, alternating). Never
   upload tiles under fresh names indefinitely; with real tile sizes that exhausts the firmware.

---

## What this is not

These prevent well-intentioned drift.

- Not a GUI, page editor, or Ulanzi Studio replacement.
- Not a network service or control plane. Add machinery only when a real key needs it.
- Not a desktop automation layer; a key's command does that if needed.

---

## Status claims

Long-lived docs rot when they carry too many roles at once — design record, shipped-state log, pickup context, acceptance record.

If a doc claims `shipped`, `built`, `implemented`, `done`, or equivalent, treat that as a claim, not evidence. The claim should name its basis — paths, tests, version/commits, feature-log entry — or be treated as historical until rechecked.

Prefer role separation when authoring: design docs explain why/how work should exist; a separate log records what actually landed; tests, receipts, migrations provide evidence.

Use this repo's local vocabulary. Don't port field names or status taxonomies wholesale from elsewhere — recognition rule travels, vocabulary doesn't.

---

## When you're unsure

Ask for clarification rather than guessing, especially around:
- Whether a key should act locally, act on another host, or only indicate
- Anything that changes what the deck shows by default
- Anything that changes a documented invariant

---

## Agent-specific instruction files

| Agent | File | Role |
|-------|------|------|
| Claude Code | `CLAUDE.md` | Full operational context, build details, conventions |
| Codex | `AGENTS.md` (this file) | Operating context + defaults |
| Any future agent | `AGENTS.md` (this file) | Start here |

---

## Commit hygiene

Do not add AI/vendor attribution trailers, signatures, promotional language, or model branding to commits.

Examples to avoid:

- `Co-Authored-By: Claude ...`
- `Generated by ...`
- model/version names in commit messages unless technically relevant
- vendor-specific boilerplate

If provenance matters, record it in project-specific receipts or metadata, not in git history.

No free advertising.
