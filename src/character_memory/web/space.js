(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before space.js");

  const sidebarSlot = document.getElementById("sidebarSpace");
  const chatShell = document.querySelector(".chat-shell");
  const topbarActions = document.querySelector(".topbar-actions");

  const nav = document.createElement("button");
  nav.className = "space-nav-button";
  nav.type = "button";
  nav.innerHTML = '<span class="space-nav-icon">◎</span><span class="space-nav-copy"><strong>空间</strong><small>角色们的近况</small></span>';
  sidebarSlot?.appendChild(nav);

  const characterEntry = document.createElement("button");
  characterEntry.id = "characterSpaceButton";
  characterEntry.className = "ghost-button space-character-entry desktop-only-control";
  characterEntry.type = "button";
  characterEntry.textContent = "查看动态";
  characterEntry.title = "查看当前人物的空间";
  topbarActions?.appendChild(characterEntry);

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
  let voicePlayer = {audio:null, button:null};
  let postsById = new Map();
  const expandedComments = new Set();

  function profileFor(id) {
    return CM.state.characters.find(item => item.id === id) || {id, name:id};
  }

  function avatarHtml(profile, className) {
    const safeName = CM.escapeHtml(profile?.name || profile?.id || "AI");
    const url = String(profile?.avatar_url || "").trim();
    if (url) return `<img class="${className}" src="${CM.escapeHtml(url)}" alt="${safeName}" loading="lazy">`;
    return `<span class="${className}">${CM.escapeHtml(CM.initialFor(profile))}</span>`;
  }

  function mediaItemsFor(post) {
    if (Array.isArray(post.media_items)) return post.media_items.slice(0, 9);
    return post.media ? [post.media] : [];
  }

  function voiceHtml(item, index) {
    const metadata = item.metadata || {};
    const durationMs = Number(metadata.duration_ms || 0);
    const seconds = durationMs > 0 ? Math.max(1, Math.round(durationMs / 1000)) : 0;
    const transcript = String(metadata.transcript || "").trim();
    const width = Math.min(280, 108 + Math.min(seconds || 4, 34) * 5);
    return `<div class="space-voice" data-space-voice="${CM.escapeHtml(item.media_id || index)}">
      <button class="space-voice-bubble" type="button" data-space-voice-play data-audio-url="${CM.escapeHtml(item.url)}" style="--space-voice-width:${width}px" aria-label="播放空间语音">
        <span class="space-voice-glyph" aria-hidden="true">)))</span>
        <span class="space-voice-duration">${seconds ? `${seconds}"` : "语音"}</span>
      </button>
      ${transcript ? `<button class="space-voice-text-button" type="button" data-space-voice-text>文本</button><div class="space-voice-transcript hidden" data-space-voice-transcript>${CM.escapeHtml(transcript)}</div>` : ""}
    </div>`;
  }

  function mediaHtml(post) {
    const items = mediaItemsFor(post).filter(item => item?.available !== false && item?.url);
    if (!items.length) return "";

    const images = items.filter(item =>
      item.media_type === "IMAGE" || String(item.mime_type || "").startsWith("image/")
    ).slice(0, 9);
    const voices = items.filter(item =>
      item.media_type === "VOICE" || String(item.mime_type || "").startsWith("audio/")
    ).slice(0, 1);
    const other = items.filter(item => !images.includes(item) && !voices.includes(item));

    let imageHtml = "";
    if (images.length) {
      const layout = images.length === 1 ? "single" : images.length <= 4 ? "quad" : "nine";
      const cells = images.map((item, index) => {
        const label = item.label || `动态图片 ${index + 1}`;
        return `<a class="space-media-cell" href="${CM.escapeHtml(item.url)}" target="_blank" rel="noreferrer"><img src="${CM.escapeHtml(item.url)}" alt="${CM.escapeHtml(label)}" loading="lazy"></a>`;
      }).join("");
      imageHtml = `<div class="space-media-grid space-media-${layout}" data-space-media-count="${images.length}">${cells}</div>`;
    }

    const voiceMediaHtml = voices.map(voiceHtml).join("");
    const links = other.map(item =>
      `<a class="space-media-link" href="${CM.escapeHtml(item.url)}" target="_blank" rel="noreferrer">查看附件 · ${CM.escapeHtml(item.label || "媒体")}</a>`
    ).join("");

    return `${imageHtml}${voiceMediaHtml}${links}`;
  }

  function stopVoice() {
    if (voicePlayer.audio) {
      voicePlayer.audio.pause();
      voicePlayer.audio.currentTime = 0;
    }
    voicePlayer.button?.classList.remove("playing");
    voicePlayer = {audio:null, button:null};
  }

  function likesHtml(post) {
    const likes = Array.isArray(post.likes) ? post.likes.slice(0, 3) : [];
    if (!post.like_count) return "";
    const names = likes.map(item => item.character?.name || item.character_id).filter(Boolean);
    const suffix = post.like_count > names.length ? ` 等 ${post.like_count} 人` : "";
    return `<div class="space-likes"><span class="space-heart">♡</span><span>${CM.escapeHtml(names.join("、"))}${CM.escapeHtml(suffix)}</span></div>`;
  }

  function commentsHtml(post) {
    const allComments = Array.isArray(post.comments) ? post.comments : [];
    const expanded = expandedComments.has(String(post.id));
    const comments = expanded ? allComments : allComments.slice(0, 3);
    const body = comments.map(comment => {
      const name = comment.author?.name || (comment.actor_type === "USER" ? "我" : comment.character_id);
      const actorClass = comment.actor_type === "USER" ? " space-comment-user" : "";
      return `<div class="space-comment${actorClass}"><strong>${CM.escapeHtml(name)}</strong><span>${CM.escapeHtml(comment.content)}</span></div>`;
    }).join("");
    const toggle = allComments.length > 3
      ? `<button class="space-comments-toggle" type="button" data-space-comments-toggle="${CM.escapeHtml(post.id)}">${expanded ? "收起评论" : `查看全部 ${allComments.length} 条评论`}</button>`
      : "";
    return `<div class="space-comments">
      <div class="space-comments-list">${body || '<div class="space-comments-empty">还没有评论</div>'}</div>
      ${toggle}
      <form class="space-comment-form" data-space-comment-form="${CM.escapeHtml(post.id)}">
        <textarea class="space-comment-input" name="content" rows="1" maxlength="1000" placeholder="评论这条动态…" aria-label="评论这条动态"></textarea>
        <button class="space-comment-submit" type="submit">发送</button>
        <div class="space-comment-error hidden" aria-live="polite"></div>
      </form>
    </div>`;
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

  function replacePost(post) {
    if (!post) return;
    postsById.set(String(post.id), post);
    const target = Array.from(feed.querySelectorAll("[data-space-post]"))
      .find(node => node.dataset.spacePost === String(post.id));
    if (target) target.outerHTML = postHtml(post);
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
      postsById = new Map(posts.map(post => [String(post.id), post]));
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
    stopVoice();
    setOpen(false);
    window.scrollTo({top:0, behavior:"auto"});
  }

  feed.addEventListener("click", event => {
    const commentsToggle = event.target.closest("[data-space-comments-toggle]");
    if (commentsToggle) {
      const postId = String(commentsToggle.dataset.spaceCommentsToggle || "");
      if (expandedComments.has(postId)) expandedComments.delete(postId);
      else expandedComments.add(postId);
      replacePost(postsById.get(postId));
      return;
    }

    const play = event.target.closest("[data-space-voice-play]");
    if (play) {
      if (voicePlayer.button === play && voicePlayer.audio) {
        if (voicePlayer.audio.paused) {
          voicePlayer.audio.play().catch(console.warn);
          play.classList.add("playing");
        } else {
          voicePlayer.audio.pause();
          play.classList.remove("playing");
        }
        return;
      }
      stopVoice();
      const audio = new Audio(play.dataset.audioUrl);
      voicePlayer = {audio, button:play};
      play.classList.add("playing");
      audio.addEventListener("ended", stopVoice, {once:true});
      audio.addEventListener("error", () => {
        play.classList.remove("playing");
        play.classList.add("broken");
        voicePlayer = {audio:null, button:null};
      }, {once:true});
      audio.play().catch(error => {
        play.classList.remove("playing");
        console.warn("space voice playback failed", error);
      });
      return;
    }
    const textButton = event.target.closest("[data-space-voice-text]");
    if (textButton) {
      textButton.parentElement?.querySelector("[data-space-voice-transcript]")?.classList.toggle("hidden");
    }
  });

  feed.addEventListener("submit", async event => {
    const form = event.target.closest("[data-space-comment-form]");
    if (!form) return;
    event.preventDefault();

    const postId = String(form.dataset.spaceCommentForm || "");
    const input = form.querySelector(".space-comment-input");
    const submit = form.querySelector(".space-comment-submit");
    const errorBox = form.querySelector(".space-comment-error");
    const content = String(input?.value || "").trim();
    if (!content) {
      input?.focus();
      return;
    }

    if (submit) submit.disabled = true;
    errorBox?.classList.add("hidden");
    try {
      const data = await CM.api(`/v1/space/posts/${encodeURIComponent(postId)}/comments`, {
        method:"POST",
        body:JSON.stringify({content}),
      });
      expandedComments.add(postId);
      replacePost(data.post);
      const updated = Array.from(feed.querySelectorAll("[data-space-post]"))
        .find(node => node.dataset.spacePost === postId);
      updated?.querySelector(".space-comment-input")?.focus();
    } catch (error) {
      if (submit) submit.disabled = false;
      if (errorBox) {
        errorBox.textContent = `评论发送失败：${error.message}`;
        errorBox.classList.remove("hidden");
      }
    }
  });

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