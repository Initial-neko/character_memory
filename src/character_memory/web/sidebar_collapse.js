(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before sidebar_collapse.js");

  const STORAGE_KEY = "character-memory:sidebar-collapsed";
  const MOBILE_QUERY = window.matchMedia("(max-width: 820px)");
  const sidebar = document.querySelector(".sidebar");
  const topbar = document.querySelector(".topbar");
  if (!sidebar || !topbar) return;

  const collapseButton = document.createElement("button");
  collapseButton.type = "button";
  collapseButton.className = "sidebar-collapse-toggle";
  sidebar.appendChild(collapseButton);

  const mobileButton = document.createElement("button");
  mobileButton.type = "button";
  mobileButton.className = "sidebar-mobile-toggle";
  mobileButton.textContent = "☰";
  topbar.insertBefore(mobileButton, topbar.firstChild);

  const backdrop = document.createElement("button");
  backdrop.type = "button";
  backdrop.className = "sidebar-mobile-backdrop";
  backdrop.setAttribute("aria-label", "关闭人物列表");
  document.body.appendChild(backdrop);

  const storedCollapsed = () => localStorage.getItem(STORAGE_KEY) === "true";

  function syncTitles() {
    CM.dom.characterList?.querySelectorAll("[data-character]").forEach(button => {
      const name = button.querySelector(".character-name")?.textContent?.trim() || button.dataset.character || "人物";
      const preview = button.querySelector(".character-tagline")?.textContent?.trim() || "";
      button.title = preview ? name + "\n" + preview : name;
    });
    document.querySelectorAll("[data-group]").forEach(button => {
      const name = button.querySelector(".group-name")?.textContent?.trim() || "群聊";
      const members = button.querySelector(".group-members")?.textContent?.trim() || "";
      button.title = members ? name + "\n" + members : name;
    });
  }

  function applyDesktopState(collapsed, persist = true) {
    document.body.classList.toggle("sidebar-collapsed", Boolean(collapsed));
    collapseButton.textContent = collapsed ? "›" : "‹";
    collapseButton.title = collapsed ? "展开侧边栏" : "收起侧边栏";
    collapseButton.setAttribute("aria-label", collapseButton.title);
    collapseButton.setAttribute("aria-expanded", String(!collapsed));
    if (persist) localStorage.setItem(STORAGE_KEY, collapsed ? "true" : "false");
    syncTitles();
  }

  function setMobileOpen(open) {
    document.body.classList.toggle("sidebar-mobile-open", Boolean(open));
    mobileButton.setAttribute("aria-expanded", String(Boolean(open)));
    mobileButton.title = open ? "关闭人物列表" : "打开人物列表";
    mobileButton.setAttribute("aria-label", mobileButton.title);
  }

  collapseButton.addEventListener("click", () => {
    if (!MOBILE_QUERY.matches) applyDesktopState(!document.body.classList.contains("sidebar-collapsed"));
  });
  mobileButton.addEventListener("click", () => setMobileOpen(!document.body.classList.contains("sidebar-mobile-open")));
  backdrop.addEventListener("click", () => setMobileOpen(false));

  sidebar.addEventListener("click", event => {
    if (MOBILE_QUERY.matches && event.target.closest("[data-character], [data-group]")) setMobileOpen(false);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") setMobileOpen(false);
  });
  MOBILE_QUERY.addEventListener?.("change", event => {
    setMobileOpen(false);
    if (!event.matches) applyDesktopState(storedCollapsed(), false);
  });

  const observer = new MutationObserver(syncTitles);
  observer.observe(CM.dom.characterList, {childList:true, subtree:true});
  const groupSection = document.querySelector(".group-section");
  if (groupSection) observer.observe(groupSection, {childList:true, subtree:true});
  CM.on("charactersLoaded", syncTitles);
  CM.on("conversationChanged", syncTitles);

  applyDesktopState(storedCollapsed(), false);
  setMobileOpen(false);
  syncTitles();

  CM.registerFeature("sidebarCollapse", {
    collapse: () => applyDesktopState(true),
    expand: () => applyDesktopState(false),
    toggle: () => applyDesktopState(!document.body.classList.contains("sidebar-collapsed")),
    syncTitles,
  });
})();
