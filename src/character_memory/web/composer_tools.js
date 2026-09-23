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

  function open() {
    // Half of the one-popover-at-a-time contract; stickers.js is the other
    // half. Both triggers stop propagation so their own panel survives the
    // document click that closes the other, which is exactly why the click
    // alone cannot be the contract.
    CM.emit("composerPopoverOpened", "composerTools");
    menu.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
  }

  function syncDisabled() {
    trigger.disabled = toolButtons.every(({button}) => button.disabled);
  }

  trigger.addEventListener("click", event => {
    event.stopPropagation();
    if (menu.classList.contains("hidden")) open();
    else close();
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
  CM.on("composerPopoverOpened", name => { if (name !== "composerTools") close(); });
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
