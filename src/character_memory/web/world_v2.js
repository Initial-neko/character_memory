(() => {
  const CM = window.CM;
  const encounter = document.querySelector('.encounter-section');
  if (!CM || !encounter) return;
  const page = document.createElement('section');
  page.className = 'cm-world-page hidden';
  page.setAttribute('aria-label', 'World 人物发现');
  page.innerHTML = '<header class="cm-world-heading"><div><span class="cm-world-eyebrow">WORLD · 遇见</span><h1>遇见新的朋友</h1><p>从已有的邂逅候选开始认识，不急着决定让谁留下。</p></div><a href="/sources">订阅阅读</a></header><div class="cm-world-layout"><div class="cm-world-candidates"></div><aside class="cm-world-note"><h2>认识，不等于留下</h2><p>先看看介绍和灵感来源，再决定是否打个招呼。只有你确认留下，才会加入正式人物列表。</p><p>这里展示已有邂逅记录，不把候选人物当作已经发生的公开来访。</p><a href="/life">查看人物生活 →</a></aside></div>';
  page.querySelector('.cm-world-candidates').append(encounter);
  const retained = document.createElement('section');
  retained.className = 'cm-world-note cm-world-retained';
  retained.setAttribute('aria-label', '已留下的人物');
  page.querySelector('.cm-world-layout').append(retained);
  let retainedRequest = 0;
  async function loadRetained() {
    const request = ++retainedRequest;
    retained.innerHTML = '<h2>已留下的人物</h2><p>正在读取最近的邂逅记录…</p>';
    try {
      const data = await CM.api('/v1/encounters?include_closed=true&limit=50');
      if (request !== retainedRequest) return;
      const accepted = (data.encounters || []).filter(item => item.status === 'ACCEPTED' && item.accepted_character_id);
      retained.innerHTML = '<h2>已留下的人物</h2><p>最近 50 条邂逅中已确认留下的朋友。进入主页查看她实际发布的动态；接纳不代表已经发生公开互动。</p>' + (accepted.map(item => {
        const id = item.accepted_character_id;
        const profile = CM.state.characters.find(character => character.id === id);
        return `<a class="cm-retained-person" href="/?view=profile&character=${encodeURIComponent(id)}">${profile?.avatar_url ? `<img src="${CM.escapeHtml(profile.avatar_url)}" alt="" loading="lazy">` : ''}<span>${CM.escapeHtml(profile?.name || item.draft?.name || id)}<small>人物主页与真实动态 →</small></span></a>`;
      }).join('') || '<p>这批记录中还没有已留下的人物。认识、试聊和留下是不同阶段。</p>');
    } catch { if (request === retainedRequest) retained.innerHTML = '<h2>已留下的人物</h2><p>记录暂时无法读取，请重新进入 World 重试。</p>'; }
  }
  document.querySelector('.chat-shell').append(page);
  function close() { page.classList.add('hidden'); document.body.classList.remove('world-mode'); }
  function open() {
    CM.features.space?.close(); CM.features.profile?.close(); CM.closeDrawer();
    document.body.classList.add('world-mode'); page.classList.remove('hidden');
    CM.features.encounter?.refresh();
    loadRetained();
  }
  CM.on('conversationChanged', close);
  CM.on('charactersLoaded', () => { if (document.body.classList.contains('world-mode')) loadRetained(); });
  CM.registerFeature('world', {open, close});
  CM.on('ready', () => { if (new URLSearchParams(location.search).get('view') === 'world') open(); });
})();
