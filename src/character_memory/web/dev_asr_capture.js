(() => {
  const $ = (id) => document.getElementById(id);
  const list = $("asrCaptureList");
  if (!list) return;

  const toggle = $("asrCaptureToggle");
  const refresh = $("asrCaptureRefresh");
  const clear = $("asrCaptureClear");
  const status = $("asrCaptureStatus");
  let enabled = false;

  // Transcript text is model output, so it is never interpolated into markup --
  // every field below goes in through textContent.
  function row(item) {
    const line = document.createElement("div");
    line.className = "asr-capture-row";

    const head = document.createElement("div");
    head.className = "asr-capture-head";
    const when = document.createElement("span");
    when.className = "metric-inline";
    when.textContent = item.at || "-";
    const source = document.createElement("span");
    source.className = "pill";
    source.textContent = item.source || "unknown";
    const audioMs = document.createElement("span");
    audioMs.className = "metric-inline";
    audioMs.textContent = `${Math.round(item.audio_ms || 0)} ms 音频`;
    const inferMs = document.createElement("span");
    inferMs.className = "metric-inline";
    inferMs.textContent = `识别 ${Math.round(item.inference_ms || 0)} ms`;
    head.append(when, source, audioMs, inferMs);

    const text = document.createElement("div");
    text.className = "asr-capture-text";
    // An empty transcript is the interesting case for a noise-triggered capture,
    // so it is stated rather than left as a blank line.
    text.textContent = item.text ? item.text : "（空：没有识别出内容）";

    const player = document.createElement("audio");
    player.controls = true;
    player.preload = "none";
    player.src = `/v1/dev/asr-capture/${encodeURIComponent(item.id)}/audio`;

    line.append(head, player, text);
    return line;
  }

  function render(data) {
    enabled = Boolean(data.enabled);
    toggle.textContent = enabled ? "测试态：开" : "测试态：关";
    toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    status.textContent = `${(data.items || []).length} 条 · ${data.directory || "-"}`;

    list.innerHTML = "";
    const items = data.items || [];
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "subtle";
      empty.textContent = enabled ? "测试态已开，去单聊说几句再回来刷新。" : "尚无记录。开启测试态后，每次上传都会保存音频与识别结果。";
      list.append(empty);
      return;
    }
    items.forEach((item) => list.append(row(item)));
  }

  async function request(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
    return response.json();
  }

  async function load() {
    try {
      render(await request("/v1/dev/asr-capture"));
    } catch (error) {
      status.textContent = `读取失败：${error.message}`;
    }
  }

  toggle.addEventListener("click", async () => {
    try {
      const data = await request("/v1/dev/asr-capture/test-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !enabled }),
      });
      enabled = Boolean(data.enabled);
      await load();
    } catch (error) {
      status.textContent = `切换失败：${error.message}`;
    }
  });

  refresh.addEventListener("click", load);

  clear.addEventListener("click", async () => {
    try {
      const data = await request("/v1/dev/asr-capture/test-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clear: true }),
      });
      status.textContent = `已清空 ${data.cleared || 0} 条`;
      await load();
    } catch (error) {
      status.textContent = `清空失败：${error.message}`;
    }
  });

  load();
})();
