(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before sidebar_collapse.js");

  const STORAGE_KEY = "character-memory:sidebar-collapsed";
  const RECENT_KEY = "character-memory:recent-characters";
  const MAX_RAIL_CHARACTERS = 6;
  const MOBILE_QUERY = window.matchMedia("(max-width: 820px)");

  const sidebar = document.querySelector(".sidebar");
  const collapseButton = document.getElementById("sidebarCollapseButton");
  const mobileButton = document.getElementById("sidebarMobileButton");
  const railMore = document.getElementById("sidebarCharacterMore");
  const groupSlot = document.getElementById("sidebarGroups");
  if (!sidebar || !collapseButton || !mobileButton || !railMore) return;

  const storedCollapsed = () => localStorage.getItem(STORAGE_KEY) === "true";

  function recentIds() {
    try {
      const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch {
      return [];
    }
  }

  function rememberCharacter(characterId) {
    if (!characterId) return;
    const next = [String(characterId), ...recentIds().filter(id => id !== String(characterId))].slice(0, 20);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  }

  function syncTitles() {
    CM.dom.characterList?.querySelectorAll("[data-character]").forEach(button => {
      const name = button.querySelector(".character-name")?.textContent?.trim() || button.dataset.character || "人物";
      const preview = button.querySelector(".character-tagline")?.textContent?.trim() || "";
      button.title = preview ? name + "\n" + preview : name;
    });
    groupSlot?.querySelectorAll("[data-group]").forEach(button => {
      const name = button.querySelector(".group-name")?.textContent?.trim() || "群聊";
      const members = button.querySelector(".group-members")?.textContent?.trim() || "";
      button.title = members ? name + "\n" + members : name;
    });
  }

  function syncRailCharacters() {
    const rows = [...(CM.dom.characterList?.querySelectorAll("[data-character-row]") || [])];
    const available = new Set(rows.map(row => row.dataset.characterRow));
    const ordered = [
      CM.state.characterId,
      ...recentIds(),
      ...CM.state.characters.map(item => item.id),
    ].filter((id, index, list) => id && available.has(String(id)) && list.indexOf(id) === index);

    const visible = new Set(ordered.slice(0, MAX_RAIL_CHARACTERS).map(String));
    rows.forEach(row => row.classList.toggle("rail-visible", visible.has(row.dataset.characterRow)));

    const hiddenCount = Math.max(0, rows.length - visible.size);
    railMore.classList.toggle("hidden", hiddenCount === 0);
    railMore.title = hiddenCount ? `展开并查看另外 ${hiddenCount} 个人物` : "展开人物列表";
  }

  function syncRailGroups() {
    const rows = [...(groupSlot?.querySelectorAll("[data-group-row]") || [])];
    const active = rows.find(row => row.querySelector(".group-item.active"));
    rows.forEach((row, index) => row.classList.toggle("rail-visible", row === active || (!active && index === 0)));
  }

  function sync() {
    syncTitles();
    syncRailCharacters();
    syncRailGroups();
    CM.features.characterArchive?.syncCurrentAction?.();
  }

  function applyDesktopState(collapsed, persist = true) {
    const compact = Boolean(collapsed);
    document.body.classList.toggle("sidebar-collapsed", compact);
    sidebar.dataset.mode = compact ? "compact" : "expanded";
    collapseButton.textContent = compact ? "›" : "‹";
    collapseButton.title = compact ? "展开侧边栏" : "收起侧边栏";
    collapseButton.setAttribute("aria-label", collapseButton.title);
    collapseButton.setAttribute("aria-expanded", String(!compact));
    if (persist) localStorage.setItem(STORAGE_KEY, compact ? "true" : "false");
    sync();
  }

  function setMobileOpen(open) {
    const next = Boolean(open);
    document.body.classList.toggle("sidebar-mobile-open", next);
    mobileButton.setAttribute("aria-expanded", String(next));
    mobileButton.title = next ? "关闭侧边栏" : "打开侧边栏";
    mobileButton.setAttribute("aria-label", mobileButton.title);
  }

  collapseButton.addEventListener("click", () => {
    if (!MOBILE_QUERY.matches) applyDesktopState(sidebar.dataset.mode !== "compact");
  });
  railMore.addEventListener("click", () => applyDesktopState(false));
  mobileButton.addEventListener("click", () => setMobileOpen(!document.body.classList.contains("sidebar-mobile-open")));

  const backdrop = document.createElement("button");
  backdrop.type = "button";
  backdrop.className = "sidebar-mobile-backdrop";
  backdrop.setAttribute("aria-label", "关闭侧边栏");
  document.body.appendChild(backdrop);
  backdrop.addEventListener("click", () => setMobileOpen(false));

  sidebar.addEventListener("click", event => {
    if (MOBILE_QUERY.matches && event.target.closest("[data-character], [data-group], .space-nav-button")) {
      setMobileOpen(false);
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape") setMobileOpen(false);
  });

  MOBILE_QUERY.addEventListener?.("change", event => {
    setMobileOpen(false);
    if (!event.matches) applyDesktopState(storedCollapsed(), false);
  });

  const observer = new MutationObserver(sync);
  observer.observe(CM.dom.characterList, {childList:true, subtree:true});
  if (groupSlot) observer.observe(groupSlot, {childList:true, subtree:true});

  CM.on("charactersLoaded", () => {
    rememberCharacter(CM.state.characterId);
    sync();
  });
  CM.on("conversationChanged", detail => {
    if (detail?.type === "DIRECT") rememberCharacter(detail.characterId || CM.state.characterId);
    sync();
  });

  applyDesktopState(storedCollapsed(), false);
  setMobileOpen(false);
  sync();

  CM.registerFeature("sidebarCollapse", {
    collapse: () => applyDesktopState(true),
    expand: () => applyDesktopState(false),
    toggle: () => applyDesktopState(sidebar.dataset.mode !== "compact"),
    rememberCharacter,
    sync,
  });
})();
