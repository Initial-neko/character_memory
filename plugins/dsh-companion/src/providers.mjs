/** Existing provider protocols only. No model startup/download, retry or alternate TTS. */
function requestSignal(signal,ms){return signal?AbortSignal.any([signal,AbortSignal.timeout(ms)]):AbortSignal.timeout(ms)}
function base(endpoint){const u=new URL(endpoint);if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw Error('invalid provider endpoint');return u.href.replace(/\/$/,'')}
async function boundedBytes(response,max=20*1024*1024){
  if(Number(response.headers.get('content-length'))>max)throw Error('provider output too large');
  const reader=response.body.getReader();let total=0;const chunks=[];
  try{for(;;){const {done,value}=await reader.read();if(done)break;total+=value.length;if(total>max)throw Error('provider output too large');chunks.push(value)}}catch(e){await reader.cancel().catch(()=>{});throw e}finally{reader.releaseLock()}
  const result=new Uint8Array(total);let offset=0;for(const b of chunks){result.set(b,offset);offset+=b.length}return result;
}
async function smallJson(response){return JSON.parse(new TextDecoder().decode(await boundedBytes(response,128*1024)))}
export async function checkGsv(endpoint,{signal}={}){
  try{const r=await fetch(base(endpoint)+'/health',{signal:requestSignal(signal,3000),redirect:'error'});if(!r.ok)return {ready:false,reason:`GSV HTTP ${r.status}`};const d=await smallJson(r);const ready=d.ok===true&&d.ready===true;return {ready,...(!ready?{reason:typeof d.reason==='string'&&d.reason?d.reason:'GSV readiness not confirmed'}:{}),details:d}}catch(e){return {ready:false,reason:e.message}}
}
export async function checkAsr(endpoint,{signal,protocol='pcm16k-final'}={}){
  if(!endpoint)return {ready:false,reason:'ASR尚未配置'};
  if(protocol!=='pcm16k-final')return {ready:false,reason:'旧 voice-mode 协议依赖其独立激活状态；请使用 pcm16k-final 服务'};
  try{const r=await fetch(base(endpoint)+'/health',{signal:requestSignal(signal,3000),redirect:'error'});if(!r.ok)return {ready:false,reason:`ASR HTTP ${r.status}`};const d=await smallJson(r);const ready=d.ok===true&&d.ready===true&&d.protocol===protocol&&d.sampleRate===16000&&d.channels===1&&d.format==='float32-le';return {ready,...(!ready?{reason:d.error||'ASR readiness or PCM protocol not confirmed'}:{}),details:d}}catch(e){return {ready:false,reason:e.message}}
}
export async function synthesizeGsv({endpoint,text,voice,language='zh',speed=1,signal}){
  if(typeof text!=='string'||!text.trim()||text.length>400)throw Error('invalid GSV text (1–400 characters)');
  if(typeof voice!=='string'||!voice||voice.length>100||typeof speed!=='number'||speed<.5||speed>2)throw Error('invalid GSV voice/speed');
  const r=await fetch(base(endpoint)+'/v1/tts',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text,voice,language,speed}),signal:requestSignal(signal,180000),redirect:'error'});
  if(!r.ok)throw Error(`GSV HTTP ${r.status}; not retried`);
  const bytes=await boundedBytes(r);
  if(bytes.length<44||new TextDecoder().decode(bytes.subarray(0,4))!=='RIFF'||new TextDecoder().decode(bytes.subarray(8,12))!=='WAVE')throw Error('invalid GSV WAV output');
  return {mime:'audio/wav',bytes,voice:r.headers.get('x-tts-voice')??voice};
}
export async function transcribeAsr({endpoint,sessionId,samples,epoch=0,offset=0,final=true,signal}){
  if(!(samples instanceof Float32Array)||samples.length>16000*60||samples.some(v=>!Number.isFinite(v)||v< -1||v>1))throw Error('invalid Float32 mono 16k PCM');
  if(typeof sessionId!=='string'||!sessionId||!Number.isSafeInteger(epoch)||epoch<0||!Number.isSafeInteger(offset)||offset<0)throw Error('invalid ASR session/epoch/offset');
  const url=new URL(base(endpoint)+'/asr');for(const [k,v] of Object.entries({sessionId,epoch,offset,final:final?'1':'0',manual:'1'}))url.searchParams.set(k,String(v));
  const pcm=new Uint8Array(samples.length*4),view=new DataView(pcm.buffer);samples.forEach((v,i)=>view.setFloat32(i*4,v,true));
  const r=await fetch(url,{method:'POST',headers:{'content-type':'application/octet-stream'},body:pcm,signal:requestSignal(signal,30000),redirect:'error'});
  if(r.status===202)return {ready:false,loading:true,text:null};
  if(!r.ok)throw Error(`ASR HTTP ${r.status}; current voice session and model readiness required; not retried`);
  const d=await smallJson(r);if(typeof d.text!=='string')throw Error('invalid ASR response');
  return {ready:true,text:d.text,endpoint:d.endpoint===true};
}
