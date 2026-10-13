(() => {
  "use strict";
  const CM=window.CM, clientId=crypto.randomUUID();
  const area=document.createElement("section");
  area.setAttribute("aria-label","人物来电邀请");area.setAttribute("aria-live","polite");area.hidden=true;
  CM.dom.composer.before(area);
  let session=null, busy=false, selecting=false, generation=0, refreshRunning=false, enabled=true;
  function message(value){area.hidden=false;area.replaceChildren();const p=document.createElement("p");p.textContent=value;area.append(p);}
  async function post(id, action, body={}){
    return CM.api(`/v1/call-invitations/${encodeURIComponent(id)}/${action}`,{method:"POST",body:JSON.stringify(body),keepalive:action==="cancel"});
  }
  async function cancelSession(){
    const old=session, showRefresh=!busy;session=null;
    if(!old)return;
    try {await post(old.id,"cancel",{session_token:old.token});}catch(error){console.warn("call invitation cleanup",error);}
    if(showRefresh)await refresh();
  }
  async function accept(item){
    if(busy || session || CM.features.voice?.state?.active){message("已有通话正在进行，请先结束。邀请仍可稍后处理。");return;}
    busy=true;const ticket=generation;
    try {
      const result=await post(item.id,"accept",{client_id:clientId});
      if(!result.changed){message("这条邀请已经处理；不会再次开启麦克风。");return;}
      const current={id:item.id,token:result.session_token,character:item.character_id,conversation:item.conversation_id};session=current;
      if(ticket!==generation){await cancelSession();return;}
      selecting=true;
      try {
        localStorage.setItem(`character-memory:conversation:${item.character_id}`,item.conversation_id);
        CM.closeDirectStream();await CM.switchCharacter(item.character_id);CM.connectDirectStream();
      } finally {selecting=false;}
      if(session!==current || CM.isGroupConversation() || CM.state.characterId!==item.character_id){await cancelSession();return;}
      message("已接受邀请，正在准备麦克风；尚未表示已经通话。");
      await CM.features.voice.start();
      if(session!==current){await CM.features.voice.stop();return;}
      if(!CM.features.voice.state.active || !CM.features.voice.state.micActive){
        await CM.features.voice.stop();message("麦克风未开启，邀请已取消；没有成功通话记录。");return;
      }
      // Permission may arrive after expiry, hangup or a different tab's action.
      await post(current.id,"lease",{session_token:current.token,connected:true});
      if(session!==current){await CM.features.voice.stop();return;}
      message("应用内语音已开启。仅实际识别和回复的消息进入既有聊天记录。");
    } catch(error){
      if(session){await CM.features.voice?.stop?.();await cancelSession();}
      message(`未接通：${error.message}`);
    } finally {busy=false;}
  }
  async function refresh(){
    if(refreshRunning || document.hidden)return;
    refreshRunning=true;
    try {
      const data=await CM.api('/v1/call-invitations');enabled=data.enabled;
      if(session){
        const live=data.items.find(i=>i.id===session.id && i.status==="ACCEPTED");
        if(!live){await CM.features.voice?.stop?.();await cancelSession();message("邀请已结束；不会自动重新接通。");}
        return;
      }
      if(busy)return;
      area.replaceChildren();area.hidden=!data.items.length;
      for(const item of data.items){
        const card=document.createElement("article"), title=document.createElement("h3"), text=document.createElement("p"), scope=document.createElement("p");
        title.textContent=`${CM.state.characters.find(c=>c.id===item.character_id)?.name||item.character_id} 邀请通话`;
        text.textContent=item.message;
        scope.textContent=item.status==="PENDING"?`应用内 Direct 邀请 · ${new Date(item.expires_at).toLocaleTimeString()} 前有效。接受前不开麦；未接不代表聊过。`:"已接受邀请；本页不会自动接通。原页面失联后邀请会取消。";
        card.append(title,text,scope);
        if(item.reason){const reason=document.createElement("p");reason.textContent=`公开依据：${item.reason}`;card.append(reason);}
        if(item.status==="PENDING"){
          for(const [label,action] of [["接受并开启语音","accept"],["拒绝","decline"]]){
            const button=document.createElement("button");button.type="button";button.textContent=label;
            button.title=action==="accept"?"先接受此人物的应用内 Direct 邀请，再请求麦克风并进入已有 ASR/TTS；不拨真实电话。":"保存拒绝状态，不请求麦克风，不生成通话经历。";
            button.onclick=async()=>{
              if(busy)return;button.disabled=true;
              if(action==="accept")await accept(item);
              else {busy=true;try {await post(item.id,"decline");}catch(error){message(error.message);}finally{busy=false;await refresh();}}
            };
            card.append(button);
          }
        }
        area.append(card);
      }
    } catch(error){if(!session && enabled)message("来电邀请暂不可读取；未开启麦克风。");}
    finally {refreshRunning=false;}
  }
  CM.on("ready",refresh);
  CM.on("voiceStopped",cancelSession);
  CM.on("conversationChanged",()=>{
    if(selecting)return;
    generation++;
    if(session && (CM.isGroupConversation() || CM.state.characterId!==session.character))CM.features.voice.stop().catch(console.error);
    else refresh();
  });
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
  setInterval(()=>{
    if(session && CM.features.voice.state.active && CM.features.voice.state.micActive){
      const current=session;
      post(current.id,"lease",{session_token:current.token}).catch(async()=>{
        if(session===current){await CM.features.voice.stop();message("通话连接已失效，已关闭麦克风；不会自动重拨。");}
      });
    } else if(enabled)refresh();
  },30000);
  window.addEventListener("beforeunload",()=>{if(session)cancelSession();});
  CM.registerFeature("callInvitations",{refresh});
})();
