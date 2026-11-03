# Agent attention buttons

Defaults: key 11 Claude, 12 Codex, 13 Kimi. Antigravity (`agy`) may replace one
key by selecting `agent_agy` and `agy-deck ack`. You can choose other keys, but
set `ULANZI_<AGENT>_KEY` consistently in the hooks and acknowledge command.
The helpers share `ulanzi/agent_deck.py`; they do not read transcripts, prompt
content, tool arguments, credentials or process tables. They require no API key.

| State | Meaning | Press |
| --- | --- | --- |
| QUIET | No known active or pending event; not a liveness claim | Reconcile known state |
| ACTIVE | Recent prompt, turn or tool activity; not a health claim | Leave activity untouched |
| WAIT | An explicit permission/input event was observed | Keeps WAIT; check the agent |
| DONE | A turn-end event was observed, with no success claim | Acknowledge |
| ERROR | An explicit turn-failure event was observed | Acknowledge |
| STOPPED | An explicit interrupt event was observed | Acknowledge |
| STALE | No matching attention observation for 30 minutes | Check the source; no approval |

WAIT outranks ERROR, STOPPED, DONE and ACTIVE. The tile includes the matching session
count and project names. A bright amber border emphasizes WAIT; identity colors are
stable per agent. A finished turn can still require review and can have failed.
Pressing acknowledges only DONE, ERROR and STOPPED, never ACTIVE. It never approves,
denies, answers, resumes or focuses an agent.
The daemon marks old attention stale; stored sessions expire after 12 hours when
a helper next runs. Hook delivery is best-effort: the CLI must remain usable if
USB, daemon or state storage is unavailable. Diagnostics are explicit:

```sh
claude-deck show
codex-deck sync
kimi-deck show
```

`show` prints stored states and the last delivery error. `sync` sends them again.
Each hook reconciles with the daemon, which suppresses unchanged image uploads.
After daemon restart or a manual reset, the next hook/sync restores known pending
state. Without a new hook or sync, overrides lost on restart are not restored.
There is no continuous agent poller.

## Configuration

Agent keys show ACTIVE by default. Set `show_active = false` on an agent key to
hide only ordinary activity; WAIT, DONE, ERROR and STOPPED still appear. The
option is valid only with an agent widget:

```toml
[keys.13]
widget = "agent_kimi"
run = "kimi-deck ack"
show_active = false
```

The bundled `examples/` files are **templates**: replace
`/absolute/path/to/ulanzi/.venv/bin/` with the absolute directory containing your
installed helpers. Quote that executable path in the command string if it contains
spaces. Test with `<helper> show` before registering hooks. Hook commands run on
the same host/user as the deck daemon, or must be explicitly transported over your
existing SSH setup; installing this package on another computer is not a remote
connection to the deck.

### Claude Code

Merge the `hooks` entries from [claude-hooks.json](../examples/claude-hooks.json)
into your `~/.claude/settings.json`. Preserve unrelated settings and hooks.
The adapter observes `PermissionRequest`, permission/elicitation Notifications,
`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `StopFailure` and
`SessionEnd`. Generic Notifications and idle reminders do not create WAIT.
A prompt or post-tool event clears a previous WAIT. If an interrupt emits no
completion event, WAIT can remain until another event; stale labeling makes that
uncertainty visible.

Contract: [Claude Code hooks](https://code.claude.com/docs/en/hooks).

### Codex

Merge [codex-hooks.json](../examples/codex-hooks.json) into `~/.codex/hooks.json`
or a trusted project's `.codex/hooks.json`. Review and trust new/changed hooks in
Codex `/hooks`; untrusted hooks do not run. Do not add duplicate definitions in
both `hooks.json` and inline `config.toml`.

`PermissionRequest` means WAIT. `PostToolUse`/`UserPromptSubmit` clear it, `Stop`
means DONE, `Interrupt` means STOPPED, and `SessionEnd` removes the session.
These are documented hook names, not inferred transcript events.

Older versions with only `notify` can use
`notify = ["/absolute/path/to/venv/bin/codex-deck", "notify"]` in `config.toml`.
That mode observes `agent-turn-complete` only: no WAIT or prompt/tool clearing.
Use lifecycle hooks or notify, not both.

Contract: [Codex hooks](https://learn.chatgpt.com/docs/hooks).

### Kimi Code

Append [kimi-hooks.toml](../examples/kimi-hooks.toml) to
`~/.kimi-code/config.toml`, preserving other configuration. Kimi Code 2.3.2 is the
locally inspected client; older Kimi CLI releases may use a different contract.
`PermissionRequest` means WAIT; `PermissionResult`, prompt, turn-start and
post-tool events clear it. `Stop`, `StopFailure`, `Interrupt`, and `SessionEnd`
have the meanings above. Background-task Notifications are not treated as a
main-turn completion. No output from this observer blocks or approves anything.

Contract: [Kimi Code hooks](https://moonshotai.github.io/kimi-code/en/customization/hooks).

### Antigravity

Merge [agy-hooks.json](../examples/agy-hooks.json) into the project's
`.agents/hooks.json` or `~/.gemini/config/hooks.json`, preserving existing entries.
Its named top-level hook group and flat non-matcher handlers differ from the
Claude/Codex schema; matcher events use nested `hooks` arrays as documented.
The adapter uses explicit event arguments because the documented common input
uses `conversationId`/`workspacePaths` and does not include `hook_event_name`.
`PreInvocation` and `PostToolUse` clear prior attention; `Stop` sets DONE.
`PreToolUse` for `ask_question` sets WAIT. Generic `PreToolUse` does not prove a
permission wait. There is no documented dedicated permission-request hook in
this contract, so this adapter does not claim permission WAIT or error status.
It emits no approval decision, override, injected context or continuation output.

Antigravity support is contract/fixture tested; the installed `agy` binary was
not found on this host's PATH. No live Antigravity session has been qualified.
Contract: [Antigravity hooks](https://www.antigravity.google/docs/hooks/).

## Device targeting

For multiple controllers, set `ULANZI_CLAUDE_DEVICE`, `ULANZI_CODEX_DEVICE`,
`ULANZI_KIMI_DEVICE` or `ULANZI_AGY_DEVICE` to one role/identity from
`ulanzictl devices` in **both** the hook and key command:

```toml
[keys.11]
widget = "agent_claude"
run = "ULANZI_CLAUDE_DEVICE=desk claude-deck ack"
```

Prefix the matching hook command with `ULANZI_CLAUDE_DEVICE=desk` too. `all` and
whitespace-containing targets are rejected. For one known deck, omit the target;
the helper resolves it from inventory, as it also does for the physical key's
inherited identity. Explicitly targeted session stores use the exact configured device string,
socket and key; use the identical value in hooks and acknowledgment. Resolution
is used for routing only, so offline operation and role renaming do not move the
store. Untargeted single-deck hooks retain a stable socket/key scope even offline.
Session filenames are hashed.

State is under `$XDG_RUNTIME_DIR/ulanzi/{claude,codex,kimi,agy}` (fallback
`/run/user/UID`). It contains only session state, bounded project/cwd identity and
timestamps; at most 256 recent records per destination. Do not configure two
agents for the same key unless replacing one intentionally. During upgrades,
legacy attention-state directories are left untouched; the new scoped store
starts empty until fresh hooks arrive.
