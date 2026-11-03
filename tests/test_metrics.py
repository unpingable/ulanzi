import os

import pytest

from ulanzi import metrics

STAT = """cpu  100 0 50 800 50 0 0 0 0 0
cpu0 50 0 25 400 25 0 0 0 0 0
intr 1 2 3
"""
MEMINFO = """MemTotal:       1000000 kB
MemFree:         200000 kB
MemAvailable:    750000 kB
"""
NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999999     10    0    0    0     0          0         0   999999     10    0    0    0     0       0          0
  eth0:   1000     10    0    0    0     0          0         0     2000     20    0    0    0     0       0          0
 wlan0:    500      5    0    0    0     0          0         0      100      1    0    0    0     0       0          0
"""


def test_parsers():
    assert metrics.parse_loadavg('2.31 1.50 0.75 3/900 1234\n') == (2.31, 1.5, 0.75)
    assert metrics.parse_cpu_times(STAT) == (150, 1000)
    assert metrics.parse_meminfo(MEMINFO) == pytest.approx(25.0)
    assert metrics.parse_net_dev(NET_DEV) == (1500, 2100)


@pytest.mark.parametrize('parse, text', [
    (metrics.parse_loadavg, ''),
    (metrics.parse_loadavg, 'x y z'),
    (metrics.parse_cpu_times, 'intr 1 2\n'),
    (metrics.parse_cpu_times, 'cpu 1 2\n'),
    (metrics.parse_meminfo, 'MemFree: 3 kB\n'),
    (metrics.parse_net_dev, 'h\nh\n eth0: x y\n'),
])
def test_parsers_reject_malformed(parse, text):
    with pytest.raises((ValueError, KeyError, IndexError)):
        parse(text)


def test_delta_rate():
    assert metrics.delta_rate(100, 300, 2.0) == 100.0
    assert metrics.delta_rate(None, 300, 2.0) is None
    assert metrics.delta_rate(300, 100, 2.0) is None  # counter reset / wrap
    assert metrics.delta_rate(100, 300, 0) is None


class FakeProc:
    def __init__(self, root):
        self.root = root
        (root / 'net').mkdir()

    def set(self, stat=STAT, meminfo=MEMINFO, net=NET_DEV, loadavg='1.0 2.0 3.0 1/1 1\n'):
        (self.root / 'stat').write_text(stat)
        (self.root / 'meminfo').write_text(meminfo)
        (self.root / 'net/dev').write_text(net)
        (self.root / 'loadavg').write_text(loadavg)


def test_sampler_deltas_and_unknowns(tmp_path):
    proc = FakeProc(tmp_path)
    proc.set()
    s = metrics.Sampler(proc=tmp_path, root=str(tmp_path), history_len=3)
    first = s.sample(now=10.0)
    assert first.cpu is None and first.rx is None  # no previous sample yet
    assert first.load == (1.0, 2.0, 3.0) and first.mem == pytest.approx(25.0)
    assert first.disk is not None

    proc.set(stat=STAT.replace('cpu  100 0 50 800', 'cpu  150 0 100 900'),
             net=NET_DEV.replace('1000     10', '3000     10'))
    second = s.sample(now=12.0)
    assert second.cpu == pytest.approx(100 * 100 / 200)
    assert second.rx == pytest.approx(1000.0) and second.tx == 0

    # counters going backwards (interface reset, reboot of a counter) read as unknown
    proc.set(net=NET_DEV.replace('1000     10', '10     10'))
    third = s.sample(now=14.0)
    assert third.rx is None and third.cpu is None

    # a malformed file is unknown, not a crash, and not zero
    proc.set(meminfo='garbage\n', loadavg='')
    fourth = s.sample(now=16.0)
    assert fourth.mem is None and fourth.load is None

    assert list(s.history['mem']) == [pytest.approx(25.0), pytest.approx(25.0), None]
    assert len(s.history['cpu']) == 3  # bounded


def test_sampler_on_this_host():
    s = metrics.Sampler()
    s.sample()
    cur = s.sample()
    assert cur.load is not None and 0 <= cur.mem <= 100 and 0 <= cur.disk <= 100
    assert metrics.cpu_count() >= 1 and metrics.cpu_count() <= os.cpu_count()
