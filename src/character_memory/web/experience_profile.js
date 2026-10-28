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
  let epoch=0, current=null, draft={}, previousDraft='', view='edit';
  let busy=false, needsLatest=false, error='', status='', loaded=false;
  let compareTargetVersion=null, restoreBase=null, compareRows=[], compareState='', compareBlocked=false, compareError='';
  let candidateState='', candidateRows=[], candidateError='';
  const candidateEndpoint='/v2/me/experience-profile/candidates';
  const scopeName=scope=>scope==='LONG_TERM'?'长期':'当前场景';
  const statusName={PENDING:'待决定',APPLIED:'已采用',REVERTED:'已回退',DISMISSED:'已忽略',
    SATISFIED:'与你已选的一致',OVERRIDDEN:'你已明确选择，建议不覆盖',SUPERSEDED:'已失效',WITHDRAWN:'来源已撤回'};
  const values=p=>Object.fromEntries(dimensions.map(([key])=>[key,p?.[key]||'']));
  const summary=p=>dimensions.map(([key,,options])=>options.find(([value])=>value===p?.[key])?.[1]).filter(Boolean).join(' · ')||'未指定，先自然相处';
  const valueName=(key,value)=>dimensions.find(([name])=>name===key)?.[2].find(([choice])=>choice===value)?.[1]||'未指定';
  const dirty=()=>JSON.stringify(values(current?.preferences))!==JSON.stringify(values(draft));
  async function request(path,method='GET',body){
    const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    if(!response.ok){const failure=new Error('profile request failed');failure.status=response.status;throw failure;}
    return response.json();
  }
  const button=(text,action,extra='')=>`<button type="button" data-profile-action="${action}" ${extra}>${text}</button>`;
  function compareContent(){
    const heading='<h3>比较并恢复偏好</h3>';
    if(compareState==='loading')return `${heading}<p role="status">正在读取当前选择与版本记录…</p>`;
    if(compareState!=='ready')return `${heading}<div role="alert" class="cm-experience-warning">${escape(compareError)}</div>`;
    const same=compareRows.every(row=>!row.changed);
    const rows=compareRows.map(row=>`<tr data-compare-key="${row.key}" data-change="${row.changed?'changed':'same'}"><th scope="row"><span>${escape(row.label)}</span><span class="cm-experience-change">${row.changed?'将改变':'保持不变'}</span></th><td data-current>${escape(row.current)}</td><td data-restored${row.changed?' class="cm-experience-changed"':''}>${escape(row.restored)}</td></tr>`).join('');
    return `${heading}<p class="cm-experience-compare-meta">当前读取版本 ${restoreBase.version} · 目标版本 ${compareTargetVersion}</p>${compareBlocked?`<div role="alert" class="cm-experience-warning">${escape(compareError)}</div>`:''}<table class="cm-experience-compare-table"><caption>恢复前后的相处偏好对比</caption><thead><tr><th scope="col">偏好</th><th scope="col">当前选择</th><th scope="col">恢复后</th></tr></thead><tbody>${rows}</tbody></table><p>${same?'三项选择都相同；确认仍会保存一个新版本。':'确认后保存为新版本，不会改写人物设定、聊天和已有记录。'}</p><p>如果别处更新了偏好，请重新比较后再恢复。</p>`;
  }
  function compareFooter(){
    if(compareState==='ready'&&!compareBlocked)return button('取消','cancel-compare')+button('确认恢复','confirm-restore','class="primary"');
    if(compareState==='loading')return button('取消','cancel-compare')+button('确认恢复','confirm-restore','class="primary" disabled');
    return button('返回版本记录','cancel-compare')+button('重新比较','recompare');
  }
  function candidateContent(){
    const heading='<h3>来自反馈的偏好建议</h3>';
    if(candidateState==='loading')return `${heading}<p role="status">正在读取建议…</p>`;
    if(candidateError)return `${heading}<div role="alert" class="cm-experience-warning">${escape(candidateError)}</div>`;
    if(!candidateRows.length)return `${heading}<p>暂时没有可以采用的建议。你在建议反馈里写下的明确选择，仍会直接保存。</p>`;
    const rows=candidateRows.map(item=>{
      const proposed=item.fields&&Object.keys(item.fields).length
        ? dimensions.filter(([key])=>Object.hasOwn(item.fields,key))
            .map(([key,label])=>`${label}：${valueName(key,item.current_fields?.[key])} → ${valueName(key,item.fields[key])}`).join('，')
        : '';
      const target=dimensions.filter(([key])=>item.applicable_fields&&Object.hasOwn(item.applicable_fields,key))
        .map(([key,label])=>`${label}：${valueName(key,item.applicable_fields[key])}`).join('，');
      const evidence=item.evidence?.map(x=>x.phrase).filter(Boolean).join('、');
      const actions=[];
      if(item.status==='PENDING'){
        actions.push(`<button type="button" data-candidate-action="apply" data-candidate-id="${escape(item.signal_id)}">采用</button>`);
        actions.push(`<button type="button" data-candidate-action="dismiss" data-candidate-id="${escape(item.signal_id)}" class="quiet">忽略</button>`);
      }else if(item.status==='APPLIED'){
        actions.push(`<button type="button" data-candidate-action="revert" data-candidate-id="${escape(item.signal_id)}" class="quiet">回退这次采用</button>`);
      }
      const title=proposed||(item.status==='WITHDRAWN'?'来源反馈已撤回':'与你已选的一致');
      return `<section class="cm-experience-version" data-candidate-id="${escape(item.signal_id)}" data-candidate-status="${escape(item.status)}"><div><strong>${escape(title)}</strong><p>${escape(scopeName(item.scope))} · ${escape(statusName[item.status]||item.status)}${evidence?` · 依据「${escape(evidence)}」`:''}</p>${item.status==='PENDING'?`<p>${escape(item.note)}</p>${target?`<p>采用后会写：${escape(target)}</p>`:''}`:''}${item.comment?`<p>原话：${escape(item.comment)}</p>`:''}</div><div class="cm-experience-candidate-actions">${actions.join('')}</div></section>`;
    }).join('');
    return `${heading}<p>这些只是建议，不会自动生效。你已经明确选过的项目永远优先。</p>${rows}`;
  }
  function render(){
    const focused=dialog.contains(document.activeElement)?document.activeElement:null;
    const focusAction=focused?.dataset?.profileAction;
    const focusVersion=focused?.dataset?.compareVersion;
    let content='',footer='';
    if(!loaded){
      content=error?`<p role="alert">暂时无法读取偏好。聊天仍可继续，尚未修改任何选择。</p>`:'<p role="status">正在读取相处偏好…</p>';
      footer=error?button('重新读取','reload'):'';
    }else if(view==='clear'){
      content=`<div class="cm-experience-warning"><h3>清除所有相处偏好？</h3><p>当前选择与历史偏好快照都会删除，之后无法恢复。人物设定和聊天记录不会因此改变。</p><strong>${escape(summary(current.preferences))}</strong></div>`;
      footer=button('取消','edit')+button('确认清除','confirm-clear','class="danger"');
    }else if(view==='compare'){
      content=compareContent();
      footer=compareFooter();
    }else if(view==='candidates'){
      content=candidateContent();
      footer=(candidateState==='loading'?button('返回偏好','edit'):button('返回偏好','edit')+button('刷新建议','candidates'));
    }else if(view==='history'){
      content='<p>只保留明确保存的选择。已清除的快照无法恢复。</p><div data-profile-history></div>';
      footer=button('返回偏好','edit');
    }else{
      content=dimensions.map(([key,label,options,hint])=>`<fieldset><legend>${label}</legend><div class="cm-experience-options">${[...options,['','不指定']].map(([value,name])=>`<label><input type="radio" name="experience-${key}" data-profile-key="${key}" value="${value}" ${draft[key]===value?'checked':''}>${name}</label>`).join('')}</div><p class="cm-experience-hint">${hint}</p></fieldset>`).join('');
      if(previousDraft)content+=`<p class="cm-experience-summary" data-profile-draft>刚才未保存的草稿：${escape(previousDraft)}。上方显示最新选择；需要时请重新选择。</p>`;
      else if(dirty())content+=`<p class="cm-experience-summary" data-profile-unsaved>未保存的草稿：${escape(summary(draft))}。当前读取的是版本 ${current.version}；保存会按这个版本提交。</p>`;
      content+='<p class="cm-experience-note">下次回复开始使用新选择，正在生成的回复不变。这里不修改人物设定，也不设置主动通知。</p>';
      footer=(current.version>0?button('反馈建议','candidates')+button('版本记录','history')+button('清除偏好','clear','class="quiet"'):button('反馈建议','candidates')+button('先聊聊','close'))+button(current.version>0?'保存修改':'保存选择','save',`class="primary" ${!dirty()||needsLatest?'disabled':''}`);
    }
    dialog.innerHTML=`<header><div><h2 id="experienceTitle">相处偏好</h2><p>选你舒服的表达方式，也可以先不选。<br>所有伙伴共用，仍保留各自的观点。</p></div>${button('×','close','class="cm-experience-close" aria-label="关闭相处偏好" title="关闭面板；不会保存尚未提交的选择"')}</header><div class="cm-experience-body">${error&&loaded?`<div role="alert" class="cm-experience-warning">${escape(error)}${needsLatest?button('查看最新偏好','latest'):''}</div>`:''}${loaded&&view!=='compare'?`<p role="status" class="cm-experience-status">${escape(status)}</p>`:''}${needsLatest&&loaded&&view==='edit'&&!error?`<div role="alert" class="cm-experience-warning">偏好可能已在别处更新，尚未覆盖。请先查看最新偏好。${button('查看最新偏好','latest')}</div>`:''}${content}</div><footer>${footer}</footer>`;
    dialog.querySelectorAll('button').forEach(node=>{
      const action=node.dataset.profileAction;
      if(!action){
        if(node.dataset.candidateAction)node.onclick=()=>candidateAct(node.dataset.candidateAction,node.dataset.candidateId);
        return;
      }
      const allowedWhenStale={edit:['close','latest','reload'],history:['close','edit','history','cancel-compare'],compare:['close','recompare','cancel-compare'],candidates:['close','edit','candidates'],clear:['close']}[view]||['close'];
      // A fresh comparison read is a valid baseline for the restore write even
      // though the older edit draft still requires an explicit latest read.
      if(view==='compare'&&compareState==='ready'&&!compareBlocked)allowedWhenStale.push('confirm-restore');
      // Aborting a read-only comparison is always safe; a restore POST is not.
      const cancelLoad=action==='cancel-compare'&&view==='compare'&&compareState==='loading';
      if((busy&&action!=='close'&&!cancelLoad)||(needsLatest&&!allowedWhenStale.includes(action)))node.disabled=true;
      node.onclick=()=>act(action,node);
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
      const replacement=[...dialog.querySelectorAll('button')].find(node=>node.dataset.profileAction===focusAction&&node.dataset.compareVersion===focusVersion&&!node.disabled)
        ||[...dialog.querySelectorAll('button')].find(node=>node.dataset.profileAction===focusAction&&!node.disabled);
      (replacement||dialog.querySelector('[data-profile-action="close"]'))?.focus({preventScroll:true});
    }
  }
  async function candidates({notice=''}={}){
    view='candidates';error='';busy=true;candidateState=candidateState||'loading';render();
    const token=++epoch;
    try{
      const result=await request(candidateEndpoint);
      if(token!==epoch||!dialog.open||view!=='candidates')return;
      if(!Array.isArray(result.candidates))throw Error('invalid candidates response');
      candidateRows=result.candidates;candidateState='ready';candidateError='';
      if(Number.isSafeInteger(result.profile_version)){current={...current,version:result.profile_version,preferences:result.preferences||current.preferences};}
      if(notice)status=notice;
    }catch(failure){
      if(token!==epoch)return;
      candidateState='error';candidateError='暂时无法读取偏好建议；没有自动采用任何建议。';
    }finally{if(token===epoch&&dialog.open){busy=false;render();}}
  }
  async function candidateAct(action,signalId){
    if(busy||!signalId)return;
    const row=candidateRows.find(item=>item.signal_id===signalId);
    if(!row||(row.status!=='PENDING'&&row.status!=='APPLIED'))return;
    const token=++epoch,requestId=crypto.randomUUID();
    const target=action==='apply'?`${candidateEndpoint}/${encodeURIComponent(signalId)}/apply`
      :action==='revert'?`${candidateEndpoint}/${encodeURIComponent(signalId)}/revert`
      :`${candidateEndpoint}/${encodeURIComponent(signalId)}/dismiss`;
    const body=action==='dismiss'?{request_id:requestId}
      :{request_id:requestId,expected_profile_version:current.version,...(action==='apply'?{as_long_term:row.scope==='LONG_TERM'}:{})};
    busy=true;error='';status='';render();
    let notice='';
    try{
      const result=await request(target,'POST',body);
      if(token!==epoch||!dialog.open)return;
      const applied=result.applied_profile_version;
      let latest=null;
      if(applied!=null)latest=await request(endpoint).catch(()=>null);
      if(token!==epoch||!dialog.open)return;
      if(applied!=null){
        if(latest?.version===applied){current={version:latest.version,preferences:latest.preferences};draft=values(latest.preferences);needsLatest=false;}
        else{needsLatest=true;}
      }
      notice=applied!=null?'已保存为新版本，下次回复开始使用。':action==='revert'?'已回退这次采用，作为新版本保存；历史版本保留。':action==='dismiss'?'已忽略，这条建议不会自动生效。':'这项选择已经是你当前明确选择，没有写入新版本。';
      CM.emit('experienceProfileChanged').catch(console.error);
    }catch(failure){
      if(token!==epoch)return;
      needsLatest=true;
      notice=failure.status===409?'偏好或建议已在别处变化，未覆盖。请刷新建议后再决定。':failure.status===404?'这条建议已失效，不再提供采用。':'未确认结果；没有自动重试。';
    }finally{
      if(token===epoch&&dialog.open){busy=false;candidateState='';await candidates({notice});}
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
      compareState='';compareTargetVersion=null;restoreBase=null;compareRows=[];compareBlocked=false;compareError='';
    }catch(failure){if(token!==epoch)return;error=loaded?'未能读取最新偏好，请稍后再试。':'读取失败';}
    finally{if(token===epoch&&dialog.open){busy=false;render();}}
  }
  async function history(){
    view='history';error='';status='';busy=true;render();
    const token=++epoch;
    try{
      const result=await request(endpoint+'/versions');
      if(token!==epoch||!dialog.open||view!=='history')return;
      if(!Array.isArray(result.versions))throw Error('invalid versions response');
      busy=false;render();
      const host=dialog.querySelector('[data-profile-history]');
      for(const item of result.versions){
        const row=document.createElement('section');row.className='cm-experience-version';
        row.innerHTML=`<div><strong>${escape(summary(item.preferences))}</strong><p>版本 ${escape(item.version)}${item.version===current.version?' · 当前选择':''}</p></div>`;
        if(item.version!==current.version){
          const restore=document.createElement('button');restore.type='button';restore.textContent='比较并恢复';
          restore.title='先查看当前读取版本与目标版本的三项差异，再决定是否恢复';restore.dataset.compareVersion=String(item.version);
          restore.onclick=()=>compare(item.version);row.append(restore);
        }
        host.append(row);
      }
      if(!result.versions.length)host.textContent='还没有保存过选择。';
    }catch(failure){
      if(token!==epoch)return;
      busy=false;view='edit';error='暂时无法读取版本记录，当前草稿仍保留。';render();
    }
  }
  async function compare(targetVersion){
    if(busy||!Number.isSafeInteger(targetVersion))return;
    const token=++epoch;
    compareTargetVersion=targetVersion;restoreBase=null;compareRows=[];compareState='loading';compareBlocked=false;compareError='';
    view='compare';error='';status='';busy=true;render();
    try{
      const [profile,historyResult]=await Promise.all([request(endpoint),request(endpoint+'/versions')]);
      if(token!==epoch||!dialog.open||view!=='compare')return;
      if(!Number.isSafeInteger(profile?.version)||!profile.preferences||!Array.isArray(historyResult?.versions))throw Error('invalid compare response');
      const target=historyResult.versions.find(item=>item.version===compareTargetVersion);
      const latest=historyResult.versions[0];
      if(!target){
        compareState='removed';compareError='旧版本已清除，或偏好正在变化。请返回版本记录重新选择。';
      }else if(!latest||latest.version!==profile.version||JSON.stringify(values(latest.preferences))!==JSON.stringify(values(profile.preferences))){
        compareState='unavailable';compareError='当前偏好已在别处更新或读取不一致。请重新比较后再恢复。';
      }else{
        restoreBase=profile;
        compareRows=dimensions.map(([key,label])=>({key,label,current:valueName(key,profile.preferences?.[key]),restored:valueName(key,target.preferences?.[key]),changed:(profile.preferences?.[key]||'')!==(target.preferences?.[key]||'')}));
        compareState='ready';compareBlocked=false;compareError='';
      }
    }catch(failure){
      if(token!==epoch)return;
      compareState='unavailable';compareError='暂时无法比较偏好，尚未修改任何选择。';
    }finally{if(token===epoch&&dialog.open){busy=false;render();}}
  }
  async function confirmRestore(){
    if(busy||compareState!=='ready'||compareBlocked||!restoreBase||compareTargetVersion==null)return;
    const token=++epoch, baseVersion=restoreBase.version, targetVersion=compareTargetVersion;
    busy=true;error='';status='';render();
    try{
      const result=await request(endpoint+'/restore','POST',{expected_version:baseVersion,target_version:targetVersion});
      if(token!==epoch||!dialog.open)return;
      if(!Number.isSafeInteger(result.version)||!result.preferences)throw Error('invalid profile response');
      current=result;draft=values(result.preferences);view='edit';previousDraft='';needsLatest=false;
      compareTargetVersion=null;restoreBase=null;compareRows=[];compareState='';compareBlocked=false;compareError='';
      status='已恢复为新版本；历史仍保留，下次回复开始使用恢复后的选择。';
      CM.emit('experienceProfileChanged',result).catch(console.error);
    }catch(failure){
      if(token!==epoch)return;
      busy=false;compareBlocked=true;needsLatest=true;
      if(failure.status===409){compareError='偏好已在另一处更新，尚未恢复。请重新比较最新偏好后再确认。';}
      else if(failure.status===404){compareState='removed';compareError='旧版本已清除，不能恢复。请返回版本记录重新选择。';}
      else{compareError='未确认恢复结果，尚未自动重试。请重新比较最新偏好后再决定是否恢复。';}
    }finally{if(token===epoch&&dialog.open){busy=false;render();}}
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
    }else return;
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
  function act(action,node){
    if(action==='close'){dialog.close();return;}
    const cancelLoad=action==='cancel-compare'&&view==='compare'&&compareState==='loading';
    if(busy&&!cancelLoad)return;
    if(action==='reload'||action==='latest'){read({keepDraft:loaded});return;}
    if(action==='history'){history();return;}
    if(action==='candidates'){candidateState='';candidateError='';candidates();return;}
    if(action==='compare'){const version=Number(node?.dataset?.compareVersion);if(Number.isSafeInteger(version))compare(version);return;}
    if(action==='cancel-compare'){epoch++;busy=false;history();return;}
    if(action==='recompare'){if(compareTargetVersion!=null)compare(compareTargetVersion);return;}
    if(action==='confirm-restore'){confirmRestore();return;}
    if(['save','confirm-clear'].includes(action)){mutate(action);return;}
    if(action==='clear'&&dirty())previousDraft=summary(draft);
    view=action;error='';status='';render();
  }
  function open(){
    if(dialog.open)return;
    current=null;draft={};previousDraft='';loaded=false;needsLatest=false;view='edit';busy=false;error='';status='';
    compareTargetVersion=null;restoreBase=null;compareRows=[];compareState='';compareBlocked=false;compareError='';
    candidateState='';candidateRows=[];candidateError='';
    render();dialog.showModal();read();
  }
  dialog.addEventListener('close',()=>{epoch++;busy=false;});
  entry.addEventListener('click',open);
  CM.on('conversationChanged',()=>{if(dialog.open)dialog.close();});
  CM.registerFeature('experienceProfile',{open});
})();
