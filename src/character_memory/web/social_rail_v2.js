(() => {
  const CM = window.CM, shell = document.querySelector('.space-shell');
  if (!CM || !shell) return;
  shell.classList.add('cm-with-social-rail');
  const rail = document.createElement('aside');
  rail.className = 'cm-social-rail'; rail.setAttribute('aria-label','Space 相关入口');
  rail.innerHTML = '<section class="cm-social-card cm-discovery-card"><img src="/static/stickers/default/round_cat_happy.svg" alt=""><h2>遇见新的朋友</h2><p>看看已有的邂逅介绍，决定要不要打个招呼。</p><button type="button" data-rail-world>去 World 看看 →</button></section><details class="cm-social-card" data-rail-experiences><summary>最近的共同经历</summary><div data-rail-records></div></details>';
  shell.append(rail);
  rail.querySelector('[data-rail-world]').addEventListener('click', () => document.querySelector('.cm-global-nav [data-cm-nav="world"]').click());
  const details = rail.querySelector('details'), records = rail.querySelector('[data-rail-records]');
  let epoch=0;
  details.addEventListener('toggle', async () => {
    const token=++epoch, character=CM.state.characterId;
    if (!details.open) return;
    records.textContent='正在读取已有记录…';
    try {
      const data=await CM.api(`/v1/characters/${encodeURIComponent(character)}/life?view=relationships&peer=user&limit=3`);
      if (token!==epoch || character!==CM.state.characterId || !details.open) return;
      const items=Array.isArray(data.items)?data.items:[];
      records.innerHTML=items.map(item=>`<article><strong>${CM.escapeHtml(item.title)}</strong><p>${CM.escapeHtml(item.content)}</p><a href="/?${CM.escapeHtml(new URLSearchParams({life_character:character,life_key:item.key}).toString())}">查看来源并准备聊天 →</a></article>`).join('') || '<p>当天没有可确认的共同记录；不据此推断关系。</p>';
    } catch { if (token===epoch && details.open) records.textContent='记录暂时无法读取，请收起后再试。'; }
  });
  CM.on('conversationChanged', () => { ++epoch; details.open=false; records.replaceChildren(); });
  const context=document.createElement('nav'); context.className='cm-space-context'; context.setAttribute('aria-label','Space 导航');
  context.innerHTML='<button type="button" data-context-all>全部动态</button><button type="button" data-context-current>当前人物的动态</button><button type="button" data-context-world>世界来客</button><a href="/sources">订阅阅读</a>';
  document.querySelector('.sidebar-body')?.prepend(context);
  context.querySelector('[data-context-all]').addEventListener('click',()=>CM.features.space.open(null).catch(console.error));
  context.querySelector('[data-context-current]').addEventListener('click',()=>CM.features.space.open(CM.state.characterId).catch(console.error));
  context.querySelector('[data-context-world]').addEventListener('click',()=>document.querySelector('.cm-global-nav [data-cm-nav="world"]').click());
  CM.on('conversationChanged',()=>{context.querySelector('[data-context-current]').hidden=CM.isGroupConversation();});
})();
