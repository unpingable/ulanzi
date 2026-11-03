# ulanzi

A small, headless Linux driver for the **Ulanzi Stream Controller D200**. Keys run
shell commands, show local system metrics, show agent activity, or indicate that
an agent needs you. One daemon supports multiple controllers. The single-device
path has been checked on physical hardware; two-device cable/recovery acceptance
remains incomplete. No Ulanzi Studio, display server, cloud account or AI API key
is required.

The default layout pairs conventional graphs on the top row with
[weather-style instruments](docs/weather-tiles.md) directly underneath:

| Keys | 1 / 6 | 2 / 7 | 3 / 8 | 4 / 9 | 5 / 10 |
| --- | --- | --- | --- | --- | --- |
| Measurement | Load | CPU | Memory | Network RX/TX | Root disk fullness |

Keys 11–13 are optional [Claude, Codex and Kimi attention buttons](docs/agents.md).
Antigravity can replace any one of them. ACTIVE reports observed agent activity;
QUIET means no known active or pending events. Neither is a health claim. Set
`show_active = false` on an agent key to hide ACTIVE. Pressing an attention button
acknowledges a finished turn, not active work; it never approves an action or
answers a question.

This is a Linux D200 project, not a general Stream Deck driver or desktop
application. Named configurations are supported; there is no GUI or page editor.
See [hardware observations and limits](docs/qualification/README.md) and
[public readiness](docs/public-readiness.md). Firmware workarounds are summarized
under Metrics and refresh below. This is an independent project, unaffiliated
with Ulanzi.

## Requirements

- Linux with hidraw and Python **3.11 or newer**.
- USB access to a D200 (vendor/product `2207:0019`). No root daemon is needed.
- DejaVu fonts recommended; a Pillow fallback is available.
- For the commands below: Git, Python venv support, and an internet connection
  to install Python dependencies. Debian/Ubuntu packages are `git`,
  `python3-venv`, `fonts-dejavu-core`. Other distributions use their equivalents.

## Install

From a checkout of this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
ulanzid --init-config
ulanzid --check
```

`--init-config` creates `$XDG_CONFIG_HOME/ulanzi/buttons.toml` (normally
`~/.config/ulanzi/buttons.toml`) from the bundled example and refuses to overwrite
an existing file. Edit your copy. Installation also works from a locally built
wheel; a source checkout is not required at runtime.

Configuration precedence: `--config PATH` (or the legacy positional path),
`ULANZI_CONFIG`, an existing user config, then `buttons.toml` in a source checkout.
A packaged install with no user config reports an error: run `--init-config`.
`--check` validates all profiles/device mappings and exits without accessing USB
or opening the control socket. Use `ulanzid --help` for CLI options.

### USB permissions

For headless use, the supplied rule grants `plugdev` access to the D200's hidraw
nodes (both USB interfaces). Membership may also grant access to other devices
covered by your distribution's rules:

```sh
sudo groupadd -f plugdev
sudo usermod -aG plugdev "$USER"
sudo cp udev/70-ulanzi-d200.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger
```

Log out and back in, then replug the D200. If a lingering user service manager
still has the old groups, reboot. Check group membership with `id` and access
with `ls -l /dev/hidraw*`. Only the D200 nodes should match this rule. This group
is created explicitly because it is not present by default on every distro.
For a wheel install, the rule and service template are under
`$VIRTUAL_ENV/share/ulanzi/{udev,systemd}/`.

### First run

Run **either** foreground mode **or** the service:

```sh
ulanzid
```

In a second terminal with the same venv activated:

```sh
ulanzictl devices
ulanzictl status
```

Look for `state: connected`. A second daemon using the same socket refuses to
start. Do not run separate daemons against the same physical controller, even
with different sockets. Ctrl-C stops foreground mode and its key commands.

### User service

Stop foreground mode first. Run from the checkout with the venv activated:

```sh
mkdir -p ~/.config/systemd/user
sed "s|@REPO@|$PWD|g" systemd/ulanzid.service.in > ~/.config/systemd/user/ulanzid.service
systemctl --user daemon-reload
systemctl --user enable --now ulanzid
journalctl --user -u ulanzid -f
```

The template expects a `.venv` in the checkout. For a wheel-only installation,
copy `$VIRTUAL_ENV/share/ulanzi/systemd/ulanzid.service.in` and replace the entire
`ExecStart` value with the quoted absolute path to your venv's `bin/ulanzid`.
The example uses the default user config. If you pass `--config`, use an absolute
path. Optional headless operation: `loginctl enable-linger "$USER"`.
The `sed` recipe assumes the checkout path has no `|`, `&` or `%`. Otherwise edit
`ExecStart` directly and escape systemd specifiers as required.

The service does not assume an SSH agent socket. If key commands need one, set
`SSH_AUTH_SOCK` to your actual agent socket in a user-service override. The daemon
adds its own installed helpers to the environment of key commands.

## Configure and control

Keys are numbered **1–13**, left to right, top to bottom. The small clock touch
area is `window`; it accepts `run` but has no tile. A key has a `label` or a
`widget`, and optionally a shell `run` command. Commands run as your user, in your
home directory; they should stay in the foreground. A key ignores repeat presses
while its command is running. Stopping the daemon stops those command groups.

```toml
[keys.1]
widget = "load"
run = "uptime"

[keys.6]
widget = "weather_load"

