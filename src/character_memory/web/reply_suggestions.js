/* User-controlled drafts. The server alone classifies accepted text. */
(() => {
  const CM=window.CM;if(!CM)return;
  const api='/v2/reply-suggestions', esc=CM.escapeHtml;
  const host=document.createElement('section');host.className='cm-replies';host.setAttribute('aria-label','回复建议');
  CM.dom.composer.parentElement.prepend(host);
  const dialog=document.createElement('dialog');dialog.className='cm-experience cm-reply-feedback';dialog.setAttribute('aria-labelledby','replyFeedbackTitle');document.body.append(dialog);
  let epoch=0, active=null, selected=null, lastBatch=null, busy=false, notice='', pendingGeneration=null, scopeSeen='';
  let feedbackEpoch=0, feedbackBatch=null, feedbackBusy=false, editing=null, rows=[], feedbackNotice='';
  let feedbackProfile=null, preferenceDraft={}, preferenceConflict=false, preferenceOpen=false;
  const pendingFeedback=new Map();
  const preferenceFields=[['tone','反馈表达风格',[['NATURAL','自然'],['CUTE','可爱']]],['energy','反馈相处节奏',[['QUIET','安静'],['LIVELY','活泼']]],['verbosity','反馈回答长度',[['BRIEF','简短'],['BALANCED','适中'],['DETAILED','详细']]]];
  const scope=()=>CM.isGroupConversation()?`GROUP:${CM.state.conversation.groupId}`:`DIRECT:${CM.state.characterId}`;
  const latest=()=>CM.isGroupConversation()?CM.features.groups?.latestMessage?.():CM.state.directHistory.messages.at(-1);
  const ref=()=>{const m=latest();return m?.id?{kind:CM.isGroupConversation()?'CONVERSATION_EVENT':'EVENT',id:Number(m.id),...(CM.isGroupConversation()?{conversation_id:CM.state.conversation.groupId}:{})}:null;};
  const contextKey=()=>JSON.stringify([scope(),ref()]);
  const blocked=()=>Boolean(CM.state.lifeReference||CM.features.voice?.state?.active);
  const storageKey=()=>`cm-reply-last:${scope()}`;
  function remember(id){lastBatch=id;try{sessionStorage.setItem(storageKey(),id);}catch{}}
  async function request(path,method='GET',body){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),45000);
    try{
      const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},signal:controller.signal,...(body?{body:JSON.stringify(body)}:{})});
      if(!response.ok){const e=new Error('request failed');e.status=response.status;throw e;}
      return {status:response.status,data:await response.json()};
    }finally{clearTimeout(timer);}
  }
  function invalidate(text='聊天或偏好已变化，原草稿仍可编辑；需要时重新生成。'){
    epoch++;active=null;selected=null;pendingGeneration=null;busy=false;notice=text;render();
  }
  function sync(){
    if(scopeSeen!==scope()){
      scopeSeen=scope();lastBatch=null;try{lastBatch=sessionStorage.getItem(storageKey());}catch{}
      if(dialog.open)dialog.close();invalidate('');return;
    }
    if((active&&active.context!==contextKey())||(pendingGeneration&&pendingGeneration.context!==contextKey()))invalidate();
    else render();
  }
  function render(){
    const held=active?.data?.status==='PENDING';
    const label=held?'查看生成结果':pendingGeneration?.unknown?'重试原生成':'帮我想想';
    host.innerHTML=`<div class="cm-replies-head"><button type="button" data-reply-generate title="明确点击才生成回复草稿；不会自动发消息">${label}</button>${lastBatch?'<button type="button" data-reply-feedback title="查看、记录、更正或撤回明确文字反馈">建议反馈</button>':''}<span role="status">${esc(busy?'正在准备建议…':notice)}</span></div>${active?.data.status==='READY'?`<div class="cm-replies-options">${active.data.suggestions.map(item=>`<button type="button" data-suggestion-id="${esc(item.id)}" ${selected===item.id?'aria-pressed="true"':'aria-pressed="false"'}><small>${esc(item.intent)}</small><span>${esc(item.text)}</span></button>`).join('')}</div><div class="cm-replies-foot"><span>点击仅填入草稿，可编辑或直接写自己的话。</span><button type="button" data-reply-ignore>不用这些</button></div>`:''}`;
    const generate=host.querySelector('[data-reply-generate]');generate.disabled=busy||blocked()||!ref();
    if(blocked())generate.title='建议归因目前仅用于普通文本聊天；通话或生活引用期间请直接输入';
    else if(!ref())generate.title='先聊一句，有真实上下文后再生成建议';
    generate.onclick=()=>generateSuggestions();
    host.querySelector('[data-reply-feedback]')?.addEventListener('click',openFeedback);
    host.querySelectorAll('[data-suggestion-id]').forEach(button=>button.onclick=async()=>{
      if(busy||!active||active.context!==contextKey()||blocked()){if(!busy)invalidate();return;}
      const batch=active,target=contextKey(),choiceId=button.dataset.suggestionId,token=++epoch;
      busy=true;render();
      try{
        // Profile changes in another page have no local event. Revalidate before
        // inserting a model-authored draft, without spending another model call.
        const result=await request(`${api}/${batch.data.batch_id}`);
        if(token!==epoch||target!==contextKey()||blocked())return;
        if(result.data.status!=='READY')throw Error('suggestions not ready');
        const choice=result.data.suggestions.find(x=>x.id===choiceId);
        if(!choice)throw Error('choice missing');
        if(CM.dom.input.value.trim()&&CM.dom.input.value!==choice.text&&!confirm('用这条建议替换当前草稿？当前草稿尚未发送。'))return;
        selected=choice.id;CM.dom.input.value=choice.text;CM.dom.input.dispatchEvent(new Event('input',{bubbles:true}));
        notice='已放入草稿，尚未发送。';
      }catch(error){
        if(token!==epoch)return;
        if(error.status===409){invalidate('这批建议已过期，原文字仍保留；需要时重新生成。');return;}
        notice='暂时无法确认建议是否仍有效，未修改草稿。';
      }finally{if(token===epoch){busy=false;render();CM.dom.input.focus();}}
    });
    host.querySelector('[data-reply-ignore]')?.addEventListener('click',ignore);
  }
  async function generateSuggestions(){
    if(busy)return;
    if(blocked()){notice='通话或生活引用期间请直接输入，建议仅用于普通文本聊天。';render();return;}
    if(!ref())return;
    const target=contextKey(), token=++epoch;busy=true;notice='';render();
    try{
      let result;
      if(active?.data.status==='PENDING')result=await request(`${api}/${active.data.batch_id}`);
      else{
        if(!pendingGeneration){
          const profile=(await request('/v2/me/experience-profile')).data;
          if(token!==epoch||target!==contextKey())return;
          pendingGeneration={context:target,body:{request_id:crypto.randomUUID(),context_ref:ref(),expected_profile_version:profile.version}};
        }
        result=await request(api,'POST',pendingGeneration.body);
      }
      if(token!==epoch||target!==contextKey())return;
      if(!result.data.batch_id||!['READY','PENDING'].includes(result.data.status))throw Error('invalid batch');
      active={context:target,data:result.data};selected=null;remember(result.data.batch_id);pendingGeneration=null;
      notice=result.data.status==='PENDING'?'生成尚未完成；可继续输入，稍后点查看。':'你来决定怎么说，不必选其中一条。';
    }catch(error){
      if(token!==epoch)return;
      active=null;selected=null;
      if(error.status){pendingGeneration=null;notice=error.status===409?'上下文或偏好已更新，请重新生成；原文字仍保留。':'暂时没能生成建议，仍可自由输入；不会自动重试。';}
      else{if(pendingGeneration)pendingGeneration.unknown=true;notice='生成结果尚未确认；重试原生成沿用同一请求，不会重复领取。';}
    }finally{if(token===epoch){busy=false;render();}}
  }
  async function ignore(){
    if(!active||busy)return;
    const batch=active, token=++epoch;
    if(!batch.ignoreRequest)batch.ignoreRequest={request_id:crypto.randomUUID(),batch_id:batch.data.batch_id,kind:'IGNORE'};
    busy=true;render();
    try{
      await request(api+'/feedback','POST',batch.ignoreRequest);
      if(token!==epoch)return;
      active=null;selected=null;notice='已记录不用这些，不推断你的偏好。';
    }catch(error){if(token===epoch)notice=error.status===409?'这批建议已过期；没有记录忽略或推断偏好。':'未确认记录结果，可重试“不用这些”；仍沿用原请求。';}
    finally{if(token===epoch){busy=false;render();}}
  }
  function capture(){
    if(blocked()){if(active)invalidate('当前按通话或引用输入发送，不附加普通聊天建议归因。');return undefined;}
    if(!active||active.data.status!=='READY'||active.context!==contextKey())return undefined;
    return {batch_id:active.data.batch_id,...(selected?{suggestion_id:selected}:{})};
  }
  function recorded(record){
    const payload=JSON.parse(record.body);
    if(payload.reply_suggestion&&payload.reply_suggestion.batch_id===active?.data.batch_id)invalidate('草稿已进入发送记录；结果以消息送达状态为准。');
  }
  const textarea=()=>dialog.querySelector('[data-feedback-text]');
  function feedbackRender(text=''){
    const field=textarea(),restoreFocus=document.activeElement===field;
    const selection=restoreFocus?[field.selectionStart,field.selectionEnd]:null;
    const pending=pendingFeedback.get(feedbackBatch);
    dialog.innerHTML=`<header><div><h2 id="replyFeedbackTitle">建议反馈</h2><p>文字反馈仅记录；下方明确选择的相处偏好才会保存，供下次回复使用。</p></div><button type="button" data-feedback-close class="cm-experience-close" aria-label="关闭建议反馈" title="关闭不会提交尚未发送的文字">×</button></header><div class="cm-experience-body"><p role="status">${esc(feedbackNotice)}</p><label for="replyFeedbackText">反馈内容</label><textarea id="replyFeedbackText" data-feedback-text maxlength="2000" rows="3" ${pending?'disabled':''}></textarea>${!editing?`<details data-feedback-preferences ${preferenceOpen?'open':''}><summary>同时调整相处偏好（可选）</summary><p>未选择的项目保持原样；撤销仅清除该项明确偏好。</p>${preferenceFields.map(([key,label,options])=>`<label>${label}<select aria-label="${label}" data-feedback-pref="${key}" ${feedbackBusy||pending||!feedbackProfile?'disabled':''}><option value="_KEEP">保持原样</option>${options.map(([value,name])=>`<option value="${value}">${name}</option>`).join('')}<option value="_CLEAR">撤销这项偏好</option></select></label>`).join('')}<p>当前偏好版本：${feedbackProfile?esc(feedbackProfile.version):'未读取'}</p><button type="button" data-feedback-latest ${feedbackBusy||pending?'disabled':''}>读取最新偏好</button></details>`:''}<button type="button" data-feedback-profile>查看或恢复相处偏好</button><button type="button" data-feedback-submit>${pending?'重试原提交':editing?'保存更正':'提交反馈'}</button>${editing?'<button type="button" data-feedback-cancel>取消更正</button>':''}<div data-feedback-records></div></div>`;
    textarea().value=pending?.body?.comment||text;
    if(restoreFocus&&!textarea().disabled){textarea().focus({preventScroll:true});textarea().setSelectionRange(...selection);}
    dialog.querySelector('[data-feedback-close]').onclick=()=>dialog.close();
    const changes=pending?.body?.preference_changes||preferenceDraft;
    dialog.querySelectorAll('[data-feedback-pref]').forEach(field=>{
      field.value=Object.hasOwn(changes,field.dataset.feedbackPref)?changes[field.dataset.feedbackPref]??'_CLEAR':'_KEEP';
      field.onchange=()=>{const key=field.dataset.feedbackPref;if(field.value==='_KEEP')delete preferenceDraft[key];else preferenceDraft[key]=field.value==='_CLEAR'?null:field.value;};
    });
    const details=dialog.querySelector('[data-feedback-preferences]');if(details)details.ontoggle=()=>{preferenceOpen=details.open;};
    dialog.querySelector('[data-feedback-latest]')?.addEventListener('click',refreshPreference);
    dialog.querySelector('[data-feedback-profile]').onclick=()=>{
      if(pending||feedbackBusy)return;
      if((textarea().value.trim()||Object.keys(preferenceDraft).length)&&!confirm('离开未提交的反馈草稿，查看或恢复相处偏好？'))return;
      dialog.close();CM.features.experienceProfile.open();
    };
    dialog.querySelector('[data-feedback-profile]').disabled=feedbackBusy||Boolean(pending);
    dialog.querySelector('[data-feedback-submit]').disabled=feedbackBusy||(!pending&&preferenceConflict&&!editing);
    dialog.querySelector('[data-feedback-submit]').onclick=()=>submitFeedback();
    dialog.querySelector('[data-feedback-cancel]')?.addEventListener('click',()=>{editing=null;feedbackRender(textarea().value);});
    const list=dialog.querySelector('[data-feedback-records]');
    for(const item of rows.filter(x=>x.kind==='EXPLICIT_FEEDBACK')){
      const row=document.createElement('section');row.className='cm-experience-version';row.dataset.signalId=item.id;
      row.innerHTML=`<div><strong>${item.withdrawn?'已撤回':esc(item.comment||'无文字')}</strong><p>版本 ${esc(item.revision)}${item.applied_profile_version!=null?' · 当时同时保存过偏好；当前以最新选择为准':''}</p></div>`;
      if(!item.withdrawn){
        for(const [action,label]of [['correct','更正'],['withdraw','撤回']]){
          const button=document.createElement('button');button.type='button';button.textContent=label;button.disabled=feedbackBusy||Boolean(pending);
          button.onclick=()=>{if(action==='correct'){editing=item;feedbackNotice='正在更正这条反馈；不会修改原消息或偏好。';feedbackRender(item.comment);}else if(confirm('撤回这条文字反馈？原消息与独立保存的偏好不变。'))sendFeedback({path:`${api}/feedback/${item.id}/withdraw`,method:'POST',body:{request_id:crypto.randomUUID(),expected_revision:item.revision}});};row.append(button);
        }
      }
      list.append(row);
    }
    if(!list.children.length)list.textContent='还没有明确的文字反馈。选择、编辑与自由输入的发送记录由服务器保存。';
  }
  async function loadFeedback({keepText='',successNotice=''}={}){
    const token=++feedbackEpoch, batch=feedbackBatch;feedbackBusy=true;feedbackNotice='正在读取反馈记录…';feedbackRender(keepText);
    try{
      const results=await Promise.allSettled([request(`${api}/${batch}/signals`),request('/v2/me/experience-profile')]);
      if(token!==feedbackEpoch||!dialog.open)return;
      if(results[0].status==='fulfilled')rows=results[0].value.data.signals;
      feedbackProfile=results[1].status==='fulfilled'?results[1].value.data:null;
      feedbackNotice=successNotice||(results.every(x=>x.status==='fulfilled')?'记录可更正或撤回；文字更正和撤回不会回滚独立保存的偏好。':'部分记录未读到；偏好读取失败时不可提交偏好修改。');
    }
    catch(error){if(token!==feedbackEpoch)return;feedbackNotice='暂时无法读取记录；未修改任何反馈。';}
    finally{if(token===feedbackEpoch&&dialog.open){const liveText=textarea()?.value??keepText;feedbackBusy=false;feedbackRender(liveText);}}
  }
  async function refreshPreference(){
    if(feedbackBusy||pendingFeedback.has(feedbackBatch))return;
    const token=++feedbackEpoch,text=textarea().value;feedbackBusy=true;feedbackRender(text);
    try{
      const result=await request('/v2/me/experience-profile');
      if(token!==feedbackEpoch||!dialog.open)return;
      if(Object.keys(preferenceDraft).length&&!confirm('已读取最新偏好。保留本次明确选择，以最新版本重新确认？尚未提交任何修改。'))return;
      feedbackProfile=result.data;preferenceConflict=false;feedbackNotice='已读取最新偏好；请确认草稿后再次提交，未自动保存。';
    }catch{if(token===feedbackEpoch)feedbackNotice='暂时无法读取最新偏好，未修改草稿或保存。';}
    finally{if(token===feedbackEpoch&&dialog.open){feedbackBusy=false;feedbackRender(textarea()?.value??text);}}
  }
  function openFeedback(){
    if(!lastBatch)return;
    feedbackBatch=lastBatch;rows=[];editing=null;feedbackBusy=false;feedbackNotice='';feedbackProfile=null;preferenceDraft={};preferenceConflict=false;preferenceOpen=false;feedbackRender();dialog.showModal();loadFeedback();
  }
  function submitFeedback(){
    if(feedbackBusy)return;
    const pending=pendingFeedback.get(feedbackBatch);if(pending){sendFeedback(pending);return;}
    const text=textarea().value.trim(),hasChanges=!editing&&Object.keys(preferenceDraft).length>0;if(preferenceConflict&&!editing)return;if(hasChanges&&!feedbackProfile)return;if(!text&&!hasChanges){feedbackNotice='请先写下想反馈的内容。';feedbackRender();return;}
    sendFeedback(editing?{path:`${api}/feedback/${editing.id}`,method:'PATCH',body:{request_id:crypto.randomUUID(),expected_revision:editing.revision,comment:text}}:{path:api+'/feedback',method:'POST',body:{request_id:crypto.randomUUID(),batch_id:feedbackBatch,kind:'EXPLICIT_FEEDBACK',comment:text||null,...(hasChanges?{preference_changes:{...preferenceDraft},expected_profile_version:feedbackProfile.version}:{})}});
  }
  async function sendFeedback(operation){
    if(feedbackBusy)return;
    const batch=feedbackBatch,token=++feedbackEpoch,text=textarea()?.value||'';
    pendingFeedback.set(batch,operation);feedbackBusy=true;feedbackNotice='正在提交…';feedbackRender(text);
    try{
      const result=await request(operation.path,operation.method,operation.body);
      // A closed/reopened dialog may still show this pending identity. Keep it
      // until that view explicitly reconciles it, rather than creating a new note.
      if(token!==feedbackEpoch||!dialog.open)return;
      pendingFeedback.delete(batch);
      editing=null;preferenceDraft={};preferenceConflict=false;feedbackBusy=false;
      let successNotice='反馈已保存。文字更正和撤回不会回滚独立保存的偏好。';
      if(result.data.applied_profile_version!=null){
        const current=await request('/v2/me/experience-profile').catch(()=>null);
        if(token!==feedbackEpoch||!dialog.open)return;
        successNotice=current?.data.version===result.data.applied_profile_version?'已保存明确选择，供下次回复使用。':'偏好已有后续更改或暂未确认，未重复应用旧选择；以最新偏好为准。';
        CM.emit('experienceProfileChanged').catch(console.error);
      }
      await loadFeedback({successNotice});
    }catch(error){
      if(token!==feedbackEpoch)return;
      if(error.status)pendingFeedback.delete(batch);
      if(error.status===409&&operation.body.preference_changes)preferenceConflict=true;
      feedbackBusy=false;feedbackNotice=error.status===409?'记录或偏好已变化，未覆盖新内容。偏好修改请先读取最新偏好，再确认提交；反馈更正请重新查看记录。':error.status?'未能提交反馈，文字仍保留。':'提交结果尚未确认，重试原提交会沿用同一身份。';feedbackRender(text);
    }
  }
  dialog.addEventListener('close',()=>{feedbackEpoch++;feedbackBusy=false;});
  CM.registerFeature('replySuggestions',{capture,recorded});
  CM.on('conversationChanged',sync);CM.on('historyLoaded',sync);CM.on('ready',sync);
  CM.on('experienceProfileChanged',()=>invalidate('相处偏好已更新，原草稿仍保留；需要时重新生成。'));
  new MutationObserver(sync).observe(CM.dom.chat,{childList:true,subtree:true});
  const wrap=CM.dom.composer.parentElement;
  new ResizeObserver(()=>{CM.dom.chat.style.paddingBottom=`${Math.ceil(wrap.getBoundingClientRect().height)+20}px`;}).observe(wrap);
  sync();
})();
