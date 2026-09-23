(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    asrBlob: null,
    asrUrl: null,
    ttsUrl: null,
    recorder: null,
    resourceTimer: null,
  };

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  function setBadge(el, ok) {
    el.textContent = ok ? "ready" : "down";
    el.classList.toggle("ok", !!ok);
    el.classList.toggle("bad", !ok);
  }

  async function responseError(response) {
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { text }; }
    const detail = data.detail || data.text || `${response.status} ${response.statusText}`;
    return new Error(typeof detail === "string" ? detail : pretty(detail));
  }

  async function jsonFetch(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) throw await responseError(response);
    const text = await response.text();
    try { return text ? JSON.parse(text) : {}; } catch { return { text }; }
  }

  async function refreshStatus() {
    try {
      const data = await jsonFetch("/v1/dev/status");
      setBadge($("characterBadge"), data.character?.ok);
      setBadge($("mediaBadge"), data.media?.ok);
      setBadge($("llmBadge"), data.dev?.llm_configured);
      $("characterStatus").textContent = pretty(data.character);
      $("mediaStatus").textContent = pretty(data.media);
      $("modelStatus").textContent = pretty(data.dev);
    } catch (error) {
      // The raw error text lives in the collapsed debug blocks now, so the
      // badges are the only thing left saying the probe failed -- leave them
      // at "unknown" and a dead runtime looks like a slow one.
      setBadge($("characterBadge"), false);
      setBadge($("mediaBadge"), false);
      setBadge($("llmBadge"), false);
      $("characterStatus").textContent = String(error);
      $("mediaStatus").textContent = String(error);
      $("modelStatus").textContent = String(error);
    }
  }

  async function runLlm() {
    const button = $("runLlm");
    button.disabled = true;
    $("llmReply").textContent = "请求中...";
    $("llmLatency").textContent = "-";
    try {
      const data = await jsonFetch("/v1/dev/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: $("llmPrompt").value,
          system_prompt: $("llmSystem").value,
        }),
      });
      // Reply stays on screen; the raw response body sits behind the collapsed
      // debug block so a JSON-mode prompt cannot spray the page with JSON.
      $("llmReply").textContent = data.reply || "";
      $("llmResult").textContent = pretty(data);
      $("llmLatency").textContent = `${data.total_ms} ms · ${data.model}`;
      refreshResources();
    } catch (error) {
      $("llmReply").textContent = `ERROR: ${error.message}`;
      $("llmResult").textContent = pretty({ ok: false, error: error.message });
    } finally {
      button.disabled = false;
    }
  }

  async function loadSpaceCharacters() {
    const select = $("spaceCharacter");
    if (!select) return;
    try {
      const data = await jsonFetch("/v1/dev/characters");
      const items = data.characters || [];
      const current = select.value;
      select.replaceChildren(...items.map((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.name ? `${item.name} · ${item.id}` : item.id;
        return option;
      }));
      if (items.some((item) => item.id === current)) select.value = current;
    } catch (error) {
      $("spaceResult").textContent = `ERROR loading characters: ${error.message}`;
    }
  }

  async function refreshSpaceStatus() {
    try {
      const data = await jsonFetch("/v1/dev/space/status");
      $("spaceStatus").textContent = pretty(data);
      if ($("spaceStatusAge")) $("spaceStatusAge").textContent = `读取时间 ${new Date().toLocaleTimeString()}`;
      if ($("spaceEnabled")) $("spaceEnabled").checked = Boolean(data.enabled);
      if ($("spaceIntervalMinutes")) $("spaceIntervalMinutes").value = String(data.interval_minutes ?? 1440);
      if ($("spaceMaxPostsPerDay")) $("spaceMaxPostsPerDay").value = String(data.max_posts_per_day ?? 0);
      if ($("spaceMediaEnabled")) $("spaceMediaEnabled").checked = Boolean(data.media_enabled ?? true);
      if ($("spaceMediaMaxItems")) $("spaceMediaMaxItems").value = String(data.media_max_items ?? 3);
      if ($("spaceImageSearchEnabled")) $("spaceImageSearchEnabled").checked = Boolean(data.image_search_enabled ?? true);
      if ($("spaceImageGenerationEnabled")) $("spaceImageGenerationEnabled").checked = Boolean(data.image_generation_enabled ?? true);
      if ($("spaceWorldObservationEnabled")) $("spaceWorldObservationEnabled").checked = Boolean(data.world_observation_enabled ?? true);
      if ($("spaceWorldMaxPages")) $("spaceWorldMaxPages").value = String(data.world_max_pages ?? 2);
      if ($("spaceWorldMaxChars")) $("spaceWorldMaxChars").value = String(data.world_max_chars_per_page ?? 6000);
      if ($("spaceAudienceSize")) $("spaceAudienceSize").value = String(data.audience_size ?? 5);
      if ($("spacePollSeconds")) $("spacePollSeconds").value = String(data.poll_seconds ?? 60);
    } catch (error) {
      $("spaceStatus").textContent = `ERROR: ${error.message}`;
    }
  }

  async function applySpaceConfig() {
    const button = $("applySpaceConfig");
    button.disabled = true;
    $("spaceResult").textContent = "正在热应用 Space 临时配置（仅当前 Runtime）...";
    try {
      const data = await jsonFetch("/v1/dev/space/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          enabled: $("spaceEnabled").checked,
          interval_minutes: Number($("spaceIntervalMinutes").value || 1440),
          max_posts_per_day: Number($("spaceMaxPostsPerDay").value || 0),
          media_enabled: $("spaceMediaEnabled").checked,
          media_max_items: Number($("spaceMediaMaxItems").value || 0),
          image_search_enabled: $("spaceImageSearchEnabled").checked,
          image_generation_enabled: $("spaceImageGenerationEnabled").checked,
          world_observation_enabled: $("spaceWorldObservationEnabled").checked,
          world_max_pages: Number($("spaceWorldMaxPages").value || 2),
          world_max_chars_per_page: Number($("spaceWorldMaxChars").value || 6000),
          audience_size: Number($("spaceAudienceSize").value || 0),
          poll_seconds: Number($("spacePollSeconds").value || 60),
          rearm: true,
        }),
      });
      $("spaceResult").textContent = pretty(data);
      await refreshSpaceStatus();
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function forceSpaceDue() {
    const button = $("forceSpaceDue");
    const characterId = $("spaceCharacter").value;
    button.disabled = true;
    $("spaceResult").textContent = `正在让 ${characterId} 的 next opportunity 到期...`;
    try {
      const data = await jsonFetch(`/v1/dev/space/due/${encodeURIComponent(characterId)}`, {
        method: "POST",
      });
      $("spaceResult").textContent = pretty(data);
      await refreshSpaceStatus();
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runSpaceOpportunity() {
    const button = $("runSpaceOpportunity");
    const characterId = $("spaceCharacter").value;
    button.disabled = true;
    $("spaceResult").textContent = "正在执行一次手动 Space Opportunity...";
    try {
      const data = await jsonFetch(`/v1/dev/space/opportunity/${encodeURIComponent(characterId)}`, {
        method: "POST",
      });
      $("spaceResult").textContent = pretty(data);
      const postId = data.post?.id;
      if (postId) $("spacePostId").value = String(postId);
      refreshSpaceStatus();
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function loadSpaceRunRaw() {
    const runId = Number($("spaceRunId").value || 0);
    if (!runId) {
      $("spaceRunResult").textContent = "先填一个 run id：刷新调度状态后，在 recent_runs 里能看到最近几次决策的 id。";
      return;
    }
    $("spaceRunResult").textContent = `正在读取 run ${runId} ...`;
    try {
      const data = await jsonFetch(`/v1/dev/space/opportunity/run/${encodeURIComponent(runId)}`);
      $("spaceRunResult").textContent = pretty(data);
    } catch (error) {
      $("spaceRunResult").textContent = `ERROR: ${error.message}`;
    }
  }

  async function runSpaceMedia() {
    const button = $("runSpaceMedia");
    const characterId = $("spaceCharacter").value;
    const type = $("spaceMediaType").value;
    button.disabled = true;
    $("spaceResult").textContent = `正在测试 ${type} Space 动态...`;
    try {
      const data = await jsonFetch(`/v1/dev/space/media/${encodeURIComponent(characterId)}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          type,
          count: Number($("spaceMediaCount").value || 1),
          query: $("spaceMediaQuery").value,
          purpose: $("spaceMediaPurpose").value,
          visual_intent: $("spaceMediaVisualIntent").value,
          voice_text: $("spaceMediaVoiceText").value,
          content: $("spaceMediaPostText").value,
        }),
      });
      $("spaceResult").textContent = pretty(data);
      if (data.post?.id) $("spacePostId").value = String(data.post.id);
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runWorldSearch() {
    const button = $("runWorldSearch");
    button.disabled = true;
    $("spaceResult").textContent = "正在搜索并用无头浏览器打开网页...";
    try {
      const data = await jsonFetch("/v1/dev/world/search", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          query: $("worldQuery").value,
          max_pages: Number($("worldPages").value || 2),
          max_chars_per_page: Number($("spaceWorldMaxChars").value || 6000),
        }),
      });
      $("spaceResult").textContent = pretty(data);
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runWorldFetch() {
    const button = $("runWorldFetch");
    button.disabled = true;
    $("spaceResult").textContent = "正在通过 Playwright Chromium 渲染 URL...";
    try {
      const data = await jsonFetch("/v1/dev/world/fetch", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          url: $("worldUrl").value,
          max_chars: Number($("spaceWorldMaxChars").value || 6000),
        }),
      });
      $("spaceResult").textContent = pretty(data);
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runSpaceAudience() {
    const button = $("runSpaceAudience");
    const postId = Number($("spacePostId").value || 0);
    if (!postId) {
      $("spaceResult").textContent = "ERROR: 请先填写 Post ID，或先触发一次会发动态的 Daily Life。";
      return;
    }
    button.disabled = true;
    $("spaceResult").textContent = `正在模拟 Post #${postId} 的 Audience...`;
    try {
      const data = await jsonFetch(`/v1/dev/space/audience/${postId}`, {
        method: "POST",
      });
      $("spaceResult").textContent = pretty(data);
    } catch (error) {
      $("spaceResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function loadGroupAutonomyGroups() {
    const select = $("groupAutonomyGroup");
    if (!select) return;
    try {
      const data = await jsonFetch("/v1/dev/groups");
      const items = data.groups || [];
      const current = select.value;
      select.replaceChildren(...items.map((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.name ? `${item.name} · ${item.id}` : item.id;
        return option;
      }));
      if (!items.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无群聊";
        select.appendChild(option);
      } else if (items.some((item) => item.id === current)) {
        select.value = current;
      }
    } catch (error) {
      $("groupAutonomyResult").textContent = `ERROR loading groups: ${error.message}`;
    }
  }

  async function refreshGroupAutonomyStatus() {
    try {
      const data = await jsonFetch("/v1/dev/group-autonomy/status");
      $("groupAutonomyStatus").textContent = pretty(data);
      $("groupAutonomyStatusAge").textContent = `读取时间 ${new Date().toLocaleTimeString()}`;
      $("groupAutonomyEnabled").checked = Boolean(data.enabled);
      $("groupAutonomyInterval").value = String(data.interval_minutes ?? 360);
      $("groupAutonomyMaxMessages").value = String(data.max_messages ?? 3);
      $("groupAutonomyQuietMinutes").value = String(data.user_quiet_minutes ?? 30);
      $("groupAutonomyPollSeconds").value = String(data.poll_seconds ?? 60);
      await loadGroupAutonomyGroups();
    } catch (error) {
      $("groupAutonomyStatus").textContent = `ERROR: ${error.message}`;
    }
  }

  async function applyGroupAutonomyConfig() {
    const button = $("applyGroupAutonomyConfig");
    button.disabled = true;
    $("groupAutonomyResult").textContent = "正在热应用自主群聊临时配置（仅当前 Runtime）...";
    try {
      const data = await jsonFetch("/v1/dev/group-autonomy/config", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          enabled:$("groupAutonomyEnabled").checked,
          interval_minutes:Number($("groupAutonomyInterval").value || 360),
          max_messages:Number($("groupAutonomyMaxMessages").value || 3),
          user_quiet_minutes:Number($("groupAutonomyQuietMinutes").value || 0),
          poll_seconds:Number($("groupAutonomyPollSeconds").value || 60),
          rearm:true,
        }),
      });
      $("groupAutonomyResult").textContent = pretty(data);
      await refreshGroupAutonomyStatus();
    } catch (error) {
      $("groupAutonomyResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runGroupAutonomyOpportunity() {
    const button = $("runGroupAutonomyOpportunity");
    const groupId = $("groupAutonomyGroup").value;
    if (!groupId) {
      $("groupAutonomyResult").textContent = "ERROR: 请先创建并选择一个群聊。";
      return;
    }
    button.disabled = true;
    $("groupAutonomyResult").textContent = "正在执行一次手动自主群聊 Opportunity...";
    try {
      const data = await jsonFetch(`/v1/dev/group-autonomy/opportunity/${encodeURIComponent(groupId)}`, {method:"POST"});
      $("groupAutonomyResult").textContent = pretty(data);
      await refreshGroupAutonomyStatus();
    } catch (error) {
      $("groupAutonomyResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function forceGroupAutonomyDue() {
    const button = $("forceGroupAutonomyDue");
    const groupId = $("groupAutonomyGroup").value;
    if (!groupId) {
      $("groupAutonomyResult").textContent = "ERROR: 请先创建并选择一个群聊。";
      return;
    }
    button.disabled = true;
    $("groupAutonomyResult").textContent = "正在让选中群的 next opportunity 到期...";
    try {
      const data = await jsonFetch(`/v1/dev/group-autonomy/due/${encodeURIComponent(groupId)}`, {method:"POST"});
      $("groupAutonomyResult").textContent = pretty(data);
      await refreshGroupAutonomyStatus();
    } catch (error) {
      $("groupAutonomyResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runTts() {
    const button = $("runTts");
    button.disabled = true;
    $("ttsResult").textContent = "生成中...";
    $("ttsLatency").textContent = "生成中...";
    try {
      const started = performance.now();
      const response = await fetch("/v1/dev/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: $("ttsText").value,
          speaker_id: Number($("speakerId").value || 0),
          speed: Number($("ttsSpeed").value || 1),
        }),
      });
      if (!response.ok) throw await responseError(response);
      const blob = await response.blob();
      if (state.ttsUrl) URL.revokeObjectURL(state.ttsUrl);
      state.ttsUrl = URL.createObjectURL(blob);
      $("ttsAudio").src = state.ttsUrl;
      const inference = response.headers.get("x-media-inference-ms");
      const audioMs = response.headers.get("x-media-audio-ms");
      const provider = response.headers.get("x-media-provider");
      const device = response.headers.get("x-media-device");
      const total = response.headers.get("x-dev-total-ms") || (performance.now() - started).toFixed(1);
      const rtf = inference && audioMs ? (Number(inference) / Number(audioMs)).toFixed(3) : "-";
      // Provider / device / latency stay outside the collapsed block so the run
      // still reports its outcome without opening the raw JSON.
      $("ttsLatency").textContent = [provider, device, `${total} ms`].filter(Boolean).join(" · ");
      $("ttsResult").textContent = pretty({
        provider,
        device,
        inference_ms: inference,
        audio_ms: audioMs,
        sample_rate: response.headers.get("x-media-sample-rate"),
        rtf,
        total_ms: total,
      });
      refreshStatus();
      refreshMetrics();
      refreshResources();
    } catch (error) {
      $("ttsLatency").textContent = `ERROR: ${error.message}`;
      $("ttsResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  function concatFloat32(chunks) {
    const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const out = new Float32Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  }

  function resampleLinear(samples, sourceRate, targetRate) {
    if (sourceRate === targetRate) return samples;
    const outLength = Math.max(1, Math.round(samples.length * targetRate / sourceRate));
    const out = new Float32Array(outLength);
    const ratio = sourceRate / targetRate;
    for (let i = 0; i < outLength; i += 1) {
      const pos = i * ratio;
      const left = Math.floor(pos);
      const right = Math.min(left + 1, samples.length - 1);
      const frac = pos - left;
      out[i] = samples[left] * (1 - frac) + samples[right] * frac;
    }
    return out;
  }

  function wavBlob(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const write = (offset, text) => { for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i)); };
    write(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    write(8, "WAVE");
    write(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, "data");
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (const sample of samples) {
      const value = Math.max(-1, Math.min(1, sample));
      view.setInt16(offset, value < 0 ? value * 32768 : value * 32767, true);
      offset += 2;
    }
    return new Blob([buffer], { type: "audio/wav" });
  }

  function setAsrBlob(blob, label, durationMs = null) {
    state.asrBlob = blob;
    if (state.asrUrl) URL.revokeObjectURL(state.asrUrl);
    state.asrUrl = URL.createObjectURL(blob);
    $("asrAudio").src = state.asrUrl;
    $("asrSource").textContent = label;
    $("asrDuration").textContent = durationMs == null ? `${(blob.size / 1024).toFixed(1)} KB` : `${(durationMs / 1000).toFixed(2)} s`;
    $("runAsr").disabled = false;
  }

  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia) {
      $("asrResult").textContent = "ERROR: 当前浏览器不支持麦克风录音";
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
    source.connect(processor);
    processor.connect(context.destination);
    state.recorder = { stream, context, source, processor, chunks, started: performance.now(), sampleRate: context.sampleRate };
    $("recordAsr").disabled = true;
    $("stopAsr").disabled = false;
    $("asrSource").textContent = "录音中...";
  }

  async function stopRecording() {
    const recorder = state.recorder;
    if (!recorder) return;
    recorder.processor.disconnect();
    recorder.source.disconnect();
    recorder.stream.getTracks().forEach((track) => track.stop());
    await recorder.context.close();
    const raw = concatFloat32(recorder.chunks);
    const targetRate = 16000;
    const resampled = resampleLinear(raw, recorder.sampleRate, targetRate);
    const durationMs = resampled.length * 1000 / targetRate;
    setAsrBlob(wavBlob(resampled, targetRate), "浏览器麦克风 · PCM16 16kHz", durationMs);
    state.recorder = null;
    $("recordAsr").disabled = false;
    $("stopAsr").disabled = true;
  }

  async function runAsr() {
    if (!state.asrBlob) return;
    const button = $("runAsr");
    button.disabled = true;
    $("asrResult").textContent = "识别中...";
    $("asrLatency").textContent = "-";
    $("asrText").textContent = "识别中...";
    try {
      const data = await jsonFetch("/v1/dev/asr", {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: state.asrBlob,
      });
      const rtf = data.inference_ms && data.audio_ms ? (Number(data.inference_ms) / Number(data.audio_ms)).toFixed(3) : "-";
      $("asrLatency").textContent = `${data.http_total_ms} ms total`;
      // The transcript is the answer; the raw payload is the debug block.
      $("asrText").textContent = data.text || "(无识别结果)";
      $("asrResult").textContent = pretty({ ...data, rtf });
      refreshStatus();
      refreshMetrics();
      refreshResources();
    } catch (error) {
      $("asrText").textContent = `ERROR: ${error.message}`;
      $("asrResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = !state.asrBlob;
    }
  }

  async function runMediaSmoke() {
    const button = $("runMediaSmoke");
    button.disabled = true;
    $("mediaSmokeResult").textContent = "真实推理中：TTS → WAV → ASR ...";
    $("mediaSmokeLatency").textContent = "-";
    $("mediaSmokeText").textContent = "真实推理中...";
    try {
      const data = await jsonFetch("/v1/dev/media-smoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: $("ttsText").value || "你好，这是 Character Memory 的媒体自检。",
          speaker_id: Number($("speakerId").value || 0),
          speed: Number($("ttsSpeed").value || 1),
        }),
      });
      $("mediaSmokeLatency").textContent = `${data.total_ms} ms total`;
      // Round-trip proof stays on screen: the transcript ASR heard back.
      $("mediaSmokeText").textContent = `识别：${data.transcript || "(空)"}`;
      $("mediaSmokeResult").textContent = pretty(data);
      refreshStatus();
      refreshMetrics();
      refreshResources();
    } catch (error) {
      $("mediaSmokeText").textContent = `ERROR: ${error.message}`;
      $("mediaSmokeResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  function processState(item) {
    if (item.name === "Media Runtime") {
      const asr = item.asr_loaded == null ? "?" : (item.asr_loaded ? "loaded" : "idle");
      const tts = item.tts_loaded == null ? "?" : (item.tts_loaded ? "loaded" : "idle");
      return `ASR ${asr} · TTS ${tts}`;
    }
    if (item.name === "Character Runtime") {
      if (item.runtime_loaded == null) return "unknown";
      return item.runtime_loaded ? "runtime loaded" : "runtime idle";
    }
    return item.pid ? "running" : "unavailable";
  }

  async function refreshResources() {
    const body = $("resourceBody");
    try {
      const data = await jsonFetch("/v1/dev/resources");
      const ram = data.system_memory || {};
      const gpu = data.gpu || {};
      const gpuSummary = gpu.available
        ? (gpu.devices || []).map((device) => `${device.name}: ${device.used_mb ?? "?"} / ${device.total_mb ?? "?"} MB`).join("\n") || "NVIDIA GPU detected"
        : `unavailable${gpu.error ? ` · ${gpu.error}` : ""}`;
      $("resourceSummary").textContent = pretty({
        system_ram: ram.available ? `${ram.used_mb} / ${ram.total_mb} MB (${ram.used_percent}%)` : "unavailable",
        gpu: gpuSummary,
      });

      const items = data.processes || [];
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="5">暂无数据</td></tr>';
      } else {
        body.replaceChildren(...items.map((item) => {
          const row = document.createElement("tr");
          const values = [
            item.name,
            item.pid ?? "N/A",
            item.rss_mb == null ? "N/A" : `${item.rss_mb} MB`,
            item.gpu_vram_mb == null ? "N/A" : `${item.gpu_vram_mb} MB`,
            processState(item),
          ];
          for (const value of values) {
            const cell = document.createElement("td");
            cell.textContent = String(value);
            row.appendChild(cell);
          }
          return row;
        }));
      }
      const sampled = data.sampled_at ? new Date(data.sampled_at * 1000) : new Date();
      $("resourceUpdated").textContent = `上次采样：${sampled.toLocaleTimeString()}`;
    } catch (error) {
      $("resourceSummary").textContent = `Resource metrics unavailable: ${error.message}`;
      body.innerHTML = "";
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = `Resource metrics unavailable: ${error.message}`;
      row.appendChild(cell);
      body.appendChild(row);
    }
  }

  function scheduleResourceRefresh() {
    if (state.resourceTimer) {
      clearInterval(state.resourceTimer);
      state.resourceTimer = null;
    }
    const seconds = Number($("resourceInterval").value || 0);
    if (seconds > 0) {
      state.resourceTimer = setInterval(refreshResources, seconds * 1000);
    }
  }

  async function refreshWorldActivity() {
    const statusNode = $("worldActivityStatus");
    if (!statusNode) return;
    try {
      const [status, pulse] = await Promise.all([
        jsonFetch("/v1/dev/world/activity"),
        jsonFetch("/v1/dev/world/pulse"),
      ]);
      statusNode.textContent = pretty({status, pulse});
      const latest = pulse.topics?.[0];
      if (latest?.id && !$("worldPulseTopicId").value) {
        $("worldPulseTopicId").value = String(latest.id);
      }
    } catch (error) {
      statusNode.textContent = `ERROR: ${error.message}`;
    }
  }

  async function refreshWorldPulse() {
    const button = $("refreshWorldPulse");
    button.disabled = true;
    $("worldActivityResult").textContent = "正在读取聚合站点并汇总 World Pulse...";
    try {
      const data = await jsonFetch("/v1/dev/world/pulse/refresh", {method:"POST"});
      $("worldActivityResult").textContent = pretty(data);
      const latest = data.topics?.[0];
      if (latest?.id) $("worldPulseTopicId").value = String(latest.id);
      await refreshWorldActivity();
    } catch (error) {
      $("worldActivityResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function discussWorldPulse() {
    const button = $("discussWorldPulse");
    const topicId = Number($("worldPulseTopicId").value || 0);
    if (!topicId) {
      $("worldActivityResult").textContent = "ERROR: 请先刷新 Pulse 或填写 Topic ID。";
      return;
    }
    button.disabled = true;
    $("worldActivityResult").textContent = `正在让角色判断是否评论 World Pulse #${topicId}...`;
    try {
      const data = await jsonFetch(`/v1/dev/world/pulse/${topicId}/discuss`, {method:"POST"});
      $("worldActivityResult").textContent = pretty(data);
      await refreshWorldActivity();
    } catch (error) {
      $("worldActivityResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function browseWorldAsCharacter() {
    const button = $("browseWorldAsCharacter");
    const characterId = $("spaceCharacter").value;
    if (!characterId) {
      $("worldActivityResult").textContent = "ERROR: 请先选择 Character。";
      return;
    }
    button.disabled = true;
    $("worldActivityResult").textContent = `正在让 ${characterId} 独立上网浏览...`;
    try {
      const data = await jsonFetch(`/v1/dev/world/browse/${encodeURIComponent(characterId)}`, {method:"POST"});
      $("worldActivityResult").textContent = pretty(data);
      await refreshWorldActivity();
    } catch (error) {
      $("worldActivityResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runWorldActivity() {
    const button = $("runWorldActivity");
    button.disabled = true;
    $("worldActivityResult").textContent = "正在执行当前已经到期的 World Activity...";
    try {
      const data = await jsonFetch("/v1/dev/world/activity/run", {method:"POST"});
      $("worldActivityResult").textContent = pretty(data);
      await refreshWorldActivity();
    } catch (error) {
      $("worldActivityResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function refreshEncounterStatus() {
    const status = $("encounterStatus");
    if (!status) return;
    try {
      const data = await jsonFetch("/v1/dev/encounters/status");
      status.textContent = pretty(data);
      if ($("encounterStatusAge")) $("encounterStatusAge").textContent = `读取时间 ${new Date().toLocaleTimeString()}`;
    } catch (error) {
      status.textContent = `ERROR: ${error.message}`;
    }
  }

  async function runEncounterOpportunity() {
    const button = $("runEncounterOpportunity");
    const source = $("encounterSource")?.value || "AUTO";
    button.disabled = true;
    $("encounterResult").textContent = `正在生成 ${source} 邂逅...`;
    try {
      const data = await jsonFetch(`/v1/dev/encounters/opportunity?source_type=${encodeURIComponent(source)}`, {
        method: "POST",
      });
      $("encounterResult").textContent = pretty(data);
      await refreshEncounterStatus();
    } catch (error) {
      $("encounterResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function forceEncounterDue() {
    const button = $("forceEncounterDue");
    button.disabled = true;
    $("encounterResult").textContent = "正在让 Random Encounter Scheduler 立即到期...";
    try {
      const data = await jsonFetch("/v1/dev/encounters/due", {method: "POST"});
      $("encounterResult").textContent = pretty(data);
      await refreshEncounterStatus();
    } catch (error) {
      $("encounterResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function refreshMetrics() {
    const body = $("metricsBody");
    try {
      const data = await jsonFetch("/v1/dev/metrics?limit=30");
      const items = [...(data.metrics || [])].reverse();
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="6">暂无数据</td></tr>';
        return;
      }
      body.replaceChildren(...items.map((item) => {
        const row = document.createElement("tr");
        const values = [item.kind, item.provider || "-", item.device || "-", item.inference_ms ?? "-", item.audio_ms ?? "-", item.total_ms ?? "-"];
        for (const value of values) {
          const cell = document.createElement("td");
          cell.textContent = String(value);
          row.appendChild(cell);
        }
        return row;
      }));
    } catch (error) {
      body.innerHTML = "";
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent = `Metrics unavailable: ${error.message}`;
      row.appendChild(cell);
      body.appendChild(row);
    }
  }

  $("refreshAll").addEventListener("click", () => { refreshStatus(); refreshMetrics(); refreshResources(); refreshSpaceStatus(); refreshGroupAutonomyStatus(); refreshEncounterStatus(); refreshWorldActivity(); });
  $("runLlm").addEventListener("click", runLlm);
  $("runSpaceOpportunity").addEventListener("click", runSpaceOpportunity);
  $("forceSpaceDue").addEventListener("click", forceSpaceDue);
  $("applySpaceConfig").addEventListener("click", applySpaceConfig);
  document.querySelectorAll(".space-preset").forEach((button) => {
    button.addEventListener("click", () => {
      $("spaceIntervalMinutes").value = button.dataset.minutes || "1440";
    });
  });
  $("runSpaceAudience").addEventListener("click", runSpaceAudience);
  $("runSpaceMedia").addEventListener("click", runSpaceMedia);
  $("runWorldSearch").addEventListener("click", runWorldSearch);
  $("runWorldFetch").addEventListener("click", runWorldFetch);
  $("refreshSpaceStatus").addEventListener("click", refreshSpaceStatus);
  $("loadSpaceRunRaw").addEventListener("click", loadSpaceRunRaw);
  $("applyGroupAutonomyConfig").addEventListener("click", applyGroupAutonomyConfig);
  $("runGroupAutonomyOpportunity").addEventListener("click", runGroupAutonomyOpportunity);
  $("forceGroupAutonomyDue").addEventListener("click", forceGroupAutonomyDue);
  $("refreshGroupAutonomyStatus").addEventListener("click", refreshGroupAutonomyStatus);
  $("runEncounterOpportunity").addEventListener("click", runEncounterOpportunity);
  $("forceEncounterDue").addEventListener("click", forceEncounterDue);
  $("refreshEncounterStatus").addEventListener("click", refreshEncounterStatus);
  $("refreshWorldPulse").addEventListener("click", refreshWorldPulse);
  $("discussWorldPulse").addEventListener("click", discussWorldPulse);
  $("browseWorldAsCharacter").addEventListener("click", browseWorldAsCharacter);
  $("runWorldActivity").addEventListener("click", runWorldActivity);
  $("refreshWorldActivity").addEventListener("click", refreshWorldActivity);
  document.querySelectorAll(".group-autonomy-preset").forEach((button) => {
    button.addEventListener("click", () => {
      $("groupAutonomyInterval").value = button.dataset.minutes || "360";
    });
  });
  $("runTts").addEventListener("click", runTts);
  $("runAsr").addEventListener("click", runAsr);
  $("runMediaSmoke").addEventListener("click", runMediaSmoke);
  $("recordAsr").addEventListener("click", () => startRecording().catch((error) => { $("asrResult").textContent = `ERROR: ${error.message}`; }));
  $("stopAsr").addEventListener("click", () => stopRecording().catch((error) => { $("asrResult").textContent = `ERROR: ${error.message}`; }));
  $("asrFile").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) setAsrBlob(file, `文件 · ${file.name}`);
  });
  $("refreshMetrics").addEventListener("click", refreshMetrics);
  $("refreshResources").addEventListener("click", refreshResources);
  $("resourceInterval").addEventListener("change", scheduleResourceRefresh);

  refreshStatus();
  refreshMetrics();
  refreshResources();
  loadSpaceCharacters();
  refreshSpaceStatus();
  loadGroupAutonomyGroups();
  refreshGroupAutonomyStatus();
  refreshEncounterStatus();
  refreshWorldActivity();
  scheduleResourceRefresh();

  // Every control above is wired, so the failure banner in the markup can stand
  // down. Anything that stops this line from running leaves the page looking
  // alive while nothing responds, which is exactly what the banner is for.
  document.body.dataset.devBooted = "1";

  // Chrome restores a page from bfcache without re-running it, so a console
  // reactivated after a stack restart would keep showing the config it was
  // loaded with. Re-read state whenever the page is shown again.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      refreshSpaceStatus();
      loadSpaceCharacters();
      refreshEncounterStatus();
      refreshWorldActivity();
    }
  });
})();
