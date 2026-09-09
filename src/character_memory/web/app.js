const activeCharacterKey = "character-memory:active-character";
const hiddenText = "╭(╯^╰)╮不给看";
let characterId = localStorage.getItem(activeCharacterKey) || "rin";
let characters = [];
const pendingCharacters = new Set();
let lastRenderedSignature = "";

const chat = document.getElementById("chat");
const composer = document.getElementById("composer");
const input = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const runtimeButton = document.getElementById("runtimeButton");
const nowText = document.getElementById("nowText");
const characterList = document.getElementById("characterList");
const characterName = document.getElementById("characterName");
const characterIdentity = document.getElementById("characterIdentity");
const headerAvatar = document.getElementById("headerAvatar");
const drawer = document.getElementById("drawer");
const drawerBackdrop = document.getElementById("drawerBackdrop");
const drawerClose = document.getElementById("drawerClose");
const drawerTitle = document.getElementById("drawerTitle");
const drawerSubtitle = document.getElementById("drawerSubtitle");
const drawerBody = document.getElementById("drawerBody");
const typingTemplate = document.getElementById("typingTemplate");

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
function fmtTime(iso) { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}); }
function fmtDate(iso) { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(); }
function fmtMs(value) { const n = Number(value || 0); return n >= 1000 ? `${(n / 1000).toFixed(2)}s` : `${n.toFixed(0)}ms`; }
function hiddenIfEmpty(value) { return String(value ?? "").trim() || hiddenText; }
function initialFor(profile) { return (profile?.name || profile?.id || "AI").trim().slice(0, 1).toUpperCase(); }
function currentProfile() { return characters.find(item => item.id === characterId) || {id: characterId, name: characterId, identity:"", tagline:""}; }
function conversationIdFor(id) {
  const key = `character-memory:conversation:${id}`;
  let value = localStorage.getItem(key);
  if (!value) { value = crypto.randomUUID(); localStorage.setItem(key, value); }
  return value;
}
function isCurrentPending() { return pendingCharacters.has(characterId); }

