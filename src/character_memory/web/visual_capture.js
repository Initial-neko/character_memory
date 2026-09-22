(() => {
  const DEFAULTS = {
    sampleMs: 800,
    maxCandidates: 18,
    maxSide: 512,
    analysisWidth: 64,
    jpegQuality: 0.72,
    forceEveryMs: 4000,
    changeThreshold: 0.035,
  };

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function grayscale(imageData) {
    const src = imageData.data;
    const out = new Uint8Array(imageData.width * imageData.height);
    let target = 0;
    for (let i = 0; i < src.length; i += 4) {
      out[target++] = Math.round(src[i] * 0.299 + src[i + 1] * 0.587 + src[i + 2] * 0.114);
    }
    return out;
  }

  function difference(previous, current) {
    if (!previous || previous.length !== current.length) return 1;
    let total = 0;
    for (let i = 0; i < current.length; i += 1) total += Math.abs(current[i] - previous[i]);
    return total / Math.max(1, current.length * 255);
  }

  function createSession(options = {}) {
    const config = {...DEFAULTS, ...options};
    const preview = options.preview || null;
    const status = options.status || null;
    const onStateChange = typeof options.onStateChange === "function" ? options.onStateChange : () => {};
    const analysisCanvas = document.createElement("canvas");
    const analysisContext = analysisCanvas.getContext("2d", {willReadFrequently:true});
    const frameCanvas = document.createElement("canvas");
    const frameContext = frameCanvas.getContext("2d");
    const state = {
      source: null,
      stream: null,
      candidates: [],
      timer: null,
      lastAnalysis: null,
      lastAcceptedAt: 0,
      startedAt: 0,
      lastError: "",
    };
    // Every acquisition (camera or screen) takes a ticket from one counter, and a stream may
    // only be attached by a ticket newer than the one that owns the session. stop() takes a
    // fresh ticket for itself, which retires every answer the browser still owes us; a start
    // that succeeds claims the ticket it came in with, which retires the requests queued
    // behind it. A permission prompt can be answered long after the user gave up on it --
    // possibly after the call itself ended -- so whoever comes back has to prove, on the
    // ticket it holds, that it is still wanted.
    let requestSeq = 0;
    let ownerSeq = 0;

    function snapshot() {
      return {
        source: state.source,
        active: Boolean(state.stream),
        candidateCount: state.candidates.length,
        startedAt: state.startedAt,
        lastError: state.lastError,
      };
    }

    function notify(message = "") {
      if (status && message) status.textContent = message;
      onStateChange(snapshot());
    }

    function attachPreview(stream) {
      if (!preview) return;
      preview.muted = true;
      preview.autoplay = true;
      preview.playsInline = true;
      preview.srcObject = stream;
      preview.play().catch(() => {});
    }

    function clearTimer() {
      if (state.timer != null) {
        clearInterval(state.timer);
        state.timer = null;
      }
    }

    function claimTicket() {
      requestSeq += 1;
      return requestSeq;
    }

    function releaseStream(stream) {
      // A held stream is real hardware: an acquisition that lost its ticket still has to
      // hand the camera or screen back, or its indicator stays lit with nothing left in
      // the page that could switch it off.
      stream?.getTracks?.().forEach(track => track.stop());
    }

    function stopTracks() {
      releaseStream(state.stream);
      state.stream = null;
      if (preview) {
        try { preview.pause(); } catch (_) {}
        preview.srcObject = null;
      }
    }

    function resetAnalysis({clearCandidates = true} = {}) {
      state.lastAnalysis = null;
      state.lastAcceptedAt = 0;
      if (clearCandidates) state.candidates = [];
    }

    function resetSession({clearCandidates = false} = {}) {
      clearTimer();
      stopTracks();
      state.source = null;
      state.startedAt = 0;
      resetAnalysis({clearCandidates});
    }

    function stop({clearCandidates = false, reason = "视觉已关闭"} = {}) {
      ownerSeq = claimTicket(); // whatever the browser is still asking permission for is void now
      resetSession({clearCandidates});
      notify(reason);
    }

    function frameDimensions(videoWidth, videoHeight, maxSide) {
      const maxDimension = Math.max(videoWidth, videoHeight);
      const scale = maxDimension > maxSide ? maxSide / maxDimension : 1;
      return {
        width: Math.max(1, Math.round(videoWidth * scale)),
        height: Math.max(1, Math.round(videoHeight * scale)),
      };
    }

    function sample() {
      if (!state.stream || !preview || preview.readyState < 2 || !preview.videoWidth || !preview.videoHeight) return;
      const analysisWidth = config.analysisWidth;
      const analysisHeight = Math.max(1, Math.round(analysisWidth * preview.videoHeight / preview.videoWidth));
      analysisCanvas.width = analysisWidth;
      analysisCanvas.height = analysisHeight;
      analysisContext.drawImage(preview, 0, 0, analysisWidth, analysisHeight);
      const current = grayscale(analysisContext.getImageData(0, 0, analysisWidth, analysisHeight));
      const score = difference(state.lastAnalysis, current);
      state.lastAnalysis = current;

      const now = performance.now();
      const shouldKeep = state.candidates.length === 0 || score >= config.changeThreshold || now - state.lastAcceptedAt >= config.forceEveryMs;
      if (!shouldKeep) return;

      const dims = frameDimensions(preview.videoWidth, preview.videoHeight, config.maxSide);
      frameCanvas.width = dims.width;
      frameCanvas.height = dims.height;
      frameContext.drawImage(preview, 0, 0, dims.width, dims.height);
      const dataUrl = frameCanvas.toDataURL("image/jpeg", clamp(config.jpegQuality, 0.45, 0.92));
      state.candidates.push({
        capturedAt: now,
        wallTime: Date.now(),
        dataUrl,
        source: state.source,
        score,
        width: dims.width,
        height: dims.height,
      });
      while (state.candidates.length > config.maxCandidates) state.candidates.shift();
      state.lastAcceptedAt = now;
      notify(`${state.source === "CAMERA" ? "摄像头" : "屏幕"} · 已缓存 ${state.candidates.length} 个候选帧`);
    }

    async function startStream(source, stream, ticket) {
      if (ticket <= ownerSeq) {
        // The user hung up -- or aimed at another source -- while the browser still held the
        // permission prompt, and only now answered it. This stream is live hardware that no
        // session owns any more: release it, and never touch the preview.
        releaseStream(stream);
        return snapshot();
      }
      ownerSeq = ticket;
      resetSession({clearCandidates:true});
      notify("正在切换视觉来源…");
      state.source = source;
      state.stream = stream;
      state.startedAt = performance.now();
      state.lastError = "";
      attachPreview(stream);
      const track = stream.getVideoTracks?.()[0];
      if (track) {
        track.addEventListener("ended", () => {
          if (state.stream === stream) stop({clearCandidates:false, reason:"共享已由浏览器结束"});
        }, {once:true});
      }
      state.timer = setInterval(sample, config.sampleMs);
      setTimeout(() => { if (state.stream === stream) sample(); }, 180);
      notify(source === "CAMERA" ? "前置摄像头已开启" : "屏幕共享已开启");
      return snapshot();
    }

    async function startCamera() {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("当前浏览器不支持摄像头采集");
      const ticket = claimTicket();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video:{facingMode:{ideal:"user"}, width:{ideal:1280}, height:{ideal:720}},
          audio:false,
        });
        return await startStream("CAMERA", stream, ticket);
      } catch (error) {
        state.lastError = error?.message || String(error);
        notify(`摄像头失败：${state.lastError}`);
        throw error;
      }
    }

    async function startDisplay() {
      if (!navigator.mediaDevices?.getDisplayMedia) throw new Error("当前浏览器不支持屏幕共享");
      const ticket = claimTicket();
      try {
        const stream = await navigator.mediaDevices.getDisplayMedia({
          video:{displaySurface:"window", frameRate:{ideal:5, max:10}},
          audio:false,
        });
        return await startStream("DISPLAY", stream, ticket);
      } catch (error) {
        state.lastError = error?.message || String(error);
        notify(`屏幕共享失败：${state.lastError}`);
        throw error;
      }
    }

    function selectFrames({fromMs = 0, toMs = performance.now(), maxFrames = 4} = {}) {
      const limit = clamp(Math.floor(maxFrames || 4), 1, 5);
      const recentFallbackFloor = Math.max(0, toMs - 15000);
      let pool = state.candidates.filter(item => item.capturedAt >= fromMs && item.capturedAt <= toMs);
      if (!pool.length) pool = state.candidates.filter(item => item.capturedAt >= recentFallbackFloor && item.capturedAt <= toMs);
      if (!pool.length) return [];

      const selected = [];
      const add = (item) => {
        if (!item || selected.includes(item) || selected.length >= limit) return;
        if (selected.some(other => Math.abs(other.capturedAt - item.capturedAt) < 500)) return;
        selected.push(item);
      };
      add(pool[0]);
      if (pool.length > 1) add(pool[pool.length - 1]);
      for (const item of [...pool].sort((a, b) => b.score - a.score)) add(item);
      if (selected.length < limit) {
        const step = Math.max(1, Math.floor(pool.length / limit));
        for (let i = step; i < pool.length && selected.length < limit; i += step) add(pool[i]);
      }

      return selected
        .sort((a, b) => a.capturedAt - b.capturedAt)
        .slice(0, limit)
        .map((item, index) => ({
          filename:`visual-${item.source.toLowerCase()}-${item.wallTime}-${index + 1}.jpg`,
          data_url:item.dataUrl,
          source:item.source,
          captured_at_ms:Math.round(item.capturedAt),
        }));
    }

    function clearCandidates() {
      state.candidates = [];
      state.lastAcceptedAt = 0;
      notify(state.source ? `${state.source === "CAMERA" ? "摄像头" : "屏幕"} · 候选帧已清空` : "视觉候选帧已清空");
    }

    return {
      startCamera,
      startDisplay,
      stop,
      sample,
      selectFrames,
      clearCandidates,
      getState:snapshot,
      state,
    };
  }

  window.VisualCapture = {createSession};
})();
