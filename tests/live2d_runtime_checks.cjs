const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/character_memory/web/live2d.js'), 'utf8');
async function check(version) {
  const elements = new Map();
  const el = id => elements.get(id) || elements.set(id, {
    dataset:{}, classList: {toggle(){}, remove(){}}, setAttribute(){}, addEventListener(){},
    clientWidth:400, clientHeight:350, children:[], replaceChildren(){this.children=[];}, appendChild(x){this.children.push(x);},
  }).get(id);
  let destroyed=0, pauses=0, resumes=0, observers=0, pending=null, fail=false;
  const models=[];
  class App {
    constructor(options) {
      if (version===6) assert.ok(options, 'Pixi 6 requires constructor options');
      this.view=this.canvas={remove(){}}; this.stage={addChild(){}};
      this.renderer={resize(){}}; this.ticker={add(fn){this.update=fn;},stop(){pauses++;},start(){resumes++;}};
    }
    destroy(){destroyed++;}
  }
  if (version===8) App.prototype.init=async function(){};
  const events={}; const CM={on(name,fn){events[name]=fn;}};
  const sandbox={window:{CM,Live2DCubismCore:{},WebGL2RenderingContext:function(){},devicePixelRatio:1,PIXI:{VERSION:version+'.0.0',Application:App,live2d:{Live2DModel:{async from(url,options){
    assert.equal(options?.autoInteract,false,'pointer following must be disabled');
    if(version===6) assert.equal(options.autoUpdate,false,'use the call ticker');
    if(pending) await pending;
    const handlers={};
    const manager={reserveExpressionIndex:-1,resetExpression(){this.reserveExpressionIndex=-1;}};
    let resolveExpression;
    const core={_model:{parameters:{ids:['ParamMouthOpenY'],minimumValues:[0],maximumValues:[1]}},setParameterValueById(id,value){this.last=[id,value];}};
    const m={width:100,height:200,anchor:{set(){}},position:{set(){}},scale:{x:1,y:1,set(x){this.x=x;this.y=x;}},internalModel:{coreModel:core,motionManager:{expressionManager:manager},on(name,fn){handlers[name]=fn;}},motion:async (...args)=>{m.motionArgs=args;return true;},expression:async name=>{
      if(name===m.expressionName) return false;
      manager.reserveExpressionIndex=name;
      if(name==="Slow") await new Promise(resolve=>{resolveExpression=resolve;});
      if(manager.reserveExpressionIndex!==name) return false;
      manager.reserveExpressionIndex=-1; m.expressionName=name; return true;
    },finishExpression:()=>resolveExpression(),handlers,core};
    models.push(m); return m;
  }}}}},document:{getElementById:el,querySelector:el,createElement:()=>({})},ResizeObserver:class{constructor(){observers++;}observe(){}disconnect(){observers--;}},setTimeout,clearTimeout,fetch:async()=>({ok:true,json:async()=>({available:!fail,model_url:'/model.model3.json',capabilities:{revision:"a".repeat(64),motions:["Idle","Nod"],expressions:["惊讶"]}})}),console:{warn(){}}};
  vm.runInNewContext(fs.readFileSync(require("node:path").join(__dirname,"../src/character_memory/web/live2d_behavior.js"),"utf8"),sandbox);
  vm.runInNewContext(source,sandbox);
  const flush=async()=>{for(let i=0;i<12;i++) await new Promise(resolve=>setImmediate(resolve));};
  CM.live2d.setCharacter('school'); CM.live2d.toggle(); await flush();
  assert.equal(models.length,1);
  const identity=CM.live2d.requestContext("school");
  assert.ok(identity,"one toggle enables reply presentation");
  assert.equal(CM.live2d.requestContext("other"),null);
  assert.equal(JSON.parse(el("voiceLive2dStage").dataset.live2dBehavior).automatic,true);
  assert.equal(CM.live2d.onReply("school",{...identity,motion:"Nod"},"event-1"),true);
  await flush(); assert.equal(models[0].motionArgs[0],"Nod");
  assert.equal(await CM.live2d.startMotion('Nod',0),true);
  assert.equal(await CM.live2d.setExpression('惊讶'),true);
  assert.equal(models[0].expressionName,'惊讶');
  const slow=CM.live2d.setExpression("Slow");
  await CM.live2d.setExpression("惊讶"); // already active: SDK would not cancel old load
  models[0].finishExpression();
  assert.equal(await slow,false);
  assert.equal(models[0].expressionName,"惊讶","late expression cannot override current manual/reset expression");
  const slowBeforeMotion=CM.live2d.setExpression("Slow");
  await CM.live2d.startMotion("Nod");
  models[0].finishExpression();
  assert.equal(await slowBeforeMotion,false,"manual motion cancels pending automatic expression");
  assert.equal(models[0].expressionName,"惊讶");
  assert.equal(CM.live2d.setParameter('ParamMouthOpenY',9),true);
  models[0].handlers.beforeModelUpdate(); assert.deepEqual(models[0].core.last,['ParamMouthOpenY',1]);
  assert.equal(CM.live2d.setParameter('missing',1),false);
  assert.equal(CM.live2d.setParameter('ParamMouthOpenY',NaN),false);
  CM.live2d.clearParameter('ParamMouthOpenY'); models[0].core.last=null;
  models[0].handlers.beforeModelUpdate(); assert.equal(models[0].core.last,null);
  CM.live2d.pause(); CM.live2d.resume(); assert.equal(pauses,1); assert.equal(resumes,1);
  const oldIdentity=CM.live2d.requestContext();
  events.live2dModelChanged({characterId:"school"}); await flush();
  assert.equal(models.length,2,"replacement reloads one renderer within the existing call");
  assert.notEqual(CM.live2d.requestContext().token,oldIdentity.token);
  assert.equal(CM.live2d.onReply("school",{...oldIdentity,motion:"Nod"},"old-after-replace"),false);
  assert.equal(destroyed,1);
  CM.live2d.stop(); assert.equal(destroyed,2); assert.equal(observers,0);
  assert.equal(CM.live2d.setParameter('ParamMouthOpenY',1),false);
  assert.equal(CM.live2d.requestContext(),null);
  assert.equal(CM.live2d.onReply("school",{...identity,motion:"Nod"},"late-event"),false);
  CM.live2d.setCharacter('school'); let release; pending=new Promise(r=>release=r);
  CM.live2d.toggle(); await flush(); CM.live2d.stop();
  assert.equal(destroyed,3,'hangup must release the app even while model download is pending');
  release(); await flush();
  assert.equal(destroyed,3,'late-loaded renderer must be destroyed'); assert.equal(observers,0);
  pending=null; fail=true; CM.live2d.setCharacter('missing'); CM.live2d.toggle(); await flush();
  assert.equal(models.length,3,'missing model retains portrait without renderer');
  return {pixi:version,pass:true,destroyed,observers};
}
function checkMasks() {
  const model={renderOrders:new Int32Array([1,0]),drawables:{masks:[new Int32Array([1]),new Int32Array()],dynamicFlags:new Uint8Array(2),resetDynamicFlags(){this.dynamicFlags.fill(0);}}};
  const window={PurismCore:{Model:{fromMoc:()=>model}}};
  const bridge=fs.readFileSync(require('node:path').join(__dirname,'../src/character_memory/web/live2d_purism_bridge.js'),'utf8');
  vm.runInNewContext(bridge,{window});
  const loaded=window.Live2DCubismCore.Model.fromMoc({});
  assert.equal(loaded.drawables.renderOrders,loaded.renderOrders);
  for(let frame=0;frame<120;frame++) {
    loaded.drawables.resetDynamicFlags();
    assert.equal(loaded.drawables.dynamicFlags[1]&32,32);
    assert.equal(loaded.drawables.dynamicFlags[0],0);
  }
  return {mask_frames:120,pass:true};
}
(async()=>console.log(JSON.stringify({checks:[await check(6),await check(8),checkMasks()]})))().catch(e=>{console.error(e);process.exitCode=1;});

