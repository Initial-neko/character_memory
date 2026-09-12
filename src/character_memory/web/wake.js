(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before wake.js");

  const topbar = document.querySelector(".topbar-actions");
  const button = document.createElement("button");
  button.id = "wakeButton";
  button.className = "ghost-button wake-button";
  button.type = "button";
  button.title = "给当前人物一次主动思考机会，不保证一定回复";
  button.textContent = "唤醒";
  topbar?.insertBefore(button, CM.dom.runtimeButton || null);

  function updateButton() {
    const group = CM.isGroupConversation();
    button.hidden = group;
    if (!button.dataset.busy) button.disabled = group;
  }

  async function manualWake() {
    if (CM.isGroupConversation() || button.dataset.busy) return;
    const characterId = CM.state.characterId;
    const original = button.textContent;
    button.dataset.busy = "1";
    button.disabled = true;
    button.textContent = "唤醒中…";
    CM.connectDirectStream();
    try {
      const result = await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/wake`, {
        method:"POST",
        body:JSON.stringify({conversation_id:CM.conversationIdFor(characterId)}),
      });
      if (!CM.isGroupConversation() && characterId === CM.state.characterId) {
        button.textContent = result.silent ? "没有想说什么" : "已唤醒";
        setTimeout(() => {
          if (!button.dataset.busy) return;
          button.textContent = original;
        }, 1200);
      }
    } catch (error) {
      button.textContent = "唤醒失败";
      console.error("[manual wake]", error);
    } finally {
      setTimeout(() => {
        delete button.dataset.busy;
        button.textContent = original;
        updateButton();
      }, 1300);
    }
  }

  button.addEventListener("click", () => manualWake().catch(console.error));
  CM.on("conversationChanged", updateButton);
  CM.on("charactersLoaded", updateButton);
  updateButton();

  CM.registerFeature("wake", {manualWake, updateButton});
})();
