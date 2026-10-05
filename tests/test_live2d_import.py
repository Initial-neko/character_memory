import io
import json
import zipfile
import hashlib
import struct
import zlib

import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.live2d_web import attach_live2d_routes


def package(label="first", extra=None, missing=False):
    image=io.BytesIO();Image.new("RGBA",(2,2),(255,0,0,255)).save(image,format="PNG")
    manifest={"Version":3,"FileReferences":{"Moc":"model.moc3","Textures":["textures/texture.png"],"Motions":{"Idle":[{"File":"idle.motion3.json"}]},"Expressions":[{"Name":"普通","File":"neutral.exp3.json"}]}}
    items={"export/model.model3.json":json.dumps(manifest),"export/model.moc3":b"MOC3"+bytes([5])+bytes(59)+label.encode(),"export/textures/texture.png":image.getvalue(),"export/idle.motion3.json":"{}","export/neutral.exp3.json":"{}"}
    if missing:items.pop("export/model.moc3")
    if extra:items.update(extra)
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        for name,data in items.items():z.writestr(name,data)
    return out.getvalue()


@pytest.fixture
def client(tmp_path):
    app=FastAPI();attach_live2d_routes(app,tmp_path,lambda:[{"id":"rin"},{"id":"haru"}])
    with TestClient(app) as c:yield c


def upload(client,data,cid="rin"):
    return client.post(f"/v1/characters/{cid}/live2d",content=data,headers={"Content-Type":"application/zip"})


def test_import_replace_unbind_preserves_old_asset_urls(client):
    first=upload(client,package());assert first.status_code==200,first.text
    old=first.json();assert old["available"] and old["name"]=="model.model3.json"
    old_moc=old["model_url"].rsplit("/",1)[0]+"/model.moc3"
    assert client.get(old_moc).status_code==200
    second=upload(client,package("second"));assert second.status_code==200
    assert second.json()["model_url"]!=old["model_url"]
    assert client.get(old_moc).content.endswith(b"first")
    assert client.get('/v1/characters/haru/live2d').json()["available"] is False
    assert client.delete('/v1/characters/rin/live2d').status_code==200
    assert client.get('/v1/characters/rin/live2d').json()["available"] is False
    assert client.get(old_moc).status_code==200  # running calls retain immutable resources
    assert upload(client,package("again")).json()["available"] is True


@pytest.mark.parametrize("data",[b"not zip",package(missing=True),package(extra={"../escape.json":"{}"}),package(extra={"export/texture.PNG":"x","export/TEXTURE.png":"y"}),package(extra={"export/evil.js":"alert(1)"}),package(extra={"export/other.model3.json":"{}"})],ids=["not-zip","missing-moc","traversal","case-collision","script","multiple-manifests"])
def test_invalid_import_keeps_previous_binding(client,data):
    before=upload(client,package()).json()
    response=upload(client,data)
    assert response.status_code==400,response.text
    assert client.get('/v1/characters/rin/live2d').json()["model_url"]==before["model_url"]


def test_unknown_character_and_oversized_upload(client,monkeypatch):
    assert upload(client,package(),"unknown").status_code==404
    import character_memory.live2d_web as web
    monkeypatch.setattr(web,"MAX_UPLOAD_BYTES",32)
    assert upload(client,b"x"*33).status_code==413


def test_legacy_directory_can_be_unbound_without_deleting_files(client,tmp_path):
    folder=tmp_path/'rin';folder.mkdir()
    (folder/'old.model3.json').write_text('{"Version":3}',encoding='utf-8')
    assert client.get('/v1/characters/rin/live2d').json()['available']
    assert client.delete('/v1/characters/rin/live2d').status_code==200
    assert (folder/'old.model3.json').exists()
    assert not client.get('/v1/characters/rin/live2d').json()['available']


@pytest.mark.parametrize("change", ["bad-moc", "bad-texture", "bad-json", "remote-ref"])
def test_corrupt_runtime_resources_are_rejected(client,change):
    raw=package()
    with zipfile.ZipFile(io.BytesIO(raw)) as z: files={name:z.read(name) for name in z.namelist()}
    if change=="bad-moc": files["export/model.moc3"]=b"broken"
    elif change=="bad-texture": files["export/textures/texture.png"]=b"broken"
    elif change=="bad-json":files["export/idle.motion3.json"]=b"broken"
    else:
        manifest=json.loads(files["export/model.model3.json"]);manifest["FileReferences"]["Moc"]="https://example.com/a.moc3";files["export/model.model3.json"]=json.dumps(manifest).encode()
    data=io.BytesIO()
    with zipfile.ZipFile(data,"w") as z:
        for name,value in files.items():z.writestr(name,value)
    assert upload(client,data.getvalue()).status_code==400
    assert not client.get('/v1/characters/rin/live2d').json()['available']


def test_publish_failure_preserves_binding_and_cleans_staging(client,tmp_path,monkeypatch):
    from character_memory import live2d_import
    before=upload(client,package()).json()
    versions=tmp_path/'rin'/'_versions';old=list(versions.iterdir())
    def fail(*args):raise OSError("disk unavailable")
    monkeypatch.setattr(live2d_import,"publish",fail)
    assert upload(client,package("new")).status_code==507
    assert list(versions.iterdir())==old
    assert client.get('/v1/characters/rin/live2d').json()['model_url']==before['model_url']


def test_zip_symlink_and_expansion_limit(client,monkeypatch):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,"w") as z:
        link=zipfile.ZipInfo('link');link.external_attr=(0o120777<<16);z.writestr(link,'target')
    assert upload(client,raw.getvalue()).status_code==400
    from character_memory import live2d_import
    monkeypatch.setattr(live2d_import,'MAX_EXPANDED_BYTES',32)
    assert upload(client,package()).status_code==400



def test_file_directory_prefix_collision_is_bad_zip_not_storage_error(client):
    good=package()
    with zipfile.ZipFile(io.BytesIO(good)) as z:texture=z.read('export/textures/texture.png')
    response=upload(client,package(extra={'export/textures/texture.png/nested.png':texture}))
    assert response.status_code==400
    assert not client.get('/v1/characters/rin/live2d').json()['available']


def test_invalid_pixel_stream_preserves_previous_binding(client):
    before = upload(client, package()).json()
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 0)) + chunk(b'IDAT', b'invalid zlib stream') + chunk(b'IEND', b'')
    with Image.open(io.BytesIO(png)) as image:
        image.verify()  # Structural verification alone misses broken compressed pixels.
    response = upload(client, package(extra={'export/textures/texture.png': png}))
    assert response.status_code == 400
    assert client.get('/v1/characters/rin/live2d').json()['model_url'] == before['model_url']
