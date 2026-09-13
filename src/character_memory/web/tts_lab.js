(() => {
  const $ = (id) => document.getElementById(id);
  const state = {providers: []};

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

  function metadataFromHeaders(headers) {
    return {
      provider: headers.get("x-tts-provider"),
      voice: headers.get("x-tts-voice"),
      model: headers.get("x-tts-model"),
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

  $("refreshProviders").addEventListener("click", loadProviders);
  $("ttsLabProvider").addEventListener("change", renderVoiceSelect);
  $("generateTtsLab").addEventListener("click", generateSingle);
  $("compareReady").addEventListener("click", compareReady);
  window.addEventListener("beforeunload", () => {
    document.querySelectorAll("audio[data-object-url]").forEach((audio) => {
      if (audio.dataset.objectUrl) URL.revokeObjectURL(audio.dataset.objectUrl);
    });
  });
  loadProviders();
})();
