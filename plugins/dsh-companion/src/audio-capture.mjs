/** Area averaging for bounded mono PCM; real ASR accuracy remains a live gate. */
export function resample16k(input,rate){
  if(!(input instanceof Float32Array)||!Number.isFinite(rate)||rate<16000||rate>192000||input.some(x=>!Number.isFinite(x)))throw Error('invalid PCM sample rate/data');
  const ratio=rate/16000,n=Math.floor(input.length/ratio),out=new Float32Array(n);
  for(let i=0;i<n;i++){const start=i*ratio,end=(i+1)*ratio;let sum=0;for(let j=Math.floor(start);j<Math.ceil(end);j++)sum+=input[j]*Math.max(0,Math.min(j+1,end)-Math.max(j,start));out[i]=Math.max(-1,Math.min(1,sum/ratio))}return out;
}
export async function recordMic({deviceId='',signal,onLimit=()=>{},mediaDevices=globalThis.navigator?.mediaDevices,AudioContext=globalThis.AudioContext}={}){
  if(!mediaDevices?.getUserMedia||!AudioContext)throw Error('host microphone capture unavailable');
  const stream=await mediaDevices.getUserMedia({audio:{...(deviceId?{deviceId:{exact:deviceId}}:{}),channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
  const closeTracks=()=>stream.getTracks().forEach(t=>t.stop());
  if(signal?.aborted){closeTracks();throw Error('recording cancelled')}
  let context,source,node,gain,timer,closed=false,result=null;const chunks=[];let count=0;
  const cleanup=()=>{if(closed)return;closed=true;clearTimeout(timer);signal?.removeEventListener('abort',cancel);if(node)node.onaudioprocess=null;for(const x of [node,source,gain])try{x?.disconnect()}catch{}closeTracks();context?.close().catch(()=>{})};
  const cancel=()=>{cleanup();chunks.length=0;result=new Float32Array(0)};
  try{
    context=new AudioContext();await context.resume();if(signal?.aborted){cancel();throw Error('recording cancelled')}
    source=context.createMediaStreamSource(stream);node=context.createScriptProcessor(4096,1,1);gain=context.createGain();gain.gain.value=0;
    node.onaudioprocess=event=>{if(closed)return;const data=event.inputBuffer.getChannelData(0);if(count+data.length>context.sampleRate*60){onLimit();return}chunks.push(data.slice());count+=data.length};
    source.connect(node);node.connect(gain);gain.connect(context.destination);signal?.addEventListener('abort',cancel,{once:true});
    timer=setTimeout(onLimit,60000);
    return {mute(value){for(const t of stream.getAudioTracks())t.enabled=!value},cancel,stop(){if(result)return result;const rate=context.sampleRate;cleanup();const all=new Float32Array(count);let offset=0;for(const chunk of chunks){all.set(chunk,offset);offset+=chunk.length}chunks.length=0;result=resample16k(all,rate);return result}};
  }catch(e){cancel();throw e}
}
