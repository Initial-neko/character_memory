(() => {
  const $ = (id) => document.getElementById(id);
  const state = {providers: [], voiceDesignUrl: null};

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
      $("voiceDesignResult").textContent = pretty({
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
      const status = await fetch("/v1/voice-design/status").then(r => r.ok ? r.json() : null).catch(() => null);
      button.disabled = !status?.voice_design?.ready;
    }
  }
  $("refreshProviders").addEventListener("click", loadProviders);
  $("ttsLabProvider").addEventListener("change", renderVoiceSelect);
  $("generateTtsLab").addEventListener("click", generateSingle);
  $("compareReady").addEventListener("click", compareReady);
  $("refreshVoiceDesign").addEventListener("click", loadVoiceDesignStatus);
  $("polishVoiceDesign").addEventListener("click", polishVoiceDesign);
  $("generateVoiceDesign").addEventListener("click", generateVoiceDesign);
  window.addEventListener("beforeunload", () => {
    document.querySelectorAll("audio[data-object-url]").forEach((audio) => {
      if (audio.dataset.objectUrl) URL.revokeObjectURL(audio.dataset.objectUrl);
    });
    if (state.voiceDesignUrl) URL.revokeObjectURL(state.voiceDesignUrl);
  });
  loadProviders();
  loadVoiceDesignStatus();
})();
