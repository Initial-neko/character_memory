(() => {
  const $ = (id) => document.getElementById(id);
  const STATUS_TIMEOUT_MS = 8000;
  const state = {
    providers: [],
    providersState: "idle",
    voiceDesignState: "idle",
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

  async function fetchJsonWithTimeout(url, options = {}, timeoutMs = STATUS_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      if (!response.ok) {
        const body = await response.text();
        throw new Error(body || `HTTP ${response.status}`);
      }
      return await response.json();
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new Error(`请求超时（${Math.round(timeoutMs / 1000)} 秒）`);
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function providerLoadMessage(text, tone = "") {
    const node = $("providerLoadStatus");
    node.textContent = text;
    node.className = `subtle load-state-message ${tone}`.trim();
  }

  function voiceDesignLoadMessage(text, tone = "") {
    const node = $("voiceDesignLoadStatus");
    node.textContent = text;
    node.className = `subtle load-state-message ${tone}`.trim();
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
      badge.textContent = provider.ready ? (provider.loaded ? "已加载" : "就绪") : "不可用";
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
      // The badge already answers "usable?"; the raw probe stays behind a click
      // so the grid does not open onto a wall of per-provider JSON.
      const debug = document.createElement("details");
      debug.className = "debug-output";
      const debugSummary = document.createElement("summary");
      debugSummary.textContent = "原始响应（调试用）";
      debug.append(debugSummary, body);
      card.append(header, debug);
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
      option.textContent = `${provider.label || provider.id}${provider.ready ? "" : " · 不可用"}`;
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
    if (!provider) {
      $("ttsLabSpeed").disabled = true;
      $("generateTtsLab").disabled = true;
      return;
    }
    const voices = provider.voices?.length ? provider.voices : [provider.default_voice || "default"];
    for (const value of voices) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(value);
      voice.appendChild(option);
    }
    if (provider.default_voice && voices.includes(provider.default_voice)) voice.value = provider.default_voice;
    $("ttsLabSpeed").disabled = provider.supports_speed === false;
    $("generateTtsLab").disabled = !provider.ready;
  }

  async function loadProviders() {
    const started = performance.now();
    state.providersState = "loading";
    state.providers = [];
    $("refreshProviders").disabled = true;
    $("generateTtsLab").disabled = true;
    $("compareReady").disabled = true;
    $("providerStatusGrid").innerHTML = '<article class="card status-card status-placeholder"><div class="subtle">正在读取 Provider 状态…</div></article>';
    providerLoadMessage("正在读取 Provider 状态…");
    try {
      const data = await fetchJsonWithTimeout("/v1/providers");
      state.providers = data.providers || [];
      state.providersState = "ready";
      renderProviderStatus();
      renderProviderSelect();
      const readyCount = state.providers.filter((item) => item.ready).length;
      $("compareReady").disabled = readyCount === 0;
      providerLoadMessage(
        `已读取 ${state.providers.length} 个 Provider · ${readyCount} 个可用 · ${Math.round(performance.now() - started)} ms`,
        readyCount ? "ok" : "warn",
      );
      if (!state.providers.length) {
        $("providerStatusGrid").innerHTML = '<article class="card status-card"><div class="subtle">Provider 列表为空，请检查 TTS Lab Runtime。</div></article>';
      }
    } catch (error) {
      state.providersState = "error";
      state.providers = [];
      renderProviderSelect();
      $("providerStatusGrid").innerHTML = `<article class="card status-card"><div class="load-error"><strong>Provider 状态读取失败</strong><p>${String(error.message || error)}</p><p class="subtle">可以点击右上角“刷新 Provider”重试。</p></div></article>`;
      providerLoadMessage(`读取失败：${error.message}`, "bad");
      $("compareReady").disabled = true;
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
    if (!provider?.ready) {
      $("ttsLabSummary").textContent = state.providersState === "loading"
        ? "Provider 状态仍在加载，请稍候。"
        : "当前没有可用 Provider，请先刷新 Provider 状态。";
      $("ttsLabResult").textContent = $("ttsLabSummary").textContent;
      $("generateTtsLab").disabled = true;
      return;
    }
    const button = $("generateTtsLab");
    button.disabled = true;
    $("ttsLabSummary").textContent = "生成中...";
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
      // Which provider/device produced the clip stays visible outside the
      // collapsed raw block.
      $("ttsLabSummary").textContent = [
        result.meta.provider,
        result.meta.device,
        result.meta.inference_ms ? `${result.meta.inference_ms} ms` : null,
      ].filter(Boolean).join(" · ");
      $("ttsLabResult").textContent = pretty(result.meta);
      await $("ttsLabAudio").play().catch(() => {});
      await loadProviders();
    } catch (error) {
      $("ttsLabSummary").textContent = `ERROR: ${error.message}`;
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
    const status = document.createElement("div");
    status.className = "subtle";
    status.textContent = "waiting...";
    const result = document.createElement("pre");
    result.className = "result";
    result.textContent = "waiting...";
    // Same rule as the single-provider card: outcome line visible, raw header
    // dump behind a click.
    const debug = document.createElement("details");
    debug.className = "debug-output";
    const debugSummary = document.createElement("summary");
    debugSummary.textContent = "原始响应（调试用）";
    debug.append(debugSummary, result);
    card.append(title, voice, audio, status, debug);
    return {card, audio, status, result};
  }

  async function compareReady() {
    const button = $("compareReady");
    button.disabled = true;
    const root = $("comparisonGrid");
    root.innerHTML = "";
    const providers = state.providers.filter((item) => item.ready);
    if (!providers.length) {
      root.innerHTML = '<div class="subtle">当前没有可用 Provider。请先刷新 Provider 状态并检查 Runtime。</div>';
      button.disabled = true;
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
      view.status.textContent = "generating...";
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
        view.status.textContent = [
          result.meta.device,
          result.meta.inference_ms ? `${result.meta.inference_ms} ms` : null,
        ].filter(Boolean).join(" · ") || "done";
        view.result.textContent = pretty(result.meta);
      } catch (error) {
        view.status.textContent = `ERROR: ${error.message}`;
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
    badge.textContent = ready ? (payload.loaded ? "已加载" : "就绪") : "不可用";
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

  async function loadVoiceDesignStatus(options = {}) {
    const quiet = Boolean(options?.quiet);
    const started = performance.now();
    state.voiceDesignState = "loading";
    $("refreshVoiceDesign").disabled = true;
    $("generateVoiceDesign").disabled = true;
    if (!quiet) {
      $("voiceDesignBadge").className = "badge";
      $("voiceDesignBadge").textContent = "读取中";
      voiceDesignLoadMessage("正在读取 Voice Design 状态…");
    }
    try {
      const data = await fetchJsonWithTimeout("/v1/voice-design/status");
      state.voiceDesignState = "ready";
      renderVoiceDesignStatus(data);
      const payload = data?.voice_design || {};
      voiceDesignLoadMessage(
        `${payload.ready ? "Voice Design 已就绪" : "Voice Design 当前不可用"} · ${Math.round(performance.now() - started)} ms`,
        payload.ready ? "ok" : "warn",
      );
      return Boolean(payload.ready);
    } catch (error) {
      state.voiceDesignState = "error";
      renderVoiceDesignStatus({voice_design: {ready: false, reason: error.message}});
      voiceDesignLoadMessage(`状态读取失败：${error.message}`, "bad");
      return false;
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
      $("voiceDesignResultSummary").textContent = "ERROR: Instruct 和测试文本都不能为空。";
      $("voiceDesignResult").textContent = "ERROR: Instruct 和测试文本都不能为空。";
      return;
    }
    button.disabled = true;
    $("voiceDesignResultSummary").textContent = "生成中...";
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
      const model = decodedHeader(response.headers, "x-voice-design-model");
      const device = response.headers.get("x-voice-design-device");
      const inferenceMs = Number(response.headers.get("x-voice-design-inference-ms") || 0);
      const audioMs = Number(response.headers.get("x-voice-design-audio-ms") || 0);
      const sampleRate = Number(response.headers.get("x-voice-design-sample-rate") || 0);
      // Which model/device produced this take stays visible; the header dump
      // stays behind the collapsed debug block.
      $("voiceDesignResultSummary").textContent = [
        device,
        inferenceMs ? `${inferenceMs} ms` : null,
        sampleRate ? `${sampleRate} Hz` : null,
      ].filter(Boolean).join(" · ") || "已生成";
      $("voiceDesignResult").textContent = pretty({
        artifact_id: state.voiceDesignArtifact,
        model,
        device,
        inference_ms: inferenceMs,
        audio_ms: audioMs,
        sample_rate: sampleRate,
        http_ms: Math.round(performance.now() - started),
      });
      await $("voiceDesignAudio").play().catch(() => {});
      await loadVoiceDesignStatus();
    } catch (error) {
      $("voiceDesignResultSummary").textContent = "ERROR: " + error.message;
      $("voiceDesignResult").textContent = "ERROR: " + error.message;
    } finally {
      renderFreezeState();
      await loadVoiceDesignStatus({quiet: true});
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
