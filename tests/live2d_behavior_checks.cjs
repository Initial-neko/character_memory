const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
let now=10000, next=0; const timers=new Map(), motions=[], expressions=[];
const CM={}; const context={window:{CM},Date:{now:()=>now},console,setTimeout:(fn,ms)=>{timers.set(++next,{fn,at:now+ms});return next;},clearTimeout:id=>timers.delete(id)};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/character_memory/web/live2d_behavior.js'),'utf8'),context);
const caps={revision:'rev',motions:['Idle','Nod','Thinking'],expressions:['普通','开心']};
const b=CM.createLive2DBehavior({startMotion:async name=>{motions.push(name);},setExpression:async name=>{expressions.push(name);}},caps,{token:'call-1',characterId:'rin'});
const hint={token:'call-1',character_id:'rin',revision:'rev',motion:'Nod',expression:'开心'};
b.reply({...hint,token:'old'},1); assert.equal(motions.length,0);
b.reply({...hint,character_id:'other'},1); assert.equal(motions.length,0);
b.reply(hint,2); assert.deepEqual(motions,['Nod']); assert.deepEqual(expressions,['开心']);
b.reply(hint,2); assert.equal(motions.length,1,'deduplicate SSE');
b.reply(hint,3); assert.equal(motions.length,1,'rate-limit motion');
b.pause(); now+=10000; b.reply(hint,4); now+=10000; b.resume(); assert.equal(motions.length,1,'discard stale hidden hint');
now+=3000; b.manual(); b.reply(hint,5); assert.equal(motions.length,1,'manual temporarily wins');
now+=6000; b.reply(hint,6); assert.equal(motions.length,2);
b.destroy(); assert.equal(timers.size,0); b.reply(hint,7); assert.equal(motions.length,2);

(async()=>{
  let reserved=null, actual="普通", finish;
  const trace=[];
  const slowRenderer={
    cancelPendingExpression(){reserved=null;},
    async setExpression(name){
      reserved=null;
      if(name===actual) return false;
      reserved=name;
      if(name==="Slow") await new Promise(resolve=>{finish=resolve;});
      if(reserved!==name) return false;
      actual=name; reserved=null; return true;
    },
    async startMotion(){}
  };
  const slowCaps={revision:'rev',motions:['Nod'],expressions:['普通','Slow']};
  const slow=CM.createLive2DBehavior(slowRenderer,slowCaps,{token:'call-1',characterId:'rin'},value=>trace.push(value));
  slow.reply({...hint,expression:'Slow'},10);
  now+=6000;
  for(const [id,timer] of [...timers]) if(timer.at<=now){timers.delete(id);timer.fn();}
  finish(); for(let i=0;i<5;i++) await Promise.resolve();
  assert.equal(actual,'普通','late automatic expression stays expired');
  assert.ok(trace.every(state=>state.expression!=='Slow'),'old promise cannot publish stale expression');
  slow.reply({...hint,expression:'Slow'},11);
  slow.manual(); await slowRenderer.startMotion('Nod');
  finish(); for(let i=0;i<5;i++) await Promise.resolve();
  assert.equal(actual,'普通','manual motion cancels old automatic expression');
  slow.destroy(); assert.equal(timers.size,0);
  console.log(JSON.stringify({pass:true,token_and_character_guards:true,cooldown:true,dedup:true,stale_hidden_hint:true,manual_priority:true,slow_expression_expiry:true,manual_motion_cancels_pending_expression:true,timers_after_destroy:timers.size}));
})().catch(error=>{console.error(error);process.exitCode=1;});
