(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before persona.js");

  let personaDraft = null;
  const examples = [
    "反差型冷面吐槽役，平时话少，遇到真正感兴趣的事突然特别投入",
    "脑洞很多的快乐行动派，会记住一起做过的奇怪小事，但也有自己的脾气",
    "温柔可靠，但不会一味安慰；不同意的时候会认真讲自己的判断",
    "神秘观察者型，喜欢从小细节猜事情，熟悉以后偶尔冒出很怪但很好笑的比喻",
  ];

  const formHtml = () => `<div class="persona-builder">
    <div class="builder-intro">先描述你想认识的那个人。AI 会生成一份可编辑的人物草稿，确认后才会真正创建。</div>
    <label class="builder-field"><span>你想认识一个什么样的人？</span><textarea id="personaDescription" rows="7" placeholder="比如：有点冷，但熟悉以后会明显变得话多；很独立，不会什么都顺着我……"></textarea></label>
    <div class="builder-grid"><label class="builder-field"><span>名字（可选）</span><input id="personaName" placeholder="Miko"></label><label class="builder-field"><span>年龄（可选）</span><input id="personaAge" type="number" min="1" max="120" placeholder="24"></label></div>
    <div class="builder-examples"><span>试试这些方向</span>${examples.map((text, index) => `<button type="button" data-persona-example="${index}">${CM.escapeHtml(text)}</button>`).join("")}</div>
    <div class="builder-actions"><button class="primary" id="generatePersona" type="button">AI 帮我生成</button></div><div id="personaDraftArea"></div>
  </div>`;

  const behaviorEditor = (label, key, value) => `<label class="builder-field"><span>${label}</span><textarea rows="3" data-draft-field="${key}">${CM.escapeHtml(value || "")}</textarea></label>`;

  function renderDraft(draft) {
    personaDraft = draft;
    const area = document.getElementById("personaDraftArea");
    if (!area) return;
    area.innerHTML = `<div class="persona-card">
      <div class="persona-card-head"><div class="persona-avatar-large">${CM.escapeHtml((draft.name || "AI").slice(0,1))}</div><div><h3>${CM.escapeHtml(draft.name)}</h3><p>${CM.escapeHtml([draft.age ? `${draft.age} 岁` : "", draft.tagline].filter(Boolean).join(" · "))}</p></div></div>
      <p class="persona-description">${CM.escapeHtml(draft.description)}</p>
      <div class="persona-section"><h4>这个人是什么样</h4><ul>${(draft.personality || []).map(item => `<li>${CM.escapeHtml(item)}</li>`).join("")}</ul></div>
      <div class="persona-section"><h4>聊天与关系</h4><ul><li>${CM.escapeHtml(draft.conversation)}</li><li>${CM.escapeHtml(draft.questions)}</li><li>${CM.escapeHtml(draft.disagreement)}</li><li>${CM.escapeHtml(draft.care)}</li></ul></div>
      <details class="advanced-persona"><summary>高级修改</summary>
        <label class="builder-field"><span>名字</span><input data-draft-field="name" value="${CM.escapeHtml(draft.name)}"></label>
        <label class="builder-field"><span>一句话身份</span><input data-draft-field="identity" value="${CM.escapeHtml(draft.identity)}"></label>
        <label class="builder-field"><span>Tagline</span><input data-draft-field="tagline" value="${CM.escapeHtml(draft.tagline)}"></label>
        <label class="builder-field"><span>人物说明</span><textarea rows="6" data-draft-field="description">${CM.escapeHtml(draft.description)}</textarea></label>
        <label class="builder-field"><span>性格（每行一条）</span><textarea rows="6" data-draft-list="personality">${CM.escapeHtml((draft.personality || []).join("\n"))}</textarea></label>
        ${behaviorEditor("聊天方式", "conversation", draft.conversation)}${behaviorEditor("表达方式", "expression", draft.expression)}${behaviorEditor("追问方式", "questions", draft.questions)}${behaviorEditor("沉默方式", "silence", draft.silence)}${behaviorEditor("主动方式", "initiative", draft.initiative)}${behaviorEditor("分歧处理", "disagreement", draft.disagreement)}${behaviorEditor("关心方式", "care", draft.care)}
        <label class="builder-field"><span>边界（每行一条）</span><textarea rows="5" data-draft-list="boundaries">${CM.escapeHtml((draft.boundaries || []).join("\n"))}</textarea></label>
      </details>
      <div class="builder-actions split"><button id="regeneratePersona" type="button">重新生成</button><button class="primary" id="createPersona" type="button">创建人物</button></div>
    </div>`;
  }

  function collectDraftEdits() {
    if (!personaDraft) return null;
    const next = {...personaDraft};
    document.querySelectorAll("[data-draft-field]").forEach(el => { next[el.dataset.draftField] = el.value.trim(); });
    document.querySelectorAll("[data-draft-list]").forEach(el => { next[el.dataset.draftList] = el.value.split("\n").map(v => v.trim()).filter(Boolean); });
    return next;
  }

  async function generatePersona() {
    const description = document.getElementById("personaDescription")?.value.trim() || "";
    const name = document.getElementById("personaName")?.value.trim() || "";
    const rawAge = document.getElementById("personaAge")?.value.trim() || "";
    if (!description) throw new Error("先描述一下你想认识的人");
    const button = document.getElementById("generatePersona");
    if (button) { button.disabled = true; button.textContent = "正在生成…"; }
    try {
      const result = await CM.api("/v1/characters/draft", {method:"POST", body:JSON.stringify({description, name, age:rawAge ? Number(rawAge) : null, tags:[]})});
      renderDraft(result.draft);
    } finally {
      if (button) { button.disabled = false; button.textContent = "AI 帮我生成"; }
    }
  }

  async function createPersona() {
    const draft = collectDraftEdits();
    if (!draft) return;
    const button = document.getElementById("createPersona");
    if (button) { button.disabled = true; button.textContent = "正在创建…"; }
    try {
      const capacity = CM.state.characterCapacity || {activeTotal:CM.state.characters.length,softLimit:10,hardLimit:20};
      const needsConfirm = Number(capacity.activeTotal || 0) >= Number(capacity.softLimit || 10);
      if (Number(capacity.activeTotal || 0) >= Number(capacity.hardLimit || 20)) {
        throw new Error(`角色已达到 ${capacity.hardLimit || 20} 位上限，请先归档一位人物。`);
      }
      if (needsConfirm && !window.confirm(`当前已有 ${capacity.activeTotal} 位角色。继续创建会超过 10 位提醒阈值，是否仍然新增？`)) return;
      const result = await CM.api("/v1/characters", {method:"POST", body:JSON.stringify({draft,confirm_over_soft_limit:needsConfirm})});
      await CM.loadCharacters();
      CM.closeDrawer();
      await CM.switchCharacter(result.character.id);
    } finally {
      if (button) { button.disabled = false; button.textContent = "创建人物"; }
    }
  }

  function openBuilder() {
    personaDraft = null;
    CM.openDrawer("创建新人物", "AI 先生成草稿，你确认以后才保存");
    CM.dom.drawerBody.innerHTML = formHtml();
  }

  const createButton = document.createElement("button");
  createButton.type = "button";
  createButton.className = "create-character-button";
  createButton.textContent = "＋ 新建人物";
  const characterActions = document.getElementById("characterActions");
  (characterActions || CM.dom.characterList).appendChild(createButton);
  createButton.addEventListener("click", openBuilder);

  CM.dom.drawerBody.addEventListener("click", event => {
    const example = event.target.closest("[data-persona-example]");
    if (example) {
      const textarea = document.getElementById("personaDescription");
      if (textarea) textarea.value = examples[Number(example.dataset.personaExample)] || "";
      return;
    }
    const area = () => document.getElementById("personaDraftArea");
    if (event.target.closest("#generatePersona") || event.target.closest("#regeneratePersona")) {
      generatePersona().catch(error => { if (area()) area().innerHTML = `<div class="error">${CM.escapeHtml(error.message)}</div>`; });
    }
    if (event.target.closest("#createPersona")) {
      createPersona().catch(error => { if (area()) area().insertAdjacentHTML("beforeend", `<div class="error">${CM.escapeHtml(error.message)}</div>`); });
    }
  });

  CM.registerFeature("persona", {open:openBuilder});
})();
