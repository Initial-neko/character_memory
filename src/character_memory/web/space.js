(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before space.js");

  const sidebarSlot = document.getElementById("sidebarSpace");
  const chatShell = document.querySelector(".chat-shell");
  const topbarActions = document.querySelector(".topbar-actions");

  const nav = document.createElement("button");
  nav.className = "space-nav-button";
  nav.type = "button";
  nav.innerHTML = '<span class="space-nav-icon">◎</span><span class="space-nav-copy"><strong>空间</strong><small>角色们的近况</small></span><span class="space-nav-unread hidden" data-space-unread aria-label="未读空间提醒"></span>';
  sidebarSlot?.appendChild(nav);

  const notificationsButton = document.createElement("button");
  notificationsButton.className = "ghost-button space-notification-button";
  notificationsButton.type = "button";
  notificationsButton.innerHTML = '<span>空间提醒</span><span class="space-notification-count hidden" data-space-unread></span>';
  topbarActions?.appendChild(notificationsButton);

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
      <section class="space-notification-inbox hidden" aria-live="polite" aria-label="未读空间提醒"></section>
      <div class="space-feed" aria-live="polite"></div>
      <button class="space-feed-more hidden" type="button" aria-live="polite">继续向下滚动加载更多</button>
    </div>
  `;
  chatShell?.appendChild(shell);

  const lightbox = document.createElement("div");
  lightbox.className = "space-lightbox hidden";
  lightbox.setAttribute("role", "dialog");
  lightbox.setAttribute("aria-modal", "true");
  lightbox.setAttribute("aria-label", "动态图片预览");
  lightbox.innerHTML = `
    <button class="space-lightbox-backdrop" type="button" data-space-lightbox-close aria-label="关闭预览"></button>
    <div class="space-lightbox-frame">
      <button class="space-lightbox-close" type="button" data-space-lightbox-close aria-label="关闭">×</button>
      <button class="space-lightbox-nav prev" type="button" data-space-lightbox-prev aria-label="上一张">‹</button>
      <img class="space-lightbox-image" alt="动态图片预览">
      <button class="space-lightbox-nav next" type="button" data-space-lightbox-next aria-label="下一张">›</button>
      <div class="space-lightbox-caption"><span data-space-lightbox-label></span><span data-space-lightbox-count></span></div>
    </div>
  `;
  document.body.appendChild(lightbox);

  const feed = shell.querySelector(".space-feed");
  const meta = shell.querySelector(".space-feed-meta");
  const notificationInbox = shell.querySelector(".space-notification-inbox");
  const more = shell.querySelector(".space-feed-more");
  const title = shell.querySelector(".space-title");
  const subtitle = shell.querySelector(".space-subtitle");
  const closeButton = shell.querySelector(".space-close-button");

  let opened = false;
  let filterCharacterId = null;
  let voicePlayer = {audio:null, button:null};
  let lightboxItems = [];
  let lightboxIndex = 0;
  let postsById = new Map();
  let feedHasMore = false;
  let feedNextBeforeId = null;
  let feedTotal = 0;
  let feedActiveCharacterCount = 0;
  let feedLoading = false;
  let feedEpoch = 0;
  let autoPrefetchPages = 0;
  let autoPrefetchTimer = null;
  const AUTO_PREFETCH_PAGES = 2;
  const AUTO_PREFETCH_AHEAD_PX = 1600;
  const expandedCommentPanels = new Set();
  const expandedComments = new Set();
  const expandedThreads = new Set();
  const replyTargets = new Map();
  const mentionTargets = new Map();
  const commentRequestIds = new Map();
  let mentionProfiles = null;
  let unreadNotifications = [];
  let notificationLoading = false;
  const unreadIndicators = [...document.querySelectorAll("[data-space-unread]")];

  function profileFor(id) {
    return CM.state.characters.find(item => item.id === id)
      || mentionProfiles?.find(item => item.id === id)
      || {id, name:id};
  }

  async function loadMentionProfiles() {
    try {
      const data = await CM.api("/v1/characters?include_deferred=true");
      mentionProfiles = Array.isArray(data.characters) ? data.characters : [];
    } catch (error) {
      console.error("[space] mention profiles", error);
      mentionProfiles = CM.state.characters || [];
    }
  }

  function selectedMentions(postId) {
    return mentionTargets.get(String(postId)) || [];
  }

  function commentRequestId(postId, body) {
    const key = String(postId);
    const fingerprint = JSON.stringify(body);
    const existing = commentRequestIds.get(key);
    if (existing?.fingerprint === fingerprint) return existing.id;
    const id = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    commentRequestIds.set(key, {id, fingerprint});
    return id;
  }

  function mentionControlsHtml(postId) {
    const selected = selectedMentions(postId);
    const roles = (mentionProfiles || CM.state.characters || []).filter(
      item => !item.archived_at && !item.archived
    );
    const chips = selected.map(id => {
      const profile = profileFor(id);
      return `<span class="space-mention-chip">@${CM.escapeHtml(profile.name || id)}<button type="button" data-space-mention-remove="${CM.escapeHtml(id)}" aria-label="移除提及 ${CM.escapeHtml(profile.name || id)}">×</button></span>`;
    }).join("");
    const options = roles.map(profile => {
      const id = String(profile.id || "");
      const pressed = selected.includes(id);
      const disabled = !pressed && selected.length >= 4;
      return `<button type="button" class="space-mention-option${pressed ? " selected" : ""}" data-space-mention-id="${CM.escapeHtml(id)}" aria-pressed="${pressed}" ${disabled ? "disabled" : ""}>${CM.escapeHtml(profile.name || id)}${pressed ? " ✓" : ""}</button>`;
    }).join("");
    return `<div class="space-mention-controls" data-space-mention-controls="${CM.escapeHtml(postId)}">
      <div class="space-mention-selected">${chips}<span class="space-mention-hint">${selected.length ? `已提醒 ${selected.length}/4 位角色` : "可 @ 最多 4 位角色"}</span></div>
      <div class="space-mention-picker hidden" data-space-mention-picker>
        <strong>选择要提醒的角色</strong>${options || '<span class="space-mention-hint">暂无可用角色</span>'}
      </div>
    </div>`;
  }

  function updateUnreadIndicators(count) {
    const unread = Math.max(0, Number(count || 0));
    unreadIndicators.forEach(indicator => {
      indicator.classList.toggle("hidden", unread === 0);
      if (indicator.matches(".space-notification-count")) {
        indicator.textContent = unread > 99 ? "99+" : String(unread);
      } else {
        indicator.textContent = unread > 0 ? (unread > 9 ? "9+" : String(unread)) : "";
      }
    });
    notificationsButton.classList.toggle("has-unread", unread > 0);
  }

  function renderNotificationInbox() {
    const count = unreadNotifications.length;
    notificationInbox.classList.toggle("hidden", count === 0);
    if (!count) {
      notificationInbox.innerHTML = "";
      return;
    }
    notificationInbox.innerHTML = `
      <div class="space-notification-heading"><strong>角色在空间喊你</strong><span>${count} 条未读 · 页面打开时同步</span></div>
      <div class="space-notification-list">${unreadNotifications.map(item => {
        const comment = item.comment || {};
        const post = item.post || {};
        const labels = (item.reasons || []).map(reason => reason === "MENTION" ? "@了你" : reason === "REPLY" ? "回复了你" : "空间提醒");
        const actor = comment.author?.name || comment.character_id || "角色";
        const excerpt = String(comment.content || "发送了一个表情").trim();
        return `<button type="button" class="space-notification-item" data-space-notification="${CM.escapeHtml(item.id)}" data-space-notification-post="${CM.escapeHtml(item.post_id)}" data-space-notification-comment="${CM.escapeHtml(item.comment_id)}">
          <span class="space-notification-label">${CM.escapeHtml(labels.join(" · "))}</span>
          <strong>${CM.escapeHtml(actor)}：${CM.escapeHtml(excerpt)}</strong>
          <small>来自 ${CM.escapeHtml(post.author?.name || post.character_id || "空间动态")} · 查看动态</small>
        </button>`;
      }).join("")}</div>`;
  }

  async function refreshNotifications() {
    if (notificationLoading || document.visibilityState === "hidden") return;
    notificationLoading = true;
    try {
      const data = await CM.api("/v1/space/notifications?unread_only=true&limit=20");
      unreadNotifications = Array.isArray(data.notifications) ? data.notifications : [];
      updateUnreadIndicators(Number(data.unread_count || 0));
      if (opened) renderNotificationInbox();
    } catch (error) {
      console.warn("Space notification refresh failed", error);
    } finally {
      notificationLoading = false;
    }
  }

  async function openNotification(item) {
    const postId = String(item.post_id || "");
    const commentId = String(item.comment_id || "");
    const data = await CM.api(`/v1/space/posts/${encodeURIComponent(postId)}`);
    const post = data.post;
    filterCharacterId = null;
    setOpen(true);
    title.textContent = "空间";
    subtitle.textContent = "角色们公开留下的近况";
    const key = String(post.id);
    if (!feed.querySelector(`[data-space-post="${CSS.escape(key)}"]`)) {
      postsById.set(key, post);
      feed.insertAdjacentHTML("afterbegin", postHtml(post));
    } else {
      replacePost(post);
    }
    expandedCommentPanels.add(key);
    expandedComments.add(key);
    const rootId = threadRootId(post, commentId);
    expandedThreads.add(`${key}:${rootId}`);
    replacePost(post);
    const notification = unreadNotifications.find(row => String(row.id) === String(item.id));
    if (notification) {
      await CM.api(`/v1/space/notifications/${encodeURIComponent(item.id)}/read`, {method:"POST"});
      await refreshNotifications();
    }
    requestAnimationFrame(() => {
      const card = feed.querySelector(`[data-space-post="${CSS.escape(key)}"]`);
      card?.scrollIntoView({behavior:"smooth", block:"center"});
      card?.querySelector(`[data-space-comment="${CSS.escape(commentId)}"]`)?.classList.add("space-comment-notified");
    });
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
      ${transcript ? `<div class="space-voice-transcript" data-space-voice-transcript>${CM.escapeHtml(transcript)}</div>` : ""}
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
    const videos = items.filter(item =>
      item.media_type === "VIDEO" || String(item.mime_type || "").startsWith("video/")
    ).slice(0, 1);
    const other = items.filter(item =>
      !images.includes(item) && !voices.includes(item) && !videos.includes(item)
    );

    let imageHtml = "";
    if (images.length) {
      const layout = images.length === 1 ? "single" : images.length <= 4 ? "quad" : "nine";
      const cells = images.map((item, index) => {
        const label = item.label || `动态图片 ${index + 1}`;
        return `<button class="space-media-cell" type="button" data-space-image-open data-image-url="${CM.escapeHtml(item.url)}" data-image-label="${CM.escapeHtml(label)}" aria-label="放大查看 ${CM.escapeHtml(label)}"><img src="${CM.escapeHtml(item.url)}" alt="${CM.escapeHtml(label)}" loading="lazy"></button>`;
      }).join("");
      imageHtml = `<div class="space-media-grid space-media-${layout}" data-space-media-count="${images.length}">${cells}</div>`;
    }

    const voiceMediaHtml = voices.map(voiceHtml).join("");
    const videoHtml = videos.map(item => {
      const metadata = item.metadata || {};
      const seconds = Number(metadata.duration_seconds || 0);
      const detail = [seconds > 0 ? `${seconds}s` : "", String(metadata.resolution || "")].filter(Boolean).join(" · ");
      return `<figure class="space-video">
        <video class="space-video-player" controls playsinline preload="metadata" src="${CM.escapeHtml(item.url)}" aria-label="${CM.escapeHtml(item.label || "角色生成视频")}"></video>
        ${detail ? `<figcaption>${CM.escapeHtml(detail)}</figcaption>` : ""}
      </figure>`;
    }).join("");
    const links = other.map(item =>
      `<a class="space-media-link" href="${CM.escapeHtml(item.url)}" target="_blank" rel="noreferrer">查看附件 · ${CM.escapeHtml(item.label || "媒体")}</a>`
    ).join("");

    return `${imageHtml}${voiceMediaHtml}${videoHtml}${links}`;
  }

  function renderLightbox() {
    const item = lightboxItems[lightboxIndex] || null;
    if (!item) return;
    const image = lightbox.querySelector(".space-lightbox-image");
    image.src = item.url;
    image.alt = item.label || "动态图片预览";
    lightbox.querySelector("[data-space-lightbox-label]").textContent = item.label || "";
    lightbox.querySelector("[data-space-lightbox-count]").textContent = lightboxItems.length > 1 ? `${lightboxIndex + 1} / ${lightboxItems.length}` : "";
    lightbox.querySelector("[data-space-lightbox-prev]").classList.toggle("hidden", lightboxItems.length <= 1);
    lightbox.querySelector("[data-space-lightbox-next]").classList.toggle("hidden", lightboxItems.length <= 1);
  }

  function openLightbox(button) {
    const grid = button.closest(".space-media-grid");
    const buttons = [...(grid?.querySelectorAll("[data-space-image-open]") || [])];
    lightboxItems = buttons.map(node => ({url:String(node.dataset.imageUrl || ""),label:String(node.dataset.imageLabel || "动态图片")})).filter(item => item.url);
    lightboxIndex = Math.max(0, buttons.indexOf(button));
    if (!lightboxItems.length) return;
    renderLightbox();
    lightbox.classList.remove("hidden");
    document.body.classList.add("space-lightbox-open");
  }

  function closeLightbox() {
    lightbox.classList.add("hidden");
    document.body.classList.remove("space-lightbox-open");
    lightboxItems = [];
    lightboxIndex = 0;
    lightbox.querySelector(".space-lightbox-image").removeAttribute("src");
  }

  function moveLightbox(delta) {
    if (lightboxItems.length <= 1) return;
    lightboxIndex = (lightboxIndex + delta + lightboxItems.length) % lightboxItems.length;
    renderLightbox();
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

  function commentName(comment) {
    return comment?.author?.name || (comment?.actor_type === "USER" ? "我" : comment?.character_id) || "未知";
  }

  function commentStickerHtml(comment) {
    const sticker = comment?.sticker || (
      comment?.sticker_id
        ? {id:comment.sticker_id, label:"表情包", url:`/v1/stickers/${encodeURIComponent(comment.sticker_id)}/asset`}
        : null
    );
    if (!sticker?.url) return "";
    return `<div class="space-comment-sticker"><img src="${CM.escapeHtml(sticker.url)}" alt="${CM.escapeHtml(sticker.label || "表情包")}" loading="lazy"></div>`;
  }

  function threadRootId(post, commentId) {
    const comments = Array.isArray(post.comments) ? post.comments : [];
    const byId = new Map(comments.map(item => [String(item.id), item]));
    let current = byId.get(String(commentId));
    const seen = new Set();
    while (current?.reply_to_comment_id != null) {
      const key = String(current.id);
      if (seen.has(key)) break;
      seen.add(key);
      const parent = byId.get(String(current.reply_to_comment_id));
      if (!parent) break;
      current = parent;
    }
    return current?.id ?? commentId;
  }

  function commentRowHtml(post, comment, {reply = false} = {}) {
    const name = commentName(comment);
    const actorClass = comment.actor_type === "USER" ? " space-comment-user" : "";
    const allComments = Array.isArray(post.comments) ? post.comments : [];
    const parent = comment.reply_to_comment_id == null
      ? null
      : allComments.find(item => String(item.id) === String(comment.reply_to_comment_id));
    const replyTo = reply && parent
      ? `<span class="space-comment-reply-to">回复 <strong>${CM.escapeHtml(commentName(parent))}</strong></span>`
      : "";
    const markers = [];
    if (comment.actor_type === "CHARACTER" && parent?.actor_type === "USER") markers.push("回复了你");
    if (comment.actor_type === "CHARACTER" && comment.mentions_user) markers.push("@了你");
    const markerHtml = markers.map(label => `<span class="space-comment-alert">${label}</span>`).join("");
    const mentionHtml = (comment.mentions || []).map(id => {
      const profile = profileFor(id);
      return `<span class="space-comment-mention">@${CM.escapeHtml(profile.name || id)}</span>`;
    }).join("");
    const content = comment.content
      ? `<span class="space-comment-text">${CM.escapeHtml(comment.content)}</span>`
      : "";
    return `<div class="space-comment${actorClass}${reply ? " space-comment-reply" : ""}${markers.length ? " space-comment-notice" : ""}" data-space-comment="${CM.escapeHtml(comment.id)}">
      <div class="space-comment-main">
        <strong class="space-comment-author">${CM.escapeHtml(name)}</strong>
        ${markerHtml}
        ${replyTo}
        ${content}
        ${mentionHtml}
        ${commentStickerHtml(comment)}
      </div>
      <button class="space-comment-reply-button" type="button"
        data-space-reply-post="${CM.escapeHtml(post.id)}"
        data-space-reply-comment="${CM.escapeHtml(comment.id)}"
        data-space-reply-name="${CM.escapeHtml(name)}">回复</button>
    </div>`;
  }

  function commentsHtml(post) {
    const allComments = Array.isArray(post.comments) ? post.comments : [];
    const byId = new Map(allComments.map(item => [String(item.id), item]));
    const roots = allComments.filter(comment => (
      comment.reply_to_comment_id == null || !byId.has(String(comment.reply_to_comment_id))
    ));
    const rootIds = new Set(roots.map(root => String(root.id)));
    const repliesByRoot = new Map();
    for (const comment of allComments) {
      if (rootIds.has(String(comment.id))) continue;
      const rootId = String(threadRootId(post, comment.id));
      if (!repliesByRoot.has(rootId)) repliesByRoot.set(rootId, []);
      repliesByRoot.get(rootId).push(comment);
    }

    const postId = String(post.id);
    const panelExpanded = expandedCommentPanels.has(postId);
    if (!panelExpanded) {
      const latest = roots[0] || allComments[0] || null;
      const preview = latest
        ? `<span class="space-comment-preview"><strong>${CM.escapeHtml(commentName(latest))}</strong> ${CM.escapeHtml(String(latest.content || "").trim() || "发送了一个表情")}</span>`
        : '<span class="space-comment-preview">还没有评论</span>';
      return `<div class="space-comments space-comments-collapsed">
        <button class="space-comments-summary" type="button" data-space-comments-panel-toggle="${CM.escapeHtml(postId)}">
          <span>评论 ${allComments.length}</span>
          ${preview}
          <span class="space-comments-summary-action">${allComments.length ? "查看评论" : "写评论"}</span>
        </button>
      </div>`;
    }

    const expandedRoots = expandedComments.has(postId);
    const visibleRoots = expandedRoots ? roots : roots.slice(0, 3);
    const body = visibleRoots.map(root => {
      const rootKey = String(root.id);
      const replies = repliesByRoot.get(rootKey) || [];
      const threadKey = `${postId}:${rootKey}`;
      const expanded = expandedThreads.has(threadKey);
      const visibleReplies = expanded ? replies : replies.slice(0, 2);
      const repliesHtml = visibleReplies.map(comment => commentRowHtml(post, comment, {reply:true})).join("");
      const hiddenCount = Math.max(0, replies.length - visibleReplies.length);
      const threadToggle = replies.length > 2
        ? `<button class="space-thread-toggle" type="button" data-space-thread-toggle="${CM.escapeHtml(threadKey)}">${expanded ? "收起回复" : `展开 ${hiddenCount} 条回复`}</button>`
        : "";
      return `<div class="space-comment-thread" data-space-thread-root="${CM.escapeHtml(root.id)}">
        ${commentRowHtml(post, root)}
        ${replies.length ? `<div class="space-comment-replies">${repliesHtml}${threadToggle}</div>` : ""}
      </div>`;
    }).join("");

    const rootToggle = roots.length > 3
      ? `<button class="space-comments-toggle" type="button" data-space-comments-toggle="${CM.escapeHtml(post.id)}">${expandedRoots ? "收起评论" : `查看全部 ${roots.length} 条主评论`}</button>`
      : "";
    const target = replyTargets.get(postId);
    const mentionControls = mentionControlsHtml(postId);
    const replyBanner = target
      ? `<div class="space-comment-replying">回复 <strong>${CM.escapeHtml(target.name)}</strong><button type="button" data-space-reply-cancel="${CM.escapeHtml(postId)}">取消</button></div>`
      : "";
    const placeholder = target ? `回复 ${target.name}…` : "评论这条动态…";
    return `<div class="space-comments">
      <button class="space-comments-collapse" type="button" data-space-comments-panel-toggle="${CM.escapeHtml(postId)}">收起评论</button>
      <div class="space-comments-list">${body || '<div class="space-comments-empty">还没有评论</div>'}</div>
      ${rootToggle}
      <form class="space-comment-form" data-space-comment-form="${CM.escapeHtml(postId)}">
        ${replyBanner}
        ${mentionControls}
        <textarea class="space-comment-input" name="content" rows="1" maxlength="1000" placeholder="${CM.escapeHtml(placeholder)}" aria-label="${CM.escapeHtml(placeholder)}"></textarea>
        <button class="space-mention-toggle" type="button" data-space-mention-toggle aria-expanded="false" aria-label="提及角色">@</button>
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
            <time class="space-time">${CM.escapeHtml(CM.fmtTime(post.created_at))}</time>
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

  async function refreshPost(postId, epoch) {
    if (!opened || feedEpoch !== epoch || !postsById.has(postId)) return;
    const data = await CM.api(`/v1/space/posts/${encodeURIComponent(postId)}`);
    if (!opened || feedEpoch !== epoch || !postsById.has(postId)) return;
    const current = Array.from(feed.querySelectorAll("[data-space-post]"))
      .find(node => node.dataset.spacePost === String(postId));
    const draft = current?.querySelector(".space-comment-input");
    // Never replace a card containing unsent text. The submit handler will
    // reconcile the card after the next comment. An empty, focused composer is
    // safe to refresh, provided we restore focus after the replacement.
    if (draft?.value.trim()) return;
    const restoreFocus = draft && document.activeElement === draft;
    replacePost(data.post);
    if (restoreFocus) {
      const updated = Array.from(feed.querySelectorAll("[data-space-post]"))
        .find(node => node.dataset.spacePost === String(postId));
      updated?.querySelector(".space-comment-input")?.focus();
    }
  }

  function scheduleCommentReconciliation(postId) {
    // Comment-thread reactions run on the scheduler after the POST response.
    // Space has no push channel yet, so use a bounded set of re-reads rather
    // than leaving an open panel permanently stale or polling forever.
    const epoch = feedEpoch;
    for (const delay of [2500, 6500, 12000, 22000]) {
      window.setTimeout(() => {
        refreshPost(postId, epoch).catch(console.warn);
      }, delay);
    }
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

  function updateFeedMeta() {
    const loaded = postsById.size;
    meta.textContent = loaded
      ? `已加载 ${loaded}${feedTotal ? ` / ${feedTotal}` : ""} 条 · ${feedActiveCharacterCount} 个活跃角色可看到新动态`
      : `${feedActiveCharacterCount} 个活跃角色可看到新动态`;
  }

  function updateFeedMore() {
    if (!more) return;
    if (!opened || !postsById.size) {
      more.classList.add("hidden");
      return;
    }
    more.classList.remove("hidden");
    more.disabled = feedLoading || !feedHasMore;
    more.textContent = feedLoading
      ? "正在自动加载更多动态…"
      : feedHasMore
        ? "更多动态会自动加载"
        : "已经看到全部动态";
  }

  function clearAutoPrefetch() {
    if (autoPrefetchTimer != null) {
      window.clearTimeout(autoPrefetchTimer);
      autoPrefetchTimer = null;
    }
  }

  function shouldAutoPrefetch() {
    if (!opened || feedLoading || !feedHasMore || !feedNextBeforeId) return false;
    if (autoPrefetchPages < AUTO_PREFETCH_PAGES) return true;
    if (!more) return false;
    return more.getBoundingClientRect().top <= window.innerHeight + AUTO_PREFETCH_AHEAD_PX;
  }

  function scheduleAutoPrefetch() {
    clearAutoPrefetch();
    if (!shouldAutoPrefetch()) return;
    autoPrefetchTimer = window.setTimeout(() => {
      autoPrefetchTimer = null;
      if (!shouldAutoPrefetch()) return;
      loadFeed({append:true, automatic:true}).catch(console.warn);
    }, 40);
  }

  async function loadFeed({append = false, automatic = false} = {}) {
    if (append && (feedLoading || !feedHasMore || !feedNextBeforeId)) return;

    const epoch = append ? feedEpoch : ++feedEpoch;
    if (!append) {
      clearAutoPrefetch();
      autoPrefetchPages = 0;
      postsById = new Map();
      feedHasMore = false;
      feedNextBeforeId = null;
      feedTotal = 0;
      feedActiveCharacterCount = 0;
      feed.innerHTML = '<div class="space-loading">正在读取空间…</div>';
      more?.classList.add("hidden");
      CM.features.encounter?.refresh?.().catch?.(console.warn);
    }

    feedLoading = true;
    if (append) updateFeedMore();

    const params = new URLSearchParams({limit:"10"});
    if (filterCharacterId) params.set("character_id", filterCharacterId);
    if (append && feedNextBeforeId) params.set("before_id", String(feedNextBeforeId));

    let failed = false;
    try {
      const data = await CM.api(`/v1/space/posts?${params.toString()}`);
      if (epoch !== feedEpoch) return;

      const posts = Array.isArray(data.posts) ? data.posts : [];
      const freshPosts = posts.filter(post => !postsById.has(String(post.id)));
      posts.forEach(post => postsById.set(String(post.id), post));
      feedHasMore = Boolean(data.has_more);
      feedNextBeforeId = data.next_before_id ?? null;
      feedTotal = Number(data.total || postsById.size);
      feedActiveCharacterCount = Number(data.active_character_count || 0);

      if (filterCharacterId) {
        const profile = profileFor(filterCharacterId);
        title.textContent = `${profile.name || profile.id} 的空间`;
        subtitle.textContent = "这个角色公开留下的动态";
      } else {
        title.textContent = "空间";
        subtitle.textContent = "角色们公开留下的近况";
      }

      if (append) {
        if (freshPosts.length) feed.insertAdjacentHTML("beforeend", freshPosts.map(postHtml).join(""));
      } else {
        feed.innerHTML = posts.length
          ? posts.map(postHtml).join("")
          : '<div class="space-empty"><strong>这里还没有动态</strong><span>角色真正想公开表达时，内容会出现在这里。</span></div>';
      }
      if (append && automatic && freshPosts.length) autoPrefetchPages += 1;
      updateFeedMeta();
      if (opened) refreshNotifications();
    } catch (error) {
      if (epoch !== feedEpoch) return;
      failed = true;
      if (append && more) {
        more.disabled = false;
        more.textContent = `自动加载失败：${error.message} · 点此重试`;
        more.classList.remove("hidden");
      } else {
        feed.innerHTML = `<div class="error">空间读取失败：${CM.escapeHtml(error.message)}</div>`;
      }
    } finally {
      if (epoch === feedEpoch) {
        feedLoading = false;
        if (!failed) {
          updateFeedMore();
          scheduleAutoPrefetch();
        }
      }
    }
  }

  async function open(characterId = null) {
    filterCharacterId = characterId || null;
    setOpen(true);
    window.scrollTo({top:0, behavior:"auto"});
    await loadMentionProfiles();
    await loadFeed();
    await refreshNotifications();
  }

  async function openNotifications() {
    if (!opened) await open(null);
    else await refreshNotifications();
    notificationInbox.scrollIntoView({behavior:"smooth", block:"start"});
  }

  function close() {
    clearAutoPrefetch();
    stopVoice();
    closeLightbox();
    setOpen(false);
    window.scrollTo({top:0, behavior:"auto"});
  }

  feed.addEventListener("click", event => {
    const mentionToggle = event.target.closest("[data-space-mention-toggle]");
    if (mentionToggle) {
      const form = mentionToggle.closest("[data-space-comment-form]");
      const picker = form?.querySelector("[data-space-mention-picker]");
      if (picker) {
        picker.classList.toggle("hidden");
        mentionToggle.setAttribute("aria-expanded", String(!picker.classList.contains("hidden")));
      }
      return;
    }

    const mentionChoice = event.target.closest("[data-space-mention-id]");
    const mentionRemove = event.target.closest("[data-space-mention-remove]");
    if (mentionChoice || mentionRemove) {
      const form = event.target.closest("[data-space-comment-form]");
      const postId = String(form?.dataset.spaceCommentForm || "");
      const id = String(mentionChoice?.dataset.spaceMentionId || mentionRemove?.dataset.spaceMentionRemove || "");
      if (!postId || !id) return;
      const selected = selectedMentions(postId).slice();
      const existing = selected.indexOf(id);
      if (existing >= 0) selected.splice(existing, 1);
      else if (selected.length < 4) selected.push(id);
      mentionTargets.set(postId, selected);
      const controls = form.querySelector("[data-space-mention-controls]");
      if (controls) {
        const keepOpen = !form.querySelector("[data-space-mention-picker]")?.classList.contains("hidden");
        controls.outerHTML = mentionControlsHtml(postId);
        form.querySelector("[data-space-mention-picker]")?.classList.toggle("hidden", !keepOpen);
        form.querySelector("[data-space-mention-toggle]")?.setAttribute("aria-expanded", String(keepOpen));
      }
      return;
    }

    const imageButton = event.target.closest("[data-space-image-open]");
    if (imageButton) {
      event.preventDefault();
      openLightbox(imageButton);
      return;
    }

    const commentsPanelToggle = event.target.closest("[data-space-comments-panel-toggle]");
    if (commentsPanelToggle) {
      const postId = String(commentsPanelToggle.dataset.spaceCommentsPanelToggle || "");
      if (expandedCommentPanels.has(postId)) {
        expandedCommentPanels.delete(postId);
        replyTargets.delete(postId);
      } else {
        expandedCommentPanels.add(postId);
      }
      replacePost(postsById.get(postId));
      if (expandedCommentPanels.has(postId)) {
        const updated = Array.from(feed.querySelectorAll("[data-space-post]"))
          .find(node => node.dataset.spacePost === postId);
        updated?.querySelector(".space-comment-input")?.focus();
      }
      return;
    }

    const commentsToggle = event.target.closest("[data-space-comments-toggle]");
    if (commentsToggle) {
      const postId = String(commentsToggle.dataset.spaceCommentsToggle || "");
      if (expandedComments.has(postId)) expandedComments.delete(postId);
      else expandedComments.add(postId);
      replacePost(postsById.get(postId));
      return;
    }

    const threadToggle = event.target.closest("[data-space-thread-toggle]");
    if (threadToggle) {
      const threadKey = String(threadToggle.dataset.spaceThreadToggle || "");
      if (expandedThreads.has(threadKey)) expandedThreads.delete(threadKey);
      else expandedThreads.add(threadKey);
      const postId = threadKey.split(":", 1)[0];
      replacePost(postsById.get(postId));
      return;
    }

    const replyButton = event.target.closest("[data-space-reply-comment]");
    if (replyButton) {
      const postId = String(replyButton.dataset.spaceReplyPost || "");
      const commentId = String(replyButton.dataset.spaceReplyComment || "");
      const name = String(replyButton.dataset.spaceReplyName || "评论");
      replyTargets.set(postId, {commentId, name});
      expandedCommentPanels.add(postId);
      const post = postsById.get(postId);
      if (post) {
        const rootId = threadRootId(post, commentId);
        expandedThreads.add(`${postId}:${rootId}`);
      }
      replacePost(post);
      const updated = Array.from(feed.querySelectorAll("[data-space-post]"))
        .find(node => node.dataset.spacePost === postId);
      updated?.querySelector(".space-comment-input")?.focus();
      return;
    }

    const cancelReply = event.target.closest("[data-space-reply-cancel]");
    if (cancelReply) {
      const postId = String(cancelReply.dataset.spaceReplyCancel || "");
      replyTargets.delete(postId);
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
  });

  feed.addEventListener("keydown", event => {
    const input = event.target?.closest?.(".space-comment-input");
    if (!input) return;
    // Enter sends and Shift+Enter keeps the newline. A composing IME reports the
    // confirming Enter as well (keyCode 229 outside the standard isComposing
    // flag), so a Chinese input method must not be cut off mid-word.
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    const form = input.closest("[data-space-comment-form]");
    if (!form) return;
    event.preventDefault();
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.dispatchEvent(new Event("submit", {cancelable:true, bubbles:true}));
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
      const replyTarget = replyTargets.get(postId);
      const body = {content};
      if (replyTarget?.commentId) body.reply_to_comment_id = Number(replyTarget.commentId);
      const mentions = selectedMentions(postId);
      if (mentions.length) body.mentions = mentions;
      body.client_request_id = commentRequestId(postId, body);
      const data = await CM.api(`/v1/space/posts/${encodeURIComponent(postId)}/comments`, {
        method:"POST",
        body:JSON.stringify(body),
      });
      expandedComments.add(postId);
      if (replyTarget?.commentId) {
        const rootId = threadRootId(data.post, replyTarget.commentId);
        expandedThreads.add(`${postId}:${rootId}`);
      }
      replyTargets.delete(postId);
      mentionTargets.delete(postId);
      commentRequestIds.delete(postId);
      replacePost(data.post);
      scheduleCommentReconciliation(postId);
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

  lightbox.addEventListener("click", event => {
    if (event.target.closest("[data-space-lightbox-close]")) { closeLightbox(); return; }
    if (event.target.closest("[data-space-lightbox-prev]")) { moveLightbox(-1); return; }
    if (event.target.closest("[data-space-lightbox-next]")) moveLightbox(1);
  });
  document.addEventListener("keydown", event => {
    if (lightbox.classList.contains("hidden")) return;
    if (event.key === "Escape") closeLightbox();
    else if (event.key === "ArrowLeft") moveLightbox(-1);
    else if (event.key === "ArrowRight") moveLightbox(1);
  });

  more?.addEventListener("click", () => {
    if (feedHasMore && !feedLoading) loadFeed({append:true}).catch(console.warn);
  });

  if (more && "IntersectionObserver" in window) {
    const feedObserver = new IntersectionObserver(entries => {
      if (!opened || feedLoading || !feedHasMore) return;
      if (entries.some(entry => entry.isIntersecting)) scheduleAutoPrefetch();
    }, {rootMargin:`${AUTO_PREFETCH_AHEAD_PX}px 0px`});
    feedObserver.observe(more);
  } else {
    window.addEventListener("scroll", scheduleAutoPrefetch, {passive:true});
  }

  nav.addEventListener("click", () => open(null).catch(console.error));
  notificationsButton.addEventListener("click", () => openNotifications().catch(console.error));
  notificationInbox.addEventListener("click", event => {
    const button = event.target.closest("[data-space-notification]");
    if (!button) return;
    const item = unreadNotifications.find(row => String(row.id) === String(button.dataset.spaceNotification));
    if (item) openNotification(item).catch(error => console.warn("Could not open Space notification", error));
  });
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
  refreshNotifications();
  window.setInterval(refreshNotifications, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") refreshNotifications();
  });

  CM.registerFeature("space", {open, close, reload:loadFeed});
})();
