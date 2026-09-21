(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before topbar_menu.js");

  const actions = document.querySelector(".topbar-actions");
  const runtimeButton = CM.dom.runtimeButton;
  if (!actions || !runtimeButton) return;

  const trigger = document.createElement("button");
  trigger.id = "topbarMoreButton";
  trigger.className = "ghost-button topbar-more-button";
  trigger.type = "button";
  trigger.title = "更多操作";
  trigger.setAttribute("aria-label", "更多操作");
  trigger.setAttribute("aria-haspopup", "true");
  trigger.setAttribute("aria-expanded", "false");
  trigger.textContent = "⋯";

  const menu = document.createElement("div");
  menu.id = "topbarMenu";
  menu.className = "topbar-menu hidden";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "更多操作");

  actions.append(trigger, menu);

  // Only conversation-frequency actions stay visible in the header:
  // call, search, and this overflow trigger. Everything diagnostic or
  // configuration-oriented moves into one predictable menu.
  const settingsLink = document.getElementById("settingsLink");
  const wakeButton = document.getElementById("wakeButton");
  const intentButton = document.getElementById("intentButton");
  const spaceButton = document.getElementById("characterSpaceButton");
  const archiveButton = document.getElementById("archiveCurrentCharacterButton");
  const items = [spaceButton, intentButton, wakeButton, runtimeButton, settingsLink, archiveButton].filter(Boolean);

  for (const item of items) {
    item.classList.remove("desktop-only-control");
    if (item === settingsLink) {
      item.textContent = "设置";
      item.title = "打开 Settings Center";
    } else if (item === runtimeButton) {
      item.textContent = "运行状态";
    } else if (item === intentButton) {
      item.textContent = "Intent";
    } else if (item === wakeButton) {
      item.textContent = "唤醒人物";
    }
    menu.append(item);
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
    const action = event.target.closest("button, a");
    // Wake keeps its live status label in-place while running. Other actions
    // either open a drawer or navigate, so the menu can close immediately.
    if (action && action !== wakeButton) close();
  });

  document.addEventListener("click", event => {
    if (!event.target.closest(".topbar-actions")) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") close();
  });

  CM.registerFeature("topbarMenu", {close});
})();
