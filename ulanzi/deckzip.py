"""Build the SET_BUTTONS zip for the D200: text tiles rendered as PNG icons.

Firmware behavior this works around (see redphx/strmdck and marcelobrake/ulanzi-linux):
- A key with only manifest Text renders black; it needs an Icon.
- The parser can discard the last archive entry, so an empty sentinel goes last.
- Icons are cached by name; the daemon alternates two bounded names per key.
- The zip is streamed in 1024-byte frames. The byte that starts each frame after
  the first must not be 0x00 or 0x7c, so dummy.txt is padded until none are.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from functools import lru_cache

from PIL import Image, ImageDraw
from .tiles import font

COLS = 5
TILE = 196
MARGIN = 10
BACKGROUND = (0, 0, 0)
FOREGROUND = (255, 255, 255)
FIRST_FRAME_DATA = 1024 - 8
FRAME = 1024
BAD_FRAME_START = (0x00, 0x7C)


def _font(size: int):
    return font('bold', size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines = []
    for paragraph in text.split('\n'):
        line = ''
        for word in paragraph.split():
            candidate = f'{line} {word}'.strip()
            if not line or draw.textlength(candidate, font=font) <= width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


@lru_cache(maxsize=256)
def render_tile(text: str) -> bytes:
    """A TILE x TILE PNG with the text wrapped and shrunk until it fits."""
    img = Image.new('RGB', (TILE, TILE), BACKGROUND)
    draw = ImageDraw.Draw(img)
    room = TILE - 2 * MARGIN
    for size in range(44, 11, -2):
        font = _font(size)
        lines = _wrap(draw, text, font, room)
        spacing = size // 5
        box = draw.multiline_textbbox((0, 0), '\n'.join(lines), font=font, spacing=spacing)
        if box[2] - box[0] <= room and box[3] - box[1] <= room:
            break
    draw.multiline_text((TILE / 2, TILE / 2), '\n'.join(lines), font=font, fill=FOREGROUND,
                        anchor='mm', align='center', spacing=spacing)
    out = io.BytesIO()
    img.save(out, 'PNG')
    return out.getvalue()


def frame_starts_ok(blob: bytes) -> bool:
    return all(blob[i] not in BAD_FRAME_START for i in range(FIRST_FRAME_DATA, len(blob), FRAME))


def _assemble(manifest: dict, icons: dict[str, bytes], padding: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.writestr('manifest.json', json.dumps(manifest, sort_keys=True, separators=(',', ':')))
        zf.writestr(zipfile.ZipInfo('dummy.txt'), b'A' * padding)
        for name, data in icons.items():
            zf.writestr(name, data)
        zf.writestr(zipfile.ZipInfo('sentinel.txt'), b'')
    return buf.getvalue()


def slot_name(n: int, slot: int) -> str:
    """Icon filename for key index n in slot 0 or 1.

    Realistic tile uploads under a fresh name each time stopped rendering after
    ~490-900 updates (docs/qualification). Two names per key keep the set bounded,
    and alternating means a key never gets new content under the name it is
    showing right now."""
    return f'k{n + 1:02d}{"ab"[slot]}.png'


def build_zip(labels: dict[int, str]) -> bytes:
    """labels maps key index to text; empty keys are blank."""
    return build_zip_images({n: render_tile(text) if text else None for n, text in labels.items()})


def build_zip_images(images: dict[int, bytes | None], names: dict[int, str] | None = None) -> bytes:
    """images maps key index to a PNG, or None to blank the key. Keys not in the
    map are left out of the manifest, which is what a partial update wants.
    names optionally fixes a key's icon filename; the default is a content hash."""
    manifest, icons = {}, {}
    for n, png in sorted(images.items()):
        view = {}
        if png:
            name = f'icons/{names[n]}' if names and n in names else \
                f'icons/{hashlib.sha1(png).hexdigest()[:16]}.png'
            icons[name] = png
            view['Icon'] = name
        manifest[f'{n % COLS}_{n // COLS}'] = {'State': 0, 'ViewParam': [view]}

    for padding in range(4096):
        blob = _assemble(manifest, icons, padding)
        if frame_starts_ok(blob):
            return blob
    raise RuntimeError('no zip padding avoids bad frame-start bytes')
