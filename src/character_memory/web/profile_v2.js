(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before profile_v2.js");

  const chatShell = document.querySelector(".chat-shell");
  if (!chatShell) throw new Error("chat shell is required for the profile page");

  const shell = document.createElement("section");
  shell.className = "profile-v2-shell";
  shell.hidden = true;
  shell.setAttribute("aria-labelledby", "profileV2PageTitle");
  shell.innerHTML = `
    <div class="profile-v2-page">
      <header class="profile-v2-pagebar">
        <div><p class="profile-v2-eyebrow">CHARACTER MEMORY</p><h1 id="profileV2PageTitle">人物主页</h1></div>
        <button type="button" class="profile-v2-quiet-button" data-profile-close>返回聊天</button>
      </header>
      <div class="profile-v2-layout">
        <main class="profile-v2-main">
          <section class="profile-v2-hero" aria-label="人物资料">
            <img class="profile-v2-hero-photo" data-profile-backdrop alt="" aria-hidden="true" hidden>
            <div class="profile-v2-hero-content">
              <div class="profile-v2-avatar">
                <span data-profile-initial aria-hidden="true"></span>
                <img data-profile-portrait alt="" hidden>
              </div>
              <div class="profile-v2-identity">
                <p class="profile-v2-identity-line" data-profile-identity></p>
                <h2 data-profile-name></h2>
                <p class="profile-v2-tagline" data-profile-tagline></p>
              </div>
              <div class="profile-v2-hero-actions">
                <button type="button" class="profile-v2-primary-button" data-profile-chat>和她聊聊</button>
                <button type="button" class="profile-v2-secondary-button" data-profile-open-life>查看共同经历</button>
              </div>
            </div>
          </section>
          <nav class="profile-v2-tabs" role="tablist" aria-label="人物主页内容">
            <button type="button" role="tab" data-profile-tab="daily" aria-selected="true">她的日常</button>
            <button type="button" role="tab" data-profile-tab="stories" aria-selected="false">我们的故事</button>
            <button type="button" role="tab" data-profile-tab="about" aria-selected="false">认识她</button>
          </nav>
          <section class="profile-v2-panel" data-profile-panel role="tabpanel" aria-live="polite"></section>
        </main>
        <aside class="profile-v2-sidebar" aria-label="人物补充信息">
          <section class="profile-v2-side-card">
            <h2>人物资料</h2>
            <dl class="profile-v2-facts">
              <dt>身份</dt><dd data-profile-side-identity></dd>
              <dt>简介</dt><dd data-profile-side-tagline></dd>
            </dl>
            <button type="button" class="profile-v2-text-button" data-profile-inspect>查看完整人物设定 →</button>
          </section>
          <section class="profile-v2-side-card">
            <div class="profile-v2-side-heading"><h2>最近的共同互动</h2><span>只读记录</span></div>
            <div class="profile-v2-side-stories" data-profile-side-stories></div>
            <button type="button" class="profile-v2-primary-button profile-v2-side-link" data-profile-open-life>查看共同经历 →</button>
          </section>
        </aside>
      </div>
      <p class="profile-v2-status" data-profile-status aria-live="polite"></p>
    </div>
  `;
  chatShell.appendChild(shell);

  const $ = selector => shell.querySelector(selector);
  const panel = $("[data-profile-panel]");
  const chatButton = $("[data-profile-chat]");
  const status = $("[data-profile-status]");
  const tabButtons = [...shell.querySelectorAll("[data-profile-tab]")];

  let opened = false;
  let switchingForChat = false;
  let activeCharacterId = "";
  let activeProfile = null;
  let activeTab = "daily";
  let requestEpoch = 0;
  let posts = [];
  let stories = [];
  let postsState = "idle";
  let storiesState = "idle";
  let postsFailed = false;
  let storiesFailed = false;

  function text(value) {
    return String(value ?? "");
  }

  function safeImageUrl(value) {
    try {
      const url = new URL(text(value).trim(), location.origin);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return "";
      return url.href;
    } catch {
      return "";
    }
  }

  function make(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = text(value);
    return node;
  }

  function profileFromState(id) {
    return (CM.state.characters || []).find(item => item.id === id) || null;
  }

  async function resolveProfile(id) {
    const current = profileFromState(id);
    if (current) return current;
    const active = await CM.api("/v1/characters?include_deferred=true");
    const match = (active.characters || []).find(item => item.id === id);
    if (match) return match;
    const archived = await CM.api("/v1/characters?archived=true&include_deferred=true");
    return (archived.characters || []).find(item => item.id === id) || null;
  }

  function setImage(image, url, alt = "") {
    image.hidden = true;
    image.alt = alt;
    image.onerror = () => { image.hidden = true; };
    const safeUrl = safeImageUrl(url);
    if (!safeUrl) return;
    image.src = safeUrl;
    image.hidden = false;
  }

  function renderIdentity() {
    const profile = activeProfile || {id: activeCharacterId, name: activeCharacterId};
    const name = text(profile.name || profile.id || activeCharacterId);
    const identity = text(profile.identity || "").trim();
    const tagline = text(profile.tagline || "").trim();
    const initial = text(CM.initialFor(profile) || name.slice(0, 1) || "人");
    $("[data-profile-name]").textContent = name;
    $("[data-profile-identity]").textContent = identity;
    $("[data-profile-identity]").hidden = !identity;
    $("[data-profile-tagline]").textContent = tagline || "还没有填写一句话简介。";
    $("[data-profile-side-identity]").textContent = identity || "尚未填写";
    $("[data-profile-side-tagline]").textContent = tagline || "尚未填写";
    $("[data-profile-initial]").textContent = initial;
    setImage($("[data-profile-portrait]"), profile.avatar_url, `${name}的头像`);
    // Render the identity portrait as a clear illustration, never a blurred room.
    setImage($("[data-profile-backdrop]"), profile.avatar_url);

    const archived = Boolean(profile.archived || profile.archived_at);
    chatButton.disabled = archived;
    chatButton.textContent = archived ? "恢复后可聊天" : "和她聊聊";
    chatButton.title = archived ? "人物已归档，恢复后可以继续聊天" : "切换到这个人物的私聊，不会自动发送消息";
  }

  function formatTime(value) {
    if (!value) return "时间未记录";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "时间未记录"
      : date.toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }

  function emptyMessage(message) {
    return make("p", "profile-v2-empty", message);
  }

  function mediaImage(post) {
    const media = Array.isArray(post.media_items) ? post.media_items : post.media ? [post.media] : [];
    const image = media.find(item => item?.available !== false && item?.url &&
      (item.media_type === "IMAGE" || text(item.mime_type).startsWith("image/")));
    const url = safeImageUrl(image?.url);
    if (!url) return null;
    const node = document.createElement("img");
    node.className = "profile-v2-post-image";
    node.alt = "Space 动态附带图片";
    node.loading = "lazy";
    node.referrerPolicy = "no-referrer";
    node.onerror = () => node.remove();
    node.src = url;
    return node;
  }

  function renderPost(post) {
    const article = make("article", "profile-v2-post-card");
    article.dataset.profilePost = String(post.id ?? "");
    article.append(make("p", "profile-v2-card-label", "Space 动态"));
    const time = make("time", "profile-v2-card-time", formatTime(post.created_at));
    if (post.created_at) time.dateTime = text(post.created_at);
    article.append(time);
    const image = mediaImage(post);
    if (image) article.append(image);
    article.append(make("p", "profile-v2-card-content", post.content || (image ? "图片动态" : "这条动态没有正文。")));
    const link = make("a", "profile-v2-post-link");
    link.href = `/?view=space&space_post=${encodeURIComponent(post.id)}`;
    link.setAttribute("aria-label", "查看动态");
    while (article.firstChild) link.append(article.firstChild);
    link.append(make("span", "profile-v2-text-button", "查看动态"));
    article.append(link);
    return article;
  }

  function renderStory(item) {
    const article = make("article", "profile-v2-story-card");
    article.dataset.profileStory = text(item.key || "");
    const time = make("time", "profile-v2-card-time", formatTime(item.time));
    if (item.time) time.dateTime = text(item.time);
    article.append(time);
    article.append(make("h3", "profile-v2-story-title", item.title || "已记录的互动"));
    article.append(make("p", "profile-v2-card-content", item.content || "来源记录没有正文。"));
    return article;
  }

  function renderSideStories() {
    const target = $("[data-profile-side-stories]");
    target.replaceChildren();
    if (storiesState === "loading" || storiesState === "idle") {
      target.append(emptyMessage("正在读取已有互动记录…"));
      return;
    }
    if (storiesFailed) {
      target.append(emptyMessage("互动记录暂时无法读取。"));
      return;
    }
    if (!stories.length) {
      target.append(emptyMessage("暂时没有可展示的来源记录。"));
      return;
    }
    stories.slice(0, 2).forEach(item => {
      const preview = make("article", "profile-v2-side-story");
      preview.append(make("time", "profile-v2-card-time", formatTime(item.time)));
      preview.append(make("strong", "", item.title || "已记录的互动"));
      target.append(preview);
    });
  }

  function renderDailyPanel() {
    panel.append(make("h2", "profile-v2-section-title", "她的日常"));
    panel.append(make("p", "profile-v2-section-hint", "这里展示她已经发布的 Space 动态。"));
    if (postsState === "loading" || postsState === "idle") {
      panel.append(emptyMessage("正在读取已发布的动态…"));
      return;
    }
    if (postsFailed) {
      panel.append(emptyMessage("Space 动态暂时无法读取。"));
      panel.append(makeRetryButton());
      return;
    }
    if (!posts.length) {
      panel.append(emptyMessage("还没有可展示的公开动态。"));
      return;
    }
    const grid = make("div", "profile-v2-post-grid");
    posts.forEach(post => grid.append(renderPost(post)));
    panel.append(grid);
  }

  function renderStoriesPanel() {
    panel.append(make("h2", "profile-v2-section-title", "我们的故事"));
    panel.append(make("p", "profile-v2-section-hint", "只展示已有生活来源记录，不推断未记录的经历。"));
    const link = make("button", "profile-v2-text-button", "查看共同经历 →");
    link.type = "button";
    link.dataset.profileOpenLife = "";
    panel.append(link);
    if (storiesState === "loading" || storiesState === "idle") {
      panel.append(emptyMessage("正在读取已有互动记录…"));
      return;
    }
    if (storiesFailed) {
      panel.append(emptyMessage("生活记录暂时无法读取。"));
      panel.append(makeRetryButton());
      return;
    }
    if (!stories.length) {
      panel.append(emptyMessage("暂时没有可展示的互动来源记录。"));
      return;
    }
    const list = make("div", "profile-v2-story-list");
    stories.forEach(item => list.append(renderStory(item)));
    panel.append(list);
  }

  function renderAboutPanel() {
    panel.append(make("h2", "profile-v2-section-title", "认识她"));
    panel.append(make("p", "profile-v2-section-hint", "以下资料来自人物当前设定；完整 Persona 可以只读查看。"));
    const details = make("dl", "profile-v2-about-facts");
    details.append(make("dt", "", "身份"), make("dd", "", activeProfile?.identity || "尚未填写"));
    details.append(make("dt", "", "一句话简介"), make("dd", "", activeProfile?.tagline || "尚未填写"));
    panel.append(details);
    const inspect = make("button", "profile-v2-primary-button", "查看完整人物设定");
    inspect.type = "button";
    inspect.dataset.profileInspect = "";
    panel.append(inspect);
  }

  function makeRetryButton() {
    const button = make("button", "profile-v2-quiet-button", "重新读取");
    button.type = "button";
    button.dataset.profileRetry = "";
    return button;
  }

  function renderPanel() {
    tabButtons.forEach(button => {
      const selected = button.dataset.profileTab === activeTab;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    panel.replaceChildren();
    if (activeTab === "stories") renderStoriesPanel();
    else if (activeTab === "about") renderAboutPanel();
    else renderDailyPanel();
  }

  function renderSidebar() {
    renderSideStories();
  }

  function lifeUrl() {
    const params = new URLSearchParams({character: activeCharacterId, view: "relationships"});
    return `/life?${params.toString()}`;
  }

  async function loadData(id, token) {
    posts = [];
    stories = [];
    postsState = "loading";
    storiesState = "loading";
    postsFailed = false;
    storiesFailed = false;
    status.textContent = "正在读取已有动态与互动来源…";
    renderPanel();
    renderSidebar();

    const params = new URLSearchParams({character_id: id, limit: "4"});
    const results = await Promise.allSettled([
      CM.api(`/v1/space/posts?${params.toString()}`),
      CM.api(`/v1/characters/${encodeURIComponent(id)}/life?view=relationships&peer=user&limit=4`),
    ]);
    if (!opened || token !== requestEpoch || id !== activeCharacterId) return;

    if (results[0].status === "fulfilled") {
      posts = Array.isArray(results[0].value.posts) ? results[0].value.posts.slice(0, 4) : [];
      postsState = "loaded";
    } else {
      postsFailed = true;
      postsState = "failed";
    }
    if (results[1].status === "fulfilled") {
      stories = Array.isArray(results[1].value.items) ? results[1].value.items.slice(0, 4) : [];
      storiesState = "loaded";
    } else {
      storiesFailed = true;
      storiesState = "failed";
    }
    status.textContent = postsFailed || storiesFailed ? "有部分只读记录暂时无法读取。" : "";
    renderPanel();
    renderSidebar();
  }

  async function open(characterId = CM.state.characterId) {
    const id = text(characterId).trim();
    if (!id) return false;
    if (document.body.classList.contains("space-mode")) CM.features.space?.close?.();

    const token = ++requestEpoch;
    activeCharacterId = id;
    activeProfile = profileFromState(id) || {id, name: id};
    activeTab = "daily";
    opened = true;
    shell.hidden = false;
    document.body.classList.add("profile-mode");
    renderIdentity();
    postsState = "loading";
    storiesState = "loading";
    renderPanel();
    renderSidebar();
    window.scrollTo({top: 0, behavior: "auto"});
    CM.emit("profileChanged", {open: true, characterId: id}).catch(error => console.warn("profile state listener", error));

    try {
      const resolvedProfile = await resolveProfile(id);
      if (!opened || token !== requestEpoch || id !== activeCharacterId) return false;
      activeProfile = resolvedProfile || activeProfile;
    } catch {
      if (!opened || token !== requestEpoch || id !== activeCharacterId) return false;
      status.textContent = "人物资料暂时无法读取。";
    }
    if (!opened || token !== requestEpoch || id !== activeCharacterId) return false;
    renderIdentity();
    await loadData(id, token);
    return true;
  }

  function close() {
    const wasOpen = opened;
    const previousId = activeCharacterId;
    requestEpoch += 1;
    opened = false;
    shell.hidden = true;
    document.body.classList.remove("profile-mode");
    status.textContent = "";
    window.scrollTo({top: 0, behavior: "auto"});
    if (wasOpen) {
      CM.emit("profileChanged", {open: false, characterId: previousId}).catch(error => console.warn("profile state listener", error));
    }
  }

  async function chatWithCharacter() {
    if (!activeCharacterId || chatButton.disabled) return;
    const targetId = activeCharacterId;
    chatButton.disabled = true;
    status.textContent = "正在打开私聊…";
    switchingForChat = true;
    try {
      // A character created by a group build stays out of the sidebar until the
      // user opens direct chat. Doing that only through switchCharacter left
      // the entry missing, and the next reload fell back to another character.
      const profile = activeProfile || profileFromState(targetId);
      if (profile?.direct_pending) {
        await CM.api(`/v1/characters/${encodeURIComponent(targetId)}/open-direct`, {method: "POST"});
        await CM.loadCharacters();
      }
      if (CM.isGroupConversation() || CM.state.characterId !== targetId) {
        await CM.switchCharacter(targetId);
      }
      close();
    } catch (error) {
      status.textContent = `无法打开私聊：${text(error?.message || "请稍后重试")}`;
    } finally {
      switchingForChat = false;
      if (opened) chatButton.disabled = false;
    }
  }

  async function inspectPersona() {
    if (!activeCharacterId) return;
    const inspector = CM.features.persona?.inspect;
    if (typeof inspector !== "function") {
      status.textContent = "人物设定暂时无法打开。";
      return;
    }
    try {
      await inspector(activeCharacterId);
    } catch (error) {
      status.textContent = `人物设定读取失败：${text(error?.message || "请稍后重试")}`;
    }
  }

  shell.addEventListener("click", event => {
    const tab = event.target.closest("[data-profile-tab]");
    if (tab) {
      activeTab = tab.dataset.profileTab;
      renderPanel();
      return;
    }
    if (event.target.closest("[data-profile-close]")) {
      close();
      return;
    }
    if (event.target.closest("[data-profile-chat]")) {
      chatWithCharacter().catch(error => console.error("profile chat navigation", error));
      return;
    }
    if (event.target.closest("[data-profile-open-life]")) {
      if (activeCharacterId) location.assign(lifeUrl());
      return;
    }
    if (event.target.closest("[data-profile-inspect]")) {
      inspectPersona().catch(error => console.error("profile persona inspector", error));
      return;
    }
    if (event.target.closest("[data-profile-retry]")) {
      const token = ++requestEpoch;
      loadData(activeCharacterId, token).catch(error => console.error("profile reload", error));
    }
  });

  shell.addEventListener("keydown", event => {
    if (!event.target.matches("[data-profile-tab]") || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = tabButtons.indexOf(event.target);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabButtons.length - 1 :
      (index + (event.key === "ArrowRight" ? 1 : -1) + tabButtons.length) % tabButtons.length;
    tabButtons[next].focus();
    tabButtons[next].click();
  });

  CM.on("conversationChanged", payload => {
    if (opened && !switchingForChat && payload?.type === "DIRECT" && payload.characterId && payload.characterId !== activeCharacterId) {
      return open(payload.characterId);
    }
  });

  const modeObserver = new MutationObserver(() => {
    if (opened && document.body.classList.contains("space-mode")) close();
  });
  modeObserver.observe(document.body, {attributes: true, attributeFilter: ["class"]});

  CM.registerFeature("profile", {open, close});
})();
