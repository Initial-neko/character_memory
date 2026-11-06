/** Pure ownership/state boundary. Provider and UI work must carry token(). */
export class CallController {
  #resources = new Map();
  #listeners = new Set();
  #disposed = false;
  #generation = 0;
  #snapshot = Object.freeze({phase:'IDLE',sessionId:null,personaRevision:null,configRevision:0,generation:0,minimized:false,inputMuted:false,screen:'OFF',screenLabel:'',cleanupErrors:[]});

  get snapshot(){return this.#snapshot}
  getSnapshot=()=>this.#snapshot;
  subscribe=(listener)=>{this.#listeners.add(listener);return()=>this.#listeners.delete(listener)};
  #set(patch){
    this.#snapshot=Object.freeze({...this.#snapshot,...patch,generation:this.#generation});
    for(const listener of this.#listeners){try{listener()}catch{/* A renderer cannot prevent resource cleanup. */}}
  }
  start({sessionId,personaRevision,configRevision}={}){
    if(this.#disposed)throw Error('controller disposed');
    if(typeof sessionId!=='string'||!sessionId||typeof personaRevision!=='string'||!personaRevision||!Number.isSafeInteger(configRevision)||configRevision<0)throw Error('valid session, persona and config revision required');
    if(!['IDLE','PAUSED','ERROR'].includes(this.snapshot.phase))throw Error('invalid call state');
    this.#releaseAll();this.#generation++;
    this.#set({phase:'CONNECTING',sessionId,personaRevision,configRevision,minimized:false,inputMuted:false,screen:'OFF',screenLabel:'',cleanupErrors:[],error:null});
    return this.token();
  }
  connected(){if(this.snapshot.phase!=='CONNECTING')throw Error('invalid call state');this.#set({phase:'ACTIVE'})}
  token(){const {sessionId,personaRevision,configRevision}=this.snapshot;return Object.freeze({sessionId,personaRevision,configRevision,generation:this.#generation})}
  accepts(token){return !!token&&!this.#disposed&&['ACTIVE','CONNECTING'].includes(this.snapshot.phase)&&['sessionId','personaRevision','configRevision','generation'].every(key=>token[key]===this.token()[key])}
  own(name,release,token=this.token()){
    if(typeof release!=='function')throw TypeError('resource requires cleanup');
    if(!this.accepts(token)){try{release()}catch{/* Already inactive; still refuse ownership. */}return false}
    this.#release(name);this.#resources.set(name,release);return true;
  }
  #release(name){const release=this.#resources.get(name);if(!release)return;this.#resources.delete(name);try{release()}catch(error){this.#snapshot=Object.freeze({...this.#snapshot,cleanupErrors:[...this.#snapshot.cleanupErrors,String(error.message??error)]})}}
  #releaseAll(){for(const name of [...this.#resources.keys()])this.#release(name)}
  minimize(){if(['ACTIVE','CONNECTING','PAUSED'].includes(this.snapshot.phase))this.#set({minimized:true})}
  expand(){this.#set({minimized:false})}
  muteInput(muted){if(this.snapshot.phase==='ACTIVE')this.#set({inputMuted:!!muted})}
  screenStarted(label){if(this.snapshot.phase!=='ACTIVE')throw Error('invalid call state');this.#set({screen:'ON',screenLabel:String(label)})}
  stopScreen(){this.#release('screen');this.#set({screen:'OFF',screenLabel:''})}
  #pause(reason){this.#generation++;this.#releaseAll();this.#set({phase:'PAUSED',screen:'OFF',screenLabel:'',reason})}
  navigate(sessionId){if(this.snapshot.phase!=='IDLE'&&sessionId!==this.snapshot.sessionId)this.#pause('session-changed')}
  configure(revision){if(!Number.isSafeInteger(revision)||revision<0)throw Error('invalid config revision');if(this.snapshot.phase!=='IDLE'&&revision!==this.snapshot.configRevision)this.#pause('config-changed')}
  fail(error,token=this.token()){if(this.accepts(token)){this.#generation++;this.#releaseAll();this.#set({phase:'ERROR',screen:'OFF',screenLabel:'',error:String(error.message??error)})}}
  end(reason='user'){
    if(this.snapshot.phase==='IDLE')return;
    this.#generation++;this.#releaseAll();this.#set({phase:'IDLE',sessionId:null,personaRevision:null,screen:'OFF',screenLabel:'',inputMuted:false,minimized:false,reason});
  }
  dispose(){this.end('unload');this.#disposed=true;this.#listeners.clear()}
}
