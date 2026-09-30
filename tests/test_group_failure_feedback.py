"""Execute the production Group SSE listeners and redraws in a DOM stub."""
from pathlib import Path
import shutil
import subprocess

import pytest


HARNESS = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.slots = new Map();
    this.classList = {add(){},remove(){},toggle(){},contains(){return false;}}; }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html || ''; }
  appendChild(child) { this.children.push(child); }
  querySelector(selector) {
    if (selector === '.group-pending-note') return this.children.find(e=>e.className===selector.slice(1)) || null;
    if (!this.slots.has(selector)) this.slots.set(selector, new Element());
    return this.slots.get(selector);
  }
  querySelectorAll() { return []; }
  addEventListener() {}
  focus() {}
  remove() {}
}
const elements = new Map();
global.document = {body:new Element(), addEventListener(){}, createElement:()=>new Element(),
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }};
global.window = {addEventListener(){},scrollTo(){},scrollY:0};
global.requestAnimationFrame = fn => fn();
global.EventSource = class {
  constructor() { this.listeners = new Map(); global.source = this; }
  addEventListener(kind, fn) { this.listeners.set(kind, fn); }
  emit(kind, data) { this.listeners.get(kind)?.({data:JSON.stringify(data)}); }
  close() {}
};
const group = {id:'g1',name:'group',member_ids:['a','b'],members:[]};
const CM = window.CM = {
  state:{conversation:{type:'DIRECT',groupId:null}}, features:{},
  dom:Object.fromEntries(['chat','drawerBody','characterName','characterIdentity','headerAvatar','input',
    'runtimeButton','sendButton'].map(id=>[id,new Element()])),
  isGroupConversation(){ return this.state.conversation.type==='GROUP'; },
  registerFeature(name, api){ this.features[name]=api; }, on(){},
  api:async path => path==='/v1/groups' ? {groups:[group]} : {group,messages:[],has_more:false},
  escapeHtml:String, fmtDate:String, fmtTime:String, bindMessageContent(){},
  closeDrawer(){},renderCharacterList(){},updateHeader(){},updateComposerState(){},scrollToBottom(){},
  emit:async()=>{},
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  await CM.features.groups.loadGroups();
  await CM.features.groups.enter('g1');
  const hasError = text => CM.dom.chat.children.some(row=>row.className?.split(' ').includes('error') && row.textContent?.includes(text));
  source.emit('reaction_status', {state:'typing',watermark:10});
  source.emit('reaction_error', {watermark:10,message:'provider unavailable'});
  assert(hasError('provider unavailable'));
  source.emit('reaction_status', {state:'idle',watermark:10});
  assert(hasError('provider unavailable'), 'terminal idle must retain the Group failure');
  await CM.features.groups.reconcileLatest('g1');
  assert(hasError('provider unavailable'), 'history reconciliation must retain the Group failure');
  CM.messageContentHtml = message=>message.content;
  CM.api = async()=>({group,messages:[{id:1,role:'user',turn_id:'t1',content:'hello',event_time:'2026-09-22T12:00:00Z'}]});
  await CM.features.groups.reconcileLatest('g1');
  assert(hasError('provider unavailable'), 'populated history must retain the Group failure');
  source.emit('reaction_status', {state:'queued',watermark:11});
  assert(!hasError('provider unavailable'), 'a newer attempt clears the old failure');
  source.emit('reaction_error', {watermark:10,message:'late old failure'});
  source.emit('reaction_status', {state:'typing',watermark:11});
  assert(!hasError('late old failure'), 'older failure must not replace newer attempt feedback');
  source.emit('reaction_error', {watermark:11,message:'newer provider failed'});
  source.emit('reaction_status', {state:'idle',watermark:11});
  assert(hasError('newer provider failed'));
})().catch(error=>{ console.error(error); process.exitCode=1; });
'''


def test_group_failure_survives_terminal_idle_and_history_reconciliation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute Group listeners")
    source = Path(__file__).resolve().parents[1] / "src/character_memory/web/groups.js"
    result = subprocess.run([node, "-e", HARNESS, str(source)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
