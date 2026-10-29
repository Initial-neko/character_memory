import {createServer} from 'node:http';
import {trusted} from './http.mjs';
const json=(res,status,data)=>{res.writeHead(status,{'content-type':'application/json; charset=utf-8','cache-control':'no-store'});res.end(JSON.stringify(data))};
/** A single warm recognizer behind HTTP; every utterance gets its own native stream. */
export function createAsrService(engine){
  let busy=false,requests=0;
  return createServer(async(req,res)=>{
    try{
      if(!trusted(req)){req.resume();return json(res,403,{error:'loopback and same-origin access required'})}
      const url=new URL(req.url,`http://${req.headers.host}`),status=engine.status();
      if(url.pathname==='/health'&&req.method==='GET')return json(res,200,{ok:true,...status,busy:busy||!!status.busy,requests,protocol:'pcm16k-final',sampleRate:16000,channels:1,format:'float32-le'});
      if(url.pathname!=='/asr')return json(res,404,{error:'not found'});
      if(req.method!=='POST')return json(res,405,{error:'POST required'});
      const sid=url.searchParams.get('sessionId');
      if(!sid||sid.length>200||url.searchParams.get('final')!=='1'||req.headers['content-type']!=='application/octet-stream'){req.resume();return json(res,400,{error:'sessionId, final=1 and Float32 LE PCM required'})}
      if(!status.ready){req.resume();return json(res,status.error?503:202,{loading:!status.error,error:status.error??null})}
      if(busy||status.busy){req.resume();return json(res,429,{error:'recognizer busy; not queued or retried'})}
      busy=true;
      try{
        let length=0;const chunks=[];
        for await(const chunk of req){length+=chunk.length;if(length>16000*60*4)return json(res,413,{error:'maximum utterance is 60 seconds'});chunks.push(chunk)}
        if(!length||length%4)return json(res,400,{error:'invalid PCM length'});
        const raw=Buffer.concat(chunks),samples=new Float32Array(length/4);
        for(let i=0;i<samples.length;i++){samples[i]=raw.readFloatLE(i*4);if(!Number.isFinite(samples[i])||Math.abs(samples[i])>1)return json(res,400,{error:'invalid PCM sample'})}
        requests++;const started=performance.now(),text=await engine.recognize(samples);
        if(typeof text!=='string')throw Error('recognizer returned invalid text');
        if(!res.destroyed)json(res,200,{text,elapsedMs:Math.round(performance.now()-started)});
      }finally{busy=false}
    }catch(e){if(!res.headersSent&&!res.destroyed)json(res,502,{error:String(e.message??e)});else res.destroy()}
  });
}
