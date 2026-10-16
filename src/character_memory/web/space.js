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
  shell.className = "space-shell space-layout-v2 hidden";
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
      <section class="space-detail-view hidden" aria-label="动态详情"><div class="space-detail-toolbar"><button class="space-detail-back" type="button" data-space-detail-back>← 返回 Space</button><span>动态详情</span></div><div class="space-detail-content"></div></section>
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
  const feedWrap = shell.querySelector(".space-feed-wrap");
  const meta = shell.querySelector(".space-feed-meta");
  const notificationInbox = shell.querySelector(".space-notification-inbox");
  const detailView = shell.querySelector(".space-detail-view");
  const detailContent = shell.querySelector(".space-detail-content");
  const more = shell.querySelector(".space-feed-more");
  const pageHeading = document.createElement("header");
  pageHeading.className = "space-page-heading";
  const pageHeadingCopy = document.createElement("div");
  pageHeadingCopy.className = "space-page-heading-copy";
  const title = document.createElement("h1");
  title.className = "space-title";
  title.textContent = "Space";
  const subtitle = document.createElement("p");
  subtitle.className = "space-subtitle";
  subtitle.textContent = "角色近况、订阅资讯与世界话题，在这里相遇";
  pageHeadingCopy.append(title, subtitle);
  const pageNav = document.createElement("nav");
  pageNav.className = "space-page-nav";
  pageNav.setAttribute("aria-label", "Space 页面导航");
  pageNav.innerHTML = '<button type="button" data-space-page-action="all" aria-current="page">全部</button><button type="button" data-space-page-action="POST">角色动态</button><button type="button" data-space-page-action="RSS">订阅资讯</button><button type="button" data-space-page-action="TOPIC">热点讨论</button><button type="button" data-space-page-action="current">当前人物</button><button type="button" data-space-page-action="world">世界来客</button>';
  const currentPageButton = pageNav.querySelector('[data-space-page-action="current"]');
  pageHeading.append(pageHeadingCopy, pageNav);
  shell.prepend(pageHeading);
  shell.querySelector(".space-header")?.remove();
  const sourceFilter = document.createElement("label");
  sourceFilter.className = "space-source-filter";
  sourceFilter.innerHTML = '订阅来源 <select aria-label="按订阅来源筛选 Space" title="只筛选资讯，不代表角色已经阅读"><option value="">全部订阅</option></select><a href="/sources">管理订阅 ↗</a>';
  pageHeading.after(sourceFilter);
  const sourceSelect = sourceFilter.querySelector("select");

  let opened = false;
  let filterCharacterId = null;
  let filterKind = "ALL";
  let filterSourceId = null;
  let navigationEpoch = 0;
  let feedKeys = new Set();
  let detailPostId = null;
  let detailReturn = null;
  let detailRequestEpoch = 0;
  let voicePlayer = {audio:null, button:null};
  let lightboxItems = [];
  let lightboxIndex = 0;
  let lightboxReturnFocus = null;
  const lightboxBackgroundInert = new Map();
  let postsById = new Map();
  let feedHasMore = false;
  let feedNextBeforeId = null;
  let feedTotal = 0;
  let feedActiveCharacterCount = 0;
  let pulseCollectionEnabled = null;
  let feedLoading = false;
  let feedLoaded = false;
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
    lightboxReturnFocus = button;
    lightboxBackgroundInert.clear();
    for (const element of [...document.body.children]) {
      if (element === lightbox) continue;
      lightboxBackgroundInert.set(element, Boolean(element.inert));
      element.inert = true;
    }
    renderLightbox();
    lightbox.classList.remove("hidden");
    document.body.classList.add("space-lightbox-open");
    lightbox.querySelector("[data-space-lightbox-close]:not(.space-lightbox-backdrop)")?.focus({preventScroll:true});
  }

  function closeLightbox({restoreFocus = true} = {}) {
    lightbox.classList.add("hidden");
    document.body.classList.remove("space-lightbox-open");
    for (const [element, wasInert] of lightboxBackgroundInert) {
      if (element.isConnected) element.inert = wasInert;
    }
    lightboxBackgroundInert.clear();
    lightboxItems = [];
    lightboxIndex = 0;
    lightbox.querySelector(".space-lightbox-image").removeAttribute("src");
    const returnFocus = lightboxReturnFocus;
    lightboxReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected) returnFocus.focus({preventScroll:true});
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
      <span class="space-comment-avatar">${comment.actor_type === "CHARACTER" ? avatarHtml(profileFor(comment.character_id), "space-avatar") : '<span class="space-avatar">你</span>'}</span>
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

  function feedEntryHtml(item) {
    if (item.kind === "POST") return item.post ? postHtml(item.post) : "";
    const esc = CM.escapeHtml;
    const rss = item.kind === "RSS";
    const urls = rss ? [item.source?.url].filter(Boolean) : (item.source_urls || []);
    const links = urls.map((url, index) => `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${rss ? "查看原文" : `聚合来源 ${index + 1}`} ↗</a>`).join("");
    const comments = (item.comments || []).map(comment => `<div class="space-topic-take">${avatarHtml(comment.author || profileFor(comment.character_id), "space-avatar")}<div><strong>${esc(comment.author?.name || comment.character_id)}</strong><p>${esc(comment.content)}</p><time>${esc(CM.fmtTime(comment.created_at))}</time></div></div>`).join("");
    const evidence = (item.evidence || []).map(entry => `<blockquote class="space-outside-excerpt"><small>${entry.kind === "OPINION" ? "外界观点" : "来源节选"} · ${esc(entry.author || "作者未标明")}${entry.published_at ? ` · ${esc(entry.published_at)}` : " · 原文时间未标明"}</small><p>${esc(entry.quote)}</p><a href="${esc(entry.url)}" target="_blank" rel="noopener noreferrer">查看引用所在页面 ↗</a></blockquote>`).join("");
    return `<article class="space-post space-external-card" data-space-entry="${esc(item.key)}">
      ${rss && item.image_url ? `<a href="/sources?item=${esc(item.id)}" class="space-external-cover"><img loading="lazy" src="${esc(item.image_url)}" alt="${esc(item.title)}"></a>` : ""}
      <div class="space-post-body">
        <div class="space-external-label"><span>${rss ? "订阅资讯" : "热点讨论"}</span><time>${esc(CM.fmtTime(item.created_at))}</time></div>
        <h2>${esc(item.title)}</h2>
        <p class="space-external-summary">${esc(item.summary || "该来源未提供摘要。")}</p>
        <p class="space-source-name">${rss ? esc(item.source?.name || "RSS 来源") : "World Pulse · 聚合话题"}</p>
        <p class="space-source-caveat">${rss ? `${item.published_at ? "Feed 摘要或节选" : "发布时间未知 · 按收集时间排列"} · 来源内容未经独立核实，出现在这里不代表角色已读` : "聚合摘要未经独立核实，热度不代表事实；请核对原始信源"}</p>
        <div class="space-source-links">${rss ? `<a href="/sources?item=${esc(item.id)}">阅读 Feed 内容 →</a>` : ""}${links || '<span>原始链接缺失</span>'}</div>
        ${evidence ? `<details class="space-topic-discussion"><summary>来源节选与外界观点 · ${(item.evidence || []).length}</summary>${evidence}</details>` : ""}
        ${!rss ? `<details class="space-topic-discussion"><summary>角色的回应 · ${(item.comments || []).length}</summary>${comments || '<p class="space-source-caveat">尚无角色回应；没有自然想法时可以不评论。</p>'}</details>` : ""}
      </div></article>`;
  }

  function postHtml(post, {detail = false} = {}) {
    const author = post.author || profileFor(post.character_id);
    const archived = author.archived ? '<span class="space-archived-badge">已归档</span>' : "";
    const media = mediaHtml(post);
    const detailClass = detail ? " space-post-detail" : "";
    const textOnlyClass = detail && !media ? " space-post-detail-text-only" : "";
    return `
      <article class="space-post${detailClass}${textOnlyClass}" data-space-post="${CM.escapeHtml(post.id)}">
        <div class="space-post-body">
          <div class="space-post-head">
            <a class="space-post-author" href="/?view=profile&character=${encodeURIComponent(post.character_id || author.id)}" aria-label="查看 ${CM.escapeHtml(author.name || author.id)} 的人物主页">${avatarHtml(author, "space-avatar")}<span class="space-post-author-copy"><strong class="space-author">${CM.escapeHtml(author.name || author.id)}</strong>${archived}<time class="space-time">${CM.escapeHtml(CM.fmtTime(post.created_at))}</time></span></a>
            ${detail ? "" : `<button class="space-post-detail-trigger" type="button" data-space-post-open="${CM.escapeHtml(post.id)}" aria-label="打开 ${CM.escapeHtml(author.name || author.id)} 的动态详情">详情 ›</button>`}
          </div>
          ${media ? `<div class="space-post-media">${media}</div>` : ""}
          ${post.content ? `<div class="space-content">${CM.escapeHtml(post.content)}</div>` : ""}
          ${likesHtml(post)}
          ${commentsHtml(post)}
        </div>
      </article>
    `;
  }

  function findPostElement(postId) {
    const key = String(postId);
    const selector = `[data-space-post="${CSS.escape(key)}"]`;
    return detailContent.querySelector(selector) || feed.querySelector(selector);
  }

  function commentStateFor(postId) {
    const prefix = `${postId}:`;
    return {
      panel:expandedCommentPanels.has(postId),
      expanded:expandedComments.has(postId),
      threads:[...expandedThreads].filter(key => key.startsWith(prefix)),
    };
  }

  function expandPostComments(postId, post) {
    expandedCommentPanels.add(postId);
    expandedComments.add(postId);
    for (const comment of post?.comments || []) {
      const rootId = threadRootId(post, comment.id);
      expandedThreads.add(`${postId}:${rootId}`);
    }
  }

  function restoreCommentState(postId, state) {
    if (!state) return;
    if (state.panel) expandedCommentPanels.add(postId);
    else expandedCommentPanels.delete(postId);
    if (state.expanded) expandedComments.add(postId);
    else expandedComments.delete(postId);
    for (const key of [...expandedThreads]) {
      if (key.startsWith(`${postId}:`)) expandedThreads.delete(key);
    }
    state.threads.forEach(key => expandedThreads.add(key));
  }

  function setSpacePostUrl(postId, {push = false} = {}) {
    const url = new URL(window.location.href);
    const state = {...(window.history.state || {})};
    if (postId) {
      url.searchParams.set("space_post", String(postId));
      if (push) state.cmSpacePostId = String(postId);
    } else {
      url.searchParams.delete("space_post");
      delete state.cmSpacePostId;
    }
    const target = `${url.pathname}${url.search}${url.hash}`;
    if (push) window.history.pushState(state, "", target);
    else window.history.replaceState(state, "", target);
  }

  async function openPostDetail(postId, {pushHistory = false, historyEntry = false} = {}) {
    const key = String(postId || "");
    if (!key || detailPostId === key) return;
    if (detailPostId) await restorePostDetail({clearUrl:false, restoreFocus:false, loadFeedAfter:false});
    if (!opened) setOpen(true);
    const previousFilterCharacterId = filterCharacterId;
    filterCharacterId = null;
    nav.classList.add("active");
    title.textContent = "Space";
    subtitle.textContent = "角色们公开留下的近况";
    updatePageNav();
    const current = findPostElement(key);
    const placeholder = current?.parentElement === feed ? document.createComment(`space-post:${key}`) : null;
    if (placeholder) current.before(placeholder);
    detailPostId = key;
    detailReturn = {
      scrollY:window.scrollY,
      placeholder,
      focusTarget:current?.querySelector("[data-space-post-open]") || null,
      commentState:commentStateFor(key),
      filterCharacterId:previousFilterCharacterId,
      historyEntry:Boolean(historyEntry || pushHistory),
    };
    const requestEpoch = ++detailRequestEpoch;
    if (current) {
      current.classList.add("space-post-detail");
      detailContent.append(current);
      const post = postsById.get(key);
      if (post) {
        expandPostComments(key, post);
        current.outerHTML = postHtml(post, {detail:true});
      }
    } else {
      detailContent.innerHTML = '<div class="space-loading">正在读取这条动态…</div>';
    }
    detailView.classList.remove("hidden");
    feedWrap.classList.add("is-detail");
    more.classList.add("hidden");
    clearAutoPrefetch();
    if (pushHistory) setSpacePostUrl(key, {push:true});
    window.scrollTo({top:0, behavior:"auto"});
    detailView.querySelector("[data-space-detail-back]")?.focus({preventScroll:true});

    if (current) return;
    try {
      const data = await CM.api(`/v1/space/posts/${encodeURIComponent(key)}`);
      if (detailPostId !== key || detailRequestEpoch !== requestEpoch) return;
      postsById.set(key, data.post);
      expandPostComments(key, data.post);
      detailContent.innerHTML = postHtml(data.post, {detail:true});
    } catch (error) {
      if (detailPostId !== key || detailRequestEpoch !== requestEpoch) return;
      detailContent.innerHTML = `<div class="error" role="alert">动态读取失败：${CM.escapeHtml(error.message)}</div>`;
    }
  }

  async function restorePostDetail({clearUrl = true, restoreFocus = true, loadFeedAfter = true} = {}) {
    if (!detailPostId) return;
    const key = detailPostId;
    const previous = detailReturn;
    const current = detailContent.querySelector("[data-space-post]");
    ++detailRequestEpoch;
    detailPostId = null;
    detailReturn = null;
    filterCharacterId = previous?.filterCharacterId || null;
    title.textContent = filterCharacterId
      ? `${profileFor(filterCharacterId).name || filterCharacterId} 的空间`
      : "Space";
    subtitle.textContent = filterCharacterId ? "这个角色公开留下的动态" : "角色们公开留下的近况";
    nav.classList.toggle("active", opened && !filterCharacterId);
    updatePageNav();
    detailView.classList.add("hidden");
    feedWrap.classList.remove("is-detail");
    if (current && previous?.placeholder?.parentNode) {
      current.classList.remove("space-post-detail", "space-post-detail-text-only");
      previous.placeholder.replaceWith(current);
    }
    restoreCommentState(key, previous?.commentState);
    if (previous?.placeholder) replacePost(postsById.get(key));
    detailContent.innerHTML = "";
    if (clearUrl) setSpacePostUrl(null);
    if (!feedLoaded && loadFeedAfter) await loadFeed();
    updateFeedMore();
    requestAnimationFrame(() => {
      // Closing Space for another page must not carry the old feed position
      // into that page after its navigation has already completed.
      if (!opened) return;
      window.scrollTo({top:Number(previous?.scrollY || 0), behavior:"auto"});
      const focusTarget = previous?.focusTarget?.isConnected
        ? previous.focusTarget
        : findPostElement(key)?.querySelector("[data-space-post-open]");
      if (restoreFocus) focusTarget?.focus?.({preventScroll:true});
    });
  }

  function backFromPostDetail() {
    if (!detailPostId) return;
    if (detailReturn?.historyEntry && new URLSearchParams(location.search).has("space_post")) {
      window.history.back();
      return;
    }
    restorePostDetail({clearUrl:true}).catch(console.warn);
  }
  function replacePost(post) {
    if (!post) return;
    postsById.set(String(post.id), post);
    const target = findPostElement(post.id);
    const detail = Boolean(target?.classList.contains("space-post-detail"));
    if (detail && String(post.id) === detailPostId) expandPostComments(String(post.id), post);
    if (target) target.outerHTML = postHtml(post, {detail});
  }

  async function refreshPost(postId, epoch) {
    if (!opened || feedEpoch !== epoch || !postsById.has(postId)) return;
    const data = await CM.api(`/v1/space/posts/${encodeURIComponent(postId)}`);
    if (!opened || feedEpoch !== epoch || !postsById.has(postId)) return;
    const current = findPostElement(postId);
    const draft = current?.querySelector(".space-comment-input");
    // Never replace a card containing unsent text. The submit handler will
    // reconcile the card after the next comment. An empty, focused composer is
    // safe to refresh, provided we restore focus after the replacement.
    if (draft?.value.trim()) return;
    const restoreFocus = draft && document.activeElement === draft;
    replacePost(data.post);
    if (restoreFocus) {
      const updated = findPostElement(postId);
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
    updatePageNav();
  }

  function updatePageNav() {
    const selected = filterCharacterId ? "current" : filterKind === "ALL" ? "all" : filterKind;
    for (const button of pageNav.querySelectorAll("[data-space-page-action]")) {
      if (button.dataset.spacePageAction === selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    currentPageButton.hidden = CM.isGroupConversation() || !CM.state.characterId;
    sourceFilter.hidden = Boolean(filterCharacterId) || !["ALL", "RSS"].includes(filterKind) || Boolean(detailPostId);
  }

  function updateFeedMeta() {
    const loaded = feedKeys.size;
    meta.textContent = loaded
      ? `已加载 ${loaded}${feedTotal ? ` / ${feedTotal}` : ""} 条 · 资讯不是人物经历，回应由角色自主决定`
      : `${feedActiveCharacterCount} 个活跃角色可看到新动态`;
    if (filterKind === "TOPIC" && pulseCollectionEnabled === false) meta.textContent += " · 热点采集未开启，当前展示历史话题";
  }

  function updateFeedMore() {
    if (!more) return;
    if (!opened || detailPostId || !feedKeys.size) {
      more.classList.add("hidden");
      return;
    }
    more.classList.remove("hidden");
    more.disabled = feedLoading || !feedHasMore;
    more.textContent = feedLoading
      ? "正在自动加载更多动态…"
      : feedHasMore
        ? "更多动态会自动加载"
        : "已经看到全部内容";
  }

  function clearAutoPrefetch() {
    if (autoPrefetchTimer != null) {
      window.clearTimeout(autoPrefetchTimer);
      autoPrefetchTimer = null;
    }
  }

  function shouldAutoPrefetch() {
    if (!opened || detailPostId || feedLoading || !feedHasMore || !feedNextBeforeId) return false;
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
      feedKeys = new Set();
      feedLoaded = false;
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
    const mixed = !filterCharacterId && filterKind !== "POST";
    if (mixed) {
      params.set("kind", filterKind);
      if (filterSourceId) params.set("source_id", String(filterSourceId));
    }
    if (append && feedNextBeforeId) params.set(mixed ? "cursor" : "before_id", String(feedNextBeforeId));

    let failed = false;
    try {
      const data = await CM.api(`/v1/space/${mixed ? "feed" : "posts"}?${params.toString()}`);
      if (epoch !== feedEpoch) return;

      const entries = mixed ? (data.items || []) : (data.posts || []).map(post => ({key:`post:${post.id}`, kind:"POST", post}));
      const posts = entries.filter(item => item.kind === "POST" && item.post).map(item => item.post);
      feedLoaded = true;
      const freshEntries = entries.filter(item => !feedKeys.has(item.key));
      entries.forEach(item => feedKeys.add(item.key));
      posts.forEach(post => postsById.set(String(post.id), post));
      feedHasMore = Boolean(data.has_more);
      feedNextBeforeId = (mixed ? data.next_cursor : data.next_before_id) ?? null;
      feedTotal = Number(data.total || 0);
      feedActiveCharacterCount = Number(data.active_character_count || 0);
      pulseCollectionEnabled = data.collection_status?.pulse_enabled ?? null;

      if (filterCharacterId) {
        const profile = profileFor(filterCharacterId);
        title.textContent = `${profile.name || profile.id} 的空间`;
        subtitle.textContent = "这个角色公开留下的动态";
      } else {
        title.textContent = "Space";
        subtitle.textContent = filterKind === "TOPIC" ? "先看来源，再看不同角色的真实回应；历史话题保留原日期" : "角色近况、订阅资讯与世界话题，在这里相遇";
      }
      updatePageNav();

      if (append) {
        if (freshEntries.length) feed.insertAdjacentHTML("beforeend", freshEntries.map(feedEntryHtml).join(""));
      } else {
        feed.innerHTML = entries.length
          ? entries.map(feedEntryHtml).join("")
          : '<div class="space-empty"><strong>这里还没有内容</strong><span>已收集的订阅资讯、热点和角色自主表达会出现在这里，不强制生成评论。</span></div>';
      }
      if (append && automatic && freshEntries.length) autoPrefetchPages += 1;
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

  async function open(characterId = null, kind = "ALL", sourceId = null) {
    const navigation = ++navigationEpoch;
    if (detailPostId) await restorePostDetail({clearUrl:true, restoreFocus:false, loadFeedAfter:false});
    if (navigation !== navigationEpoch) return;
    clearAutoPrefetch();
    ++feedEpoch;
    feed.innerHTML = '<div class="space-loading">正在读取空间…</div>';
    filterCharacterId = characterId || null;
    filterKind = kind;
    filterSourceId = sourceId;
    sourceSelect.value = sourceId || "";
    setOpen(true);
    updatePageNav();
    window.scrollTo({top:0, behavior:"auto"});
    await loadMentionProfiles();
    if (navigation !== navigationEpoch || !opened) return;
    if (!characterId && ["ALL", "RSS"].includes(kind)) {
      try {
        const data = await CM.api("/v1/rss/sources");
        if (navigation !== navigationEpoch || !opened) return;
        sourceSelect.innerHTML = '<option value="">全部订阅</option>' + (data.sources || []).map(source => `<option value="${CM.escapeHtml(source.id)}">${CM.escapeHtml(source.name)}</option>`).join("");
        sourceSelect.value = sourceId || "";
      } catch (error) { console.warn("[space] subscription filters", error); }
    }
    if (navigation !== navigationEpoch || !opened) return;
    await loadFeed();
    await refreshNotifications();
  }

  async function openNotifications() {
    if (detailPostId) await restorePostDetail({clearUrl:true, restoreFocus:false, loadFeedAfter:false});
    if (!opened) await open(null);
    else await refreshNotifications();
    notificationInbox.scrollIntoView({behavior:"smooth", block:"start"});
  }

  function close() {
    ++navigationEpoch;
    ++feedEpoch;
    clearAutoPrefetch();
    stopVoice();
    closeLightbox({restoreFocus:false});
    if (detailPostId) restorePostDetail({clearUrl:true, restoreFocus:false, loadFeedAfter:false}).catch(console.warn);
    setOpen(false);
    window.scrollTo({top:0, behavior:"auto"});
  }

  shell.addEventListener("click", event => {
    const detailBack = event.target.closest("[data-space-detail-back]");
    if (detailBack) { backFromPostDetail(); return; }

    const postOpen = event.target.closest("[data-space-post-open]");
    if (postOpen) {
      event.preventDefault();
      openPostDetail(postOpen.dataset.spacePostOpen, {pushHistory:true}).catch(console.warn);
      return;
    }

    const postCard = event.target.closest("[data-space-post]:not(.space-post-detail)");
    const interactive = event.target.closest("button, a, input, textarea, select, video, audio, [contenteditable=\"true\"]");
    if (postCard && !interactive) {
      openPostDetail(postCard.dataset.spacePost, {pushHistory:true}).catch(console.warn);
      return;
    }
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
        const updated = findPostElement(postId);
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
      const updated = findPostElement(postId);
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

  shell.addEventListener("keydown", event => {
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

  shell.addEventListener("submit", async event => {
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
      const updated = findPostElement(postId);
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
    if (!lightbox.classList.contains("hidden")) {
      if (event.key === "Tab") {
        const focusable = [...lightbox.querySelectorAll("button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])")]
          .filter(element => element.tabIndex >= 0 && !element.closest(".hidden") && element.getClientRects().length > 0);
        if (!focusable.length) {
          event.preventDefault();
          lightbox.querySelector("[data-space-lightbox-close]:not(.space-lightbox-backdrop)")?.focus({preventScroll:true});
          return;
        }
        const activeIndex = focusable.indexOf(document.activeElement);
        const atBoundary = event.shiftKey ? activeIndex <= 0 : activeIndex === -1 || activeIndex === focusable.length - 1;
        if (atBoundary) {
          event.preventDefault();
          const next = event.shiftKey ? focusable[focusable.length - 1] : focusable[0];
          next?.focus({preventScroll:true});
        }
        return;
      }
      if (event.key === "Escape") { event.preventDefault(); closeLightbox(); }
      else if (event.key === "ArrowLeft") moveLightbox(-1);
      else if (event.key === "ArrowRight") moveLightbox(1);
      return;
    }
    if (event.key === "Escape" && detailPostId) {
      event.preventDefault();
      backFromPostDetail();
    }
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
  pageNav.addEventListener("click", event => {
    const button = event.target.closest("[data-space-page-action]");
    if (!button) return;
    if (button.dataset.spacePageAction === "all") open(null).catch(console.error);
    else if (["POST", "RSS", "TOPIC"].includes(button.dataset.spacePageAction)) open(null, button.dataset.spacePageAction).catch(console.error);
    else if (button.dataset.spacePageAction === "current") {
      if (!CM.isGroupConversation() && CM.state.characterId) open(CM.state.characterId).catch(console.error);
    } else if (button.dataset.spacePageAction === "world") {
      if (CM.features.world?.open) CM.features.world.open();
      else window.location.assign("/?view=world");
    }
  });
  sourceSelect.addEventListener("change", () => open(null, "RSS", sourceSelect.value || null).catch(console.error));
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
  CM.dom.characterList?.addEventListener("click", event => {
    if (opened && event.target.closest("[data-character]")) close();
  });
  CM.on("conversationChanged", () => {
    if (opened) close();
    updateCharacterEntry();
    updatePageNav();
  });
  CM.on("charactersLoaded", () => { updateCharacterEntry(); updatePageNav(); });
  updateCharacterEntry();
  refreshNotifications();
  window.setInterval(refreshNotifications, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") refreshNotifications();
  });

  CM.registerFeature("space", {open, close, reload:loadFeed});
  window.addEventListener("popstate", () => {
    const postId = new URLSearchParams(location.search).get("space_post");
    if (postId) {
      if (postId !== detailPostId) openPostDetail(postId, {historyEntry:false}).catch(console.warn);
    } else if (detailPostId) {
      restorePostDetail({clearUrl:false}).catch(console.warn);
    }
  });
  CM.on("ready", () => {
    const params = new URLSearchParams(location.search);
    const postId = params.get("space_post");
    if (postId) {
      open(null).then(() => openPostDetail(postId, {historyEntry:false})).catch(console.warn);
      return;
    }
    if (params.get("view") === "space") return open(null);
  });
})();
