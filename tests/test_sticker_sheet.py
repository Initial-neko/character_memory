from io import BytesIO
import json
import struct
import zlib
import zipfile
import sys

import pytest

from character_memory.sticker_sheet import sticker_sheet_bundle
from character_memory.stickers import import_sticker_bundle, load_sticker_catalog


def png(width, height, pixels):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    raw = b''.join(b'\0' + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def sheet(blank_cell=False):
    pixels = bytearray(30 * 30 * 4)
    for row in range(3):
        for col in range(3):
            if blank_cell and (row, col) == (2, 2):
                continue
            for y in range(row * 10 + 3, row * 10 + 7):
                for x in range(col * 10 + 3, col * 10 + 7):
                    index = (y * 30 + x) * 4
                    pixels[index:index + 4] = bytes((row * 70, col * 70, 90, 255))
    return png(30, 30, pixels)


def test_sheet_becomes_nine_transparent_assets_with_stable_pack_and_ids(tmp_path):
    first = sticker_sheet_bundle(sheet(), pack_name='猫咪')
    second = sticker_sheet_bundle(sheet(), pack_name='猫咪')
    assert first == second
    with zipfile.ZipFile(BytesIO(first)) as archive:
        rows = json.loads(archive.read('all_tags.json'))
        assert len(rows) == 9
        assert len({row['id'] for row in rows}) == 9
        assert len({row['set_id'] for row in rows}) == 1
        for row in rows:
            data = archive.read(row['filename'])
            assert data[:8] == b'\x89PNG\r\n\x1a\n'
            assert struct.unpack('>II', data[16:24]) == (8, 8) # 4x4 alpha bounds plus 2px padding.
    target = tmp_path / 'stickers'
    for _ in range(2):
        result = import_sticker_bundle(tmp_path / 'persona.yaml', first, target_dir=target)
        assert result['imported'] == 9
    catalog = load_sticker_catalog(tmp_path / 'persona.yaml')
    imported = [item for item in catalog.stickers if item.pack_id.startswith('sheet_')]
    assert len(imported) == 9
    assert any(item.pack_id == 'default' for item in catalog.stickers)
    assert all(catalog.asset_path(item.id).is_file() for item in catalog.stickers)


@pytest.mark.parametrize('data', [b'bad png', sheet(True), png(31, 30, bytes(31 * 30 * 4))])
def test_invalid_sheet_fails_before_creating_assets(data, tmp_path):
    with pytest.raises(ValueError):
        sticker_sheet_bundle(data)
    assert list(tmp_path.iterdir()) == []


def test_corrupt_crc_is_rejected():
    data = bytearray(sheet())
    data[29] ^= 1
    with pytest.raises(ValueError, match='CRC'):
        sticker_sheet_bundle(bytes(data))


def test_opaque_sheet_requires_explicit_transparent_input():
    with pytest.raises(ValueError, match='transparent'):
        sticker_sheet_bundle(png(30, 30, bytes((1, 2, 3, 255)) * 900))


@pytest.mark.parametrize('filter_type', [1, 2, 3, 4])
def test_png_standard_filters_preserve_cell_colors_and_alpha(filter_type):
    source = sheet()
    length = struct.unpack('>I', source[33:37])[0]
    raw = zlib.decompress(source[41:41 + length])
    previous = bytes(120)
    encoded = bytearray()
    for y in range(30):
        row = raw[y * 121 + 1:(y + 1) * 121]
        encoded.append(filter_type)
        for x, value in enumerate(row):
            a, b, c = row[x - 4] if x >= 4 else 0, previous[x], previous[x - 4] if x >= 4 else 0
            if filter_type == 1:
                predictor = a
            elif filter_type == 2:
                predictor = b
            elif filter_type == 3:
                predictor = (a + b) // 2
            else:
                p = a + b - c
                predictor = min(enumerate((a, b, c)), key=lambda item: (abs(p - item[1]), item[0]))[1]
            encoded.append((value - predictor) & 255)
        previous = row
    compressed = zlib.compress(encoded)
    filtered = source[:33] + struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', zlib.crc32(b'IDAT' + compressed) & 0xffffffff) + source[-12:]
    def assets(data):
        with zipfile.ZipFile(BytesIO(sticker_sheet_bundle(data))) as archive:
            return [archive.read(name) for name in archive.namelist() if name.endswith('.png')]
    assert assets(source) == assets(filtered)


def test_cli_png_import_uses_global_library(tmp_path, monkeypatch):
    from test_sticker_import_cli import _write_config
    from character_memory.sticker_import_cli import main
    config, _ = _write_config(tmp_path)
    source = tmp_path / 'cats.png'
    source.write_bytes(sheet())
    monkeypatch.setattr(sys, 'argv', ['sticker-import', str(source), '--config', str(config)])
    assert main() == 0
    assert (tmp_path / 'data/stickers/manifest.yaml').is_file()


def test_api_png_import_reloads_global_assets_and_bad_sheet_keeps_library(tmp_path):
    from fastapi.testclient import TestClient
    from test_sticker_api import _bundle
    from character_memory.api import create_api
    from character_memory.storage.sqlite import SQLiteStore
    store = SQLiteStore(tmp_path / 'x.db')
    try:
        bundle = _bundle(store)
        persona = tmp_path / 'persona.yaml'
        persona.write_text('id: rin\nname: Rin\n', encoding='utf-8')
        bundle.settings.persona_path = str(persona)
        bundle.characters[0]['persona_path'] = str(persona)
        client = TestClient(create_api(bundle=bundle))
        for _ in range(2):
            response = client.post('/v1/stickers/import?filename=cats.png&auto_tag=false', content=sheet(), headers={'Content-Type': 'image/png'})
            assert response.status_code == 200, response.text
            assert response.json()['imported'] == 9
        catalog = client.get('/v1/stickers').json()['stickers']
        imported = [item for item in catalog if item['pack_id'].startswith('sheet_')]
        assert len(imported) == 9
        assert all(client.get(item['url']).status_code == 200 for item in imported)
        response = client.post('/v1/stickers/import?filename=blank.png&auto_tag=false', content=sheet(True), headers={'Content-Type': 'image/png'})
        assert response.status_code == 400
        assert client.get('/v1/stickers').json()['stickers'] == catalog
        content = client.get('/openapi.json').json()['paths']['/v1/stickers/import']['post']['requestBody']['content']
        assert 'image/png' in content and 'application/zip' in content
    finally:
        store.close()
