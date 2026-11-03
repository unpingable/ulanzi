# Hardware observations and limits

These are summaries of earlier operator observations, not a new qualification
run or a guarantee for every D200 or firmware version. Raw development receipts
are excluded from the public source because they contain device identities and
machine details. They remain in the maintainer's private development archive.
The summaries omit those identifiers and do not replace the original evidence.

## Single device, September 2026

One D200 (USB `2207:0019`, firmware 5.0.5, hardware SSD210V100) was checked with
an operator watching the display and pressing keys.

- Silence caused standalone fallback after approximately 11–13 seconds. A
  two-second brightness heartbeat prevented fallback during the observed checks.
- LOCKSCREEN (`0x000f`) rebooted the device. The daemon never sends it.
- Repeated uploads with unbounded new icon filenames eventually failed. Two
  alternating filenames per key displayed changing images correctly.
- A 90-minute daemon soak using bounded names recorded 2,682 tile uploads with
  no observed blank/stale tiles, write failures, device loss or USB resets.
- Service restart and physical unplug/replug restored the layout in that run.

This does not establish firmware storage limits or behavior on other versions.
Whether firmware advances the small-window clock without host writes remains
undetermined.

## Two devices, October 2026

Separate discovery, different layouts, concurrent host writes and input from
both devices were observed. Automated transport checks also exercised controlled
session restarts and an upload with only one active handle.

**Physical acceptance remained incomplete.** One device fell back to the vendor
screen and required replugging. A later cable/recovery check failed peer survival
when both endpoints disconnected. These observations are not a clean pass for
independent recovery. Missing/duplicate serials, matcher collisions and handle
substitution are covered by simulations rather than these physical observations.

## Paired weather tiles and agent hooks, October 2026

The paired metric/weather layout was observed on one physical D200. Host-side
checks established continuing updates; they did not prove every frame displayed.
Agent helpers delivered synthetic events to a live daemon. Native agent event
sequences and physical acknowledgment were not qualified for every integration.

Use [agent setup](../agents.md) for current helper behavior. Earlier integration
limitations must not be read as claims about the current source: all helpers now
support explicit device targets.

## Repeating a check

The `qualify/` scripts are developer tools that send commands to real hardware.
Read each tool first, arrange exclusive access, and stop the installed daemon
before use. Restore the service afterward. A physical operator must observe
display, input and cable behavior; successful writes alone are insufficient.
Never change an incomplete or negative historical result into a pass.
