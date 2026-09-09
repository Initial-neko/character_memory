function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function deliveryDelay(action) {
  const type = String(action?.type || "MESSAGE");
  const text = String(action?.message || "");
  if (type === "EMOJI") return 180 + Math.floor(Math.random() * 220);
  const base = 300 + Math.min(text.length * 14, 700);
  const jitter = Math.floor(Math.random() * 260) - 80;
  return Math.max(260, Math.min(base + jitter, 1200));
}

async function revealActionsWithRhythm(result, sentCharacter, browserTotal) {
  const actions = visibleActions(result);
  if (!actions.length) {
    const note = document.createElement("div");
    note.className = "date-separator quiet-read";
    note.textContent = `已读 · 没有回复 · ${fmtMs(browserTotal)}`;
    chat.appendChild(note);
    return;
  }

  for (let index = 0; index < actions.length; index += 1) {
    if (sentCharacter !== characterId) return;
    if (index > 0) {
      appendTypingForCurrent();
      scrollToBottom();
      await sleep(deliveryDelay(actions[index]));
      if (sentCharacter !== characterId) return;
      chat.querySelector(".typing-row")?.remove();
    }
    const action = actions[index];
    addMessage({
      role: "assistant",
      content: action.message,
      event_time: result.event_time,
      action: action.type,
      source_event_id: result.event_id,
      has_trace: true,
      latency_ms: index === 0 ? browserTotal : 0,
    });
    scrollToBottom();
  }
}

composer.addEventListener("submit", async event => {
  event.preventDefault();
  event.stopImmediatePropagation();

  const message = input.value.trim();
  const sentCharacter = characterId;
  if (!message || pendingCharacters.has(sentCharacter)) return;

  const conversationId = conversationIdFor(sentCharacter);
  const browserStarted = performance.now();
  pendingCharacters.add(sentCharacter);
  renderCharacterList();
  updateHeader();

  if (chat.querySelector(".empty")) chat.innerHTML = "";
  addMessage({role:"user", content:message, event_time:new Date().toISOString(), has_trace:false});
  appendTypingForCurrent();
  scrollToBottom();
  input.value = "";
  input.style.height = "auto";

  try {
    const result = await api("/v1/chat", {method:"POST", body:JSON.stringify({character_id:sentCharacter, conversation_id:conversationId, message})});
    const browserTotal = performance.now() - browserStarted;
    console.info("[chat timings]", sentCharacter, {...(result.timings || {}), browser_total_ms:Number(browserTotal.toFixed(1))});
    if (sentCharacter === characterId) {
      chat.querySelector(".typing-row")?.remove();
      await revealActionsWithRhythm(result, sentCharacter, browserTotal);
    }
  } catch (error) {
    console.error("[chat failed]", sentCharacter, error);
    if (sentCharacter === characterId) {
      chat.querySelector(".typing-row")?.remove();
      const box = document.createElement("div");
      box.className = "error";
      box.textContent = `生成失败：${error.message}`;
      chat.appendChild(box);
    }
  } finally {
    pendingCharacters.delete(sentCharacter);
    renderCharacterList();
    updateHeader();
    if (sentCharacter === characterId) {
      await loadHistory().catch(error => console.warn("history refresh failed", error));
      updateComposerState();
      scrollToBottom();
    }
  }
}, true);

let personaDraft = null;
const personaExamples = [
  "反差型冷面吐槽役，平时话少，遇到真正感兴趣的事突然特别投入",
  "脑洞很多的快乐行动派，会记住一起做过的奇怪小事，但也有自己的脾气",
  "温柔可靠，但不会一味安慰；不同意的时候会认真讲自己的判断",
  "神秘观察者型，喜欢从小细节猜事情，熟悉以后偶尔冒出很怪但很好笑的比喻",
];

function builderFormHtml() {
  return `
    <div class="persona-builder">
      <div class="builder-intro">先描述你想认识的那个人。AI 会生成一份可编辑的人物草稿，确认后才会真正创建。</div>
      <label class="builder-field"><span>你想认识一个什么样的人？</span><textarea id="personaDescription" rows="7" placeholder="比如：有点冷，但熟悉以后会明显变得话多；很独立，不会什么都顺着我……"></textarea></label>
      <div class="builder-grid">
        <label class="builder-field"><span>名字（可选）</span><input id="personaName" placeholder="Miko"></label>
        <label class="builder-field"><span>年龄（可选）</span><input id="personaAge" type="number" min="1" max="120" placeholder="24"></label>
      </div>
      <div class="builder-examples"><span>试试这些方向</span>${personaExamples.map((text, index) => `<button type="button" data-persona-example="${index}">${escapeHtml(text)}</button>`).join("")}</div>
      <div class="builder-actions"><button class="primary-builder-button" id="generatePersona" type="button">AI 帮我生成</button></div>
      <div id="personaDraftArea"></div>
    </div>`;
}

