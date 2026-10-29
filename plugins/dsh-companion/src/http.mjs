import {readFile} from 'node:fs/promises';
import {HELP} from './config.mjs';
import * as defaults from './providers.mjs';
const PREFIX='/companion';
const files=new Map(['call-controller','config','screen-capture','client-ui','audio-capture','host-bridge'].map(name=>[`${PREFIX}/assets/${name}.mjs`,new URL(`./${name}.mjs`,import.meta.url)]));
const json=(res,status,data)=>{res.writeHead(status,{'content-type':'application/json; charset=utf-8','cache-control':'no-store'});res.end(JSON.stringify(data))};
async function bytes(req,max){let n=0;const list=[];for await(const b of req){n+=b.length;if(n>max)throw Object.assign(Error('request too large'),{status:413});list.push(b)}return Buffer.concat(list)}
async function body(req){try{return JSON.parse((await bytes(req,16384)).toString('utf8'))}catch(e){if(e.status)throw e;throw Object.assign(Error('invalid JSON'),{status:400})}}
export function trusted(req){
  if(!['127.0.0.1','::1','::ffff:127.0.0.1'].includes(req.socket.remoteAddress))return false;
  let host;try{host=new URL(`http://${req.headers.host}`).hostname}catch{return false}
  if(!['127.0.0.1','localhost','[::1]'].includes(host))return false;
  const origin=req.headers.origin;return !origin||origin===`http://${req.headers.host}`||origin===`https://${req.headers.host}`;
}
export function createHandler({store,providers=defaults}={}){
  const listeners=new Map();const controllers=new Set();let disposed=false;
  const handler=async(req,res)=>{
    const abort=new AbortController();controllers.add(abort);res.on('close',()=>{abort.abort();controllers.delete(abort)});
    try{
      if(!trusted(req))return json(res,403,{error:'loopback and same-origin access required'});
      if(disposed)return json(res,503,{error:'plugin is disposed'});
      const url=new URL(req.url,`http://${req.headers.host}`),p=url.pathname;
      if(files.has(p)&&req.method==='GET'){res.writeHead(200,{'content-type':'text/javascript; charset=utf-8','cache-control':'no-store'});res.end(await readFile(files.get(p)));return}
      if(p===PREFIX+'/api/config'){
        if(req.method==='GET')return json(res,200,{...store.snapshot,help:HELP});
        if(req.method==='PATCH'){const b=await body(req);return json(res,200,await store.update(b.expectedRevision,b.changes))}
        return json(res,405,{error:'GET or PATCH required'});
      }
      if(p===PREFIX+'/api/events'){
        if(req.method!=='GET')return json(res,405,{error:'GET required'});
        const sid=url.searchParams.get('sessionId');if(!sid||sid.length>200)return json(res,400,{error:'session required'});
        if(listeners.size>=16)return json(res,429,{error:'too many event streams'});
        res.writeHead(200,{'content-type':'text/event-stream','cache-control':'no-store','connection':'keep-alive'});res.write(': connected\n\n');
        listeners.set(res,sid);res.on('close',()=>listeners.delete(res));return;
      }
      const config=store.snapshot.value;
      if(p===PREFIX+'/api/health'){
        if(req.method!=='POST')return json(res,405,{error:'POST required'});
        const b=await body(req);if(b.capability==='asr')return json(res,200,await providers.checkAsr(config.asrEndpoint,{signal:abort.signal,protocol:config.asrProtocol}));if(b.capability!=='gsv')return json(res,400,{error:'unsupported health capability'});
        return json(res,200,await providers.checkGsv(config.gsvEndpoint,{signal:abort.signal}));
      }
      if(p===PREFIX+'/api/tts'){
        if(req.method!=='POST')return json(res,405,{error:'POST required'});
        const b=await body(req);const out=await providers.synthesizeGsv({endpoint:config.gsvEndpoint,text:b.text,voice:config.gsvVoice,language:config.gsvLanguage,speed:config.gsvSpeed,signal:abort.signal});
        res.writeHead(200,{'content-type':out.mime,'cache-control':'no-store','x-content-type-options':'nosniff'});res.end(out.bytes);return;
      }
      if(p===PREFIX+'/api/asr'){
        if(req.method!=='POST')return json(res,405,{error:'POST required'});
        if(!config.asrEndpoint)return json(res,503,{error:'ASR尚未配置；不会自动下载模型'});
        if(req.headers['content-type']!=='application/octet-stream')return json(res,400,{error:'Float32 LE PCM required'});
        const raw=await bytes(req,16000*60*4);if(raw.length%4)return json(res,400,{error:'invalid PCM length'});
        const samples=new Float32Array(raw.length/4);for(let i=0;i<samples.length;i++)samples[i]=raw.readFloatLE(i*4);
        const out=await providers.transcribeAsr({endpoint:config.asrEndpoint,sessionId:url.searchParams.get('sessionId'),epoch:Number(url.searchParams.get('epoch')??0),offset:Number(url.searchParams.get('offset')??0),final:url.searchParams.get('final')==='1',samples,signal:abort.signal});
        return json(res,out.ready?200:202,out);
      }
      return json(res,404,{error:'not found'});
    }catch(e){if(!res.headersSent)json(res,e.status??502,{error:e.message});else res.destroy()}
  };
  handler.emitReply=(sessionId,text)=>{if(typeof text!=='string'||!text)return;for(const [res,sid]of listeners)if(sid===sessionId){try{res.write(`data: ${JSON.stringify({type:'reply',sessionId,text})}\n\n`)}catch{listeners.delete(res)}}};
  handler.hasListeners=sessionId=>[...listeners.values()].includes(sessionId);
  handler.dispose=()=>{disposed=true;for(const c of controllers)c.abort();for(const res of listeners.keys())res.end();listeners.clear();controllers.clear()};
  return handler;
}
export async function* tapReply(source,emit){
  let text='';for await(const chunk of await source){
    if(chunk.type==='text-delta'&&typeof chunk.text==='string')text=(text+chunk.text).slice(0,20000);
    if(chunk.type==='finish'&&text){try{emit(text)}catch{/* Reading aloud must not break the Agent stream. */}text=''}
    yield chunk;
  }
}
