(() => {
  const CM=window.CM, api=CM?.features.encounter;
  if(!api)return;
  const esc=CM.escapeHtml, closed=c=>['ACCEPTED','DISMISSED','EXPIRED','FAILED'].includes(c.status);
  const page=document.createElement('section');page.className='cm-world-page hidden';page.setAttribute('aria-label','World 人物发现');
  page.innerHTML=`<div data-world-list><header class="cm-world-heading"><div><span class="cm-world-eyebrow">WORLD · 世界邂逅</span><h1>今天，会遇见谁<span>？</span></h1><p>一个熟悉的身影，一场从未有过的相遇。</p></div><div class="world-date"><strong data-world-date></strong><small>每日经典 1 位 · 原创 1 位</small></div></header><nav class="world-tabs" aria-label="邂逅列表"><button data-world-tab="today" title="查看今天的经典和原创邂逅">今日邂逅</button><button data-world-tab="ongoing" title="继续已经开始认识的临时人物">正在认识</button><button data-world-tab="kept" title="查看已留下的人物与原始邂逅">已留下</button></nav><div class="world-notice hidden" role="status" data-world-error></div><div class="cm-world-layout"><div><div class="encounter-list" data-world-cards></div><div class="world-footnote">✦ 相遇不是任务。可以聊一会儿，也可以慢慢决定。<span data-world-next></span></div></div><aside class="cm-world-note"><span class="cm-world-eyebrow">A LITTLE ENCOUNTER</span><h2>先认识，再留下。</h2><p>经典角色带着熟悉的性格而来。原创人物，也有属于自己的小小宇宙。</p><hr><p>打招呼会开启完整聊天。想听听声音，也可以试着打个电话。</p><p>TA 有权说「晚一点」。拒接电话不会扣分。</p><h3>关系，不只有一种刻度</h3><p>熟悉来自具体互动；信任需要依据。这里展示真实交谈与通话记录，不把关系折算成好感分数。</p></aside></div></div><div data-world-detail class="hidden"></div>`;
  document.querySelector('.chat-shell').append(page);
  const listView=page.querySelector('[data-world-list]'),detailView=page.querySelector('[data-world-detail]'),cards=page.querySelector('[data-world-cards]'),notice=page.querySelector('[data-world-error]');
  const more=document.createElement('button');more.className='world-load-more hidden';more.dataset.worldMore='';more.textContent='查看更多相遇';more.title='读取这一分类中更早的真实记录';cards.after(more);
  const side=document.createElement('section');side.className='sidebar-section encounter-sidebar';side.setAttribute('aria-label','正在认识的来客');document.querySelector('.sidebar-body')?.append(side);
  const dialog=document.createElement('dialog');dialog.className='encounter-call-dialog';dialog.setAttribute('aria-label','临时人物电话邀请');document.body.append(dialog);
  let selected=null,tab='today',visible=false,generation=0,timer=null,detail=null,history=[],signature='',sending=false,callGeneration=0;
  function error(text){notice.textContent=text||'';notice.classList.toggle('hidden',!text);}
  function url(id=null,nextTab=tab,{replace=false}={}){const next=new URL(location.href);next.searchParams.set('view','world');next.searchParams.set('world_tab',nextTab);if(id)next.searchParams.set('encounter',id);else next.searchParams.delete('encounter');if(next.href!==location.href)historyMethod(replace,next);}
  function historyMethod(replace,next){window.history[replace?'replaceState':'pushState'](null,'',next);}
  function portrait(c,cls='encounter-portrait'){return c.portrait_url?`<div class="${cls}"><img src="${esc(c.portrait_url)}" alt="${esc(c.draft?.name||'来客')}的肖像" loading="lazy"></div>`:`<div class="${cls} portrait-missing"><span>✧</span><small>肖像尚未生成</small>${!closed(c)?`<button data-world-portrait="${c.id}" title="使用已配置的生图服务生成并保存一次肖像，可能产生生图费用；刷新不重复生成">生成肖像</button>`:''}</div>`;}
  function renderSidebar(){const items=(api.state.ongoing||api.state.encounters||[]).filter(c=>['SEEN','CHATTING'].includes(c.status));side.innerHTML=`<h2>正在认识 <small>${items.length}</small></h2>`+items.map(c=>`<button class="encounter-sidebar-person" data-world-open="${c.id}" title="继续与${esc(c.draft?.name)}的临时聊天">${c.portrait_url?`<img src="${esc(c.portrait_url)}" alt="">`:`<span>${esc(CM.initialFor(c.draft))}</span>`}<span><strong>${esc(c.draft?.name)}</strong><small>邂逅中 · 对话已保留</small></span></button>`).join('');}
  function renderList(){
    const state=api.state;renderSidebar();if(!visible||selected)return;
    more.classList.toggle('hidden',!state.has_more);
    page.querySelector('[data-world-date]').textContent=state.today||new Date().toLocaleDateString();
    page.querySelector('[data-world-next]').textContent=state.daily_enabled?'下一次相遇 · 明天':'间隔邂逅模式';
    page.querySelectorAll('[data-world-tab]').forEach(b=>b.setAttribute('aria-current',b.dataset.worldTab===tab?'page':'false'));
    const items=(state.encounters||[]).filter(c=>tab==='kept'?c.status==='ACCEPTED':tab==='ongoing'?['SEEN','CHATTING'].includes(c.status):!closed(c)&&String(c.daily_key||c.encounter_day||c.created_at).startsWith(state.today||''));
    if(tab==='today')items.sort((a,b)=>Number(b.encounter_kind==='CLASSIC')-Number(a.encounter_kind==='CLASSIC'));
    cards.classList.add('encounter-section');
    cards.innerHTML=items.map(c=>{const d=c.draft||{},classic=c.encounter_kind==='CLASSIC';return `<article class="encounter-card" data-encounter-id="${c.id}">${portrait(c)}<div class="world-card-body"><span class="world-card-badge">${classic?'✧ 今日经典':'✦ 原创人物'}</span><div class="world-card-identity"><h2>${esc(d.name||'新来客')}</h2><small>${c.status==='ACCEPTED'?'已留下':c.status==='CHATTING'?'已有对话':'尚未交谈'}</small></div><p class="world-card-subtitle">${esc(classic?c.classic_key?.split('｜').slice(1).join('｜')||'经典角色演绎':d.identity||'原创人物')}</p><div class="world-traits">${(d.personality||[]).slice(0,3).map(t=>`<span>${esc(t)}</span>`).join('')}</div><p class="encounter-hook">${esc(c.encounter_hook)}</p><blockquote class="encounter-opening">“${esc(c.opening_message)}”</blockquote><div class="encounter-actions"><button class="primary" data-encounter-chat="${c.id}" title="打开完整临时聊天窗口；不创建正式人物">${c.status==='NEW'?'打个招呼':'继续聊天'} ↗</button><button data-encounter-accept="${c.id}" ${c.status==='ACCEPTED'?'disabled':''} title="确认后将来客加入正式人物；容量不足时需要先归档">${c.status==='ACCEPTED'?'已留下':'留下 TA'}</button></div><div class="world-card-footer"><small>${c.status==='ACCEPTED'?'已留下 · 原始邂逅记录':'先聊聊，不占正式人物位'}</small>${c.status==='ACCEPTED'&&c.accepted_character_id?`<a class="cm-retained-person" href="/?view=profile&character=${encodeURIComponent(c.accepted_character_id)}" title="查看已留下人物的主页与真实动态">人物主页与真实动态 →</a>`:''}${!closed(c)?`<button data-encounter-dismiss="${c.id}" title="结束这次临时相遇；不会删除已有记录">错过</button>`:''}</div>${c.source_urls?.length?`<details class="world-sources"><summary title="查看原创灵感所引用的公开网页">灵感来源</summary>${c.source_urls.filter(u=>/^https?:\/\//i.test(u)).map(u=>`<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(u)}</a>`).join('')}</details>`:''}</div></article>`;}).join('')||`<div class="encounter-empty"><strong>${tab==='today'?'今天的相遇还在路上':tab==='ongoing'?'从一句「你好」开始':'还没有留下的人物'}</strong><span>${tab==='today'?'后台会补齐当天名额。已有记录可在「正在认识」继续。':'可以回到今日邂逅，慢慢认识。'}</span><button data-world-refresh title="重新读取真实邂逅记录；不触发生成或生图">刷新记录</button></div>`;
    const failures=(state.daily_slots||[]).filter(s=>s.status==='FAILED');
    if(failures.length)error('部分今日邂逅暂未生成：'+failures.map(s=>`${s.kind==='CLASSIC'?'经典':'原创'}：${s.error||'生成失败'}`).join('；'));
  }
  function renderDetail(c){
    if(detail?.id===c.id&&Date.parse(c.updated_at)<Date.parse(detail.updated_at))return;
    const id=String(c.id),d=c.draft||{},fresh=detailView.dataset.id!==id;
    if(fresh){
      detailView.dataset.id=id;signature='';history=[];
      detailView.innerHTML=`<header class="world-chat-heading"><button data-world-back title="返回邂逅列表并保留对话">‹ 世界邂逅</button><div class="world-chat-person"><h1>${esc(d.name)}</h1><p>${esc(d.identity||'临时相遇')}</p></div><div class="world-chat-actions"><button data-world-call title="询问来客是否愿意接听；仅接听后才启动现有语音服务">☎ 打个电话</button><button class="primary" data-encounter-accept="${c.id}" title="留下后进入正式人物体系，临时聊天记录保留">留下 TA</button></div></header><div class="world-chat-layout"><div class="world-chat-column"><div class="world-chat-context" data-chat-context></div><button class="world-older hidden" data-world-older title="读取更早的持久化聊天记录">查看更早的消息</button><div class="world-message-log" data-world-messages role="log" aria-live="polite" aria-label="临时聊天消息"></div><div class="world-reply-status" role="status" data-reply-status></div><form data-world-composer><textarea rows="2" maxlength="4000" aria-label="临时聊天消息" placeholder="说点什么，慢慢认识彼此……" title="Enter 发送，Shift+Enter 换行；消息先保存，再等待人物回应"></textarea><div><small>临时相遇，也值得认真聊天。</small><button class="primary" type="submit" title="将消息保存到这位来客的临时对话，后台异步回应">发送 ↑</button></div></form><div class="world-chat-error hidden" role="alert" data-chat-error></div></div><aside class="world-relationship"><div data-detail-portrait></div><div class="world-relationship-body"><span class="cm-world-eyebrow">从具体的相处中，认识彼此</span><h2>共同留下的痕迹</h2><div data-world-metrics></div><details><summary title="查看指标的真实来源和通话判定依据">查看变化依据</summary><div data-world-basis></div></details><p>聊得来，也不必每次都有空。拒接电话不会扣分。</p></div></aside></div>`;
    }
    detail=c;const prior=history;history=[...new Map([...prior,...(c.messages||[])].map(m=>[m.id,m])).values()].sort((a,b)=>a.id-b.id);
    const log=detailView.querySelector('[data-world-messages]'),next=JSON.stringify(history),nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<90;
    if(next!==signature){log.innerHTML=`<div class="world-system-line">初次相遇 · ${esc(CM.fmtDate(c.created_at))}</div><div class="world-message"><span>${esc(d.name)}</span><div>${esc(c.opening_message)}</div></div>`+history.map(m=>`<div class="world-message ${m.role==='USER'?'user':''}" data-message-id="${m.id}"><span>${m.role==='USER'?'你':esc(d.name)}</span><div>${esc(m.content)}</div></div>`).join('');signature=next;if(fresh||nearBottom)log.scrollTop=log.scrollHeight;}
    detailView.querySelector('[data-world-older]').classList.toggle('hidden',history.length<50||detailView.dataset.noOlder==='true');
    detailView.querySelector('[data-detail-portrait]').innerHTML=portrait(c);
    const metrics=c.metrics||{};
    detailView.querySelector('[data-world-metrics]').innerHTML=[['双向交谈',`${metrics.mutual_turns||0} 次`],['电话接通',`${metrics.connected_calls||0} 次`],['共同经历',`${metrics.shared_experiences||0} 件`],['熟悉',metrics.familiarity||'暂无记录'],['信任',metrics.trust||'暂无足够依据']].map(([a,b])=>`<div class="world-fact"><span>${a}</span><strong>${esc(b)}</strong></div>`).join('');
    detailView.querySelector('[data-world-basis]').innerHTML=`<p>${esc(metrics.basis||'仅统计已经保存的互动记录。')}</p>`+(c.calls||[]).map(call=>`<p>${esc(CM.fmtDate(call.created_at))} ${esc(CM.fmtTime(call.created_at))} · ${esc({DECLINED:'未接听',ACCEPTED:'愿意接听',CONNECTED:'已连接',ENDED:'通话结束',CANCELLED:'已取消',EXPIRED:'已过期',FAILED:'邀请失败',RINGING:'等待回应'}[call.status]||call.status)}${call.reason?'：'+esc(call.reason):''}</p>`).join('');
    detailView.querySelector('[data-chat-context]').textContent=c.status==='ACCEPTED'?'已留下 · 这里保留原始邂逅，正式聊天可从人物列表继续':closed(c)?'这次相遇已结束 · 已有记录仍保留':'正在认识 · 不占正式人物位';
    const status=detailView.querySelector('[data-reply-status]');status.innerHTML=c.reply_pending?'TA 正在回应……':c.error?`${esc(c.error)} <button data-world-retry title="重新排队处理已保存的最后一条用户消息；不重复发送">重试回复</button>`:'';
    detailView.querySelector('textarea').disabled=closed(c);detailView.querySelector('[type=submit]').disabled=closed(c)||sending;
    detailView.querySelector('[data-world-call]').disabled=closed(c);detailView.querySelector('[data-encounter-accept]').disabled=closed(c)||!c.can_accept;
  }
  function chatError(message){const box=detailView.querySelector('[data-chat-error]');if(box){box.textContent=message||'';box.classList.toggle('hidden',!message);}}
  async function refreshSelected(epoch=generation){if(!visible||!selected)return;clearTimeout(timer);if(document.hidden){timer=setTimeout(()=>refreshSelected(epoch),2000);return;}const id=selected;try{const c=await api.detail(id);if(epoch!==generation||selected!==id||!visible)return;renderDetail(c);chatError('');}catch(e){if(epoch===generation)chatError('读取失败：'+e.message);}finally{if(epoch===generation&&visible&&selected)timer=setTimeout(()=>refreshSelected(epoch),2000);}}
  async function select(id,{write=true}={}){
    const epoch=++generation;clearTimeout(timer);selected=String(id);if(write)url(selected);listView.classList.add('hidden');detailView.classList.remove('hidden');detailView.removeAttribute('data-id');delete detailView.dataset.noOlder;detailView.innerHTML='<div class="encounter-empty">正在读取这段相遇……</div>';
    try{const c=await api.detail(id);if(epoch!==generation)return;renderDetail(c);if(c.status==='NEW')await api.seen(id);await api.refresh(tab);}catch(e){if(epoch===generation)detailView.innerHTML=`<div class="encounter-empty">${esc(e.message)}<button data-world-back title="返回邂逅列表">返回邂逅</button></div>`;}
    if(epoch===generation)timer=setTimeout(()=>refreshSelected(epoch),2000);
  }
  async function showList({write=true}={}){generation++;clearTimeout(timer);selected=null;detail=null;listView.classList.remove('hidden');detailView.classList.add('hidden');if(write)url();renderList();try{await api.refresh(tab);error('');renderList();}catch(e){error('邂逅读取失败：'+e.message);}}
  function close(){visible=false;generation++;clearTimeout(timer);page.classList.add('hidden');document.body.classList.remove('world-mode');}
  async function open(){visible=true;CM.features.space?.close();CM.features.profile?.close();CM.closeDrawer();document.body.classList.add('world-mode');page.classList.remove('hidden');const params=new URLSearchParams(location.search);tab=['today','ongoing','kept'].includes(params.get('world_tab'))?params.get('world_tab'):'today';const id=params.get('encounter');if(id)await select(id,{write:false});else await showList({write:false});}
  async function requestCall(){
    if(!selected||!detail||closed(detail))return;const id=selected,epoch=++callGeneration;
    dialog.innerHTML=`<h2>${esc(detail.draft.name)}</h2><p role="status">正在等待 TA 的回应……</p><button data-call-close title="取消等待；不会启动麦克风">取消邀请</button>`;dialog.showModal();
    try{const call=await api.requestCall(id);if(epoch!==callGeneration||selected!==id||!visible){if(call.session_token)await CM.api(`/v1/encounters/${id}/calls/${call.id}/session`,{method:'POST',body:JSON.stringify({session_token:call.session_token,action:'ENDED'})});return;}
      if(call.status==='DECLINED'){dialog.innerHTML=`<h2>TA 暂时没有接听</h2><blockquote>${esc(call.reason)}</blockquote><p>这次拒接不会扣除任何关系分数。</p><button data-call-close title="回到文字聊天，保留电话记录">回到聊天</button>`;return;}
      if(!CM.features.voice?.startEncounter)throw Error('语音服务模块未就绪，请刷新后重试。');
      dialog.close();await CM.features.voice.startEncounter(detail,call);
    }catch(e){if(epoch===callGeneration){dialog.innerHTML=`<h2>暂时无法开始电话</h2><p>${esc(e.message)}</p><button data-call-close title="关闭错误并回到文字聊天">回到聊天</button>`;if(!dialog.open)dialog.showModal();}}
  }
  dialog.addEventListener('click',e=>{if(e.target.closest('[data-call-close]')){callGeneration++;dialog.close();}});dialog.addEventListener('cancel',()=>{callGeneration++;});
  async function click(e){const b=e.target.closest('button');if(!b)return;try{
    if(b.dataset.worldOpen||b.dataset.encounterChat){if(!visible)await open();await select(b.dataset.worldOpen||b.dataset.encounterChat);}
    else if(b.hasAttribute('data-world-back'))await showList();
    else if(b.dataset.worldTab){tab=b.dataset.worldTab;url();await api.refresh(tab);renderList();}
    else if(b.hasAttribute('data-world-more')){b.disabled=true;try{await api.refresh(tab,{append:true});}finally{b.disabled=false;}}
    else if(b.hasAttribute('data-world-refresh'))await showList();
    else if(b.dataset.encounterAccept){b.disabled=true;try{const accepted=await api.accept(b.dataset.encounterAccept);if(accepted&&selected===String(accepted.id))renderDetail(await api.detail(accepted.id));}finally{b.disabled=false;}}
    else if(b.dataset.encounterDismiss){b.disabled=true;await api.dismiss(b.dataset.encounterDismiss);renderList();}
    else if(b.dataset.worldPortrait){b.disabled=true;try{await api.portrait(b.dataset.worldPortrait);await api.refresh();if(selected)renderDetail(await api.detail(selected));}finally{b.disabled=false;}}
    else if(b.hasAttribute('data-world-call'))await requestCall();
    else if(b.hasAttribute('data-world-retry')){await api.retry(selected);await refreshSelected();}
    else if(b.hasAttribute('data-world-older')){const id=selected,epoch=generation,oldHeight=detailView.querySelector('[data-world-messages]').scrollHeight;const result=await api.history(id,history[0]?.id);if(epoch!==generation)return;history=[...result.messages,...history];detailView.dataset.noOlder=String(!result.has_more);signature='';renderDetail(detail);const log=detailView.querySelector('[data-world-messages]');log.scrollTop=log.scrollHeight-oldHeight;}
  }catch(err){if(selected)chatError(err.message);else error(err.message);b.disabled=false;}}
  page.addEventListener('click',click);side.addEventListener('click',click);
  page.addEventListener('submit',async e=>{if(!e.target.matches('[data-world-composer]'))return;e.preventDefault();const input=e.target.querySelector('textarea'),message=input.value.trim(),id=selected,epoch=generation;if(!message||sending||!id)return;sending=true;e.target.querySelector('[type=submit]').disabled=true;chatError('');try{await api.send(id,message);if(epoch!==generation)return;input.value='';renderDetail(await api.detail(id));}catch(err){if(epoch===generation)chatError(err.message);}finally{sending=false;if(epoch===generation&&detail)renderDetail(detail);}});
  page.addEventListener('keydown',e=>{if(e.target.matches('textarea')&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();e.target.form.requestSubmit();}});
  const sources=document.createElement('a');sources.href='/sources';sources.className='world-sources-entry';sources.textContent='订阅阅读';sources.title='查看已有 RSS 订阅与阅读来源';page.querySelector('.cm-world-heading').append(sources);
  CM.on('encountersChanged',renderList);CM.on('conversationChanged',close);CM.registerFeature('world',{open,close,get candidateId(){return selected;}});
  CM.on('ready',()=>{if(new URLSearchParams(location.search).get('view')==='world')open();});
})();
