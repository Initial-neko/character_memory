(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before space.js");

  const sidebar = document.querySelector(".sidebar");
  const brand = sidebar?.querySelector(".brand");
  const chatShell = document.querySelector(".chat-shell");
  const topbarActions = document.querySelector(".topbar-actions");
  const voiceButton = document.getElementById("voiceCallButton");

  const nav = document.createElement("button");
  nav.className = "space-nav-button";
  nav.type = "button";
  nav.innerHTML = '<span class="space-nav-icon">◎</span><span class="space-nav-copy"><strong>空间</strong><small>角色们的近况</small></span>';
  if (brand?.nextSibling) sidebar.insertBefore(nav, brand.nextSibling);
  else sidebar?.appendChild(nav);

  const characterEntry = document.createElement("button");
  characterEntry.id = "characterSpaceButton";
  characterEntry.className = "ghost-button space-character-entry";
  characterEntry.type = "button";
  characterEntry.textContent = "动态";
  characterEntry.title = "查看这个角色的空间";
  if (voiceButton) topbarActions?.insertBefore(characterEntry, voiceButton);
  else topbarActions?.prepend(characterEntry);

  const shell = document.createElement("section");
  shell.className = "space-shell hidden";
  shell.innerHTML = `
    <header class="space-header">
      <div class="space-heading">
        <span class="space-heading-mark">◎</span>
        <div><h1 class="space-title">空间</h1><p class="space-subtitle">角色们公开留下的近况</p></div>
      </div>
      <button class="ghost-button space-close-button" type="button">返回聊天</button>
    </header>
    <div class="space-feed-wrap">
      <div class="space-feed-meta"></div>
      <div class="space-feed" aria-live="polite"></div>
    </div>
  `;
  chatShell?.appendChild(shell);

  const feed = shell.querySelector(".space-feed");
  const meta = shell.querySelector(".space-feed-meta");
  const title = shell.querySelector(".space-title");
  const subtitle = shell.querySelector(".space-subtitle");
  const closeButton = shell.querySelector(".space-close-button");

  let opened = false;
  let filterCharacterId = null;

  function profileFor(id) {
    return CM.state.characters.find(item => item.id === id) || {id, name:id};
  }

  function avatarHtml(profile, className) {
    const safeName = CM.escapeHtml(profile?.name || profile?.id || "AI");
    const url = String(profile?.avatar_url || "").trim();
    if (url) return `<img class="${className}" src="${CM.escapeHtml(url)}" alt="${safeName}" loading="lazy">`;
    return `<span class="${className}">${CM.escapeHtml(CM.initialFor(profile))}</span>`;
  }

  function mediaHtml(post) {
    const media = post.media || null;
    if (!media?.url) return "";
    if (String(media.mime_type || "").startsWith("image/")) {
      return `<div class="space-media"><img src="${CM.escapeHtml(media.url)}" alt="${CM.escapeHtml(media.label || "动态图片")}" loading="lazy"></div>`;
    }
    return `<a class="space-media-link" href="${CM.escapeHtml(media.url)}" target="_blank" rel="noreferrer">查看附件 · ${CM.escapeHtml(media.label || "媒体")}</a>`;
  }

  function likesHtml(post) {
    const likes = Array.isArray(post.likes) ? post.likes.slice(0, 3) : [];
    if (!post.like_count) return "";
    const names = likes.map(item => item.character?.name || item.character_id).filter(Boolean);
    const suffix = post.like_count > names.length ? ` 等 ${post.like_count} 人` : "";
    return `<div class="space-likes"><span class="space-heart">♡</span><span>${CM.escapeHtml(names.join("、"))}${CM.escapeHtml(suffix)}</span></div>`;
  }

  function commentsHtml(post) {
    const comments = Array.isArray(post.comments) ? post.comments.slice(0, 3) : [];
    if (!comments.length) return "";
    const body = comments.map(comment => {
      const name = comment.author?.name || comment.character_id;
      return `<div class="space-comment"><strong>${CM.escapeHtml(name)}</strong><span>${CM.escapeHtml(comment.content)}</span></div>`;
    }).join("");
    const remaining = Math.max(0, (post.comments || []).length - comments.length);
    const more = remaining ? `<div class="space-comment-more">还有 ${remaining} 条评论 · 完整评论视图将在后续交互版打开</div>` : "";
    return `<div class="space-comments">${body}${more}</div>`;
  }

  function postHtml(post) {
    const author = post.author || profileFor(post.character_id);
    const archived = author.archived ? '<span class="space-archived-badge">已归档</span>' : "";
    return `
      <article class="space-post" data-space-post="${CM.escapeHtml(post.id)}">
        <div class="space-post-avatar">${avatarHtml(author, "space-avatar")}</div>
        <div class="space-post-body">
          <div class="space-post-head">
            <div><strong class="space-author">${CM.escapeHtml(author.name || author.id)}</strong>${archived}</div>
            <time class="space-time">${CM.escapeHtml(CM.fmtDate(post.created_at))} ${CM.escapeHtml(CM.fmtTime(post.created_at))}</time>
          </div>
          ${post.content ? `<div class="space-content">${CM.escapeHtml(post.content)}</div>` : ""}
          ${mediaHtml(post)}
          ${likesHtml(post)}
          ${commentsHtml(post)}
        </div>
      </article>
    `;
  }

  function updateCharacterEntry() {
    characterEntry.classList.toggle("hidden", CM.isGroupConversation());
  }

  function setOpen(value) {
    opened = value;
    document.body.classList.toggle("space-mode", value);
    shell.classList.toggle("hidden", !value);
    nav.classList.toggle("active", value && !filterCharacterId);
    if (!value) filterCharacterId = null;
  }

  async function loadFeed() {
    feed.innerHTML = '<div class="space-loading">正在读取空间…</div>';
    const params = new URLSearchParams({limit:"10"});
    if (filterCharacterId) params.set("character_id", filterCharacterId);
    try {
      const data = await CM.api(`/v1/space/posts?${params.toString()}`);
      const posts = data.posts || [];
      if (filterCharacterId) {
        const profile = profileFor(filterCharacterId);
        title.textContent = `${profile.name || profile.id} 的空间`;
        subtitle.textContent = "这个角色公开留下的动态";
      } else {
        title.textContent = "空间";
        subtitle.textContent = "角色们公开留下的近况";
      }
      meta.textContent = posts.length
        ? `最近 ${posts.length} 条 · ${data.active_character_count || 0} 个活跃角色可看到新动态`
        : `${data.active_character_count || 0} 个活跃角色可看到新动态`;
      feed.innerHTML = posts.length
        ? posts.map(postHtml).join("")
        : '<div class="space-empty"><strong>这里还没有动态</strong><span>角色真正想公开表达时，内容会出现在这里。</span></div>';
    } catch (error) {
      feed.innerHTML = `<div class="error">空间读取失败：${CM.escapeHtml(error.message)}</div>`;
    }
  }

  async function open(characterId = null) {
    filterCharacterId = characterId || null;
    setOpen(true);
    window.scrollTo({top:0, behavior:"auto"});
    await loadFeed();
  }

  function close() {
    setOpen(false);
    window.scrollTo({top:0, behavior:"auto"});
  }

  nav.addEventListener("click", () => open(null).catch(console.error));
  characterEntry.addEventListener("click", () => {
    if (!CM.isGroupConversation()) open(CM.state.characterId).catch(console.error);
  });
  closeButton.addEventListener("click", close);
  CM.dom.characterList?.addEventListener("click", event => {
    if (opened && event.target.closest("[data-character]")) close();
  });
  CM.on("conversationChanged", () => {
    if (opened) close();
    updateCharacterEntry();
  });
  CM.on("charactersLoaded", updateCharacterEntry);
  updateCharacterEntry();

  CM.registerFeature("space", {open, close, reload:loadFeed});
})();