function behaviorEditor(label, key, value) {
  return `<label class="builder-field"><span>${label}</span><textarea rows="3" data-draft-field="${key}">${escapeHtml(value || "")}</textarea></label>`;
}

function renderPersonaDraft(draft) {
  personaDraft = draft;
  const area = document.getElementById("personaDraftArea");
  if (!area) return;
  area.innerHTML = `
    <div class="persona-card">
      <div class="persona-card-head"><div class="persona-avatar-large">${escapeHtml((draft.name || "AI").slice(0,1))}</div><div><h3>${escapeHtml(draft.name)}</h3><p>${escapeHtml([draft.age ? `${draft.age} 岁` : "", draft.tagline].filter(Boolean).join(" · "))}</p></div></div>
      <p class="persona-description">${escapeHtml(draft.description)}</p>
      <div class="persona-section"><h4>这个人是什么样</h4><ul>${(draft.personality || []).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
      <div class="persona-section"><h4>聊天与关系</h4><ul><li>${escapeHtml(draft.conversation)}</li><li>${escapeHtml(draft.questions)}</li><li>${escapeHtml(draft.disagreement)}</li><li>${escapeHtml(draft.care)}</li></ul></div>
      <details class="advanced-persona"><summary>高级修改</summary>
        <label class="builder-field"><span>名字</span><input data-draft-field="name" value="${escapeHtml(draft.name)}"></label>
        <label class="builder-field"><span>一句话身份</span><input data-draft-field="identity" value="${escapeHtml(draft.identity)}"></label>
        <label class="builder-field"><span>Tagline</span><input data-draft-field="tagline" value="${escapeHtml(draft.tagline)}"></label>
        <label class="builder-field"><span>人物说明</span><textarea rows="6" data-draft-field="description">${escapeHtml(draft.description)}</textarea></label>
        <label class="builder-field"><span>性格（每行一条）</span><textarea rows="6" data-draft-list="personality">${escapeHtml((draft.personality || []).join("\n"))}</textarea></label>
        ${behaviorEditor("聊天方式", "conversation", draft.conversation)}
        ${behaviorEditor("表达方式", "expression", draft.expression)}
        ${behaviorEditor("追问方式", "questions", draft.questions)}
        ${behaviorEditor("沉默方式", "silence", draft.silence)}
        ${behaviorEditor("主动方式", "initiative", draft.initiative)}
        ${behaviorEditor("分歧处理", "disagreement", draft.disagreement)}
        ${behaviorEditor("关心方式", "care", draft.care)}
        <label class="builder-field"><span>边界（每行一条）</span><textarea rows="5" data-draft-list="boundaries">${escapeHtml((draft.boundaries || []).join("\n"))}</textarea></label>
      </details>
      <div class="builder-actions split"><button id="regeneratePersona" type="button">重新生成</button><button class="primary-builder-button" id="createPersona" type="button">创建人物</button></div>
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
    const result = await api("/v1/characters/draft", {method:"POST", body:JSON.stringify({description, name, age:rawAge ? Number(rawAge) : null, tags:[]})});
    renderPersonaDraft(result.draft);
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
    const result = await api("/v1/characters", {method:"POST", body:JSON.stringify({draft})});
    await loadCharacters();
    closeDrawer();
    await switchCharacter(result.character.id);
  } finally {
    if (button) { button.disabled = false; button.textContent = "创建人物"; }
  }
}

function openPersonaBuilder() {
  personaDraft = null;
  openDrawer("创建新人物", "AI 先生成草稿，你确认以后才保存");
  drawerBody.innerHTML = builderFormHtml();
}

const createCharacterButton = document.createElement("button");
createCharacterButton.type = "button";
createCharacterButton.className = "create-character-button";
createCharacterButton.textContent = "＋ 新建人物";
characterList.insertAdjacentElement("afterend", createCharacterButton);
createCharacterButton.addEventListener("click", openPersonaBuilder);

drawerBody.addEventListener("click", event => {
  const example = event.target.closest("[data-persona-example]");
  if (example) {
    const textarea = document.getElementById("personaDescription");
    if (textarea) textarea.value = personaExamples[Number(example.dataset.personaExample)] || "";
    return;
  }
  if (event.target.closest("#generatePersona")) generatePersona().catch(error => {
    const area = document.getElementById("personaDraftArea");
    if (area) area.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
  });
  if (event.target.closest("#regeneratePersona")) generatePersona().catch(error => {
    const area = document.getElementById("personaDraftArea");
    if (area) area.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
  });
  if (event.target.closest("#createPersona")) createPersona().catch(error => {
    const area = document.getElementById("personaDraftArea");
    if (area) area.insertAdjacentHTML("beforeend", `<div class="error">${escapeHtml(error.message)}</div>`);
  });
});
