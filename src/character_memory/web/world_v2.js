(() => {
  const CM = window.CM;
  const encounter = document.querySelector('.encounter-section');
  if (!CM || !encounter) return;
  const page = document.createElement('section');
  page.className = 'cm-world-page hidden';
  page.setAttribute('aria-label', 'World 人物发现');
  page.innerHTML = '<header class="cm-world-heading"><div><span class="cm-world-eyebrow">WORLD · 遇见</span><h1>遇见新的朋友</h1><p>从已有的邂逅候选开始认识，不急着决定让谁留下。</p></div><a href="/sources">订阅阅读</a></header><div class="cm-world-layout"><div class="cm-world-candidates"></div><aside class="cm-world-note"><h2>认识，不等于留下</h2><p>先看看介绍和灵感来源，再决定是否打个招呼。只有你确认留下，才会加入正式人物列表。</p><p>这里展示已有邂逅记录，不把候选人物当作已经发生的公开来访。</p><a href="/life">查看人物生活 →</a></aside></div>';
  page.querySelector('.cm-world-candidates').append(encounter);
  document.querySelector('.chat-shell').append(page);
  function close() { page.classList.add('hidden'); document.body.classList.remove('world-mode'); }
  function open() {
    CM.features.space?.close(); CM.features.profile?.close(); CM.closeDrawer();
    document.body.classList.add('world-mode'); page.classList.remove('hidden');
    CM.features.encounter?.refresh();
  }
  CM.on('conversationChanged', close);
  CM.registerFeature('world', {open, close});
  CM.on('ready', () => { if (new URLSearchParams(location.search).get('view') === 'world') open(); });
})();
