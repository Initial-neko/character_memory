(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before group_settings.js");

  function syncEditableState() {
    const editable = CM.isGroupConversation() && !CM.dom.input.disabled;
    CM.dom.characterName.classList.toggle("group-name-editable", editable);
    if (CM.isGroupConversation()) {
      CM.dom.characterName.title = editable ? "点击打开群聊设置" : "群成员回复结束后可修改群聊设置";
    } else {
      CM.dom.characterName.removeAttribute("title");
    }
  }

  // The candidate list is fetched separately from the sidebar list, and has to
  // be. A character a batch build created is deferred: absent from the sidebar
  // until the user says otherwise, but a perfectly good group member from the
  // moment it exists. Asking for it explicitly is what keeps "add members"
  // offering every character, not just the ones already in the sidebar.
  let pickerProfiles = [];

  async function loadPickerProfiles() {
    try {
      const data = await CM.api("/v1/characters?include_deferred=true");
      pickerProfiles = data.characters || [];
    } catch (error) {
      // Degrade to the sidebar list rather than an empty picker: the drawer
      // still works, only deferred characters temporarily cannot be added.
      console.error("[group_settings] picker profiles", error);
      pickerProfiles = CM.state.characters;
    }
    return pickerProfiles;
  }

  function memberRows(group) {
    // Joined rows are driven by the group's own member list, not by
    // CM.state.characters: that list holds active characters only, so reading it
    // here used to hide an archived member's row completely -- and with it the
    // one "移出群聊" button that could remove them. The name still comes from the
    // server payload, which includes archived members.
    //
    // The notes read the payload's own `archived` / `direct_pending` flags for
    // the same reason they cannot read state.characters: the sidebar list now
    // hides deferred characters as well as archived ones, so "absent from it"
    // would label a brand-new group member "已归档".
    const memberById = new Map(
      (group?.members || []).map(member => [String(member?.id ?? ""), member])
    );
    const currentIds = (group?.member_ids || []).map(String);
    const joined = currentIds.map(id => {
      const member = memberById.get(id) || null;
      const name = member?.name || id;
      const notes = [];
      if (member?.archived) notes.push('<small class="group-member-note-archived">已归档，仍可移出</small>');
      if (member?.direct_pending) notes.push('<small class="group-member-note-pending">还没进私聊栏</small>');
      if (!notes.length) notes.push("<small>已在群聊中</small>");
      const actions = [];
      if (member?.direct_pending) {
        actions.push(`<button type="button" class="compact" data-group-member-open-direct="${CM.escapeHtml(id)}" title="把 TA 加入左侧的对话列表">加入我的对话</button>`);
      }
      actions.push(`<button type="button" class="danger compact" data-group-member-remove="${CM.escapeHtml(id)}">移出群聊</button>`);
      return `<div class="group-member-option group-member-option-joined"><span><strong>${CM.escapeHtml(name)}</strong>${notes.join("")}</span><span class="group-member-joined-actions">${actions.join("")}</span></div>`;
    }).join("");
    const candidates = pickerProfiles
      .filter(profile => !currentIds.includes(String(profile.id)))
      .map(profile => `<label class="group-member-option"><input type="checkbox" data-group-member-id="${CM.escapeHtml(profile.id)}"><span><strong>${CM.escapeHtml(profile.name || profile.id)}</strong><small>${CM.escapeHtml(profile.identity || "可加入群聊")}</small></span></label>`)
      .join("");
    return joined + candidates;
  }

  async function showGroupSettings() {
    if (!CM.isGroupConversation() || CM.dom.input.disabled) return;
    const group = CM.features.groups?.current?.();
    if (!group) return;
    CM.openDrawer("群聊设置", "修改群名称，添加或移出 Character；历史消息不会重写");
    CM.dom.drawerBody.innerHTML = '<p class="muted">正在读取群成员…</p>';
    await loadPickerProfiles();
    const currentName = group.name || CM.dom.characterName.textContent.trim();
    const memberCount = (group.member_ids || []).length;
    CM.dom.drawerBody.innerHTML = `<div class="group-create-form" data-group-settings-id="${CM.escapeHtml(group.id)}"><label>群名称<input type="text" data-group-rename-name maxlength="80" value="${CM.escapeHtml(currentName)}"></label><div class="group-create-actions"><button type="button" class="primary" data-group-rename-confirm>保存名称</button></div><hr><div><strong>群成员</strong><p class="muted">当前 ${memberCount}/12 人。新成员从下一轮消息开始参与；移出成员后也从下一轮起生效。</p><div class="group-member-options">${memberRows(group)}</div></div><div class="error hidden" data-group-settings-error></div><div class="group-create-actions"><button type="button" data-group-settings-cancel>关闭</button><button type="button" class="primary" data-group-members-confirm ${memberCount >= 12 ? "disabled" : ""}>添加选中成员</button></div></div>`;
  }

  // Deferred characters are not disabled, only tucked away, so opening one is a
  // one-way, idempotent action -- and it must not navigate away from the group
  // the button was pressed in. The character simply starts appearing on the
  // left from now on.
  async function openDirectChat(characterId) {
    const root = CM.dom.drawerBody.querySelector("[data-group-settings-id]");
    const groupId = root?.dataset.groupSettingsId;
    if (!groupId || !characterId) return;
    const errorBox = CM.dom.drawerBody.querySelector("[data-group-settings-error]");
    try {
      await CM.api(`/v1/characters/${encodeURIComponent(characterId)}/open-direct`, {method:"POST"});
      await CM.loadCharacters();
      await CM.features.groups?.loadGroups?.();
      await showGroupSettings();
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = `加入我的对话失败：${error.message}`;
        errorBox.classList.remove("hidden");
      }
    }
  }

  async function saveRename() {
    const root = CM.dom.drawerBody.querySelector("[data-group-settings-id]");
    const groupId = root?.dataset.groupSettingsId;
    if (!groupId || !CM.isGroupConversation() || groupId !== CM.state.conversation.groupId) return;
    const input = CM.dom.drawerBody.querySelector("[data-group-rename-name]");
    const name = input?.value.trim() || "";
    const errorBox = CM.dom.drawerBody.querySelector("[data-group-settings-error]");
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
      CM.updateHeader();
      await showGroupSettings();
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      }
    }
  }

  async function addMembers() {
    const root = CM.dom.drawerBody.querySelector("[data-group-settings-id]");
    const groupId = root?.dataset.groupSettingsId;
    if (!groupId || !CM.isGroupConversation() || groupId !== CM.state.conversation.groupId) return;
    const memberIds = [...CM.dom.drawerBody.querySelectorAll("[data-group-member-id]:checked:not(:disabled)")].map(input => input.dataset.groupMemberId).filter(Boolean);
    const errorBox = CM.dom.drawerBody.querySelector("[data-group-settings-error]");
    if (!memberIds.length) {
      if (errorBox) {
        errorBox.textContent = "请至少选择一个尚未加入的 Character。";
        errorBox.classList.remove("hidden");
      }
      return;
    }
    try {
      await CM.api(`/v1/groups/${encodeURIComponent(groupId)}/members`, {method:"POST", body:JSON.stringify({member_ids:memberIds})});
      await CM.features.groups?.loadGroups?.();
      CM.updateHeader();
      await CM.emit("groupMembersChanged", {groupId, memberIds});
      await showGroupSettings();
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      }
    }
  }

  async function removeMember(characterId) {
    const root = CM.dom.drawerBody.querySelector("[data-group-settings-id]");
    const groupId = root?.dataset.groupSettingsId;
    if (!groupId || !characterId || !CM.isGroupConversation() || groupId !== CM.state.conversation.groupId) return;
    const group = CM.features.groups?.current?.();
    if (!group || (group.member_ids || []).length <= 2) {
      const errorBox = CM.dom.drawerBody.querySelector("[data-group-settings-error]");
      if (errorBox) {
        errorBox.textContent = "群聊至少需要保留 2 个 Character。";
        errorBox.classList.remove("hidden");
      }
      return;
    }
    // The group payload is the only place a real name can be found: an archived
    // member has no entry in CM.state.characters, and a deferred one has no
    // entry there either.
    const member = (group.members || []).find(item => String(item?.id ?? "") === String(characterId));
    const label = member?.name || characterId;
    try {
      await CM.api(`/v1/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(characterId)}`, {method:"DELETE"});
      await CM.features.groups?.loadGroups?.();
      CM.updateHeader();
      await CM.emit("groupMembersChanged", {groupId, removedMemberId:characterId});
      await showGroupSettings();
    } catch (error) {
      const errorBox = CM.dom.drawerBody.querySelector("[data-group-settings-error]");
      if (errorBox) {
        errorBox.textContent = `移出「${label}」失败：${error.message}`;
        errorBox.classList.remove("hidden");
      }
    }
  }

  function showRenameGroup() { showGroupSettings().catch(console.error); }

  CM.dom.characterName.addEventListener("click", () => showGroupSettings().catch(console.error));
  CM.dom.drawerBody.addEventListener("click", event => {
    if (event.target.closest("[data-group-settings-cancel]")) CM.closeDrawer();
    if (event.target.closest("[data-group-rename-confirm]")) saveRename().catch(console.error);
    if (event.target.closest("[data-group-members-confirm]")) addMembers().catch(console.error);
    const openDirect = event.target.closest("[data-group-member-open-direct]");
    if (openDirect) openDirectChat(openDirect.dataset.groupMemberOpenDirect).catch(console.error);
    const remove = event.target.closest("[data-group-member-remove]");
    if (remove) removeMember(remove.dataset.groupMemberRemove).catch(console.error);
  });
  CM.dom.drawerBody.addEventListener("keydown", event => {
    if (!event.target.closest("[data-group-rename-name]")) return;
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    saveRename().catch(console.error);
  });

  CM.on("conversationChanged", syncEditableState);
  CM.on("ready", syncEditableState);
  CM.registerFeature("groupSettings", {showRenameGroup, showGroupSettings, addMembers, removeMember, openDirectChat, syncEditableState});
})();
