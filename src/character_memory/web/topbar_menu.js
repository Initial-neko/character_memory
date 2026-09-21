(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before topbar_menu.js");

  const actions = document.querySelector(".topbar-actions");
  const runtimeButton = CM.dom.runtimeButton;
  if (!actions || !runtimeButton) return;

  // The topbar is the narrowest row on the page: it shares its width with the
  // character's name and tagline, and six controls do not fit beside them at
  // any window width worth supporting. Before this menu existed the actions
  // simply won -- they kept their intrinsic width, the heading overflowed, and
  // the avatar was squeezed to zero. The controls the user actually reaches
  // for during a conversation stay in the open; the three diagnostic views
  // collapse behind one trigger.
  const trigger = document.createElement("button");
  trigger.id = "topbarMoreButton";
  trigger.className = "ghost-button topbar-more-button";
  trigger.type = "button";
  trigger.title = "诊断工具";
  trigger.setAttribute("aria-haspopup", "true");
  trigger.setAttribute("aria-expanded", "false");
  trigger.textContent = "⋯";

  const menu = document.createElement("div");
  menu.id = "topbarMenu";
  menu.className = "topbar-menu hidden";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "诊断工具");

  actions.append(trigger, menu);

  // wake.js and intent.js build their buttons as siblings of Runtime's, and
  // they keep owning them -- this only relocates the elements. Keep this order:
  // it is the order they used to appear in.
  const wakeButton = document.getElementById("wakeButton");
  for (const button of [wakeButton, document.getElementById("intentButton"), runtimeButton]) {
    if (button) menu.append(button);
  }

  function close() {
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
  }

  trigger.addEventListener("click", event => {
    event.stopPropagation();
    const open = menu.classList.toggle("hidden") === false;
    trigger.setAttribute("aria-expanded", String(open));
  });

  menu.addEventListener("click", event => {
    const button = event.target.closest("button");
    // 唤醒 answers on its own label ("唤醒中…", "没有想说什么"), so the menu has
    // to stay open for it. The other two open a drawer that covers the topbar
    // anyway, and leaving the menu open behind it would be untidy.
    if (button && button !== wakeButton) close();
  });

  document.addEventListener("click", event => {
    if (!event.target.closest(".topbar-actions")) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") close();
  });

  CM.registerFeature("topbarMenu", {close});
})();
