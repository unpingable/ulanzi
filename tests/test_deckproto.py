from ulanzi import deckproto
from ulanzi.deckproto import Cmd


def report(payload: bytes, command=b'\x01\x01') -> bytes:
    return b'\x7c\x7c' + command + b'\xf6\x03\x00\x00' + payload + bytes(1012)


def test_parse_report():
    assert deckproto.parse_report(report(b'\x01\x04\x01\x01')) == ('button', (4, True))
    assert deckproto.parse_report(report(b'\x01\x04\x01\x00')) == ('button', (4, False))
    # seen from the info window: constant slot is 0x00
    assert deckproto.parse_report(report(b'\x00\x0d\x00\x01')) is None
    assert deckproto.parse_report(report(b'D200 v1.2\x00', command=b'\x03\x03')) == ('info', 'D200 v1.2')
    assert deckproto.parse_report(b'\x00' * 20) is None


def test_frames_match_strmdck():
    import pytest
    pytest.importorskip('strmdck', reason='optional reference; install requirements-reference.txt')
    from strmdck.devices.ulanzi_d200 import PacketStruct

    blob = bytes(range(256)) * 9
    packets = deckproto.zip_frames(blob)
    assert packets[0] == PacketStruct.build(dict(
        command_protocol=Cmd.SET_BUTTONS, length=len(blob), data=blob[:1016].ljust(1016, b'\0')))
    assert all(len(p) == 1024 for p in packets)
    assert b''.join(p[8:] if i == 0 else p for i, p in enumerate(packets)).startswith(blob)
    assert deckproto.zip_frames(blob, partial=True)[0][2:4] == b'\x00\x0d'
    assert deckproto.brightness_frame(60) == PacketStruct.build(dict(
        command_protocol=Cmd.BRIGHTNESS, length=None, data=b'60'))


def test_clock_frame_payload():
    import time
    t = time.mktime((2026, 9, 25, 7, 8, 9, 0, 0, -1))
    f = deckproto.clock_frame(t)
    assert f[2:4] == b'\x00\x06'
    assert f[8:].rstrip(b'\0') == b'1|0|0|07:08:09|0'
    assert deckproto.clock_frame(t, cpu=37.6, mem=None)[8:].rstrip(b'\0') == b'1|38|0|07:08:09|0'


def test_select_vendor_path_skips_keyboard_interface():
    devices = [
        {'vendor_id': 0x2207, 'product_id': 0x19, 'interface_number': 1, 'path': b'/dev/hidraw1'},
        {'vendor_id': 0x1234, 'product_id': 0x19, 'interface_number': 0, 'path': b'/dev/hidraw5'},
        {'vendor_id': 0x2207, 'product_id': 0x19, 'interface_number': 0, 'path': b'/dev/hidraw0'},
    ]
    assert deckproto.select_vendor_path(devices) == b'/dev/hidraw0'
    assert deckproto.select_vendor_path(devices[:2]) is None


def test_legacy_selection_rejects_multiple_vendor_interfaces():
    import pytest
    devices = [dict(vendor_id=0x2207, product_id=0x19, interface_number=0,
                    path=f'/fake/{i}'.encode()) for i in range(2)]
    assert len(deckproto.vendor_devices(devices)) == 2
    with pytest.raises(OSError, match='multiple D200s'):
        deckproto.select_vendor_path(devices)
