"""Cheap local Linux metrics read straight from /proc and statvfs.

Each metric is a float or None (unknown). Rates and CPU % come from deltas between
samples, so the first sample after start, or after a counter goes backwards, is
unknown rather than a guess. History is in memory only and bounded.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PROC = Path('/proc')


# --- parsers: text in, numbers out; ValueError on anything malformed ---------

def parse_loadavg(text: str) -> tuple[float, float, float]:
    parts = text.split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def parse_cpu_times(text: str) -> tuple[int, int]:
    """(busy, total) jiffies from the aggregate 'cpu' line of /proc/stat."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == 'cpu':
            values = [int(v) for v in fields[1:]]
            if len(values) < 4:
                raise ValueError('short cpu line')
            # user nice system idle iowait irq softirq steal guest guest_nice;
            # guest time is already counted in user/nice
            values = values[:8]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            return total - idle, total
    raise ValueError('no aggregate cpu line')


def parse_meminfo(text: str) -> float:
    """Memory in use as a percent: 1 - MemAvailable / MemTotal."""
    fields = {}
    for line in text.splitlines():
        name, _, rest = line.partition(':')
        if rest:
            fields[name] = int(rest.split()[0])
    total, available = fields['MemTotal'], fields['MemAvailable']
    if total <= 0:
        raise ValueError('MemTotal is zero')
    return 100.0 * (1 - available / total)


def parse_net_dev(text: str) -> tuple[int, int]:
    """(rx_bytes, tx_bytes) summed over every interface except loopback."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(':')
        if not rest or name.strip() == 'lo':
            continue
        fields = rest.split()
        rx += int(fields[0])
        tx += int(fields[8])
    return rx, tx


def cpu_count() -> int:
    return len(os.sched_getaffinity(0)) or os.cpu_count() or 1


# --- sampling ----------------------------------------------------------------

@dataclass
class Sample:
    t: float
    load: tuple[float, float, float] | None = None
    cpu: float | None = None      # percent busy since previous sample
    mem: float | None = None      # percent in use
    disk: float | None = None     # percent of / in use
    rx: float | None = None       # bytes/s
    tx: float | None = None       # bytes/s


def delta_rate(prev: int | None, cur: int | None, seconds: float) -> float | None:
    """Per-second rate, or None if there is no previous value or the counter went back."""
    if prev is None or cur is None or seconds <= 0 or cur < prev:
        return None
    return (cur - prev) / seconds


@dataclass
class Sampler:
    proc: Path = PROC
    root: str = '/'
    history_len: int = 30
    history: dict[str, deque] = field(default_factory=dict)
    _cpu: tuple[int, int] | None = None
    _net: tuple[int, int] | None = None
    _t: float | None = None

    def __post_init__(self):
        for name in ('load1', 'cpu', 'mem', 'disk', 'rx', 'tx'):
            self.history[name] = deque(maxlen=self.history_len)

    def _read(self, name: str) -> str | None:
        try:
            return (self.proc / name).read_text()
        except OSError:
            return None

    def sample(self, now: float | None = None) -> Sample:
        now = time.monotonic() if now is None else now
        s = Sample(t=now)
        elapsed = None if self._t is None else now - self._t
        self._t = now

        try:
            s.load = parse_loadavg(self._read('loadavg') or '')
        except (ValueError, IndexError):
            pass

        try:
            busy, total = parse_cpu_times(self._read('stat') or '')
            if self._cpu and total > self._cpu[1] and busy >= self._cpu[0]:
                s.cpu = 100.0 * (busy - self._cpu[0]) / (total - self._cpu[1])
            self._cpu = (busy, total)
        except ValueError:
            self._cpu = None

        try:
            s.mem = parse_meminfo(self._read('meminfo') or '')
        except (ValueError, KeyError, IndexError):
            pass

        try:
            st = os.statvfs(self.root)
            used = st.f_blocks - st.f_bfree
            usable = used + st.f_bavail  # what df counts, excluding root-reserved blocks
            s.disk = 100.0 * used / usable if usable else None
        except OSError:
            pass

        try:
            rx, tx = parse_net_dev(self._read('net/dev') or '')
            if self._net and elapsed:
                s.rx = delta_rate(self._net[0], rx, elapsed)
                s.tx = delta_rate(self._net[1], tx, elapsed)
            self._net = (rx, tx)
        except (ValueError, IndexError):
            self._net = None

        for name, value in (('load1', s.load[0] if s.load else None), ('cpu', s.cpu),
                            ('mem', s.mem), ('disk', s.disk), ('rx', s.rx), ('tx', s.tx)):
            self.history[name].append(value)
        return s
