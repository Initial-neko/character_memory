(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before group_settings.js");

  function syncEditableState() {
    const editable = CM.isGroupConversation() && !CM.dom.input.disabled;
    CM.dom.characterName.classList.toggle("group-name-editable", editable);
    if (CM.isGroupConversation()) {
      CM.dom.characterName.title = editable ? "点击修改群名称" : "群成员回复结束后可修改群名称";
    } else {
      CM.dom.characterName.removeAttribute("title");
    }
  }

  function showRenameGroup() {
    if (!CM.isGroupConversation() || CM.dom.input.disabled) return;
    const groupId = CM.state.conversation.groupId;
    const currentName = CM.dom.characterName.textContent.trim();
    CM.openDrawer("修改群名称", "只修改群聊显示名称，不影响成员和历史消息");
    CM.dom.drawerBody.innerHTML = `<div class="group-create-form"><label>群名称<input type="text" data-group-rename-name maxlength="80" value="${CM.escapeHtml(currentName)}"></label><div class="group-create-error error hidden" data-group-rename-error></div><div class="group-create-actions"><button type="button" data-group-rename-cancel>取消</button><button type="button" class="primary" data-group-rename-confirm>保存</button></div></div>`;
    const input = CM.dom.drawerBody.querySelector("[data-group-rename-name]");
    input?.focus();
    input?.select();
    CM.dom.drawerBody.dataset.groupRenameId = groupId;
  }

  async function saveRename() {
    const groupId = CM.dom.drawerBody.dataset.groupRenameId;
    if (!groupId || !CM.isGroupConversation() || groupId !== CM.state.conversation.groupId) return;
    const input = CM.dom.drawerBody.querySelector("[data-group-rename-name]");
    const name = input?.value.trim() || "";
    const errorBox = CM.dom.drawerBody.querySelector("[data-group-rename-error]");
    if (!name) {
      if (errorBox) {
        errorBox.textContent = "群名称不能为空。";
        errorBox.classList.remove("hidden");
      }
      return;
    }
    try {
      await CM.api(`/v1/groups/${encodeURIComponent(groupId)}`, {method:"PATCH", body:JSON.stringify({name})});
      await CM.features.groups?.loadGroups?.();
      CM.closeDrawer();
      CM.updateHeader();
      syncEditableState();
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      }
    }
  }

  CM.dom.characterName.addEventListener("click", () => showRenameGroup());
  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-group-rename-cancel]")) CM.closeDrawer();
    if (event.target.closest("[data-group-rename-confirm]")) saveRename().catch(console.error);
  });
  CM.dom.drawerBody.addEventListener("keydown", event => {
    if (!event.target.closest("[data-group-rename-name]")) return;
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    saveRename().catch(console.error);
  });

  CM.on("conversationChanged", syncEditableState);
  CM.on("ready", syncEditableState);
  CM.registerFeature("groupSettings", {showRenameGroup, syncEditableState});
})();
