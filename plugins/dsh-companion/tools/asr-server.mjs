import {Worker} from 'node:worker_threads';
import {access} from 'node:fs/promises';
import path from 'node:path';
import {parseArgs} from 'node:util';
import {createAsrService} from '../src/asr-service.mjs';
const {values}=parseArgs({options:{module:{type:'string'},'model-dir':{type:'string'},port:{type:'string',default:'50131'}}});
const modulePath=values.module,modelDir=values['model-dir'],port=Number(values.port);
if(!modulePath||!modelDir||!path.isAbsolute(modulePath)||!path.isAbsolute(modelDir)||!Number.isInteger(port)||port<0||port>65535)throw Error('Provide --module <existing sherpa-onnx-node file> --model-dir <existing SenseVoice directory> [--port 50131]');
await Promise.all([modulePath,path.join(modelDir,'model.int8.onnx'),path.join(modelDir,'tokens.txt')].map(p=>access(p)));
const worker=new Worker(new URL('../src/asr-worker.mjs',import.meta.url),{workerData:{modulePath,modelDir}});
let ready=false,error=null,loadMs=null,version=null,pending=null,sequence=0;
function fail(reason){ready=false;error=reason;pending?.reject(Error(reason));if(pending)clearTimeout(pending.timer);pending=null}
worker.on('error',e=>fail(e.message));worker.on('exit',code=>fail(`ASR worker exited ${code}`));
worker.on('message',m=>{
  if(m.type==='ready'){ready=true;loadMs=m.loadMs;version=m.version;return}
  if(m.type==='failed'){fail(m.error);return}
  if(pending?.id===m.id){const item=pending;pending=null;clearTimeout(item.timer);m.error?item.reject(Error(m.error)):item.resolve(m.text)}
});
const server=createAsrService({status:()=>({ready,error,busy:!!pending,model:'SenseVoiceSmall',device:'cpu',loadMs,version}),recognize:samples=>new Promise((resolve,reject)=>{
  if(pending){reject(Error('recognizer busy'));return}
  const id=++sequence,timer=setTimeout(()=>reject(Error('ASR timeout; UNKNOWN; no retry')),30000);
  pending={id,timer,resolve,reject};const copy=samples.slice();worker.postMessage({id,samples:copy.buffer},[copy.buffer]);
})});
server.listen(port,'127.0.0.1',()=>console.log(JSON.stringify({service:'companion-asr',url:`http://127.0.0.1:${server.address().port}`,pid:process.pid,usesExistingModels:true,downloads:false})));
async function stop(){fail('service stopping');server.closeAllConnections();server.close();await worker.terminate()}
process.once('SIGINT',stop);process.once('SIGTERM',stop);
