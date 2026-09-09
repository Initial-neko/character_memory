const unreadBootstrapKey = "character-memory:unread-v1-initialized";
const summaryPollMs = 5000;
const characterSummaries = new Map();
let summaryPollBusy = false;

function lastReadKey(id) { return `character-memory:last-read:${id}`; }
function lastReadId(id) { return Number(localStorage.getItem(lastReadKey(id)) || 0); }
function latestAssistantId(id) { return Number(characterSummaries.get(id)?.latest_assistant_message_id || 0); }
function markRead(id) {
  const latest = latestAssistantId(id);
  if (latest > 0) localStorage.setItem(lastReadKey(id), String(latest));
}
function isUnread(id) {
  if (id === characterId) return false;
  const latest = latestAssistantId(id);
  return latest > 0 && latest > lastReadId(id);
}
function previewFor(profile) {
  const summary = characterSummaries.get(profile.id);
  const latest = summary?.latest_message;
  if (!latest?.content) return profile.tagline || profile.identity || "Persistent AI Person";
  const prefix = latest.role === "user" ? "你：" : "";
  return `${prefix}${latest.content}`;
}
function bootstrapUnreadBaseline() {
  if (localStorage.getItem(unreadBootstrapKey)) return;
  if (!characters.length || !characters.every(profile => characterSummaries.has(profile.id))) return;
  for (const profile of characters) {
    const latest = latestAssistantId(profile.id);
    if (latest > 0) localStorage.setItem(lastReadKey(profile.id), String(latest));
  }
  localStorage.setItem(unreadBootstrapKey, "1");
}

renderCharacterList = function renderCharacterListWithUnread() {
  characterList.innerHTML = characters.map(profile => {
    const pending = pendingCharacters.has(profile.id);
    const unread = isUnread(profile.id);
    const preview = previewFor(profile);
    return `
      <button class="character-item ${profile.id === characterId ? "active" : ""}" type="button" data-character="${escapeHtml(profile.id)}">
        <span class="character-avatar">${escapeHtml(initialFor(profile))}</span>
        <span class="character-copy">
          <span class="character-name character-name-line">
            <span>${escapeHtml(profile.name)}${pending ? '<span class="character-pending"> · 回复中</span>' : ""}</span>
            ${unread ? '<span class="unread-dot" title="有新消息" aria-label="有新消息"></span>' : ""}
          </span>
          <span class="character-tagline character-preview">${escapeHtml(preview)}</span>
        </span>
      </button>`;
  }).join("");
};

const p06AddMessage = addMessage;
addMessage = function addMessageWithProactiveBadge(message) {
  const row = p06AddMessage(message);
  if (message?.proactive) {
    const meta = row.querySelector(".message-meta");
    if (meta && !meta.querySelector(".proactive-badge")) {
      const badge = document.createElement("span");
      badge.className = "proactive-badge";
      badge.textContent = "主动消息";
      meta.appendChild(badge);
    }
  }
  return row;
};

const p06LoadHistory = loadHistory;
loadHistory = async function loadHistoryAndRead() {
  const requestedCharacter = characterId;
  const result = await p06LoadHistory();
  if (requestedCharacter === characterId) {
    markRead(requestedCharacter);
    renderCharacterList();
  }
  return result;
};

const p06SwitchCharacter = switchCharacter;
switchCharacter = async function switchCharacterAndRead(nextId) {
  markRead(nextId);
  renderCharacterList();
  const result = await p06SwitchCharacter(nextId);
  markRead(nextId);
  renderCharacterList();
  return result;
};

async function loadCharacterSummaries() {
  if (summaryPollBusy) return;
  summaryPollBusy = true;
  try {
    const before = latestAssistantId(characterId);
    const data = await api("/v1/characters/summaries");
    for (const item of data.characters || []) characterSummaries.set(item.id, item);
    bootstrapUnreadBaseline();

    const currentLatest = latestAssistantId(characterId);
    const currentUnread = currentLatest > 0 && currentLatest > lastReadId(characterId);
    if (currentUnread && !drawer.classList.contains("open")) {
      await loadHistory();
    } else {
      renderCharacterList();
    }

    if (currentLatest > before) {
      console.info("[unread] current character received new message", characterId, currentLatest);
    }
  } catch (error) {
    console.warn("character summary refresh failed", error);
  } finally {
    summaryPollBusy = false;
  }
}

setTimeout(() => loadCharacterSummaries(), 0);
setInterval(loadCharacterSummaries, summaryPollMs);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadCharacterSummaries();
});
