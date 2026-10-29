export const DEFAULTS=Object.freeze({gsvEndpoint:'http://127.0.0.1:9014',gsvVoice:'murasame',gsvSpeed:1,gsvLanguage:'zh',asrEndpoint:'',asrProtocol:'dsh-voice-mode',confirmTranscript:true,inputDeviceId:'',screenMode:'manual',screenMaxEdge:1280,screenQuality:0.8,screenPreview:true,visionEnabled:false,codexExecutable:'',avatarEnabled:false,avatarUrl:'',avatarScale:1});
export const HELP=Object.freeze({gsvEndpoint:'现有GSV HTTP服务地址；保存不会启动服务或生成音频。',gsvVoice:'GSV现有音色名，须与服务配置一致。',gsvSpeed:'仅用于之后的合成请求；0.5到2。',gsvLanguage:'GSV合成语言。',asrEndpoint:'已配置ASR服务地址；不自动下载模型。',asrProtocol:'当前适配DSH voice-mode的16kHz Float32 PCM协议。',confirmTranscript:'识别结果必须确认后发送，当前固定开启。',inputDeviceId:'浏览器麦克风设备；开始录音才请求权限。',screenMode:'当前仅手动读取选定画面，不持续截屏。',screenMaxEdge:'发送前图片长边上限，256到1920。',screenQuality:'JPEG编码质量，0.3到1。',screenPreview:'发送屏幕图像前必须预览，当前固定开启。',visionEnabled:'确认当前DSH模型可接收图像后开启；不会自动切换模型。',codexExecutable:'现有Codex可执行文件路径；只在明确生成时调用。',avatarEnabled:'按需开启形象，不代表通话已连接。',avatarUrl:'已获授权的形象素材地址；不执行远端脚本。',avatarScale:'形象显示倍率，0.5到2。'});
function endpoint(value,key,empty=false){
  if(empty&&value==='')return '';
  if(typeof value!=='string'||value.length>2000)throw Error(`invalid ${key}`);
  let url;try{url=new URL(value)}catch{throw Error(`invalid ${key}`)}
  if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw Error(`invalid ${key}`);
  return url.href.replace(/\/$/,'');
}
export function validateConfig(input){
  if(!input||typeof input!=='object'||Array.isArray(input))throw Error('invalid config');
  for(const key of Object.keys(input))if(!Object.hasOwn(DEFAULTS,key))throw Error(`unknown config field ${key}`);
  const c={...DEFAULTS,...input};
  c.gsvEndpoint=endpoint(c.gsvEndpoint,'gsvEndpoint');c.asrEndpoint=endpoint(c.asrEndpoint,'asrEndpoint',true);
  if(c.avatarUrl)c.avatarUrl=endpoint(c.avatarUrl,'avatarUrl');
  for(const key of ['gsvVoice','gsvLanguage','inputDeviceId','codexExecutable','avatarUrl'])if(typeof c[key]!=='string'||c[key].length>2000||/[\r\n\0]/.test(c[key]))throw Error(`invalid ${key}`);
  if(!c.gsvVoice||c.gsvVoice.length>100)throw Error('invalid gsvVoice');
  for(const [key,min,max] of [['gsvSpeed',0.5,2],['screenQuality',0.3,1],['avatarScale',0.5,2],['screenMaxEdge',256,1920]])if(typeof c[key]!=='number'||!Number.isFinite(c[key])||c[key]<min||c[key]>max)throw Error(`invalid ${key}`);
  if(!Number.isInteger(c.screenMaxEdge))throw Error('invalid screenMaxEdge');
  for(const key of ['visionEnabled','avatarEnabled'])if(typeof c[key]!=='boolean')throw Error(`invalid ${key}`);
  if(c.confirmTranscript!==true||c.screenPreview!==true||c.screenMode!=='manual'||c.asrProtocol!=='dsh-voice-mode')throw Error('unsupported automatic mode or provider protocol');
  return Object.freeze(c);
}
export class ConfigStore {
  #snapshot;#persist;#tail=Promise.resolve();
  constructor({initial={revision:0,value:{}},persist=async()=>{}}={}){
    if(!Number.isSafeInteger(initial.revision)||initial.revision<0)throw Error('invalid config revision');
    this.#snapshot=Object.freeze({revision:initial.revision,value:validateConfig(initial.value)});this.#persist=persist;
  }
  get snapshot(){return this.#snapshot}
  update(expectedRevision,patch){
    const work=this.#tail.then(async()=>{
      if(!Number.isSafeInteger(expectedRevision)||expectedRevision!==this.#snapshot.revision)throw Object.assign(Error('configuration conflict; reread before saving'),{status:409});
      if(!patch||typeof patch!=='object'||Array.isArray(patch))throw Error('invalid config patch');
      const value=validateConfig({...this.#snapshot.value,...patch});
      const next=Object.freeze({revision:this.#snapshot.revision+1,value});
      await this.#persist(next);this.#snapshot=next;return next;
    });
    this.#tail=work.catch(()=>{});return work;
  }
}
