import {parentPort,workerData} from 'node:worker_threads';
import {createRequire} from 'node:module';
import path from 'node:path';
const require=createRequire(import.meta.url);
try{
  const sherpa=require(workerData.modulePath),started=performance.now();
  const recognizer=new sherpa.OfflineRecognizer({featConfig:{sampleRate:16000,featureDim:80},modelConfig:{senseVoice:{model:path.join(workerData.modelDir,'model.int8.onnx'),language:'auto',useInverseTextNormalization:1},tokens:path.join(workerData.modelDir,'tokens.txt'),numThreads:2,provider:'cpu',debug:0}});
  parentPort.postMessage({type:'ready',loadMs:Math.round(performance.now()-started),version:sherpa.version});
  let busy=false;
  parentPort.on('message',async message=>{
    if(busy){parentPort.postMessage({id:message.id,error:'recognizer busy'});return}
    busy=true;
    try{
      const stream=recognizer.createStream();
      stream.acceptWaveform({samples:new Float32Array(message.samples),sampleRate:16000});
      const result=await recognizer.decodeAsync(stream);
      parentPort.postMessage({id:message.id,text:result.text??''});
      // Native stream handles are managed by the installed Node addon.
    }catch(e){parentPort.postMessage({id:message.id,error:String(e.message??e)})}
    finally{busy=false}
  });
}catch(e){parentPort.postMessage({type:'failed',error:String(e.message??e)})}
