(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before composer_tools.js");

  const composer = CM.dom.composer;
  const input = CM.dom.input;
  if (!composer || !input) return;

  const imageButton = CM.features.images?.trigger || document.querySelector(".image-trigger");
  const aiImageButton = CM.features.aiImages?.trigger || document.querySelector(".ai-image-trigger");
  const toolButtons = [
    imageButton ? {button:imageButton, icon:"▧", label:"发送图片"} : null,
    aiImageButton ? {button:aiImageButton, icon:"✦", label:"AI 生成图片"} : null,
  ].filter(Boolean);

  if (!toolButtons.length) return;

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "composer-tools-trigger";
  trigger.title = "添加内容";
  trigger.setAttribute("aria-label", "添加内容");
  trigger.setAttribute("aria-haspopup", "true");
  trigger.setAttribute("aria-expanded", "false");
  trigger.textContent = "＋";
  composer.insertBefore(trigger, input);

  const menu = document.createElement("div");
  menu.className = "composer-tools-menu hidden";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "添加内容");

  for (const item of toolButtons) {
    const {button, icon, label} = item;
    button.classList.add("composer-tool-menu-item");
    button.innerHTML = `<span class="composer-tool-menu-icon" aria-hidden="true">${icon}</span><span>${label}</span>`;
    menu.appendChild(button);
  }
  composer.appendChild(menu);

  function close() {
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
  }

  function syncDisabled() {
    trigger.disabled = toolButtons.every(({button}) => button.disabled);
  }

  trigger.addEventListener("click", event => {
    event.stopPropagation();
    const open = menu.classList.toggle("hidden") === false;
    trigger.setAttribute("aria-expanded", String(open));
  });

  for (const {button} of toolButtons) {
    button.addEventListener("click", () => {
      close();
      queueMicrotask(syncDisabled);
    });
  }

  document.addEventListener("click", event => {
    if (!event.target.closest(".composer-tools-menu") && !event.target.closest(".composer-tools-trigger")) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") close();
  });

  CM.on("conversationChanged", () => {
    close();
    queueMicrotask(syncDisabled);
  });
  CM.on("ready", syncDisabled);
  syncDisabled();

  CM.registerFeature("composerTools", {close, trigger, menu, syncDisabled});
})();