function updateNow() { nowText.textContent = new Date().toLocaleString([], {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"}); }
updateNow(); setInterval(updateNow, 30000);

function updateComposerState() {
  const pending = isCurrentPending();
  sendButton.disabled = pending;
  input.disabled = pending;
  if (!pending) input.focus();
}

function updateHeader() {
  const profile = currentProfile();
  characterName.textContent = profile.name || profile.id;
  characterIdentity.textContent = pendingCharacters.has(characterId)
    ? `${profile.identity || profile.tagline || "Persistent AI Person"} · 正在回复`
    : (profile.identity || profile.tagline || "Persistent AI Person");
  headerAvatar.textContent = initialFor(profile);
  input.placeholder = pendingCharacters.has(characterId)
    ? `${profile.name || profile.id} 正在回复…`
    : `给 ${profile.name || profile.id} 发消息`;
  updateComposerState();
}

function renderCharacterList() {
  characterList.innerHTML = characters.map(profile => {
    const pending = pendingCharacters.has(profile.id);
    return `
      <button class="character-item ${profile.id === characterId ? "active" : ""}" type="button" data-character="${escapeHtml(profile.id)}">
        <span class="character-avatar">${escapeHtml(initialFor(profile))}</span>
        <span class="character-copy">
          <span class="character-name">${escapeHtml(profile.name)}${pending ? '<span class="character-pending"> · 回复中</span>' : ""}</span>
          <span class="character-tagline">${escapeHtml(profile.tagline || profile.identity || "Persistent AI Person")}</span>
        </span>
      </button>`;
  }).join("");
}

async function switchCharacter(nextId) {
  if (nextId === characterId) return;
  characterId = nextId;
  localStorage.setItem(activeCharacterKey, characterId);
  lastRenderedSignature = "";
  closeDrawer();
  updateHeader();
  renderCharacterList();
  chat.innerHTML = '<div class="empty">正在加载聊天记录…</div>';
  await loadHistory();
  updateComposerState();
}

function openDrawer(title, subtitle = "") { drawerTitle.textContent = title; drawerSubtitle.textContent = subtitle; drawerBackdrop.classList.remove("hidden"); drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); }
function closeDrawer() { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); setTimeout(() => drawerBackdrop.classList.add("hidden"), 180); }
drawerClose.addEventListener("click", closeDrawer); drawerBackdrop.addEventListener("click", closeDrawer);

function addMessage(message) {
  const row = document.createElement("article");
  row.className = `message-row ${message.role}`;
  row.dataset.messageId = message.id ?? "";
  const latency = message.latency_ms ? `<span>耗时 ${fmtMs(message.latency_ms)}</span>` : "";
  const avatar = message.role === "assistant" ? initialFor(currentProfile()) : "";
  row.innerHTML = `<div class="avatar">${escapeHtml(avatar)}</div><div class="bubble-wrap"><div class="bubble">${escapeHtml(message.content)}</div><div class="message-meta"><span>${fmtTime(message.event_time)}</span>${latency}${message.action === "PROACTIVE_MESSAGE" ? "<span>主动消息</span>" : ""}${message.has_trace && message.source_event_id ? `<button class="detail-button" type="button" data-trace="${message.source_event_id}" title="查看本轮详情">···</button>` : ""}</div></div>`;
  chat.appendChild(row);
  return row;
}

function renderHistory(messages) {
  const signature = `${characterId}|` + messages.map(m => `${m.id}:${m.event_time}:${m.content}`).join("|");
  if (signature === lastRenderedSignature && chat.children.length) return;
  chat.innerHTML = "";
  if (!messages.length) {
    chat.innerHTML = `<div class="empty">还没有和 ${escapeHtml(currentProfile().name || characterId)} 的聊天记录。<br>从第一句话开始认识彼此。</div>`;
    lastRenderedSignature = signature;
    if (isCurrentPending()) appendTypingForCurrent();
    return;
  }
  let lastDate = null;
  for (const message of messages) {
    const date = fmtDate(message.event_time);
    if (date !== lastDate) {
      const sep = document.createElement("div"); sep.className = "date-separator"; sep.textContent = `── ${date} ──`; chat.appendChild(sep); lastDate = date;
    }
    addMessage(message);
  }
  if (isCurrentPending()) appendTypingForCurrent();
  lastRenderedSignature = signature;
  scrollToBottom(false);
}

function appendTypingForCurrent() {
  if (chat.querySelector(".typing-row")) return;
  const typing = typingTemplate.content.cloneNode(true);
  typing.querySelector(".avatar").textContent = initialFor(currentProfile());
  chat.appendChild(typing);
}

function scrollToBottom(smooth = true) { window.scrollTo({top: document.body.scrollHeight, behavior: smooth ? "smooth" : "auto"}); }

async function api(path, options = {}) {
  const started = performance.now();
  const response = await fetch(path, {headers:{"Content-Type":"application/json", ...(options.headers || {})}, ...options});
  const elapsed = performance.now() - started;
  if (!response.ok) {
    const text = await response.text();
    console.error("[api]", path, response.status, `${elapsed.toFixed(1)}ms`, text);
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  const data = await response.json();
  console.info("[api]", path, `${elapsed.toFixed(1)}ms`);
  return data;
}

async function loadCharacters() {
  const data = await api("/v1/characters");
  characters = data.characters || [];
  if (!characters.length) throw new Error("没有发现任何 Persona");
  if (!characters.some(item => item.id === characterId)) characterId = characters[0].id;
  localStorage.setItem(activeCharacterKey, characterId);
  renderCharacterList();
  updateHeader();
}

async function loadHistory() {
  const requestedCharacter = characterId;
  const data = await api(`/v1/chat/history?character_id=${encodeURIComponent(requestedCharacter)}&limit=180`);
  if (requestedCharacter !== characterId) return;
  renderHistory(data.messages);
}

function timingHtml(timings) {
  const entries = Object.entries(timings || {});
  if (!entries.length) return "<p>暂无 timing 数据。</p>";
  return `<div class="kv">${entries.map(([key, value]) => `<div>${escapeHtml(key)}</div><div>${escapeHtml(fmtMs(value))}</div>`).join("")}</div>`;
}

async function showTrace(sourceEventId) {
  openDrawer(`${currentProfile().name} · 本轮详情`, `source_event_id = ${sourceEventId}`);
  drawerBody.innerHTML = "<p>正在加载…</p>";
  try {
    const trace = await api(`/v1/traces/${sourceEventId}`);
    const action = trace.action || {};
    drawerBody.innerHTML = `<section class="section"><h3>耗时</h3>${timingHtml(trace.timings)}</section><section class="section"><h3>决策</h3><div class="kv"><div>Action</div><div>${escapeHtml(action.type || hiddenText)}</div><div>Action Reason</div><div>${escapeHtml(hiddenIfEmpty(action.reason))}</div><div>Perception</div><div>${escapeHtml(hiddenIfEmpty(trace.perception))}</div><div>Reaction</div><div>${escapeHtml(hiddenIfEmpty(trace.reaction))}</div><div>Model Attempt</div><div>${escapeHtml(trace.model_attempt || "—")}</div></div></section><section class="section"><h3>最终对外表达</h3><p>${escapeHtml(action.message || `没有发送消息 · ${action.type || ""}`)}</p></section><section class="section"><h3>Mental State · Before</h3><pre>${escapeHtml(hiddenIfEmpty(trace.mental_state_before))}</pre></section><section class="section"><h3>Mental State · After</h3><pre>${escapeHtml(hiddenIfEmpty(trace.mental_state_after))}</pre></section><section class="section"><h3>Recall</h3><div class="card-list">${(trace.recalled_memories || []).map(m => `<div class="card"><strong>${escapeHtml(m.memory_type)}</strong><span> · importance ${escapeHtml(m.importance)}</span><div>${escapeHtml(m.content)}</div></div>`).join("") || "<p>本轮没有 Recall 到 Memory。</p>"}</div></section><section class="section"><h3>实际发送给模型的 messages</h3>${(trace.model_messages || []).map((m, i) => `<p><strong>${i + 1}. ${escapeHtml(m.role)}</strong></p><pre>${escapeHtml(m.content)}</pre>`).join("") || `<p>${escapeHtml(hiddenText)}</p>`}</section><section class="section"><h3>Compiled Context</h3><pre>${escapeHtml(hiddenIfEmpty(trace.context))}</pre></section><section class="section"><h3>Memory Write</h3><pre>${escapeHtml(JSON.stringify({candidates:trace.memory_candidates || [], created_memory_ids:trace.created_memory_ids || []}, null, 2))}</pre></section><section class="section"><h3>Intent</h3><pre>${escapeHtml(JSON.stringify({candidates:trace.intent_candidates || [], created_intent_ids:trace.created_intent_ids || []}, null, 2))}</pre></section><section class="section"><h3>Raw Model Response</h3><pre>${escapeHtml(hiddenIfEmpty(trace.raw_model_response))}</pre></section>`;
  } catch (error) { drawerBody.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`; }
}

async function showRuntime() {
  const requestedCharacter = characterId;
  openDrawer(`${currentProfile().name} · Runtime`, "按需读取当前人物状态");
  drawerBody.innerHTML = "<p>正在加载…</p>";
  try {
    const data = await api(`/v1/runtime/${encodeURIComponent(requestedCharacter)}`); if (requestedCharacter !== characterId) return;
    const p = data.provider || {};
    drawerBody.innerHTML = `<section class="section"><h3>Runtime 初始化耗时</h3>${timingHtml(data.runtime_init_timings)}</section><section class="section"><h3>Provider</h3><div class="kv"><div>Chat Model</div><div>${escapeHtml(p.chat_model)}</div><div>Embedding</div><div>${escapeHtml(`${p.embedding_provider} / ${p.embedding_model}`)}</div><div>Runtime Loaded</div><div>${escapeHtml(data.runtime_loaded)}</div></div></section><section class="section"><h3>Mental State</h3><pre>${escapeHtml(hiddenIfEmpty(data.mental_state))}</pre></section><section class="section"><h3>Persona</h3><pre>${escapeHtml(hiddenIfEmpty(data.persona))}</pre></section><section class="section"><h3>Memory · 最近 ${data.memories.length} 条</h3><div class="card-list">${data.memories.map(m => `<div class="card"><strong>${escapeHtml(m.memory_type)}</strong><span> · ${escapeHtml(fmtTime(m.event_time))}</span><div>${escapeHtml(m.content)}</div></div>`).join("") || "<p>暂无 Memory。</p>"}</div></section><section class="section"><h3>Intent · 最近 ${data.intents.length} 条</h3><pre>${escapeHtml(JSON.stringify(data.intents, null, 2))}</pre></section>`;
  } catch (error) { drawerBody.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`; }
}

runtimeButton.addEventListener("click", showRuntime);
characterList.addEventListener("click", event => { const button = event.target.closest("[data-character]"); if (button) switchCharacter(button.dataset.character).catch(console.error); });
chat.addEventListener("click", event => { const button = event.target.closest("[data-trace]"); if (button) showTrace(button.dataset.trace); });
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 150)}px`; });
input.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } });

composer.addEventListener("submit", async event => {
  event.preventDefault();
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
      if (result.action?.message) {
        addMessage({role:"assistant", content:result.action.message, event_time:result.event_time, action:result.action.type, source_event_id:result.event_id, has_trace:true, latency_ms:browserTotal});
      } else {
        const note = document.createElement("div");
        note.className = "date-separator";
        note.textContent = `未发送消息 · ${result.action?.type || ""} · ${fmtMs(browserTotal)}`;
        chat.appendChild(note);
      }
      scrollToBottom();
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
});

async function bootstrap() {
  try { await loadCharacters(); await loadHistory(); updateComposerState(); }
  catch (error) { chat.innerHTML = `<div class="error">页面初始化失败：${escapeHtml(error.message)}</div>`; console.error(error); }
}

bootstrap();
setInterval(() => {
  if (!drawer.classList.contains("open")) loadHistory().catch(() => {});
}, 15000);
