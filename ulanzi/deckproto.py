"""D200 wire protocol: frame builders, report parsing, and a serialized writer.

Every frame is 1024 bytes: 0x7c7c, a big-endian u16 command, a little-endian u32
length, then data. A zip longer than one frame continues in raw 1024-byte chunks.
Reverse-engineered by redphx/strmdck and marcelobrake/ulanzi-linux (both MIT).
"""
from __future__ import annotations

import asyncio
import time
from enum import IntEnum

FRAME = 1024
HEADER = 8
FIRST_FRAME_DATA = FRAME - HEADER
MAGIC = b'\x7c\x7c'

VENDOR_ID = 0x2207
PRODUCT_ID = 0x0019
VENDOR_INTERFACE = 0  # interface 1 is the plain keyboard


class Cmd(IntEnum):
    SET_BUTTONS = 0x0001
    SMALL_WINDOW = 0x0006
    BRIGHTNESS = 0x000A
    LABEL_STYLE = 0x000B
    PARTIAL_BUTTONS = 0x000D
    LOCKSCREEN = 0x000F     # rebooted a tested unit; never sent by the daemon
    UNLOCKSCREEN = 0x0010   # untested (the deck had already rebooted)
    IN_BUTTON = 0x0101
    IN_DEVICE_INFO = 0x0303


CLOCK_MODE = 1


def frame(cmd: int, data: bytes = b'', length: int | None = None) -> bytes:
    if len(data) > FIRST_FRAME_DATA:
        raise ValueError('data longer than one frame; use zip_frames')
    size = len(data) if length is None else length
    return MAGIC + cmd.to_bytes(2, 'big') + size.to_bytes(4, 'little') + data.ljust(FIRST_FRAME_DATA, b'\0')


def zip_frames(blob: bytes, partial: bool = False) -> list[bytes]:
    """A buttons zip as frames: header frame with the first 1016 bytes, then raw chunks."""
    cmd = Cmd.PARTIAL_BUTTONS if partial else Cmd.SET_BUTTONS
    packets = [frame(cmd, blob[:FIRST_FRAME_DATA], length=len(blob))]
    for i in range(FIRST_FRAME_DATA, len(blob), FRAME):
        packets.append(blob[i:i + FRAME].ljust(FRAME, b'\0'))
    return packets


def brightness_frame(value: int) -> bytes:
    return frame(Cmd.BRIGHTNESS, str(value).encode())


def clock_frame(now: float | None = None, cpu: float | None = None, mem: float | None = None) -> bytes:
    """Small window in clock mode: mode|cpu|mem|time|gpu. Touching the window shows the
    firmware's own stats view of cpu/mem/gpu; the protocol has no 'unknown', so an
    unknown value goes out as 0."""
    stamp = time.strftime('%H:%M:%S', time.localtime(now))
    cpu_s = round(cpu) if cpu is not None else 0
    mem_s = round(mem) if mem is not None else 0
    return frame(Cmd.SMALL_WINDOW, f'{CLOCK_MODE}|{cpu_s}|{mem_s}|{stamp}|0'.encode())


def parse_report(report) -> tuple[str, object] | None:
    """('button', (index, pressed)), ('info', text), or None.

    BUTTON payload is state, index, 0x01, pressed. The info window also sends a
    variant with 0x00 in the constant slot; it is not a key event.
    """
    data = bytes(report)
    if len(data) < 12 or data[:2] != MAGIC:
        return None
    cmd = int.from_bytes(data[2:4], 'big')
    if cmd == Cmd.IN_BUTTON:
        if data[10] != 0x01:
            return None
        return 'button', (data[9], data[11] == 0x01)
    if cmd == Cmd.IN_DEVICE_INFO:
        return 'info', data[8:].split(b'\0', 1)[0].decode('ascii', 'replace')
    return None


def select_vendor_path(devices: list[dict]) -> bytes | None:
    """Legacy single-device helper; ambiguity must never select an arbitrary unit."""
    matches = vendor_devices(devices)
    if len(matches) > 1:
        raise OSError('multiple D200s: specify a path')
    return matches[0]['path'] if matches else None


def vendor_devices(devices: list[dict]) -> list[dict]:
    return [d for d in devices if
            (d.get('vendor_id'), d.get('product_id'), d.get('interface_number')) ==
            (VENDOR_ID, PRODUCT_ID, VENDOR_INTERFACE)]


def enumerate_devices() -> list[dict]:
    """All vendor interfaces. Topology identifies a port, never a physical unit."""
    import hidraw
    from pathlib import Path

    result = []
    for raw in vendor_devices(hidraw.enumerate(VENDOR_ID, PRODUCT_ID)):
        d = dict(raw)
        path = d['path']
        d['path'] = path if isinstance(path, bytes) else path.encode()
        d['serial'] = d.get('serial_number') or ''
        node = Path('/sys/class/hidraw') / Path(d['path'].decode()).name / 'device'
        d['topology'] = ''
        if node.exists():
            for parent in node.resolve().parents:
                if (parent / 'idVendor').exists():
                    d['topology'] = str(parent)
                    break
        result.append(d)
    return result


def open_device(path=None):
    """An opened, non-blocking hidraw device for the vendor interface, or None."""
    import os

    import hidraw

    if path is None:
        paths = vendor_devices(hidraw.enumerate(VENDOR_ID, PRODUCT_ID))
        if len(paths) > 1:
            raise OSError('multiple D200s: specify a path')
        path = paths[0]['path'] if paths else None
    if path is None:
        return None
    dev = hidraw.device()
    try:
        dev.open_path(path)
    except OSError as e:
        # hidapi's message is just "open failed"; access is the usual cause
        access = 'ok' if os.access(path, os.R_OK | os.W_OK) else 'denied (see udev/)'
        raise OSError(f'{path.decode()}: {e}; access {access}') from None
    dev.set_nonblocking(True)
    return dev


class Writer:
    """All host writes go through here, one batch at a time, off the event loop.

    Tracks when the last write finished so liveness can be judged from any write,
    not only heartbeats. Write errors propagate to the caller.
    """

    def __init__(self, dev):
        self.dev = dev
        self.lock = asyncio.Lock()
        self.last_write = 0.0  # time.monotonic() of the last completed write

    async def send(self, packets: list[bytes] | bytes) -> float:
        """Write the packets in order; returns seconds spent writing."""
        if isinstance(packets, bytes):
            packets = [packets]
        async with self.lock:
            start = time.monotonic()
            work = asyncio.create_task(asyncio.to_thread(self._write_all, packets))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # Keep the per-device lock until the worker has really stopped.
                await asyncio.gather(work, return_exceptions=True)
                raise
            self.last_write = time.monotonic()
            return self.last_write - start

    def _write_all(self, packets):
        for p in packets:
            if self.dev.write(p) < 0:
                raise OSError('hid write failed')
