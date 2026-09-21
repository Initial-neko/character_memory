(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    providers: [],
    voiceDesignUrl: null,
    // VoiceDesign is not reproducible, so the freeze step needs the opaque
    // artifact token that addresses the exact audio the user auditioned, plus
    // the inputs that produced it so the stored transcript cannot drift.
    voiceDesignArtifact: null,
    voiceDesignSnapshot: null,
    // True while voiceDesignFreezeStatus shows a freeze outcome, so re-arming
    // the gate does not wipe the result the user is reading.
    voiceDesignFreezeResult: false,
    characters: [],
  };

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  function badgeClass(ready) {
    return ready ? "badge ok" : "badge bad";
  }

  function renderProviderStatus() {
    const root = $("providerStatusGrid");
    root.innerHTML = "";
    for (const provider of state.providers) {
      const card = document.createElement("article");
      card.className = "card status-card";
      const header = document.createElement("div");
      header.className = "card-title-row";
      const title = document.createElement("h2");
      title.textContent = provider.label || provider.id;
      const badge = document.createElement("span");
      badge.className = badgeClass(provider.ready);
      badge.textContent = provider.ready ? (provider.loaded ? "loaded" : "ready") : "unavailable";
      header.append(title, badge);
      const body = document.createElement("pre");
      body.className = "status-block";
      body.textContent = pretty({
        provider: provider.id,
        ready: provider.ready,
        loaded: provider.loaded,
        model: provider.model || null,
        device: provider.device || null,
        voices: provider.voices || [],
        reason: provider.reason || null,
        note: provider.note || null,
      });
      card.append(header, body);
      root.appendChild(card);
    }
  }

  function selectedProvider() {
    return state.providers.find((item) => item.id === $("ttsLabProvider").value) || null;
  }

  function renderProviderSelect() {
    const select = $("ttsLabProvider");
    const previous = select.value;
    select.innerHTML = "";
    for (const provider of state.providers) {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = `${provider.label || provider.id}${provider.ready ? "" : " · unavailable"}`;
      select.appendChild(option);
    }
    if (state.providers.some((item) => item.id === previous)) select.value = previous;
    else if (state.providers.some((item) => item.ready)) select.value = state.providers.find((item) => item.ready).id;
    renderVoiceSelect();
  }

  function renderVoiceSelect() {
    const provider = selectedProvider();
    const voice = $("ttsLabVoice");
    voice.innerHTML = "";
    const voices = provider?.voices?.length ? provider.voices : [provider?.default_voice || "default"];
    for (const value of voices) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(value);
      voice.appendChild(option);
    }
    if (provider?.default_voice && voices.includes(provider.default_voice)) voice.value = provider.default_voice;
    $("ttsLabSpeed").disabled = provider ? provider.supports_speed === false : false;
    $("generateTtsLab").disabled = !provider?.ready;
  }

  async function loadProviders() {
    $("refreshProviders").disabled = true;
    try {
      const response = await fetch("/v1/providers");
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      state.providers = data.providers || [];
      renderProviderStatus();
      renderProviderSelect();
    } catch (error) {
      $("providerStatusGrid").innerHTML = `<article class="card"><pre class="result">ERROR: ${error.message}</pre></article>`;
    } finally {
      $("refreshProviders").disabled = false;
    }
  }

  function decodedHeader(headers, name) {
    const value = headers.get(name);
    if (!value) return value;
    try { return decodeURIComponent(value); } catch (_) { return value; }
  }

  function metadataFromHeaders(headers) {
    return {
      provider: headers.get("x-tts-provider"),
      voice: decodedHeader(headers, "x-tts-voice"),
      model: decodedHeader(headers, "x-tts-model"),
      device: headers.get("x-tts-device"),
      inference_ms: Number(headers.get("x-tts-inference-ms") || 0),
      audio_ms: Number(headers.get("x-tts-audio-ms") || 0),
      sample_rate: Number(headers.get("x-tts-sample-rate") || 0),
    };
  }

  async function requestAudio(provider, voice, text, speed) {
    const started = performance.now();
    const response = await fetch("/v1/tts", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({provider, voice, text, speed}),
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(body || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    return {
      blob,
      meta: {...metadataFromHeaders(response.headers), http_ms: Math.round(performance.now() - started)},
    };
  }

  async function generateSingle() {
    const provider = selectedProvider();
    if (!provider?.ready) return;
    const button = $("generateTtsLab");
    button.disabled = true;
    $("ttsLabResult").textContent = "生成中...";
    try {
      const result = await requestAudio(
        provider.id,
        $("ttsLabVoice").value,
        $("ttsLabText").value,
        Number($("ttsLabSpeed").value || 1),
      );
      const old = $("ttsLabAudio").dataset.objectUrl;
      if (old) URL.revokeObjectURL(old);
      const url = URL.createObjectURL(result.blob);
      $("ttsLabAudio").src = url;
      $("ttsLabAudio").dataset.objectUrl = url;
      $("ttsLabResult").textContent = pretty(result.meta);
      await $("ttsLabAudio").play().catch(() => {});
      await loadProviders();
    } catch (error) {
      $("ttsLabResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = !selectedProvider()?.ready;
    }
  }

  function comparisonCard(provider) {
    const card = document.createElement("article");
    card.className = "tts-compare-card";
    const title = document.createElement("h3");
    title.textContent = provider.label || provider.id;
    const voice = document.createElement("div");
    voice.className = "subtle";
    voice.textContent = `voice: ${provider.default_voice || provider.voices?.[0] || "default"}`;
    const audio = document.createElement("audio");
    audio.controls = true;
    const result = document.createElement("pre");
    result.className = "result";
    result.textContent = "waiting...";
    card.append(title, voice, audio, result);
    return {card, audio, result};
  }

  async function compareReady() {
    const button = $("compareReady");
    button.disabled = true;
    const root = $("comparisonGrid");
    root.innerHTML = "";
    const providers = state.providers.filter((item) => item.ready);
    if (!providers.length) {
      root.innerHTML = '<div class="subtle">当前没有可用 Provider。</div>';
      button.disabled = false;
      return;
    }
    const views = new Map();
    for (const provider of providers) {
      const view = comparisonCard(provider);
      views.set(provider.id, view);
      root.appendChild(view.card);
    }
    for (const provider of providers) {
      const view = views.get(provider.id);
      view.result.textContent = "generating...";
      try {
        const result = await requestAudio(
          provider.id,
          provider.default_voice || provider.voices?.[0] || "",
          $("ttsLabText").value,
          Number($("ttsLabSpeed").value || 1),
        );
        const url = URL.createObjectURL(result.blob);
        view.audio.src = url;
        view.audio.dataset.objectUrl = url;
        view.result.textContent = pretty(result.meta);
      } catch (error) {
        view.result.textContent = `ERROR: ${error.message}`;
      }
    }
    button.disabled = false;
    await loadProviders();
  }

  function renderVoiceDesignStatus(status) {
    const badge = $("voiceDesignBadge");
    const generate = $("generateVoiceDesign");
    const payload = status?.voice_design || {};
    const ready = Boolean(payload.ready);
    badge.className = ready ? "badge ok" : "badge bad";
    badge.textContent = ready ? (payload.loaded ? "loaded" : "ready") : "unavailable";
    generate.disabled = !ready;
    $("voiceDesignStatus").textContent = pretty({
      ready,
      loaded: Boolean(payload.loaded),
      model: payload.model || "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
      device: payload.device || null,
      base_url: payload.base_url || "http://127.0.0.1:9015",
      reason: payload.reason || null,
    });
  }

  async function loadVoiceDesignStatus() {
    $("refreshVoiceDesign").disabled = true;
    try {
      const response = await fetch("/v1/voice-design/status");
      if (!response.ok) throw new Error(await response.text());
      renderVoiceDesignStatus(await response.json());
    } catch (error) {
      renderVoiceDesignStatus({voice_design: {ready: false, reason: error.message}});
    } finally {
      $("refreshVoiceDesign").disabled = false;
    }
  }

  async function polishVoiceDesign() {
    const button = $("polishVoiceDesign");
    const description = $("voiceDesignRaw").value.trim();
    if (!description) {
      $("voiceDesignPolishStatus").textContent = "请先填写原始声线描述。";
      return;
    }
    button.disabled = true;
    $("voiceDesignPolishStatus").textContent = "AI 润色中...";
    try {
      const response = await fetch("/v1/voice-design/polish", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          description,
          language: $("voiceDesignLanguage").value,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      $("voiceDesignInstruct").value = data.instruct || "";
      $("voiceDesignPolishStatus").textContent = String(data.total_ms || 0) + " ms · " + (data.model || "standard LLM");
      // Programmatic value changes do not fire "input"; re-check the freeze gate.
      renderFreezeState();
    } catch (error) {
      $("voiceDesignPolishStatus").textContent = "润色失败：" + error.message;
    } finally {
      button.disabled = false;
    }
  }

  async function generateVoiceDesign() {
    const button = $("generateVoiceDesign");
    const instruct = $("voiceDesignInstruct").value.trim();
    const text = $("voiceDesignText").value.trim();
    if (!instruct || !text) {
      $("voiceDesignResult").textContent = "ERROR: Instruct 和测试文本都不能为空。";
      return;
    }
    button.disabled = true;
    $("voiceDesignResult").textContent = "生成中...";
    const started = performance.now();
    try {
      const response = await fetch("/v1/voice-design/generate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          text,
          language: $("voiceDesignLanguage").value,
          instruct,
          max_new_tokens: Number($("voiceDesignMaxTokens").value || 2048),
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const blob = await response.blob();
      if (state.voiceDesignUrl) URL.revokeObjectURL(state.voiceDesignUrl);
      state.voiceDesignUrl = URL.createObjectURL(blob);
      $("voiceDesignAudio").src = state.voiceDesignUrl;
      state.voiceDesignArtifact = decodedHeader(response.headers, "X-Voice-Design-Artifact") || null;
      state.voiceDesignSnapshot = voiceDesignInputs();
      state.voiceDesignFreezeResult = false;
      $("voiceDesignResult").textContent = pretty({
        artifact_id: state.voiceDesignArtifact,
        model: decodedHeader(response.headers, "x-voice-design-model"),
        device: response.headers.get("x-voice-design-device"),
        inference_ms: Number(response.headers.get("x-voice-design-inference-ms") || 0),
        audio_ms: Number(response.headers.get("x-voice-design-audio-ms") || 0),
        sample_rate: Number(response.headers.get("x-voice-design-sample-rate") || 0),
        http_ms: Math.round(performance.now() - started),
      });
      await $("voiceDesignAudio").play().catch(() => {});
      await loadVoiceDesignStatus();
    } catch (error) {
      $("voiceDesignResult").textContent = "ERROR: " + error.message;
    } finally {
      renderFreezeState();
      const status = await fetch("/v1/voice-design/status").then(r => r.ok ? r.json() : null).catch(() => null);
      button.disabled = !status?.voice_design?.ready;
    }
  }

  function voiceDesignInputs() {
    return {
      text: $("voiceDesignText").value.trim(),
      language: $("voiceDesignLanguage").value,
      instruct: $("voiceDesignInstruct").value.trim(),
      characterId: $("voiceDesignCharacter").value,
    };
  }

  function voiceDesignSnapshotMatches() {
    const snapshot = state.voiceDesignSnapshot;
    if (!snapshot) return false;
    const current = voiceDesignInputs();
    return (
      current.text === snapshot.text
      && current.instruct === snapshot.instruct
      && current.language === snapshot.language
      && current.characterId === snapshot.characterId
    );
  }

  // The stored transcript must describe the audio that was actually saved, so
  // freezing stays disabled until the live inputs still match the snapshot taken
  // at generate time. Re-evaluated on every input/change event, not only on generate.
  function renderFreezeState() {
    const button = $("freezeVoiceDesign");
    const status = $("voiceDesignFreezeStatus");
    if (!state.voiceDesignArtifact || !state.voiceDesignSnapshot) {
      button.disabled = true;
      return;
    }
    if (!voiceDesignSnapshotMatches()) {
      button.disabled = true;
      state.voiceDesignFreezeResult = false;
      status.textContent = "测试文本或 Instruct 已修改，请重新生成后再固化。";
      return;
    }
    const characterId = $("voiceDesignCharacter").value;
    if (!characterId) {
      button.disabled = true;
      state.voiceDesignFreezeResult = false;
      status.textContent = "请先选择要固化的角色。";
      return;
    }
    button.disabled = false;
    if (!state.voiceDesignFreezeResult) {
      status.textContent = `试听音频已就绪（artifact: ${state.voiceDesignArtifact}），可固化到 ${characterId}。`;
    }
  }

  async function loadCharacters() {
    const select = $("voiceDesignCharacter");
    try {
      // Same route the chat app uses for its character list.
      const response = await fetch("/v1/characters");
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      state.characters = data.characters || [];
    } catch (error) {
      state.characters = [];
      select.innerHTML = "";
      const option = document.createElement("option");
      option.value = "";
      option.textContent = `角色列表不可用：${error.message}`;
      select.appendChild(option);
      renderFreezeState();
      return;
    }
    const previous = select.value;
    select.innerHTML = "";
    for (const character of state.characters) {
      const option = document.createElement("option");
      option.value = character.id;
      option.textContent = character.name || character.id;
      select.appendChild(option);
    }
    if (state.characters.some((item) => item.id === previous)) select.value = previous;
    renderFreezeState();
  }

  async function freezeVoiceDesign() {
    const button = $("freezeVoiceDesign");
    const characterId = $("voiceDesignCharacter").value;
    if (!state.voiceDesignArtifact || !voiceDesignSnapshotMatches() || !characterId) return;
    button.disabled = true;
    $("voiceDesignFreezeStatus").textContent = "固化中...";
    try {
      const response = await fetch("/v1/voice-design/freeze", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          character_id: characterId,
          artifact_id: state.voiceDesignArtifact,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      const activated = Boolean(data.activated);
      // Overwriting a template changes every character that references it, so
      // say who else is affected instead of letting it happen silently.
      const shared = data.shared_with || [];
      state.voiceDesignFreezeResult = true;
      $("voiceDesignFreezeStatus").textContent = [
        `已固化到 ${data.character_id || characterId}。`,
        `voice_id: ${data.voice_id || ""}`,
        `ref_audio: ${data.ref_audio || ""}`,
        activated
          ? "已生效：GSV sidecar 已加载该声线，可以立即使用。"
          : `暂未热加载，声线还没生效。原因：${data.reason || "GSV sidecar 未运行或未接受该声线"}`,
        shared.length
          ? `已覆盖模板，另有 ${shared.length} 个角色（${shared.join("、")}）共用这个声音，它们也会一起改变。`
          : "",
      ].filter(Boolean).join("\n");
    } catch (error) {
      $("voiceDesignFreezeStatus").textContent = "固化失败：" + error.message;
    } finally {
      renderFreezeState();
    }
  }

  // Same guard as freezing: the stored transcript has to describe the audio
  // being saved, and this path has no character to fall back on -- the name is
  // the whole identity of a template.
  async function saveVoiceDesignTemplate() {
    const button = $("saveVoiceDesignTemplate");
    const status = $("voiceDesignTemplateStatus");
    const name = $("voiceDesignTemplateName").value.trim();
    if (!state.voiceDesignArtifact || !voiceDesignSnapshotMatches()) {
      status.textContent = "测试文本或 Instruct 已修改，请重新生成后再保存。";
      return;
    }
    if (!name) {
      status.textContent = "请先填模板名。";
      return;
    }
    button.disabled = true;
    status.textContent = "保存中...";
    try {
      const response = await fetch("/v1/voice-design/save-template", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({artifact_id: state.voiceDesignArtifact, name}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      status.textContent = `已保存为模板 ${data.template}，ref_audio: ${data.ref_audio || ""}。${
        data.activated ? "已生效。" : `未能热加载：${data.reason || "GSV sidecar 未运行"}`
      }`;
    } catch (error) {
      status.textContent = "保存失败：" + error.message;
    } finally {
      button.disabled = false;
    }
  }

  $("refreshProviders").addEventListener("click", loadProviders);
  $("ttsLabProvider").addEventListener("change", renderVoiceSelect);
  $("generateTtsLab").addEventListener("click", generateSingle);
  $("compareReady").addEventListener("click", compareReady);
  $("refreshVoiceDesign").addEventListener("click", loadVoiceDesignStatus);
  $("polishVoiceDesign").addEventListener("click", polishVoiceDesign);
  $("generateVoiceDesign").addEventListener("click", generateVoiceDesign);
  $("freezeVoiceDesign").addEventListener("click", freezeVoiceDesign);
  $("saveVoiceDesignTemplate").addEventListener("click", saveVoiceDesignTemplate);
  $("voiceDesignInstruct").addEventListener("input", renderFreezeState);
  $("voiceDesignText").addEventListener("input", renderFreezeState);
  $("voiceDesignLanguage").addEventListener("change", renderFreezeState);
  $("voiceDesignCharacter").addEventListener("change", renderFreezeState);
  window.addEventListener("beforeunload", () => {
    document.querySelectorAll("audio[data-object-url]").forEach((audio) => {
      if (audio.dataset.objectUrl) URL.revokeObjectURL(audio.dataset.objectUrl);
    });
    if (state.voiceDesignUrl) URL.revokeObjectURL(state.voiceDesignUrl);
  });
  loadProviders();
  loadVoiceDesignStatus();
  loadCharacters();
})();
