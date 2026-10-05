"""Bounded 3x3 transparent RGBA PNG import through the existing ZIP importer.

Only 8-bit, non-interlaced RGBA PNG is accepted. Separators are detected near
the thirds rather than requiring divisible dimensions or perfectly clean alpha.
No image/model downloads or optional imaging dependency is needed.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import struct
import zlib
import zipfile


SIGNATURE = b'\x89PNG\r\n\x1a\n'
MAX_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 4_000_000


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def _encode(width: int, height: int, rgba: bytes) -> bytes:
    rows = b''.join(b'\0' + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return SIGNATURE + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(rows)) + _chunk(b'IEND', b'')


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    distances = (abs(p - a), abs(p - b), abs(p - c))
    return (a, b, c)[distances.index(min(distances))]


def _decode(data: bytes) -> tuple[int, int, bytearray]:
    if not data.startswith(SIGNATURE) or len(data) > MAX_BYTES:
        raise ValueError('expected a transparent RGBA PNG sheet, maximum 16 MiB')
    position = 8
    header = None
    compressed = bytearray()
    ended = False
    while position < len(data):
        if position + 12 > len(data):
            raise ValueError('truncated PNG chunk')
        length = struct.unpack_from('>I', data, position)[0]
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            raise ValueError('truncated PNG payload')
        payload = data[position + 8:position + 8 + length]
        crc = struct.unpack_from('>I', data, position + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ValueError('invalid PNG CRC')
        if header is None and kind != b'IHDR':
            raise ValueError('PNG must start with IHDR')
        if kind == b'IHDR':
            if header is not None or length != 13:
                raise ValueError('invalid PNG header')
            header = struct.unpack('>IIBBBBB', payload)
        elif kind == b'IDAT':
            compressed.extend(payload)
        elif kind == b'IEND':
            if length or end != len(data):
                raise ValueError('invalid PNG end')
            ended = True
            break
        elif not kind or not (kind[0] & 32):
            raise ValueError('unsupported critical PNG chunk')
        position = end
    if header is None or not ended or not compressed:
        raise ValueError('incomplete PNG')
    width, height, depth, color, compression, filtering, interlace = header
    if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
        raise ValueError('expected 8-bit non-interlaced transparent RGBA PNG')
    if width < 18 or height < 18 or width * height > MAX_PIXELS:
        raise ValueError('sheet must contain a 3x3 grid within 4 million pixels')
    stride = width * 4
    expected = height * (stride + 1)
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(compressed), expected + 1)
    except zlib.error as failure:
        raise ValueError('corrupt PNG pixel stream') from failure
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('invalid or oversized PNG pixel stream')
    rgba = bytearray(width * height * 4)
    previous = bytearray(stride)
    for y in range(height):
        offset = y * (stride + 1)
        filter_type = raw[offset]
        if filter_type > 4:
            raise ValueError('invalid PNG row filter')
        row = bytearray(raw[offset + 1:offset + 1 + stride])
        if filter_type:
            for x in range(stride):
                left = row[x - 4] if x >= 4 else 0
                up = previous[x]
                diagonal = previous[x - 4] if x >= 4 else 0
                predictor = left if filter_type == 1 else up if filter_type == 2 else (left + up) // 2 if filter_type == 3 else _paeth(left, up, diagonal)
                row[x] = (row[x] + predictor) & 255
        rgba[y * stride:(y + 1) * stride] = row
        previous = row
    return width, height, rgba


def _grid_lines(
    width: int, height: int, rgba: bytearray, *, columns: bool, span: tuple[int, int] | None = None
) -> list[int]:
    """Find nearly empty separator lines around 1/3 and 2/3 of an image.

    Thresholding tiny-alpha noise is essential for some generated PNG sheets.
    A detected separator must also have an almost-empty neighboring row/column;
    this deliberately rejects sheets without gutters instead of cutting art.

    ``span`` restricts the pixels counted for a row separator to one horizontal
    band, so each band may cut at its own y. Generated sheets frequently stagger
    the vertical placement of the three columns, and a whole-width projection
    then contains no empty row even though every band has a clean gutter.
    """
    start, stop = span or (0, width)
    length = width if columns else height
    spread = height if columns else stop - start
    counts = [0] * length
    for y in range(height):
        for x in range(start, stop):
            if rgba[(y * width + x) * 4 + 3] >= 24:
                counts[x if columns else y] += 1
    lines = [0]
    for section in (1, 2):
        center = round(section * length / 3)
        radius = max(2, length // 12)
        candidates = range(max(2, center - radius), min(length - 2, center + radius) + 1)
        selected = min(
            candidates,
            key=lambda pos: (
                counts[pos - 1] + counts[pos] + counts[pos + 1],
                abs(pos - center),
            ),
        )
        if counts[selected] > max(2, spread // 100):
            raise ValueError('each cell needs a transparent border; ambiguous sheet layout')
        lines.append(selected)
    lines.append(length)
    return lines


def sticker_sheet_bundle(data: bytes, *, pack_name: str = '九宫表情') -> bytes:
    """Validate all nine cells before returning an importer-compatible ZIP.

    Each cell must have transparent separation along its border. This rejects
    ambiguous layouts instead of silently cutting a character in half. Cells
    are alpha-trimmed and receive two transparent pixels on every side.
    """
    width, height, rgba = _decode(data)
    xs = _grid_lines(width, height, rgba, columns=True)
    # Rows are cut inside each column band: staggered columns keep one global
    # column layout, but their vertical gutters rarely line up across the sheet.
    column_rows = [
        _grid_lines(width, height, rgba, columns=False, span=(xs[col], xs[col + 1]))
        for col in range(3)
    ]
    pack_id = 'sheet_' + hashlib.sha256(data).hexdigest()[:16]
    pack_name = pack_name.strip()[:80] or '九宫表情'
    assets = []
    rows = []
    for index in range(9):
        cell_col, cell_row = index % 3, index // 3
        ys = column_rows[cell_col]
        cell_x, cell_y = xs[cell_col], ys[cell_row]
        cell_width = xs[cell_col + 1] - cell_x
        cell_height = ys[cell_row + 1] - cell_y
        left, top, right, bottom = cell_width, cell_height, -1, -1
        for y in range(cell_height):
            for x in range(cell_width):
                if rgba[((cell_y + y) * width + cell_x + x) * 4 + 3] >= 24:
                    if x in (0, cell_width - 1) or y in (0, cell_height - 1):
                        raise ValueError('each cell needs a transparent border; ambiguous sheet layout')
                    left, top = min(left, x), min(top, y)
                    right, bottom = max(right, x), max(bottom, y)
        if right < 0:
            raise ValueError(f'blank sticker cell {index + 1}')
        cropped_width, cropped_height = right - left + 5, bottom - top + 5
        pixels = bytearray(cropped_width * cropped_height * 4)
        for y in range(top, bottom + 1):
            source = ((cell_y + y) * width + cell_x + left) * 4
            target = ((y - top + 2) * cropped_width + 2) * 4
            pixels[target:target + (right - left + 1) * 4] = rgba[source:source + (right - left + 1) * 4]
        asset = _encode(cropped_width, cropped_height, pixels)
        sticker_id = f'{pack_id}_{index + 1:02d}'
        filename = f'{sticker_id}.png'
        rows.append({'id': sticker_id, 'filename': filename, 'set_id': pack_id,
                     'display_name': pack_name, 'label': f'{pack_name} {index + 1}'})
        assets.append((filename, asset))
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for filename, payload in assets + [('all_tags.json', json.dumps(rows, ensure_ascii=False).encode('utf-8'))]:
            archive.writestr(zipfile.ZipInfo(filename), payload)
    return output.getvalue()
