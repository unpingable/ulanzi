import io
import json
import zipfile

from PIL import Image

from ulanzi import deckzip
from ulanzi import metrics
from ulanzi import tiles


def img(png):
    return Image.open(io.BytesIO(png))


def test_render_dimensions_and_determinism():
    spec = tiles.TileSpec('cpu', '37%', series=(10, 20, None, 40), lo=0, hi=100)
    png = tiles.render(spec)
    assert img(png).size == (196, 196) and img(png).mode == 'RGB'
    assert tiles.render(spec) == png
    assert tiles.render(tiles.TileSpec('cpu', '38%', series=spec.series, lo=0, hi=100)) != png


def test_unknown_state_is_explicit():
    unknown = tiles.render(tiles.TileSpec('cpu', None))
    zero = tiles.render(tiles.TileSpec('cpu', '0%'))
    assert unknown != zero
    # N/A is drawn in the dim unknown color, not the headline white
    colors = {c for _, c in img(unknown).getcolors(1 << 16)}
    assert tiles.UNKNOWN in colors and tiles.VALUE not in colors


class FakeSampler:
    def __init__(self, **history):
        self.history = {k: [] for k in ('load1', 'cpu', 'mem', 'disk', 'rx', 'tx')}
        self.history.update(history)


def test_load_graph_is_normalized_per_cpu(monkeypatch):
    monkeypatch.setattr(metrics, 'cpu_count', lambda: 4)
    s = FakeSampler(load1=[2.0, None, 8.0])
    spec = tiles.load_widget(s, metrics.Sample(t=0, load=(8.0, 6.0, 4.0)))
    assert spec.value == '8.00'
    assert spec.series == (0.5, None, 2.0)
    assert spec.ref == 1.0 and spec.warn  # 8 runnable on 4 CPUs
    calm = tiles.load_widget(s, metrics.Sample(t=0, load=(1.0, 1.0, 1.0)))
    assert not calm.warn
    assert tiles.load_widget(s, metrics.Sample(t=0)).value is None


def test_widgets_render_unknown_samples():
    s = FakeSampler()
    empty = metrics.Sample(t=0)
    for name, widget in tiles.WIDGETS.items():
        spec = widget(s, empty)
        assert spec.value is None, name
        assert img(tiles.render(spec)).size == (196, 196)


def test_human_rate():
    assert tiles.human_rate(None) == '?'
    assert tiles.human_rate(5) == '5'
    assert tiles.human_rate(1500) == '1.5K'
    assert tiles.human_rate(46_000) == '46K'
    assert tiles.human_rate(2_500_000_000_000) == '2500G'


def test_partial_zip_holds_only_given_keys():
    png = tiles.render(tiles.TileSpec('cpu', '1%'))
    blob = deckzip.build_zip_images({2: png, 7: None})
    assert deckzip.frame_starts_ok(blob)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.namelist()[0] == 'manifest.json' and zf.namelist()[-1] == 'sentinel.txt'
    manifest = json.loads(zf.read('manifest.json'))
    assert set(manifest) == {'2_0', '2_1'}
    assert manifest['2_1'] == {'State': 0, 'ViewParam': [{}]}
    assert zf.read(manifest['2_0']['ViewParam'][0]['Icon']) == png


def test_label_zip_layout():
    blob = deckzip.build_zip({0: 'ping', 1: '', 7: 'example: DEGRADED right now'})
    zf = zipfile.ZipFile(io.BytesIO(blob))
    manifest = json.loads(zf.read('manifest.json'))
    assert manifest['1_0'] == {'State': 0, 'ViewParam': [{}]}
    assert 'Text' not in manifest['0_0']['ViewParam'][0]
    assert img(zf.read(manifest['2_1']['ViewParam'][0]['Icon'])).size == (196, 196)


def test_fixed_icon_names():
    png = tiles.render(tiles.TileSpec('cpu', '1%'))
    zf = zipfile.ZipFile(io.BytesIO(deckzip.build_zip_images({0: png}, names={0: 'qA.png'})))
    manifest = json.loads(zf.read('manifest.json'))
    assert manifest['0_0']['ViewParam'][0]['Icon'] == 'icons/qA.png'
    assert zf.read('icons/qA.png') == png


def test_fedora_font_location_is_shared_by_labels_and_widgets(monkeypatch):
    from pathlib import Path
    sentinel = object()
    calls = []
    tiles.font.cache_clear()
    monkeypatch.setattr(Path, 'exists', lambda p: str(p) == '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf')
    monkeypatch.setattr(tiles.ImageFont, 'truetype', lambda path, size: calls.append((path, size)) or sentinel)
    try:
        assert tiles.font('bold', 20) is sentinel
        assert deckzip._font(20) is sentinel
        assert calls == [('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf', 20)]
    finally:
        tiles.font.cache_clear()
