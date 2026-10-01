"""Execute the production Direct listeners/projection without a browser runtime."""
from pathlib import Path
import shutil
import subprocess

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/character_memory/web"

HARNESS = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.parent = null; this.className = ''; this.textContent = ''; }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html || ''; }
  appendChild(child) { child.parent = this; this.children.push(child); }
  querySelector(selector) {
    if (selector === '[data-direct-error]') return this.children.find(child => child.dataset?.directError) || null;
    if (selector === '.typing-row') return this.children.find(child => String(child.className || '').split(/\s+/).includes('typing-row')) || null;
    return null;
  }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter(child => child !== this);
    this.parent = null;
  }
}
const elements = new Map();
global.document = {
  body:{scrollHeight:0},
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement() { return new Element(); }
};
global.window = {addEventListener(){}, scrollTo(){}, scrollY:0};
const stored = new Map([['character-memory:conversation:rin', 'audit']]);
global.localStorage = {getItem:k=>stored.get(k), setItem:(k,v)=>stored.set(k,v)};
global.EventSource = class {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  emit(type, data) { this.listeners.get(type)?.({data:JSON.stringify(data)}); }
  close() {}
};
vm.runInThisContext(fs.readFileSync(process.argv[1] + '/app.js', 'utf8'));
vm.runInThisContext(fs.readFileSync(process.argv[1] + '/message_content.js', 'utf8'));
const CM = window.CM;
CM.renderCharacterList = () => {};
CM.updateHeader = () => {};
CM.appendTypingForCurrent = () => {};
CM.connectDirectStream();
const source = CM.state.directStream;
'''


def run_js(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute production JavaScript")
    result = subprocess.run([node, "-e", HARNESS + script, str(WEB)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_reaction_failure_survives_idle_redraw_and_clears_on_newer_turn():
    run_js(r'''
source.emit('reaction_error', {watermark:10, message:'provider unavailable'});
source.emit('reaction_status', {state:'idle', watermark:10});
assert(CM.dom.chat.children.some(row=>row.textContent?.includes('provider unavailable')),
       'terminal idle must not erase the provider failure');
CM.state.lastRenderedSignature = '';
CM.renderHistory([]); // same redraw used by durable-history reconciliation
assert(CM.dom.chat.children.some(row=>row.textContent?.includes('provider unavailable')));
source.emit('reaction_status', {state:'queued', watermark:11});
assert(!CM.dom.chat.children.some(row=>row.textContent?.includes('provider unavailable')));
source.emit('reaction_status', {state:'idle', watermark:11});
assert(!CM.state.pendingCharacters.has('rin'));
// A newer user fact can queue before an older provider failure is reported.
source.emit('reaction_status', {state:'queued', watermark:13});
source.emit('reaction_error', {watermark:12, message:'older attempt failed'});
source.emit('reaction_status', {state:'typing', watermark:13});
assert(!CM.dom.chat.children.some(row=>row.textContent?.includes('older attempt failed')));
''')


def test_generated_media_sse_projection_matches_durable_image_route():
    run_js(r'''
const message = CM.directEventToMessage({id:12, character_id:'rin', content:'[生成图片：配图]',
  metadata:{action:'IMAGE', media_id:'generated/a b', image_id:'legacy-image'}});
assert.equal(message.media_id, 'generated/a b');
assert.equal(message.image.url, '/v1/media/generated%2Fa%20b');
assert.equal(message.image.label, null); // filenames/ids are not captions
const rendered = CM.messageContentHtml(message);
assert(rendered.includes('<img'));
assert(rendered.includes('/v1/media/generated%2Fa%20b'));
assert(!rendered.includes('[生成图片：配图]'));
const library = CM.directEventToMessage({id:13, character_id:'rin',
  metadata:{action:'IMAGE', image_id:'library', image_label:'海边'}});
assert.equal(library.image.url, '/v1/images/rin/library/asset');
assert.equal(library.image.label, '海边');
''')
