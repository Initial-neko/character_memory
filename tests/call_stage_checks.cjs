const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {dataset:{}, textContent:'', setAttribute(){},
    classList:{toggle(){}}, listeners:{}, addEventListener(k,f){this.listeners[k]=f;}});
  return elements.get(id);
}
let sessionStarts=0, captures=0, rendererToggles=0;
const CM={live2d:{isEnabled:()=>true,toggle(){rendererToggles++;}}, features:{voice:{start(){sessionStarts++;},startScreen(){captures++;}}}};
const sandbox={window:{CM}, document:{getElementById:el,querySelector:()=>el('card')}};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../src/character_memory/web/call_stage.js'),'utf8'),sandbox);
CM.callStage.setLive2d(true);
CM.callStage.setVisual({active:true,source:'DISPLAY'});
assert.equal(CM.callStage.getState().mode,'SCREEN_SHARE');
assert.equal(CM.callStage.getState().characterOverlay,true);
CM.callStage.setVisual({active:false});
assert.equal(CM.callStage.getState().mode,'LIVE2D');
CM.callStage.setHistory(true);
assert.equal(CM.callStage.getState().historyOpen,true);
CM.callStage.setHistory(false);
CM.callStage.setSubtitle('assistant','嗯，我在听。');
assert.equal(el('voiceStageSubtitle').textContent,'嗯，我在听。');
el('voiceAvatarButton').listeners.click();
assert.equal(rendererToggles,1);
assert.equal(sessionStarts,0);
assert.equal(captures,0);
console.log(JSON.stringify({status:'PASS',session_recreated:false,share_character_coexist:true,history_is_overlay:true}));
