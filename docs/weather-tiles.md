# Paired metric rows

The default layout puts load, CPU, memory, network and disk on keys 1–5, with
`weather_load`, `weather_cpu`, `weather_mem`, `weather_net` and `weather_disk`
directly underneath on keys 6–10. Key 1 runs `uptime`. Keys 11–13 show optional agent attention widgets; the clock retains its configured behavior.

Both rows consume the same sampler snapshot and bounded history during each
refresh. There is no independent animation, sensor or Synesthesia process.
The existing two-second refresh, changed-key uploads, two alternating icon names
per key, reconnect restoration and heartbeat continue to apply. Overrides still
replace a weather tile until reset.

| Column | Shape | Scale and meaning |
| --- | --- | --- |
| Load | Woven lanes | Latest 1-minute load / CPU count; full field = 1 per CPU |
| CPU | Stable stipple density | Busy percentage; full field = 100% |
| Memory | Reservoir | In-use percentage from MemAvailable; sample-and-hold |
| Network | Leftward RX / rightward TX streaks | Fixed 10 MB/s per direction; not link utilization |
| Disk | Occupancy rings | Root-filesystem fullness, not disk I/O |

Color identifies category; shape adds a cue independent of color. Higher values
fill more of the shape. Stable identities place CPU cells reproducibly. Up to
30 load, CPU and network samples leave dim traces, with older samples dimmer and
missing samples absent. Age means sample position, not elapsed wall time.
Memory and disk hold the current gauge value; they do not decay. Unknown current
data shows N/A even with older history. Network marks appear only after the
second valid sample establishes rates. A small nonzero rate gets one streak;
use the number for quantitative comparison. A clipped scale shows `CLIP`, while
the printed measurement retains its literal value. The conventional network
graph above auto-scales; the weather tile uses the stated fixed scale.

The vocabulary is inspired by Synesthesia's Weather and Meter views, without a
runtime dependency, shared protocol or event bus. This feature does not infer
agent activity or successful completion. Agent attention remains on its own keys; see [agent setup](agents.md).

## Install and verify

Follow the [installation guide](../README.md) and run `ulanzid --check` on the
host with the USB controller. The default config includes both rows. Inspect
`ulanzictl devices` and target the actual role/identity shown there for status.
After a code upgrade restart the user service; config-only edits use
`ulanzictl reload`. Both commands run on the deck host. Restarting discards
runtime overrides, so inspect the current state first.

A connected status and successful upload do not establish desk-side readability;
check the physical deck. First CPU/network samples can be N/A. The normal refresh
is two seconds, not a high-frequency animation loop. Weather captions retain the
literal value, with small scale annotations subordinate to the geometry.

To disable the visual row while retaining normal metrics, remove keys 6–10 from
your config and reload. Restore a saved config and reload for an exact layout
rollback. There are no extra system services or network listeners.
