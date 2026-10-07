(() => {
  const state = {view:"feed", period:"today", q:"", category:"", sourceId:"", sources:[], items:[],
    next:null, feedDate:null, feedRequest:0, sourceRequest:0, modalVersion:0, articleRequest:0,
    loading:false, retryAppend:false, articleOrigin:null};
  const $ = selector => document.querySelector(selector);
  const escapeHtml = value => String(value ?? "").replaceAll("&","&amp;")
    .replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#039;");
  const safeUrl = raw => {
    try { const url = new URL(raw, location.href); return ["http:","https:"].includes(url.protocol) ? url.href : ""; }
    catch { return ""; }
  };
  const imageSrc = (itemId, url) => {
    if (new URL(url, location.href).origin === location.origin) return url;
    return `/v1/rss/items/${itemId}/image?${new URLSearchParams({url})}`;
  };
  const articleContent = item => {
    const root = document.createElement("div");
    if (!item.content_html) {
      root.className = "article-text";
      root.textContent = item.content_text || item.summary || "该 Feed 没有提供内容，请打开原文阅读。";
      return root;
    }
    root.className = "article-rich";
    const allowed = new Set(["p","div","h1","h2","h3","h4","h5","h6","br","hr","ul","ol","li","blockquote","pre","code","strong","b","em","i","a","img","figure","figcaption","table","thead","tbody","tr","th","td"]);
    const drop = new Set(["script","style","iframe","object","embed","svg","math","template","noscript","head"]);
    const documentSource = new DOMParser().parseFromString(item.content_html, "text/html");
    const copy = (node, parent) => {
      if (node.nodeType === Node.TEXT_NODE) { parent.append(document.createTextNode(node.textContent)); return; }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const tag = node.localName.toLowerCase();
      if (drop.has(tag)) return;
      if (!allowed.has(tag)) { node.childNodes.forEach(child => copy(child, parent)); return; }
      const clean = document.createElement(tag);
      if (tag === "img") {
        const url = node.getAttribute("src") || "";
        if (!url || !safeUrl(url)) return;
        clean.src = imageSrc(item.id, url); clean.alt = node.getAttribute("alt") || "";
        clean.loading = "lazy"; clean.referrerPolicy = "no-referrer";
      } else if (tag === "a") {
        const url = node.getAttribute("href") ? safeUrl(node.getAttribute("href")) : "";
        if (url) { clean.href = url; clean.target = "_blank"; clean.rel = "noopener noreferrer"; }
      }
      node.childNodes.forEach(child => copy(child, clean));
      parent.append(clean);
    };
    documentSource.body.childNodes.forEach(child => copy(child, root));
    return root;
  };
  const api = async (url, options = {}) => {
    const response = await fetch(url, {...options, headers:{"Content-Type":"application/json", ...(options.headers || {})}});
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const data = await response.json(); detail = typeof data.detail === "string" ? data.detail : detail; } catch {}
      throw new Error(detail);
    }
    return response.json();
  };
  const fmtTime = raw => {
    if (!raw) return "暂无";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return "时间未知";
    const diff = Date.now() - date.getTime();
    if (diff >= 0 && diff < 3_600_000) return `${Math.max(1, Math.floor(diff / 60_000))} 分钟前`;
    if (diff >= 0 && diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
    return new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(date);
  };
  const feedback = (kind, message, failed = false) => {
    $(`#${kind}Status`).textContent = message;
    $(`#${kind}Status`).parentElement.classList.toggle("error", failed);
    $(kind === "feed" ? "#retryFeedButton" : "#retrySourcesButton").classList.toggle("hidden", !failed);
  };
  const setView = view => {
    state.view = view;
    const isFeed = view === "feed", isSources = view === "sources";
    document.querySelectorAll("[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
    $("#feedView").classList.toggle("hidden", !isFeed);
    $("#sourcesView").classList.toggle("hidden", !isSources);
    const source = state.sources.find(source => String(source.id) === state.sourceId);
    $("#viewTitle").textContent = view === "feed" ? (source?.name || "外部信息") : "RSS 订阅";
    $("#viewSubtitle").textContent = view === "feed" ? "来自你订阅的信息源" : "管理订阅、开关和抓取状态";
  };
  const loadSources = async () => {
    const request = ++state.sourceRequest;
    feedback("source", "加载订阅中…");
    try {
      const data = await api("/v1/rss/sources");
      if (request !== state.sourceRequest) return;
      state.sources = data.sources || []; renderSources(); renderSourceNavigation(); setView(state.view);
      $("#feedSourcesStatus").textContent = ""; feedback("source", "");
    } catch (error) {
      if (request === state.sourceRequest) {
        feedback("source", `加载失败：${error.message}`, true);
        $("#feedSourcesStatus").replaceChildren(document.createTextNode("订阅源加载失败 "));
        const retry = document.createElement("button"); retry.type = "button"; retry.id = "retrySourceNavigation"; retry.textContent = "重试";
        $("#feedSourcesStatus").append(retry);
      }
    }
  };
  const renderSourceNavigation = () => {
    $("#feedSources").innerHTML = [{id:"", name:"全部订阅"}, ...state.sources].map(source =>
      `<button type="button" data-feed-source="${source.id}" aria-pressed="${String(source.id) === state.sourceId}" title="${escapeHtml(source.name)}"><span>${escapeHtml(source.name)}</span>${source.id ? `<small>${source.item_count || 0}</small>` : ""}</button>`).join("");
  };
  const renderSources = () => {
    $("#sourceList").innerHTML = state.sources.length ? state.sources.map(source => `
      <article class="source-row" data-source-id="${source.id}">
        <div><h3>${escapeHtml(source.name)}</h3><div class="source-url" title="${escapeHtml(source.feed_url)}">${escapeHtml(source.feed_url)}</div>
          <div class="source-info"><span>${source.item_count || 0} 篇</span>
            <span>最近抓取：${escapeHtml(fmtTime(source.last_fetch_at))}</span><span>最近成功：${escapeHtml(fmtTime(source.last_success_at))}</span>
            ${source.last_error ? `<span class="source-error">抓取失败：${escapeHtml(source.last_error)}</span>` : ""}
          </div></div>
        <div class="source-actions"><label class="switch"><input type="checkbox" data-source-toggle="${source.id}" aria-label="启用 ${escapeHtml(source.name)}" ${source.enabled ? "checked" : ""}><span></span></label>
          <button type="button" data-source-refresh="${source.id}">刷新</button></div>
      </article>`).join("") : '<div class="rss-empty">还没有订阅源，点击“添加订阅”开始。</div>';
  };
  const renderCards = items => items.map(item => {
    const image = item.image_url && safeUrl(item.image_url) ? item.image_url : "";
    return `<article class="feed-card" tabindex="0" role="button" aria-label="阅读 ${escapeHtml(item.title)}" data-item-id="${item.id}">
      ${image ? `<img src="${escapeHtml(imageSrc(item.id, image))}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
      <div class="feed-card-copy"><h2>${escapeHtml(item.title)}</h2>${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}
        <div class="feed-meta"><span class="feed-source">${escapeHtml(item.source_name)}</span><span>${escapeHtml(item.published_at ? fmtTime(item.published_at) : "发布时间未知")}</span></div>
      </div></article>`;
  }).join("");
  const loadFeed = async (append = false) => {
    if (append && (state.loading || !state.next)) return;
    const request = ++state.feedRequest;
    state.loading = true; state.retryAppend = append;
    $("#loadMoreButton").disabled = true;
    $("#feedEmpty").classList.add("hidden");
    feedback("feed", append ? "加载更多…" : "加载文章中…");
    if (!append) { state.items = []; state.next = null; $("#feedGrid").replaceChildren(); $("#loadMoreButton").classList.add("hidden"); }
    const params = new URLSearchParams({period:state.period, limit:"30"});
    if (state.sourceId) params.set("source_id", state.sourceId);
    if (state.q) params.set("q", state.q);
    if (state.category) params.set("category", state.category);
    if (append) params.set("before_id", state.next);
    try {
      const data = await api(`/v1/rss/items?${params}`);
      if (request !== state.feedRequest) return;
      // A page opened yesterday must not append a new day's response to old cards.
      if (append && state.period === "today" && state.feedDate !== data.query?.date) return loadFeed();
      state.feedDate = data.query?.date || null;
      const items = data.items || [];
      state.items = append ? [...state.items, ...items] : items; state.next = data.next_before_id;
      if (append) $("#feedGrid").insertAdjacentHTML("beforeend", renderCards(items));
      else $("#feedGrid").innerHTML = renderCards(items);
      $("#loadMoreButton").classList.toggle("hidden", !data.has_more);
      $("#feedEmpty").textContent = state.q || state.category ? "没有符合筛选条件的文章，试试其他关键词或类型。" :
        state.period === "today" ? "今天还没有已发布的文章。可查看全部文章，或在 RSS 订阅中刷新。" : "还没有文章，先添加或刷新一个订阅源。";
      $("#feedEmpty").classList.toggle("hidden", state.items.length > 0);
      $("#feedHint").textContent = `${data.query?.date ? `${data.query.date} · ` : ""}北京时间 · 类型按标题关键词筛选`;
      feedback("feed", `已显示 ${state.items.length} 篇${data.has_more ? "" : " · 已全部加载"}`);
    } catch (error) { if (request === state.feedRequest) feedback("feed", `加载失败：${error.message}`, true); }
    finally { if (request === state.feedRequest) { state.loading = false; $("#loadMoreButton").disabled = false; } }
  };
  const loadCategories = async () => {
    const render = categories => { $("#feedCategories").innerHTML = [{id:"",label:"全部类型"}, ...categories].map(category =>
      `<button type="button" data-category="${escapeHtml(category.id)}" aria-pressed="${category.id === state.category}">${escapeHtml(category.label)}</button>`).join(""); };
    render([]);
    try { const data = await api("/v1/rss/categories"); render(data.categories || []); }
    catch { $("#feedCategories").insertAdjacentHTML("beforeend", '<button type="button" id="retryCategoriesButton">类型加载失败，重试</button>'); }
  };
  const closeArticle = () => {
    ++state.articleRequest; $("#articlePanel").classList.add("hidden"); $("#articlePanel").setAttribute("aria-hidden", "true");
    $(".rss-shell").inert = false; state.articleOrigin?.focus({preventScroll:true});
  };
  const openArticle = async (itemId, origin) => {
    const request = ++state.articleRequest; state.articleOrigin = origin;
    $("#articlePanel").classList.remove("hidden"); $("#articlePanel").setAttribute("aria-hidden", "false"); $(".rss-shell").inert = true;
    $("#articleBody").textContent = "加载文章中…"; $("#articleExternalLink").classList.add("hidden"); $("#closeArticleButton").focus(); $("#articlePanel").scrollTop = 0;
    try {
      const {item} = await api(`/v1/rss/items/${itemId}`);
      if (request !== state.articleRequest) return;
      const url = item.url ? safeUrl(item.url) : "", image = item.image_url && safeUrl(item.image_url) ? item.image_url : "";
      $("#articleExternalLink").href = url || "#"; $("#articleExternalLink").classList.toggle("hidden", !url);
      $("#articleBody").innerHTML = `<h1>${escapeHtml(item.title)}</h1>
        <div class="article-byline">${escapeHtml(item.source_name)} · ${escapeHtml(item.published_at ? fmtTime(item.published_at) : "发布时间未知")}</div>
        ${image && !item.content_html?.includes("<img ") ? `<img class="article-image" src="${escapeHtml(imageSrc(item.id, image))}" alt="" referrerpolicy="no-referrer">` : ""}
        <p class="article-note">以下是 Feed 提供的内容，可能为摘要或节选；完整内容请查看原文。</p>`;
      $("#articleBody").append(articleContent(item));
      const footer = document.createElement("footer"); footer.className = "article-original";
      if (url) {
        const link = document.createElement("a"); link.href = url; link.target = "_blank";
        link.rel = "noopener noreferrer"; link.textContent = "查看原文 ↗"; footer.append(link);
      } else footer.textContent = "该 Feed 未提供可用的原文地址。";
      const time = document.createElement("span"); time.textContent = item.published_at ? fmtTime(item.published_at) : "发布时间未知";
      footer.append(time); $("#articleBody").append(footer);
    } catch (error) {
      if (request === state.articleRequest) $("#articleBody").innerHTML = `<p role="alert">加载失败：${escapeHtml(error.message)}</p><button type="button" data-article-retry="${itemId}">重试</button>`;
    }
  };
  const hideModal = () => { ++state.modalVersion; $("#sourceModal").classList.add("hidden"); $(".rss-shell").inert = false; $("#addSourceButton").focus(); };
  const showModal = () => {
    ++state.modalVersion;
    const submit = $("#sourceForm button[type=submit]"); submit.disabled = false; submit.textContent = "添加订阅";
    $("#sourceForm").reset(); $("#sourceFormError").classList.add("hidden"); $("#sourceModal").classList.remove("hidden"); $(".rss-shell").inert = true; $("#sourceUrl").focus();
  };
  const refreshSource = async button => {
    button.disabled = true; button.textContent = "刷新中…";
    try {
      const result = await api(`/v1/rss/sources/${button.dataset.sourceRefresh}/refresh`, {method:"POST"});
      await Promise.all([loadSources(), loadFeed()]);
      if (!result.ok) feedback("source", `刷新失败：${result.error || "未知错误"}`, true);
    } catch (error) { feedback("source", `刷新失败：${error.message}`, true); }
    finally { button.disabled = false; button.textContent = "刷新"; }
  };
  document.addEventListener("click", event => {
    const target = event.target, view = target.closest("[data-view]");
    const source = target.closest("[data-feed-source]");
    if (source) {
      state.sourceId = source.dataset.feedSource; state.period = state.sourceId ? "all" : "today";
      document.querySelectorAll("[data-feed-source]").forEach(button => button.setAttribute("aria-pressed", String(button === source)));
      setView("feed");
      document.querySelectorAll("[data-period]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.period === state.period)));
      loadFeed(); return;
    }
    if (target.closest("#retrySourceNavigation")) { loadSources(); return; }
    if (view) { setView(view.dataset.view); return; }
    const period = target.closest("[data-period]");
    if (period) {
      state.period = period.dataset.period;
      document.querySelectorAll("[data-period]").forEach(button => button.setAttribute("aria-pressed", String(button === period)));
      loadFeed(); return;
    }
    const category = target.closest("[data-category]");
    if (category) {
      state.category = category.dataset.category;
      document.querySelectorAll("[data-category]").forEach(button => button.setAttribute("aria-pressed", String(button === category)));
      loadFeed(); return;
    }
    const retry = target.closest("[data-article-retry]");
    if (retry) { openArticle(retry.dataset.articleRetry, state.articleOrigin); return; }
    const card = target.closest("[data-item-id]");
    if (card) { openArticle(card.dataset.itemId, card); return; }
    const refresh = target.closest("[data-source-refresh]");
    if (refresh) { refreshSource(refresh); return; }
    if (target.closest("#retryCategoriesButton")) loadCategories();
    const imageRetry = target.closest("[data-image-retry]");
    if (imageRetry) {
      const image = document.createElement("img");
      image.className = imageRetry.dataset.imageClass || "";
      image.alt = imageRetry.dataset.imageAlt || "";
      image.src = imageRetry.dataset.imageRetry;
      imageRetry.closest(".image-failure").replaceWith(image);
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      if (!$("#sourceModal").classList.contains("hidden")) hideModal();
      else if (!$("#articlePanel").classList.contains("hidden")) closeArticle();
    }
    const card = event.target.closest("[data-item-id]");
    if (card && ["Enter", " "].includes(event.key)) { event.preventDefault(); openArticle(card.dataset.itemId, card); }
    if (event.key === "Tab") {
      const dialog = [$("#sourceModal"), $("#articlePanel")].find(node => !node.classList.contains("hidden"));
      if (!dialog) return;
      const focusable = [...dialog.querySelectorAll("button:not(:disabled), input:not(:disabled), a[href]")].filter(node => node.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  document.addEventListener("error", event => {
    const image = event.target;
    if (!image.matches?.(".feed-card img, .article-image, .article-rich img")) return;
    const notice = document.createElement("span"); notice.className = "image-failure";
    notice.textContent = image.alt ? `${image.alt} · 图片暂时无法加载 ` : "图片暂时无法加载 ";
    const retry = document.createElement("button"); retry.type = "button"; retry.textContent = "重试";
    retry.dataset.imageRetry = image.src; retry.dataset.imageClass = image.className; retry.dataset.imageAlt = image.alt;
    notice.append(retry); image.replaceWith(notice);
  }, true);
  document.addEventListener("change", async event => {
    const toggle = event.target.closest("[data-source-toggle]");
    if (!toggle) return;
    toggle.disabled = true;
    try {
      await api(`/v1/rss/sources/${toggle.dataset.sourceToggle}`, {method:"PATCH", body:JSON.stringify({enabled:toggle.checked})});
      await loadSources();
    } catch (error) { toggle.checked = !toggle.checked; feedback("source", `更新失败：${error.message}`, true); }
    finally { toggle.disabled = false; }
  });
  $("#feedSearchForm").addEventListener("submit", event => { event.preventDefault(); state.q = $("#feedSearch").value.trim(); loadFeed(); });
  $("#feedSearch").addEventListener("search", () => { if (!$("#feedSearch").value) { state.q = ""; loadFeed(); } });
  $("#loadMoreButton").addEventListener("click", () => loadFeed(true));
  $("#retryFeedButton").addEventListener("click", () => loadFeed(state.retryAppend));
  $("#retrySourcesButton").addEventListener("click", loadSources);
  $("#addSourceButton").addEventListener("click", showModal);
  $("#closeSourceModal").addEventListener("click", hideModal);
  $("#cancelSourceButton").addEventListener("click", hideModal);
  $("#closeArticleButton").addEventListener("click", closeArticle);
  $("#sourceModal").addEventListener("click", event => { if (event.target === $("#sourceModal")) hideModal(); });
  $("#sourceForm").addEventListener("submit", async event => {
    event.preventDefault();
    const version = state.modalVersion;
    const errorBox = $("#sourceFormError"), submit = $("#sourceForm button[type=submit]");
    errorBox.classList.add("hidden"); submit.disabled = true; submit.textContent = "添加中…";
    try {
      const result = await api("/v1/rss/sources", {method:"POST", body:JSON.stringify({feed_url:$("#sourceUrl").value.trim(), name:$("#sourceName").value.trim()})});
      const current = version === state.modalVersion;
      if (current) hideModal();
      await Promise.all([loadSources(), loadFeed()]);
      if (current && state.modalVersion === version + 1) {
        setView(result.refresh?.ok ? "feed" : "sources");
        if (!result.refresh?.ok) feedback("source", `订阅已保存，首次抓取失败：${result.refresh?.error || "未知错误"}`, true);
      }
    } catch (error) {
      if (version === state.modalVersion) { errorBox.textContent = error.message; errorBox.classList.remove("hidden"); }
    } finally {
      if (version === state.modalVersion) { submit.disabled = false; submit.textContent = "添加订阅"; }
    }
  });
  renderSourceNavigation(); loadSources(); loadCategories(); loadFeed();
})();
