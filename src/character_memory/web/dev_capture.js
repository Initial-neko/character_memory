(() => {
  const $ = (id) => document.getElementById(id);
  if (!window.VisualCapture || !$("visualPreview")) return;

  const state = {
    selected: [],
    session: null,
  };

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  function renderFrames(frames = state.selected) {
    const root = $("visualFrames");
    if (!root) return;
    root.innerHTML = "";
    if (!frames.length) {
      const empty = document.createElement("div");
      empty.className = "subtle";
      empty.textContent = "尚未选择关键帧";
      root.appendChild(empty);
      return;
    }
    for (const [index, frame] of frames.entries()) {
      const figure = document.createElement("figure");
      figure.className = "visual-dev-frame";
      const image = document.createElement("img");
      image.src = frame.data_url;
      image.alt = `关键帧 ${index + 1}`;
      const caption = document.createElement("figcaption");
      caption.textContent = `${index + 1} · ${frame.source}`;
      figure.append(image, caption);
      root.appendChild(figure);
    }
  }

  function updateCaptureState(value) {
    $("visualCandidateCount").textContent = `${value.candidateCount || 0} candidates`;
    $("visualCamera").classList.toggle("active", value.active && value.source === "CAMERA");
    $("visualScreen").classList.toggle("active", value.active && value.source === "DISPLAY");
    $("visualStop").disabled = !value.active;
  }

  state.session = window.VisualCapture.createSession({
    preview:$("visualPreview"),
    status:$("visualStatus"),
    onStateChange:updateCaptureState,
  });

  function selectFrames() {
    const maxFrames = Math.max(1, Math.min(5, Number($("visualFrameLimit").value || 4)));
    state.selected = state.session.selectFrames({
      fromMs:Math.max(0, performance.now() - 15000),
      toMs:performance.now(),
      maxFrames,
    });
    renderFrames();
    $("visionResult").textContent = state.selected.length
      ? `已选择 ${state.selected.length} 张关键帧，可以执行 Vision Test。`
      : "没有可用关键帧；先开启摄像头/屏幕并等待一两秒。";
    return state.selected;
  }

  async function runVision() {
    const button = $("runVision");
    const frames = state.selected.length ? state.selected : selectFrames();
    if (!frames.length) return;
    button.disabled = true;
    $("visionLatency").textContent = "推理中...";
    $("visionReply").textContent = "Vision 推理中...";
    $("visionResult").textContent = "Vision 推理中...";
    try {
      const response = await fetch("/v1/dev/vision", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          prompt:$("visionPrompt").value,
          system_prompt:$("visionSystem").value,
          image_data_urls:frames.map(item => item.data_url),
        }),
      });
      const text = await response.text();
      let data;
      try { data = text ? JSON.parse(text) : {}; } catch { data = {text}; }
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : pretty(data.detail || data));
      // The model's answer stays on screen; the raw response body is the
      // collapsed debug block, so a JSON-shaped reply cannot flood the card.
      $("visionLatency").textContent = `${data.total_ms} ms · ${data.model}`;
      $("visionReply").textContent = data.reply || "(响应里没有 reply 字段)";
      $("visionResult").textContent = pretty(data);
    } catch (error) {
      $("visionLatency").textContent = `ERROR: ${error.message}`;
      $("visionReply").textContent = `ERROR: ${error.message}`;
      $("visionResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  $("visualCamera").addEventListener("click", () => state.session.startCamera().catch(error => {
    $("visionResult").textContent = `Camera ERROR: ${error.message}`;
  }));
  $("visualScreen").addEventListener("click", () => state.session.startDisplay().catch(error => {
    if (error?.name !== "NotAllowedError") $("visionResult").textContent = `Display ERROR: ${error.message}`;
  }));
  $("visualStop").addEventListener("click", () => state.session.stop({clearCandidates:false}));
  $("visualSample").addEventListener("click", () => { state.session.sample(); selectFrames(); });
  $("visualSelect").addEventListener("click", selectFrames);
  $("runVision").addEventListener("click", runVision);
  $("visualFrameLimit").addEventListener("change", selectFrames);
  window.addEventListener("beforeunload", () => state.session.stop({clearCandidates:true}));

  renderFrames();
  updateCaptureState(state.session.getState());
})();
