/* Explicit local-operator preferences. No model calls or inferred choices. */
(() => {
  const CM=window.CM, endpoint='/v2/me/experience-profile';
  if(!CM)return;
  const dimensions=[
    ['tone','表达风格',[['NATURAL','自然'],['CUTE','可爱']],'自然表达，或多一点轻松可爱。'],
    ['energy','相处节奏',[['QUIET','安静'],['LIVELY','热闹']],'少些插话，或多些轻松话题；都允许沉默。'],
    ['verbosity','回答长度',[['BRIEF','简短'],['BALANCED','适中'],['DETAILED','丰富']],'先说重点，或在需要时展开。'],
  ];
  const escape=CM.escapeHtml;
  const entry=document.createElement('button');
  entry.type='button';entry.className='ghost-button cm-experience-entry';entry.textContent='相处偏好';
  entry.title='调整所有伙伴面向你的表达偏好；不改变人物设定，不调用模型';
  document.querySelector('.topbar-actions')?.prepend(entry);
  const dialog=document.createElement('dialog');
  dialog.className='cm-experience';dialog.setAttribute('aria-labelledby','experienceTitle');
  document.body.append(dialog);
  let epoch=0, current=null, draft={}, previousDraft='', view='edit', target=null;
  let busy=false, needsLatest=false, error='', status='', loaded=false;
  const values=p=>Object.fromEntries(dimensions.map(([key])=>[key,p?.[key]||'']));
  const summary=p=>dimensions.map(([key,,options])=>options.find(([value])=>value===p?.[key])?.[1]).filter(Boolean).join(' · ')||'未指定，先自然相处';
  const dirty=()=>JSON.stringify(values(current?.preferences))!==JSON.stringify(values(draft));
  async function request(path,method='GET',body){
    const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    if(!response.ok){const failure=new Error('profile request failed');failure.status=response.status;throw failure;}
    return response.json();
  }
  const button=(text,action,extra='')=>`<button type="button" data-profile-action="${action}" ${extra}>${text}</button>`;
  function render(){
    const focused=dialog.contains(document.activeElement)?document.activeElement:null;
    const focusAction=focused?.dataset?.profileAction;
    let content='',footer='';
    if(!loaded){
      content=error?`<p role="alert">暂时无法读取偏好。聊天仍可继续，尚未修改任何选择。</p>`:'<p role="status">正在读取相处偏好…</p>';
      footer=error?button('重新读取','reload'):'';
    }else if(view==='clear'){
      content=`<div class="cm-experience-warning"><h3>清除所有相处偏好？</h3><p>当前选择与历史偏好快照都会删除，之后无法恢复。人物设定和聊天记录不会因此改变。</p><strong>${escape(summary(current.preferences))}</strong></div>`;
      footer=button('取消','edit')+button('确认清除','confirm-clear','class="danger"');
    }else if(view==='restore'){
      content=`<h3>恢复这次选择？</h3><p class="cm-experience-summary">${escape(summary(target.preferences))}</p><p>恢复会成为一次新修改。下一次回复使用恢复后的选择，当前版本仍保留在记录中。</p>`;
      footer=button('取消','history')+button('确认恢复','confirm-restore','class="primary"');
    }else if(view==='history'){
      content='<p>只保留明确保存的选择。已清除的快照无法恢复。</p><div data-profile-history></div>';
      footer=button('返回偏好','edit');
    }else{
      content=dimensions.map(([key,label,options,hint])=>`<fieldset><legend>${label}</legend><div class="cm-experience-options">${[...options,['','不指定']].map(([value,name])=>`<label><input type="radio" name="experience-${key}" data-profile-key="${key}" value="${value}" ${draft[key]===value?'checked':''}>${name}</label>`).join('')}</div><p class="cm-experience-hint">${hint}</p></fieldset>`).join('');
      if(previousDraft)content+=`<p class="cm-experience-summary" data-profile-draft>刚才未保存的草稿：${escape(previousDraft)}。上方显示最新选择；需要时请重新选择。</p>`;
      content+='<p class="cm-experience-note">下次回复开始使用新选择，正在生成的回复不变。这里不修改人物设定，也不设置主动通知。</p>';
      footer=(current.version>0?button('版本记录','history')+button('清除偏好','clear','class="quiet"'):button('先聊聊','close'))+button(current.version>0?'保存修改':'保存选择','save',`class="primary" ${!dirty()||needsLatest?'disabled':''}`);
    }
    dialog.innerHTML=`<header><div><h2 id="experienceTitle">相处偏好</h2><p>选你舒服的表达方式，也可以先不选。<br>所有伙伴共用，仍保留各自的观点。</p></div>${button('×','close','class="cm-experience-close" aria-label="关闭相处偏好" title="关闭面板；不会保存尚未提交的选择"')}</header><div class="cm-experience-body">${error&&loaded?`<div role="alert" class="cm-experience-warning">${escape(error)}${needsLatest?button('查看最新偏好','latest'):''}</div>`:''}${loaded?`<p role="status" class="cm-experience-status">${escape(status)}</p>`:''}${content}</div><footer>${footer}</footer>`;
    dialog.querySelectorAll('button').forEach(node=>{
      const action=node.dataset.profileAction;
      if((busy&&action!=='close')||(needsLatest&&!['close','latest','reload'].includes(action)))node.disabled=true;
      node.onclick=()=>act(action);
    });
    dialog.querySelectorAll('input').forEach(node=>{
      node.disabled=busy;
      node.onchange=()=>{
        draft[node.dataset.profileKey]=node.value;
        const save=dialog.querySelector('[data-profile-action="save"]');
        if(save)save.disabled=!dirty()||needsLatest||busy;
        const live=dialog.querySelector('[role="status"]');if(live)live.textContent='选择尚未保存';
      };
    });
    dialog.setAttribute('aria-busy',String(busy));
    if(dialog.open&&focused){
      const replacement=[...dialog.querySelectorAll('button')].find(node=>node.dataset.profileAction===focusAction&&!node.disabled);
      (replacement||dialog.querySelector('[data-profile-action="close"]'))?.focus({preventScroll:true});
    }
  }
  async function read({keepDraft=false}={}){
    const token=++epoch, retained=keepDraft?summary(draft):'';
    busy=true;error='';status='';render();
    try{
      const result=await request(endpoint);
      if(token!==epoch||!dialog.open)return;
      if(!Number.isSafeInteger(result.version)||!result.preferences)throw Error('invalid profile response');
      current=result;draft=values(result.preferences);loaded=true;needsLatest=false;previousDraft=retained;view='edit';
    }catch(failure){if(token!==epoch)return;error=loaded?'未能读取最新偏好，请稍后再试。':'读取失败';}
    finally{if(token===epoch&&dialog.open){busy=false;render();}}
  }
  async function history(){
    view='history';error='';status='';busy=true;render();
    const token=++epoch;
    try{
      const result=await request(endpoint+'/versions');
      if(token!==epoch||!dialog.open)return;
      busy=false;render();
      const host=dialog.querySelector('[data-profile-history]');
      for(const item of result.versions){
        const row=document.createElement('section');row.className='cm-experience-version';
        row.innerHTML=`<div><strong>${escape(summary(item.preferences))}</strong><p>版本 ${escape(item.version)}${item.version===current.version?' · 当前选择':''}</p></div>`;
        if(item.version!==current.version){
          const restore=document.createElement('button');restore.type='button';restore.textContent='恢复这一版';
          restore.title='先查看恢复确认，不会立即覆盖当前选择';restore.dataset.restoreVersion=item.version;
          restore.onclick=()=>{target=item;view='restore';error='';render();};row.append(restore);
        }
        host.append(row);
      }
      if(!result.versions.length)host.textContent='还没有保存过选择。';
    }catch(failure){
      if(token!==epoch)return;
      busy=false;view='edit';error='暂时无法读取版本记录，当前草稿仍保留。';render();
    }
  }
  async function mutate(operation){
    if(busy||needsLatest)return;
    const token=++epoch;
    let path=endpoint,method='PATCH',body;
    if(operation==='save'){
      if(!dirty())return;
      const changes={};
      for(const [key]of dimensions)if((current.preferences[key]||'')!==draft[key])changes[key]=draft[key]||null;
      body={expected_version:current.version,changes};
    }else if(operation==='confirm-clear'){
      path+=`?expected_version=${current.version}`;method='DELETE';
    }else{
      path+='/restore';method='POST';body={expected_version:current.version,target_version:target.version};
    }
    busy=true;error='';status=operation==='save'?'正在保存…':'正在更新…';render();
    try{
      const result=await request(path,method,body);
      if(token!==epoch||!dialog.open)return;
      current=result;draft=values(result.preferences);view='edit';previousDraft='';needsLatest=false;
      status=operation==='confirm-clear'?'已清除当前选择与历史偏好；下次回复不再使用旧选择。':'已保存，下次回复开始使用。';
      CM.emit('experienceProfileChanged',result).catch(console.error);
    }catch(failure){
      if(token!==epoch)return;
      // A lost response may already have committed: read the latest before another write.
      needsLatest=true;view='edit';status='';
      error=failure.status===409?'偏好已在另一处更新。草稿仍保留，尚未覆盖最新选择。':'未确认保存结果。草稿仍保留，请先查看最新偏好，再决定是否修改。';
    }finally{if(token===epoch&&dialog.open){busy=false;render();}}
  }
  function act(action){
    if(action==='close'){dialog.close();return;}
    if(busy)return;
    if(action==='reload'||action==='latest'){read({keepDraft:loaded});return;}
    if(action==='history'){history();return;}
    if(['save','confirm-clear','confirm-restore'].includes(action)){mutate(action);return;}
    if(action==='clear'&&dirty())previousDraft=summary(draft);
    view=action;error='';status='';render();
  }
  function open(){
    if(dialog.open)return;
    current=null;draft={};previousDraft='';loaded=false;needsLatest=false;view='edit';busy=false;error='';status='';
    render();dialog.showModal();read();
  }
  dialog.addEventListener('close',()=>{epoch++;busy=false;});
  entry.addEventListener('click',open);
  CM.on('conversationChanged',()=>{if(dialog.open)dialog.close();});
  CM.registerFeature('experienceProfile',{open});
})();
