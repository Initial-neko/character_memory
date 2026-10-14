(() => {
  const CM = window.CM;
  let ready = !CM;
  let pendingNavigation = null;
  const conversationKey = () => CM ? `${CM.state.conversation.type}:${CM.state.conversation.groupId || CM.state.characterId}` : '';
  let handledConversation = conversationKey();
  document.body.classList.add('cm-user-page');
  const icon = paths => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">${paths}</svg>`;
  const chatIcon = icon('<path d="M21 11a9 9 0 0 1-9 9H4l-2 2V11a9 9 0 1 1 19 0Z"/><path d="M7 11h10"/>');
  const worldIcon = icon('<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>');
  const personIcon = icon('<circle cx="12" cy="7" r="4"/><path d="M4 22v-3a8 8 0 0 1 16 0v3"/>');
  const spaceIcon = icon('<rect x="4" y="5" width="16" height="16" rx="5"/><path d="M8 2v5M16 2v5M8 12h8M8 16h5"/>');
  const header = document.createElement('header');
  header.className = 'cm-global-header';
  header.innerHTML = `<a class="cm-global-brand" href="/" aria-label="Character Memory 首页"><img src="/static/stickers/default/round_cat_happy.svg" alt=""><span><strong>Character Memory</strong><small>让人物拥有自己的生活</small></span></a><nav class="cm-global-nav" aria-label="页面导航"><button type="button" data-cm-nav="space">${spaceIcon}Space</button><button type="button" data-cm-nav="chat">${chatIcon}聊天</button><a href="/?view=world" data-cm-nav="world">${worldIcon}World</a><a href="/?view=profile" data-cm-nav="people">${personIcon}人物</a></nav>${CM ? '<button class="cm-global-search" type="button" title="打开现有聊天搜索">搜索人物与聊天内容</button>' : ''}`;
  document.body.prepend(header);
  const notifications = document.querySelector('.space-notification-button');
  if (notifications) {
    notifications.classList.add('cm-global-notification');
    notifications.title = '查看空间提醒';
    notifications.setAttribute('aria-label', '空间提醒');
    notifications.querySelector('span').innerHTML = icon('<path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5Z"/><path d="M10 20h4"/>');
    header.append(notifications);
    notifications.addEventListener('click', event => {
      if (!ready || conversationKey() !== handledConversation) {
        event.stopImmediatePropagation(); event.preventDefault(); pendingNavigation='notifications'; return;
      }
      CM.features.world?.close(); CM.features.profile?.close();
      const next=routeUrl('space');
      if (next.href!==location.href) history.pushState(null,'',next);
    }, {capture:true});
  }
  function active() {
    const selected = location.pathname === '/sources' || document.body.classList.contains('world-mode') ? 'world' : location.pathname === '/life' || document.body.classList.contains('profile-mode') ? 'people' : document.body.classList.contains('space-mode') ? 'space' : 'chat';
    header.querySelectorAll('[data-cm-nav]').forEach(node => {
      if (node.dataset.cmNav === selected) node.setAttribute('aria-current','page');
      else node.removeAttribute('aria-current');
    });
  }
  function routeUrl(mode, original = location.href) {
    const url = new URL(original);
    url.searchParams.delete('space_post');
    if (mode === 'chat') url.searchParams.delete('view');
    else url.searchParams.set('view', mode === 'people' ? 'profile' : mode === 'notifications' ? 'space' : mode);
    return url;
  }
  function navigate(mode, {writeHistory = true} = {}) {
    if (!CM) { location.assign(mode === 'chat' ? '/' : `/?view=${mode === 'people' ? 'profile' : mode}`); return; }
    if (!ready) { pendingNavigation = mode; return; }
    // A character/group switch may have updated state before its listeners finish.
    // Honor the navigation chosen during that switch after the close handlers run.
    if (conversationKey() !== handledConversation) { pendingNavigation = mode; return; }
    const original = location.href;
    const next = routeUrl(mode, original);
    CM.features.world?.close(); CM.features.profile?.close(); CM.features.space?.close();
    if (writeHistory && next.href !== original) history.pushState(null, '', next);
    else if (writeHistory && next.href !== location.href) history.replaceState(null, '', next);
    if (mode === 'space') CM.features.space?.open(null).catch(console.error);
    else if (mode === 'world') CM.features.world?.open();
    else if (mode === 'people') CM.features.profile?.open(CM.state.characterId).catch(console.error);
    else if (mode === 'notifications') notifications?.click();
    active();
  }
  header.querySelectorAll('[data-cm-nav]').forEach(node => node.addEventListener('click', event => {
    event.preventDefault(); navigate(node.dataset.cmNav);
  }));
  header.querySelector('.cm-global-search')?.addEventListener('click', () => document.getElementById('conversationSearchButton')?.click());
  new MutationObserver(active).observe(document.body, {attributes:true, attributeFilter:['class']});
  active();
  if (!CM) return;
  CM.on('ready', () => {
    ready = true;
    document.body.dataset.cmReady = 'true';
    const requested = pendingNavigation;
    pendingNavigation = null;
    if (requested) navigate(requested);
    else if (new URLSearchParams(location.search).get('view') === 'profile') navigate('people', {writeHistory:false});
  });
  window.addEventListener('popstate', () => {
    const params = new URLSearchParams(location.search);
    if (params.has('space_post')) { CM.features.world?.close(); CM.features.profile?.close(); return; }
    const view = params.get('view');
    // Space owns its detail-to-feed restoration, including the character filter.
    if (view === 'space' && document.body.classList.contains('space-mode')) {
      CM.features.world?.close(); CM.features.profile?.close(); active(); return;
    }
    navigate(view === 'profile' ? 'people' : ['world','space'].includes(view) ? view : 'chat', {writeHistory:false});
  });
  CM.on('conversationChanged', () => {
    handledConversation = conversationKey();
    if (!ready) return;
    const requested = pendingNavigation;
    pendingNavigation = null;
    if (requested) navigate(requested);
    else history.replaceState(null, '', routeUrl('chat'));
  });
  const actions = document.querySelector('.topbar-actions');
  const shared = document.createElement('button');
  shared.className = 'ghost-button cm-shared-button';
  shared.type = 'button'; shared.textContent = '共同经历';
  shared.title = '点击查看经历入口；不会自动发送消息或调用模型';
  actions?.prepend(shared);
  shared.addEventListener('click', () => {
    const id = CM.state.characterId;
    CM.openDrawer('共同经历', '已有记录的只读入口');
    CM.dom.drawerBody.innerHTML = `<section class="cm-shared-entry"><h3>从经历继续认识彼此</h3><p>查看已记录的互动、原始来源和关联记忆。没有记录的部分不推测，也不会替你发送消息。</p><a href="/life?character=${encodeURIComponent(id)}&view=relationships">查看共同经历 →</a></section>`;
  });
  const profile = document.createElement('button');
  profile.className = 'cm-profile-link'; profile.type = 'button'; profile.textContent = '人物主页 ›';
  profile.title = '查看当前人物主页';
  document.querySelector('.character-heading-copy')?.append(profile);
  profile.addEventListener('click', () => navigate('people'));
  function sync() { shared.hidden = CM.isGroupConversation(); profile.hidden = CM.isGroupConversation(); }
  CM.on('conversationChanged', sync); CM.on('ready', sync); sync();
})();
