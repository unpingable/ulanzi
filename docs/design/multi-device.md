# D200 device ownership and identity

Implementation basis: `ulanzi/daemon.py` (`D200Device`, `DeviceRegistry`, `Daemon`,
`load_fleet`), `ulanzi/deckproto.py` (`enumerate_devices`, `Writer`), and
`tests/test_multi_device.py`. Hardware evidence belongs in qualification receipts,
not in this design note.

The previous daemon owned one transport, writer, desired state, shown-image map,
icon-slot map, push event, sync lock, lost event, key-child map, sampler and upload
statistics. `open_device()` selected the first vendor interface. Reconnect and
input routing operated on that singleton. Configuration and socket commands had
no device selector. Rendering itself (`deckzip`, `tiles`) is reusable; cached
immutable image bytes can safely be shared. Host reconciliation has a registry
mutation lock; it never serializes device uploads or input handling.

## Identity

The design supports multiple VID 2207/PID 0019 vendor interfaces.
The serials `EXAMPLE_SERIAL_A` and `EXAMPLE_SERIAL_B` below are illustrative;
obtain actual identities from `ulanzictl devices`.
Interface 1 is the keyboard interface and is excluded. Linux topology is available
through `/sys/class/hidraw/*/device`; hidapi also exposes manufacturer/product,
release, usage and bus type. None of those common model attributes distinguishes
units. A live report from the first unit also exposed SerialNumber matching its USB
serial, Dversion 5.0.5 and HardwareVersion SSD210V100. Device-info reports are
logged, not interpreted as another stable ID.

A serial unique in the current inventory and never observed as a duplicate yields `serial:<value>`. A missing or
duplicate serial falls back to `port:<absolute USB sysfs topology>`, then
`path:<hidraw path>` if topology is unavailable. Enumeration order never matches
a device. Observed duplicate serials are remembered for the daemon lifetime, so removing
one duplicate never promotes its peer back to an earlier unit's serial record.
The earlier serial record is retired; fallback records start with fresh runtime
state. Duplicate serials cannot safely satisfy a serial-only role: add a
port constraint. Ambiguous identities or overlapping role matches stop that
reconciliation and expose a discovery error rather than assign inputs incorrectly.

Serials are device descriptors and the best candidate for reconnect/reboot
identity; actual persistence must be measured. Port topology identifies a socket,
not a unit, survives ordinary replug to that socket, and changes when moved or
when the host topology changes. `/dev/hidrawN` is attachment-local and reusable;
a path rule explicitly opts into that limitation. With no serial, a substituted
unit at the same port cannot be distinguished. A remove/reinsert wholly between
polls is likewise undetectable unless the old handle fails or descriptors change.
No persistent identity is fabricated.

## Ownership and lifecycle

The host owns a Unix socket, configuration, and bounded polling reconciliation
(every two seconds). Each physical record owns desired state, selected profile,
child commands, sampler/history, transport, per-device writer and sync locks,
input reader, push/clock/heartbeat/tile tasks, shown images, alternating icon
slots, error and generation. A session owns a new opened handle and tasks. Before accepting input or writing a layout, the opened handle's serial is
checked against enumeration when the transport exposes it. A mismatch rejects and
closes the handle, preventing a reused hidraw path from binding the wrong unit.
Every successful verified open increments generation; removal cancels/drains that session,
closes its handle, and clears shown state. The physical record retains icon-slot
alternation, since a handle reopen need not clear the firmware image cache. Reconnect sends the entire layout.
No peer's lock protects another peer's writes. Input readers yield after every
report, including ignored reports, so a continuously readable unit cannot starve
its peers. Image rendering/zip construction
runs off the event loop. Cancelled writes retain the writer lock until their
worker really finishes, avoiding close/write and old/new-session overlap.

Absent physical records retain desired overrides. Configured roles can receive
overrides before their first attachment. Unmatched units independently use the
base profile and are named `@<identity>`. Historical records remain visible for
the daemon lifetime; history is not persisted across daemon restarts. Open/read/
write/render failures stay local and are retried by reconciliation. Enumeration
failure preserves current sessions because it does not prove unplug.

## Configuration and control

Existing `[deck]` and `[keys]` remain the base `default` profile. Each named
`[profiles.NAME.deck]` inherits base deck settings. `[profiles.NAME.keys.N]`
replaces that key's entire base specification; omitted keys inherit. Blank a
base key with `label = ''` in that profile. Profiles do not need copied full
configuration files. The selected profile is the device's layout/page.

```toml
[profiles.ops.keys.1]
label = 'OPS'
[profiles.studio.keys.1]
label = 'STUDIO'
[devices.matrix]
match.serial = 'EXAMPLE_SERIAL_A'
profile = 'ops'
[devices.synth]
match.serial = 'EXAMPLE_SERIAL_B'
profile = 'studio'
```

Matchers support serial, topology and path, with AND semantics within a role.
Names, matchers and profiles are separate. One unit keeps unqualified control
commands; multiple records/roles require `target NAME COMMAND` or explicit
`target all COMMAND`. Offline legacy overrides are adopted by the sole unit.
An inventory with absent peers remains ambiguous: explicitly target the connected
unit. This prevents a disconnected peer from changing the meaning of a command.

`devices`/`inventory` return one-line JSON including absent configured roles,
physical identity, descriptor, connection/error state, profile, generation and
running keys. `target matrix status` retains label detail. `target matrix profile
studio` switches that device's layout until reload. `target all label 1 text`
and `target all reset all` explicitly broadcast. Key children receive
`ULANZI_DEVICE` and `ULANZI_IDENTITY`; `ulanzictl` inherits the device selector
so existing key commands continue to affect their originating physical unit even
if its logical role changes. Agent helpers share `ulanzi/agent_deck.py` and support
an explicit role/identity through `ULANZI_<AGENT>_DEVICE`, or the key child identity.
Destination state is scoped by socket, target and key. See [agent setup](../agents.md)
and `tests/test_agent_attention.py` for current behavior. User hook settings and
trust are not installed automatically.

`reload` or SIGHUP parses and validates before replacement, then reconciles
profiles/matchers onto live sessions, retaining overrides. Reload resets temporary
profile choices to configuration. Changes to a role/profile keep the physical
session; descriptor/transport changes start a new generation. Logs contain
`device=`, `identity=`, `generation=` and lifecycle event fields. No TCP listener,
service framework, detached key children, or LOCKSCREEN commands are introduced.

## Deliberately deferred

Groups/mirrors, coordinated pages, animations, overlays, network APIs, persistent
runtime overrides and separate USB notification subscriptions. Polling and named
profiles cover the current operator need. A blocked HID write can delay draining
that device; healthy device workers continue independently. The firmware has no
layout acknowledgement, so successful writes do not establish visible rendering.
