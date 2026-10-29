import {CallController} from './call-controller.mjs';
import {ScreenCapture} from './screen-capture.mjs';
import {HostBridge} from './host-bridge.mjs';
import {recordMic} from './audio-capture.mjs';
import {HELP,LABELS} from './config.mjs';
const base='/companion/api';
async function request(route,options={}){const r=await fetch(base+route,options);const data=await r.json();if(!r.ok)throw Error(data.error??`HTTP ${r.status}`);return data}
const post=data=>({method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});
const css=`.dsh-companion-root{font:14px/1.5 system-ui,sans-serif;color:var(--dsw-alias-label-primary,#36333f)}.dsh-companion-root button{font:inherit;cursor:pointer;border:1px solid #dbd8e5;border-radius:10px;background:var(--dsw-alias-bg-layer-1,#fff);color:inherit;padding:7px 11px}.dsh-companion-root button:disabled{opacity:.5;cursor:default}.dsh-companion-menu,.dsh-companion-panel,.dsh-companion-capsule{position:fixed;z-index:70;background:var(--dsw-alias-bg-layer-1,#fffcf8);border:1px solid #dfdbe7;box-shadow:0 8px 24px #25203018;border-radius:16px}.dsh-companion-menu{right:30px;bottom:94px;width:210px;padding:8px;display:grid;gap:4px}.dsh-companion-menu button{text-align:left;border:0}.dsh-companion-panel{right:22px;bottom:90px;width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 130px);overflow:auto;padding:18px}.dsh-companion-panel h2{font-size:17px;margin:0}.dsh-companion-panel header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.dsh-companion-panel label{display:block;font-size:13px;margin:10px 0}.dsh-companion-panel input:not([type=checkbox]),.dsh-companion-panel textarea{width:100%;box-sizing:border-box;border:1px solid #cec9da;background:var(--dsw-alias-bg-base,#fff);color:inherit;border-radius:8px;padding:8px;font:inherit}.dsh-companion-panel small{display:block;opacity:.7}.dsh-companion-panel footer{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.dsh-companion-panel img,.dsh-companion-panel video{width:100%;max-height:260px;object-fit:contain;border-radius:10px}.dsh-companion-capsule{top:72px;right:24px;display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:30px;background:var(--dsw-alias-bg-layer-1,#f2effc)}.dsh-companion-capsule strong{max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}.dsh-companion-root .end{background:#b64d61;color:white;border-color:transparent}.dsh-companion-status{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:12px;font-size:13px}.dsh-companion-avatar{font-size:23px;color:#7b72a1}.dsh-companion-trigger{border-radius:16px!important;padding:5px 10px!important}.dsh-companion-config{border:0;padding:0;margin:0}`;
export function createComponents(React){
  const h=React.createElement,call=new CallController(),screen=new ScreenCapture({call});
  const listeners=new Set();let state={menu:false,panel:null,config:null,status:'',recording:false,frame:null,preview:null,mode:'voice',elapsed:0};
  let propsRef={},recorder=null,recordToken=null,transcriptToken=null,clock=null,triggerElement=null;
  const bridge=new HostBridge(()=>({sessionId:propsRef.sessionId,input:propsRef.input,inputActions:propsRef.inputActions,addFiles:propsRef.addFiles}));
  function update(patch){state={...state,...patch};for(const fn of listeners)fn()}
  const subscribe=fn=>{listeners.add(fn);return()=>listeners.delete(fn)},snapshot=()=>state;
  async function load(){const config=await request('/config');update({config});return config}
  function clearPreview(){if(state.preview)URL.revokeObjectURL(state.preview);update({preview:null,frame:null})}
  function end(){call.end('user');clearInterval(clock);clock=null;recorder=null;clearPreview();update({panel:null,menu:false,recording:false,status:'已结束，录音、播放和屏幕采集已停止'})}
  async function startRecording(){
    if(!state.config?.value.asrEndpoint)throw Error('请先配置ASR，尚未请求麦克风权限');
    const token=call.token(),abort=new AbortController();transcriptToken=bridge.current();recordToken=token;
    call.own('recording-request',()=>abort.abort(),token);
    const rec=await recordMic({deviceId:state.config.value.inputDeviceId,signal:abort.signal,onLimit:()=>finishRecording().catch(showError)});
    if(!call.accepts(token)){rec.cancel();return}
    recorder=rec;call.own('microphone',()=>{rec.cancel();recorder=null;update({recording:false})},token);
    rec.mute(call.snapshot.inputMuted);update({recording:true,status:'正在录音；完成后转为可编辑草稿，不自动发送'});
  }
  async function finishRecording(){
    const rec=recorder;if(!rec)return;recorder=null;const samples=rec.stop(),token=recordToken,draft=transcriptToken;
    update({recording:false,status:'正在识别…'});const abort=new AbortController();call.own('asr-request',()=>abort.abort(),token);
    const url=`/asr?sessionId=${encodeURIComponent(token.sessionId)}&epoch=${token.generation}&final=1`;
    const r=await fetch(base+url,{method:'POST',headers:{'content-type':'application/octet-stream'},body:samples.buffer,signal:abort.signal});
    const out=await r.json();if(!call.accepts(token))return;if(!r.ok)throw Error(out.error??`ASR HTTP ${r.status}`);
    if(!out.ready){update({status:'ASR模型尚未就绪；不会自动下载或重发'});return}
    update({status:bridge.applyTranscript(draft,out.text)?'转写已放入原生草稿，请检查后发送':'草稿或会话已变化，未覆盖。识别结果：'+out.text});
  }
  function showError(e){update({status:e.message??String(e)})}
  function replies(token){
    const events=new EventSource(`${base}/events?sessionId=${encodeURIComponent(token.sessionId)}`);let queue=Promise.resolve(),audio=null;const abort=new AbortController();
    call.own('replies',()=>{events.close();abort.abort();audio?.pause();if(audio)audio.src=''},token);
    events.onmessage=event=>{if(!call.accepts(token))return;let data;try{data=JSON.parse(event.data)}catch{return}if(data.sessionId!==token.sessionId)return;
      queue=queue.then(async()=>{
        if(!call.accepts(token))return;const text=data.text.replace(/```[\s\S]*?```/g,'').trim();if(!text)return;
        for(let i=0;i<text.length;i+=380){
          if(!call.accepts(token))return;recorder?.mute(true);update({status:'正在播放 GSV 语音…'});
          const r=await fetch(base+'/tts',{...post({text:text.slice(i,i+380)}),signal:abort.signal});if(!r.ok){const error=await r.json();throw Error(error.error??`GSV HTTP ${r.status}`)}
          const blob=await r.blob();if(!call.accepts(token))return;const url=URL.createObjectURL(blob);
          try{audio=new Audio(url);await new Promise((resolve,reject)=>{audio.onended=resolve;audio.onerror=()=>reject(Error('音频播放失败'));abort.signal.addEventListener('abort',resolve,{once:true});audio.play().catch(reject)})}finally{URL.revokeObjectURL(url);audio=null}
        }
        if(call.accepts(token)){recorder?.mute(call.snapshot.inputMuted);update({status:'播放结束，可以继续说话'})}
      }).catch(e=>{if(call.accepts(token)){recorder?.mute(call.snapshot.inputMuted);showError(e)}});
    };
  }
  async function begin(mode){
    const config=await load(),current=bridge.current();if(!current.sessionId)throw Error('请先打开一个原生DSH会话');
    if(mode==='voice'&&!config.value.asrEndpoint)throw Error('请先配置ASR；不会自动下载模型');
    const token=call.start({sessionId:current.sessionId,personaRevision:'native-session',configRevision:config.revision});
    update({panel:'call',menu:false,mode,elapsed:0,status:'正在检查连接…'});
    try{
      if(mode==='voice'){const health=await request('/health',post({capability:'gsv'}));if(!health.ready)throw Error(health.reason??'GSV未就绪')}
      if(!call.accepts(token))return;call.connected();replies(token);
      clearInterval(clock);clock=setInterval(()=>{if(call.snapshot.phase==='ACTIVE')update({elapsed:state.elapsed+1})},1000);
      call.own('clock',()=>{clearInterval(clock);clock=null},token);
      if(mode==='voice')await startRecording();else update({status:'请选择摄像头或共享窗口；尚未采集画面'});
    }catch(e){call.fail(e,token);throw e}
  }
  async function chooseScreen(){if(call.snapshot.phase!=='ACTIVE')throw Error('请先开启陪伴会话');const stream=await screen.chooseFromGesture();if(!stream)return;clearPreview();update({status:'已选择画面。点击读取当前画面以截取本地预览。'})}
  async function frame(){const config=state.config.value;const f=await screen.captureFrame({maxEdge:config.screenMaxEdge,quality:config.screenQuality});clearPreview();update({frame:f,preview:URL.createObjectURL(f.blob),status:'本地预览，尚未发送到模型。请确认画面内容。'})}
  async function sendFrame(){const f=state.frame;if(!f||!screen.accepts(f.token))throw Error('画面已失效，请重新读取');if(!state.config.value.visionEnabled)throw Error('请先确认DSH当前模型支持图像，并在配置中启用视觉');const token=bridge.current();await bridge.submit({token,text:token.draft||'请描述这张画面中的内容。',files:[new File([f.blob],'screen.jpg',{type:'image/jpeg'})]});clearPreview();update({status:'画面通过原生DSH输入提交；请查看原生回复'})}
  function Trigger(){const s=React.useSyncExternalStore(subscribe,snapshot);return h('div',{className:'dsh-companion-root'},h('button',{ref:element=>{triggerElement=element},type:'button',className:'dsh-companion-trigger','aria-label':'陪伴','aria-expanded':s.menu,onClick:()=>update({menu:!state.menu}),title:'打开语音、屏幕、生图和陪伴设置'},'✿ 陪伴'))}
  function Settings(){
    const s=React.useSyncExternalStore(subscribe,snapshot),[draft,setDraft]=React.useState(null);
    React.useEffect(()=>{let alive=true;load().then(c=>{if(alive)setDraft({...c.value})}).catch(showError);return()=>{alive=false}},[]);
    if(!draft)return h('p',null,'读取配置…');
    const save=async()=>{const c=await request('/config',{...post({expectedRevision:state.config.revision,changes:draft}),method:'PATCH'});update({config:c,status:'配置已保存；当前通话需重新连接后生效'});call.configure(c.revision)};
    return h('div',{className:'dsh-companion-root'},h('form',{onSubmit:e=>{e.preventDefault();save().catch(showError)},className:'dsh-companion-config'},Object.keys(draft).map(key=>h('label',{key,title:HELP[key]},LABELS[key],h('input',{type:typeof draft[key]==='boolean'?'checkbox':typeof draft[key]==='number'?'number':'text',...(typeof draft[key]==='boolean'?{checked:draft[key]}:{value:draft[key]}),step:'any',disabled:['confirmTranscript','screenPreview','screenMode','asrProtocol'].includes(key),onChange:e=>setDraft({...draft,[key]:typeof draft[key]==='boolean'?e.target.checked:typeof draft[key]==='number'?Number(e.target.value):e.target.value}),title:HELP[key]}),h('small',null,HELP[key]))),h('footer',null,h('button',{type:'submit',title:'保存非秘密配置，不开启设备或生成内容'},'保存配置'),h('button',{type:'button',onClick:()=>request('/health',post({capability:'gsv'})).then(x=>update({status:x.ready?'GSV健康检查通过':'GSV未就绪：'+x.reason})).catch(showError),title:'仅检查GSV健康，不生成音频'},'检查GSV'))),h('p',{role:'status'},s.status));
  }
  function Overlay(props){
    const input=typeof props.useInput==='function'?props.useInput(x=>x):{};propsRef={...props,input};
    const s=React.useSyncExternalStore(subscribe,snapshot),c=React.useSyncExternalStore(call.subscribe,call.getSnapshot);
    React.useEffect(()=>{call.navigate(props.sessionId)},[props.sessionId]);
    React.useEffect(()=>{const key=e=>{if(e.key==='Escape'){update({menu:false});triggerElement?.focus()}};const outside=e=>{if(state.menu&&!e.target.closest?.('.dsh-companion-menu,.dsh-companion-trigger'))update({menu:false})};document.addEventListener('keydown',key);document.addEventListener('pointerdown',outside);return()=>{document.removeEventListener('keydown',key);document.removeEventListener('pointerdown',outside)}},[]);
    const button=(label,fn,opts={})=>h('button',{type:'button',onClick:()=>{Promise.resolve().then(fn).catch(showError)},...opts},label);
    const expanded=s.panel&&!(s.panel==='call'&&c.minimized);
    return h('div',{className:'dsh-companion-root'},h('style',null,css),
      s.menu&&h('div',{className:'dsh-companion-menu',role:'menu'},button('语音通话',()=>begin('voice')),button('视频 / 屏幕',()=>begin('screen')),button('Live2D 桌宠',()=>update({menu:false,panel:'avatar',status:'角色素材与渲染器尚未接入，不会伪装为已启动'})),button('画图',()=>update({menu:false,panel:'image',status:'生图适配正在从独立MVP接入当前会话'})),button('设置',()=>update({menu:false,panel:'settings'}))),
      c.phase!=='IDLE'&&h('div',{className:'dsh-companion-capsule',role:'region','aria-label':'通话胶囊'},h('span',{className:'dsh-companion-avatar','aria-hidden':true},'✿'),h('strong',null,c.phase==='PAUSED'?'通话已暂停':c.phase==='ERROR'?'连接失败':s.mode==='voice'?'语音陪伴':'屏幕陪伴'),h('span',null,`${Math.floor(s.elapsed/60)}:${String(s.elapsed%60).padStart(2,'0')}`),c.screen==='ON'&&h('span',{title:'正在采集所选画面'},'▣'),button(c.inputMuted?'开麦':'静音',()=>{call.muteInput(!c.inputMuted);recorder?.mute(call.snapshot.inputMuted)},{title:'仅控制麦克风输入，不结束通话'}),button('结束',end,{className:'end',title:'停止本插件录音、播放和屏幕采集'}),button('展开',()=>{call.expand();update({panel:'call'})},{title:'展开通话浮层'})),
      expanded&&h('section',{className:'dsh-companion-panel',role:'dialog','aria-label':s.panel==='settings'?'陪伴设置':'陪伴'},h('header',null,h('h2',null,s.panel==='settings'?'陪伴设置':'陪伴'),button(s.panel==='call'?'收起':'关闭',()=>{if(s.panel==='call')call.minimize();else update({panel:null})})),
        s.panel==='settings'?h(Settings):s.panel==='call'?h(React.Fragment,null,h('p',null,c.screen==='ON'?'正在共享：'+c.screenLabel:'画面未开启'),h('footer',null,button(s.recording?'完成录音':'再说一句',()=>s.recording?finishRecording():startRecording(),{disabled:c.phase!=='ACTIVE'}),button('选择共享窗口',chooseScreen,{disabled:c.phase!=='ACTIVE'}),button('读取当前画面',frame,{disabled:c.screen!=='ON'}),button('停止共享',()=>{screen.stop();clearPreview();update({status:'已停止屏幕采集；语音可继续'})},{disabled:c.screen!=='ON'})),s.preview&&h('div',null,h('img',{src:s.preview,alt:'发送前的屏幕预览'}),button('发送画面到DSH',sendFrame)),h('small',null,'收起继续通话；结束才停止所有采集。屏幕默认不发送。')):null,
        s.panel!=='settings'&&h('p',{className:'dsh-companion-status',role:'status'},s.status)),!expanded&&s.status&&c.phase==='IDLE'&&s.menu&&h('div',{role:'status'},s.status));
  }
  return {Trigger,Overlay,Settings,dispose(){end();call.dispose();listeners.clear()}};
}
