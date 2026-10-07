(() => {
  const state = {view:"feed", sources:[], items:[]};

  const $ = selector => document.querySelector(selector);
  const escapeHtml = value => String(value ?? "")
    .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
    .replaceAll('"',"&quot;").replaceAll("'","&#039;");

  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      headers: {"Content-Type":"application/json", ...(options.headers || {})},
      ...options,
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch {}
      throw new Error(detail);
    }
    return response.json();
  };

  const fmtTime = raw => {
    if (!raw) return "暂无";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return raw;
    const diff = Date.now() - date.getTime();
    const minute = 60_000;
    if (diff >= 0 && diff < 60 * minute) return `${Math.max(1, Math.floor(diff / minute))} 分钟前`;
    if (diff >= 0 && diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
    return new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(date);
  };

  const setView = view => {
    state.view = view;
    const isFeed = view === "feed";
    const isSources = view === "sources";
    document.querySelectorAll("[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
    $("#feedView").classList.toggle("hidden", !isFeed);
    $("#sourcesView").classList.toggle("hidden", !isSources);
    $("#viewTitle").textContent = isFeed ? "信息流" : "RSS 订阅";
    $("#viewSubtitle").textContent = isFeed ? "来自你订阅的信息源" : "管理订阅、开关和抓取状态";
  };

  const loadSources = async () => {
    const data = await api("/v1/rss/sources");
    state.sources = data.sources || [];
    renderSources();
  };

  const loadFeed = async () => {
    const data = await api("/v1/rss/items?limit=80");
    state.items = data.items || [];
    renderFeed();
  };

  const renderSources = () => {
    const root = $("#sourceList");
    if (!state.sources.length) {
      root.innerHTML = '<div class="rss-empty">还没有订阅源。</div>';
      return;
    }
    root.innerHTML = state.sources.map(source => `
      <article class="source-row" data-source-id="${source.id}">
        <div>
          <h3>${escapeHtml(source.name)}</h3>
          <div class="source-url" title="${escapeHtml(source.feed_url)}">${escapeHtml(source.feed_url)}</div>
          <div class="source-info">
            <span>${source.item_count || 0} 篇</span>
            <span>最近抓取：${escapeHtml(fmtTime(source.last_fetch_at))}</span>
            ${source.last_error ? `<span class="source-error" title="${escapeHtml(source.last_error)}">抓取失败</span>` : ""}
          </div>
        </div>
        <div class="source-actions">
          <label class="switch" title="${source.enabled ? "关闭订阅" : "开启订阅"}">
            <input type="checkbox" data-source-toggle="${source.id}" ${source.enabled ? "checked" : ""}>
            <span></span>
          </label>
          <button type="button" data-source-refresh="${source.id}">刷新</button>
        </div>
      </article>
    `).join("");
  };

  const renderFeed = () => {
    const root = $("#feedGrid");
    $("#feedEmpty").classList.toggle("hidden", state.items.length > 0);
    root.innerHTML = state.items.map(item => `
      <article class="feed-card" tabindex="0" data-item-id="${item.id}">
        ${item.image_url ? `<img src="${escapeHtml(item.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
        <div class="feed-card-copy">
          <h2>${escapeHtml(item.title)}</h2>
          ${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}
          <div class="feed-meta">
            <span class="feed-source">${escapeHtml(item.source_name)}</span>
            <span>${escapeHtml(fmtTime(item.published_at || item.fetched_at))}</span>
          </div>
        </div>
      </article>
    `).join("");
  };

  const openArticle = async itemId => {
    const data = await api(`/v1/rss/items/${itemId}`);
    const item = data.item;
    $("#articleExternalLink").href = item.url || "#";
    $("#articleExternalLink").classList.toggle("hidden", !item.url);
    $("#articleBody").innerHTML = `
      <h1>${escapeHtml(item.title)}</h1>
      <div class="article-byline">${escapeHtml(item.source_name)} · ${escapeHtml(fmtTime(item.published_at || item.fetched_at))}</div>
      ${item.image_url ? `<img class="article-image" src="${escapeHtml(item.image_url)}" alt="" referrerpolicy="no-referrer">` : ""}
      <div class="article-text">${escapeHtml(item.content_text || item.summary || "该 Feed 没有提供正文，请打开原文阅读。")}</div>
    `;
    $("#articlePanel").classList.remove("hidden");
    $("#articlePanel").setAttribute("aria-hidden","false");
  };

  const closeArticle = () => {
    $("#articlePanel").classList.add("hidden");
    $("#articlePanel").setAttribute("aria-hidden","true");
  };

  const showModal = () => {
    $("#sourceForm").reset();
    $("#sourceFormError").classList.add("hidden");
    $("#sourceModal").classList.remove("hidden");
    $("#sourceUrl").focus();
  };
  const hideModal = () => $("#sourceModal").classList.add("hidden");

  document.addEventListener("click", async event => {
    const viewButton = event.target.closest("[data-view]");
    if (viewButton) {
      setView(viewButton.dataset.view);
      return;
    }
    const card = event.target.closest("[data-item-id]");
    if (card) {
      await openArticle(card.dataset.itemId);
      return;
    }
    const refresh = event.target.closest("[data-source-refresh]");
    if (refresh) {
      const original = refresh.textContent;
      refresh.disabled = true;
      refresh.textContent = "刷新中…";
      try {
        await api(`/v1/rss/sources/${refresh.dataset.sourceRefresh}/refresh`, {method:"POST"});
        await Promise.all([loadSources(), loadFeed()]);
      } catch (error) {
        alert(`刷新失败：${error.message}`);
      } finally {
        refresh.disabled = false;
        refresh.textContent = original;
      }
    }
  });

  document.addEventListener("change", async event => {
    const toggle = event.target.closest("[data-source-toggle]");
    if (!toggle) return;
    try {
      await api(`/v1/rss/sources/${toggle.dataset.sourceToggle}`, {
        method:"PATCH",
        body:JSON.stringify({enabled:toggle.checked}),
      });
      await loadSources();
    } catch (error) {
      toggle.checked = !toggle.checked;
      alert(`更新失败：${error.message}`);
    }
  });

  $("#addSourceButton").addEventListener("click", showModal);
  $("#closeSourceModal").addEventListener("click", hideModal);
  $("#cancelSourceButton").addEventListener("click", hideModal);
  $("#closeArticleButton").addEventListener("click", closeArticle);
  $("#sourceModal").addEventListener("click", event => { if (event.target === $("#sourceModal")) hideModal(); });

  $("#sourceForm").addEventListener("submit", async event => {
    event.preventDefault();
    const errorBox = $("#sourceFormError");
    errorBox.classList.add("hidden");
    const submit = event.submitter;
    if (submit) {
      submit.disabled = true;
      submit.textContent = "添加中…";
    }
    try {
      const payload = {
        feed_url: $("#sourceUrl").value.trim(),
        name: $("#sourceName").value.trim(),
      };
      const result = await api("/v1/rss/sources", {method:"POST", body:JSON.stringify(payload)});
      hideModal();
      await Promise.all([loadSources(), loadFeed()]);
      setView(result.refresh?.ok ? "feed" : "sources");
      if (!result.refresh?.ok) alert(`订阅已保存，但首次抓取失败：${result.refresh?.error || "未知错误"}`);
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.classList.remove("hidden");
    } finally {
      if (submit) {
        submit.disabled = false;
        submit.textContent = "添加订阅";
      }
    }
  });

  Promise.all([loadSources(), loadFeed()]).catch(error => {
    $("#feedEmpty").textContent = `加载失败：${error.message}`;
    $("#feedEmpty").classList.remove("hidden");
  });
})();
