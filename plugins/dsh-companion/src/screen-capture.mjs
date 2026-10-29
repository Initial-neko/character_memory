/** Permission/capture ownership only; never sends frames or starts a model itself. */
export class ScreenCapture {
  #call;#media;#revision=0;#stream=null;#ended=null;#video=null;
  constructor({call,mediaDevices=globalThis.navigator?.mediaDevices}){this.#call=call;this.#media=mediaDevices}
  get stream(){return this.#stream}
  token(){return Object.freeze({call:this.#call.token(),screenRevision:this.#revision})}
  accepts(token){return !!this.#stream&&token?.screenRevision===this.#revision&&this.#call.accepts(token.call)&&this.#call.snapshot.screen==='ON'}
  async chooseFromGesture(){
    if(this.#call.snapshot.phase!=='ACTIVE')throw Error('start call before choosing screen');
    if(typeof this.#media?.getDisplayMedia!=='function')throw Error('host screen picker unavailable');
    this.stop();const revision=++this.#revision,callToken=this.#call.token();
    // Invoke before any await: browser transient user activation is required.
    const stream=await this.#media.getDisplayMedia({video:true,audio:false});
    if(revision!==this.#revision||!this.#call.accepts(callToken)){for(const t of stream.getTracks())t.stop();return null}
    if(!stream.getVideoTracks().length){for(const t of stream.getTracks())t.stop();throw Error('screen source has no video')}
    this.#stream=stream;this.#ended=()=>this.stop();
    for(const track of stream.getTracks())track.addEventListener('ended',this.#ended);
    this.#call.own('screen',()=>this.#clear(),callToken);
    this.#call.screenStarted(stream.getVideoTracks()[0].label||'选定画面');return stream;
  }
  #clear(){
    this.#revision++;
    const stream=this.#stream,ended=this.#ended;this.#stream=null;this.#ended=null;
    if(this.#video){this.#video.pause();this.#video.srcObject=null;this.#video=null}
    if(stream)for(const track of stream.getTracks()){track.removeEventListener('ended',ended);track.stop()}
  }
  stop(){this.#clear();this.#call.stopScreen()}
  async captureFrame({maxEdge=1280,quality=.8,document=globalThis.document}={}){
    const token=this.token();if(!this.accepts(token))throw Error('screen sharing is off');
    if(!Number.isInteger(maxEdge)||maxEdge<256||maxEdge>1920||!Number.isFinite(quality)||quality<.3||quality>1)throw Error('invalid frame encoding bounds');
    const video=this.#video??document.createElement('video');this.#video=video;
    video.muted=true;video.playsInline=true;video.srcObject=this.#stream;await video.play();
    if(!this.accepts(token))throw Error('screen capture was stopped');
    const w=video.videoWidth,h=video.videoHeight;if(!w||!h)throw Error('screen frame not ready');
    const scale=Math.min(1,maxEdge/Math.max(w,h));const canvas=document.createElement('canvas');
    canvas.width=Math.max(1,Math.round(w*scale));canvas.height=Math.max(1,Math.round(h*scale));
    const context=canvas.getContext('2d');if(!context)throw Error('frame encoder unavailable');context.drawImage(video,0,0,canvas.width,canvas.height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',quality));
    if(!this.accepts(token))throw Error('screen capture was stopped');if(!blob||blob.size>4*1024*1024)throw Error('invalid/oversized screen frame');
    return {blob,width:canvas.width,height:canvas.height,capturedAt:new Date().toISOString(),token};
  }
}
