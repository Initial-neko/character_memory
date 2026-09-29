(() => {
  const WORKLET_URL = "/static/asr_pcm_worklet.js";
  const PROCESSOR_NAME = "character-memory-asr-capture";
  const loadedContexts = new WeakSet();

  function websocketUrl(mediaBase) {
    const base = new URL(String(mediaBase || window.location.origin), window.location.href);
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    base.pathname = `${base.pathname.replace(/\/$/, "")}/v1/asr/stream`;
    base.search = "";
    base.hash = "";
    return base.toString();
  }

  function createSession(options = {}) {
    const source = String(options.source || "unknown");
    const onPartial = typeof options.onPartial === "function" ? options.onPartial : () => {};
    const onFinal = typeof options.onFinal === "function" ? options.onFinal : () => {};
    const onError = typeof options.onError === "function" ? options.onError : () => {};
    const onStateChange = typeof options.onStateChange === "function" ? options.onStateChange : () => {};
    const workletUrl = String(options.workletUrl || WORKLET_URL);

    let socket = null;
    let ready = false;
    let closed = false;
    let audioContext = null;
    let sourceNode = null;
    let workletNode = null;
    let silentGain = null;
    let readyPromise = null;

    function snapshot() {
      return {
        ready,
        closed,
        connected: Boolean(socket && socket.readyState === WebSocket.OPEN),
        attached: Boolean(workletNode),
      };
    }

    function notify() {
      onStateChange(snapshot());
    }

    function fail(error) {
      const value = error instanceof Error ? error : new Error(String(error || "ASR stream failed"));
      onError(value);
      return value;
    }

    function connect() {
      if (closed) return Promise.reject(new Error("ASR stream is closed"));
      if (ready) return Promise.resolve(snapshot());
      if (readyPromise) return readyPromise;
      if (!window.WebSocket) return Promise.reject(new Error("当前浏览器不支持 WebSocket ASR"));

      readyPromise = new Promise((resolve, reject) => {
        let settled = false;
        socket = new WebSocket(websocketUrl(options.mediaBase));
        socket.binaryType = "arraybuffer";

        socket.addEventListener("open", () => {
          socket.send(JSON.stringify({op:"start", source}));
          notify();
        });
        socket.addEventListener("message", event => {
          let data;
          try {
            data = JSON.parse(String(event.data || "{}"));
          } catch (error) {
            fail(error);
            return;
          }
          if (data.kind === "ready") {
            ready = true;
            notify();
            if (!settled) {
              settled = true;
              resolve(snapshot());
            }
            return;
          }
          if (data.kind === "partial") {
            onPartial(data);
            return;
          }
          if (data.kind === "final") {
            onFinal(data);
            return;
          }
          if (data.kind === "error") {
            const error = fail(new Error(data.detail || data.code || "ASR stream failed"));
            if (!settled) {
              settled = true;
              reject(error);
            }
          }
        });
        socket.addEventListener("error", () => {
          const error = fail(new Error("ASR streaming connection failed"));
          if (!settled) {
            settled = true;
            reject(error);
          }
        });
        socket.addEventListener("close", () => {
          ready = false;
          notify();
          if (!closed && !settled) {
            settled = true;
            reject(fail(new Error("ASR streaming connection closed before ready")));
          }
        });
      });
      return readyPromise;
    }

    async function attach(context, inputNode) {
      if (closed) throw new Error("ASR stream is closed");
      if (!context?.audioWorklet || !window.AudioWorkletNode) {
        throw new Error("当前浏览器不支持 AudioWorklet");
      }
      await connect();
      if (!loadedContexts.has(context)) {
        await context.audioWorklet.addModule(workletUrl);
        loadedContexts.add(context);
      }
      detach();
      audioContext = context;
      sourceNode = inputNode;
      workletNode = new AudioWorkletNode(context, PROCESSOR_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {targetSampleRate:16000},
      });
      workletNode.port.onmessage = event => {
        if (!ready || closed || !(event.data instanceof ArrayBuffer)) return;
        if (socket?.readyState === WebSocket.OPEN) socket.send(event.data);
      };
      silentGain = context.createGain();
      silentGain.gain.value = 0;
      sourceNode.connect(workletNode);
      workletNode.connect(silentGain);
      silentGain.connect(context.destination);
      notify();
      return snapshot();
    }

    function detach() {
      try { sourceNode?.disconnect?.(workletNode); } catch (_) {}
      try { workletNode?.disconnect?.(); } catch (_) {}
      try { silentGain?.disconnect?.(); } catch (_) {}
      if (workletNode?.port) workletNode.port.onmessage = null;
      sourceNode = null;
      workletNode = null;
      silentGain = null;
      audioContext = null;
      notify();
    }

    function command(op, extra = {}) {
      if (!ready || !socket || socket.readyState !== WebSocket.OPEN) return false;
      socket.send(JSON.stringify({op, ...extra}));
      return true;
    }

    function flush(reason = "user_stop") {
      return command("flush", {reason});
    }

    function cancel() {
      command("cancel");
      close();
    }

    function close() {
      if (closed) return;
      closed = true;
      ready = false;
      detach();
      try { socket?.close?.(1000, "client_close"); } catch (_) {}
      socket = null;
      notify();
    }

    return {
      connect,
      attach,
      detach,
      flush,
      cancel,
      close,
      getState:snapshot,
    };
  }

  window.StreamingAsr = {
    createSession,
    websocketUrl,
    WORKLET_URL,
    PROCESSOR_NAME,
  };
})();