[keys.window]
run = "date"
```

After editing, use `ulanzictl reload`: it reports errors and keeps the last valid
configuration if validation fails. Runtime label overrides survive reload and
replug, but not a daemon restart. Reload also resets temporary profile selections.

```sh
ulanzictl label 10 'build: finished'
ulanzictl reset 10
ulanzictl press 1
ulanzictl reload
ulanzictl devices
```

`ulanzictl` uses only Python's standard library. Exit codes: 0 success, 1 rejected
command, 2 daemon unreachable or no command supplied. The daemon listens only on a user-owned Unix
socket. `ULANZI_SOCKET` selects the socket for both daemon and client; `--socket`
overrides it for the daemon. Default: `$XDG_RUNTIME_DIR/ulanzi.sock`, falling back
to `/run/user/UID/ulanzi.sock`. Remote control uses your existing SSH access:

```sh
ssh deckhost /absolute/path/to/venv/bin/ulanzictl label 10 'build: finished'
```

### Metrics and refresh

`load`, `cpu`, `mem`, `net`, `disk` are conventional widgets; prefix them with
`weather_` for the matching visual instrument. Unknown readings show N/A.
CPU and network rates require two samples. History contains **30 samples**:
about one minute at the default two-second refresh, longer at slower refresh.

Load is the Linux 1-minute load average, normalized by CPU count in its graph;
it includes runnable and uninterruptible tasks, not CPU busy percentage. Memory
is `1 - MemAvailable / MemTotal`, not a memory-pressure measurement. Network
combines all non-loopback interfaces (virtual interfaces may double-count
traffic). Disk is the `df`-style fullness of `/`, not disk throughput. Conventional
warning colors activate at load/CPU ≥1, CPU ≥90%, memory ≥90%, disk ≥90%.

```toml
[deck]
brightness = 60  # 0..100
heartbeat = 2.0  # 0.5..3 seconds, firmware keep-alive
refresh = 2.0    # 1..60 seconds, metric sampling and tile updates
clock = 1.0      # 0..60 seconds; 0 leaves the clock to firmware
```

[Weather scales and meanings](docs/weather-tiles.md) are fixed and labeled.
No high-frequency animation is promised. The daemon uses two alternating image
filenames per key because unbounded new filenames can exhaust firmware resources.
LOCKSCREEN is never sent: it rebooted a tested unit.

## Multiple controllers

Use `ulanzictl devices` to obtain identities. Every device has its own state,
transport, heartbeat, commands and reconnect generation. Unqualified commands
work for a single known device; with multiple devices or configured roles, select
one explicitly. `ulanzictl status` returns an inventory for zero/multiple physical
devices; targeted `status` gives key details. `devices` always returns an inventory.

Two-device testing observed separate layouts and input, but a cable/recovery check
failed when both endpoints disconnected. Independent recovery of the surviving
peer is not physically qualified. See the [hardware summary](docs/qualification/README.md).

```toml
[profiles.ops.keys.1]
label = "OPS"
[devices.desk]
match.serial = "YOUR_D200_SERIAL"
profile = "ops"
```

A profile inherits base deck settings and omitted keys. A specified key replaces
that key's entire definition. Use `label = ''` to blank one. `match.topology`
identifies a USB port; `match.path` is an unstable last resort. Details and collision
handling: [multi-device design](docs/design/multi-device.md).

```sh
ulanzictl target desk status
ulanzictl target desk profile ops
ulanzictl target all reset all
```

Key commands inherit `ULANZI_DEVICE`, `ULANZI_IDENTITY`, `ULANZI_KEY` and
`ULANZI_SOCKET`. `ulanzictl` automatically targets their originating device.
Agent hooks also support explicit targets; see [agent setup](docs/agents.md).

## Troubleshooting and removal

- **Access denied:** check `plugdev`, login/manager group refresh, rule installation
  and replug. Repeated identical open failures are logged once, rather than flooded.
- **Cannot reach daemon:** check the service, `ULANZI_SOCKET` and `XDG_RUNTIME_DIR`.
  A different login/su environment may select a different socket.
- **Target required/ambiguous:** use `devices`, choose the exact identity or configured
  role, and check overlapping serial/topology rules.
- **Standalone splash:** check the service and journal for disconnect/write failures.
  Replug if firmware has stopped accepting images. Run only one driver per unit.
- **Agent button stuck/missing:** `<agent>-deck show` reports stored state and the last
  delivery error; `<agent>-deck sync` reconciles it without another agent event.
  Restart recovery occurs on the next hook or explicit sync, not by polling agents.

To remove a user-service install, disable and stop it, remove only its
`~/.config/systemd/user/ulanzid.service`, then run `systemctl --user daemon-reload`.
Remove only the deck hook entries you added from each agent's config. Remove
`/etc/udev/rules.d/70-ulanzi-d200.rules` if you installed this rule, then reload
udev. Optionally remove your `plugdev` membership if you added it solely for this
project. `python -m pip uninstall ulanzi` removes the Python install. Keep or remove
your own `~/.config/ulanzi` separately. Do not disable lingering if other services
need it. An exact previous config is sufficient to roll back the layout; reload
it after restoration. Back up config before upgrading, reinstall the previous
package to roll back code, and restart the service after code changes.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
# Optional independent framing reference:
.venv/bin/python -m pip install -r requirements-reference.txt
```

Tests use temporary sockets and fake USB transports. Live hardware qualification
is separate and requires exclusive access and desk-side checks; see `qualify/`.
An API review or unit test does not establish a physical display result.
[Fixture previews](experiments/README.md) are explicitly synthetic.

The wire protocol and firmware workarounds derive from
[redphx/strmdck](https://github.com/redphx/strmdck) and
[marcelobrake/ulanzi-linux](https://github.com/marcelobrake/ulanzi-linux) (both MIT).
See [NOTICE](NOTICE), [third-party licenses](THIRD_PARTY_LICENSES.txt) and
[PROVENANCE.md](PROVENANCE.md). Licensed under [Apache-2.0](LICENSE).
For contributions and local checks, see [CONTRIBUTING.md](CONTRIBUTING.md).
