(() => {
  const state = { snapshot: null };
  const sections = document.getElementById("settingsSections");
  const secretList = document.getElementById("secretList");
  const notice = document.getElementById("notice");
  const configPath = document.getElementById("configPath");

  function showNotice(message, error = false) {
    notice.textContent = message;
    notice.classList.toggle("error", error);
    notice.classList.remove("hidden");
  }

  function clearNotice() {
    notice.classList.add("hidden");
    notice.textContent = "";
  }

  async function jsonRequest(url, options = {}) {
    const response = await fetch(url, options);
    let payload = null;
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) {
      const detail = payload?.detail || payload || `${response.status} ${response.statusText}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function fieldInput(field, value) {
    let input;
    if (field.type === "select") {
      input = document.createElement("select");
      for (const option of field.options || []) {
        const el = document.createElement("option");
        el.value = option.value;
        el.textContent = option.label || option.value;
        el.disabled = Boolean(option.disabled);
        input.appendChild(el);
      }
      input.value = value == null ? "" : String(value);
    } else {
      input = document.createElement("input");
      input.type = field.type === "checkbox" ? "checkbox" : (field.type || "text");
      if (field.type === "checkbox") input.checked = Boolean(value);
      else input.value = value == null ? "" : String(value);
      for (const key of ["min", "max", "step", "placeholder"]) {
        if (field[key] != null) input[key] = field[key];
      }
    }
    input.dataset.setting = field.name;
    input.id = `setting-${field.name}`;
    return input;
  }

  function renderSections(snapshot) {
    sections.innerHTML = "";
    for (const section of snapshot.schema || []) {
      const card = document.createElement("article");
      card.className = "card settings-section";

      const heading = document.createElement("div");
      heading.className = "section-heading";
      heading.innerHTML = `<div><h2></h2><p></p></div>`;
      heading.querySelector("h2").textContent = section.title || section.id;
      heading.querySelector("p").textContent = section.description || "";
      card.appendChild(heading);

      const grid = document.createElement("div");
      grid.className = "field-grid";
      for (const field of section.fields || []) {
        const wrap = document.createElement("div");
        wrap.className = "field";
        const label = document.createElement("label");
        label.htmlFor = `setting-${field.name}`;
        label.textContent = field.label || field.name;
        const input = fieldInput(field, snapshot.values?.[field.name]);
        wrap.appendChild(label);
        if (field.type === "checkbox") {
          const row = document.createElement("div");
          row.className = "checkbox-row";
          row.appendChild(input);
          const copy = document.createElement("span");
          copy.textContent = input.checked ? "启用" : "关闭";
          input.addEventListener("change", () => { copy.textContent = input.checked ? "启用" : "关闭"; });
          row.appendChild(copy);
          wrap.appendChild(row);
        } else {
          wrap.appendChild(input);
        }
        grid.appendChild(wrap);
      }
      card.appendChild(grid);
      sections.appendChild(card);
    }
    wireTtsControls(snapshot);
  }

  function ttsProviderStatus(providerId) {
    return (state.snapshot?.tts?.providers || []).find(item => item.id === providerId) || null;
  }

  function wireTtsControls(snapshot) {
    const provider = document.getElementById("setting-tts_provider");
    const voice = document.getElementById("setting-tts_voice");
    const speed = document.getElementById("setting-tts_speed");
    const device = document.getElementById("setting-tts_device");
    if (!provider || !voice) return;

    const providerWrap = provider.closest(".field");
    let status = providerWrap?.querySelector(".tts-health-status");
    if (!status && providerWrap) {
      status = document.createElement("div");
      status.className = "subtle mono tts-health-status";
      providerWrap.appendChild(status);
    }

    const voiceCard = provider.closest(".settings-section");
    let previewRow = voiceCard?.querySelector(".tts-preview-row");
    let previewButton = previewRow?.querySelector("button");
    let previewAudio = previewRow?.querySelector("audio");
    let previewStatus = previewRow?.querySelector(".tts-preview-status");
    if (!previewRow && voiceCard) {
      previewRow = document.createElement("div");
      previewRow.className = "tts-preview-row";

      previewButton = document.createElement("button");
      previewButton.type = "button";
      previewButton.className = "secondary";
      previewButton.textContent = "测试当前 TTS";

      previewAudio = document.createElement("audio");
      previewAudio.controls = true;
      previewAudio.preload = "none";

      previewStatus = document.createElement("span");
      previewStatus.className = "subtle tts-preview-status";
      previewStatus.textContent = "使用当前 Provider / Voice 生成一句试听。";

      previewRow.append(previewButton, previewAudio, previewStatus);
      voiceCard.appendChild(previewRow);
    }

    function runtimeDevice(item) {
      const value = String(item?.device || "").trim().toLowerCase();
      if (value.startsWith("cuda")) return "cuda";
      if (value.startsWith("cpu")) return "cpu";
      return null;
    }

    function renderSelectedProvider(options = {}) {
      const providerChanged = Boolean(options.providerChanged);
      const item = ttsProviderStatus(provider.value);
      const previousVoice = voice.value;
      const configuredVoice = String(snapshot.values?.tts_voice || "");
      const voices = item?.voices?.length ? item.voices.map(String) : [];
      const defaultVoice = String(item?.default_voice || voices[0] || "");

      voice.innerHTML = "";
      for (const value of voices) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.disabled = !item?.ready;
        voice.appendChild(option);
      }

      let selectedVoice = "";
      if (providerChanged) selectedVoice = defaultVoice;
      else if (configuredVoice && voices.includes(configuredVoice)) selectedVoice = configuredVoice;
      else if (previousVoice && voices.includes(previousVoice)) selectedVoice = previousVoice;
      else selectedVoice = defaultVoice;
      if (selectedVoice) voice.value = selectedVoice;

      const correctedVoice = Boolean(
        item?.ready &&
        configuredVoice &&
        !voices.includes(configuredVoice) &&
        selectedVoice &&
        selectedVoice !== configuredVoice
      );

      voice.disabled = !item?.ready || voices.length === 0;
      if (speed) {
        speed.disabled = !item?.ready || item?.supports_speed === false;
        if (providerChanged && item?.supports_speed === false) speed.value = "1";
      }
      if (device) {
        const detected = runtimeDevice(item);
        const configuredDevice = String(snapshot.values?.tts_device || "").trim().toLowerCase();
        const cloud = String(item?.device || "").trim().toLowerCase() === "cloud";
        let cloudOption = device.querySelector('option[value="cloud"]');
        if (cloud && !cloudOption) {
          cloudOption = document.createElement("option");
          cloudOption.value = "cloud";
          cloudOption.textContent = "Cloud (Provider managed)";
          device.appendChild(cloudOption);
        } else if (!cloud && cloudOption) {
          cloudOption.remove();
        }
        device.disabled = !item?.ready || cloud;
        if (cloud) device.value = "cloud";
        else if (providerChanged && detected) device.value = detected;
        else if (["cpu", "cuda"].includes(configuredDevice)) device.value = configuredDevice;
        else if (detected) device.value = detected;

        const pendingDeviceRestart = Boolean(
          !cloud && detected && device.value && detected !== device.value && item?.device_hot_apply === false
        );
        device.title = cloud
          ? "该 Provider 使用云端服务，不使用本地 CPU/CUDA 设置。"
          : pendingDeviceRestart
            ? `当前 Runtime 仍在 ${detected.toUpperCase()}；保存后的 ${device.value.toUpperCase()} 需要重启对应 TTS Runtime。`
            : "";
      }
      if (previewButton) previewButton.disabled = !item?.ready || !selectedVoice;

      if (status) {
        if (!item) {
          status.textContent = snapshot.tts?.error
            ? "健康检查不可用：" + snapshot.tts.error
            : "当前 Provider 未通过健康检查。";
        } else if (item.ready) {
          const loaded = item.loaded ? "loaded" : "ready";
          const configuredDevice = String(snapshot.values?.tts_device || "").trim().toLowerCase();
          const detectedDevice = runtimeDevice(item);
          const devicePending = Boolean(
            item.device_hot_apply === false &&
            detectedDevice &&
            ["cpu", "cuda"].includes(configuredDevice) &&
            detectedDevice !== configuredDevice
          );
          status.textContent = "✓ " + loaded + " · runtime " + (item.device || "device unknown") + " · " + (item.model || item.id)
            + (devicePending ? " · configured " + configuredDevice.toUpperCase() + "（需重启对应 TTS Runtime）" : "")
            + (correctedVoice ? " · Voice 已自动切换为 " + selectedVoice + "（保存后写入配置）" : "")
            // The provider works, so this is a warning and not the ✗ line: only
            // a character with no voice of its own reaches the default template.
            + (item.default_template_problem ? " · ⚠ " + item.default_template_problem : "");
        } else {
          status.textContent = "✗ unavailable · " + (item.reason || "health check failed");
        }
      }
    }

    provider.addEventListener("change", () => renderSelectedProvider({providerChanged: true}));

    previewButton?.addEventListener("click", async () => {
      const item = ttsProviderStatus(provider.value);
      if (!item?.ready) return showNotice("当前 TTS Provider 未通过健康检查，不能试听。", true);
      previewButton.disabled = true;
      if (previewStatus) previewStatus.textContent = "正在生成试听...";
      try {
        const response = await fetch("/v1/tts-preview", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            provider: provider.value,
            voice: voice.value,
            speed: Number(speed?.value || 1),
            text: "你好，这是当前语音配置的试听。",
          }),
        });
        if (!response.ok) {
          let detail = response.status + " " + response.statusText;
          try {
            const payload = await response.json();
            detail = payload?.detail || detail;
          } catch (_) {}
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        const blob = await response.blob();
        if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
        state.previewUrl = URL.createObjectURL(blob);
        previewAudio.src = state.previewUrl;
        await previewAudio.play().catch(() => {});
        if (previewStatus) {
          const p = response.headers.get("x-tts-provider") || provider.value;
          const v = decodeURIComponent(response.headers.get("x-tts-voice") || voice.value);
          const d = response.headers.get("x-tts-device") || item.device || "";
          previewStatus.textContent = "✓ " + p + " · " + v + (d ? " · " + d : "");
        }
      } catch (error) {
        if (previewStatus) previewStatus.textContent = "试听失败：" + error.message;
        showNotice("TTS 试听失败：" + error.message, true);
      } finally {
        previewButton.disabled = !ttsProviderStatus(provider.value)?.ready;
      }
    });

    renderSelectedProvider();
  }

  function renderSecrets(snapshot) {
    secretList.innerHTML = "";
    for (const secret of snapshot.secrets || []) {
      const row = document.createElement("div");
      row.className = "secret-row";

      const meta = document.createElement("div");
      meta.className = "secret-meta";
      const status = secret.configured
        ? `已配置 · ${secret.source || "unknown"}${secret.stored_in_env && secret.source === "system" ? "（.env 作为 fallback）" : ""}`
        : "未配置";
      meta.innerHTML = `<strong></strong><span></span>`;
      meta.querySelector("strong").textContent = secret.label || secret.name;
      meta.querySelector("span").textContent = `${secret.name} · ${status}`;

      const inputWrap = document.createElement("div");
      inputWrap.className = "secret-input";
      const input = document.createElement("input");
      input.type = "password";
      input.autocomplete = "new-password";
      input.placeholder = secret.configured ? "输入新值以替换；现有值不会显示" : "输入 Secret";
      input.dataset.secret = secret.name;
      inputWrap.appendChild(input);

      const actions = document.createElement("div");
      actions.className = "secret-actions";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "secondary";
      save.textContent = "保存";
      save.addEventListener("click", async () => {
        const value = input.value.trim();
        if (!value) return showNotice(`${secret.name} 不能为空`, true);
        save.disabled = true;
        try {
          await jsonRequest(`/v1/settings/secrets/${encodeURIComponent(secret.name)}`, {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({value}),
          });
          input.value = "";
          await loadSettings();
          showNotice(`${secret.name} 已写入 .env。重启 stack 后其他进程生效。`);
        } catch (error) {
          showNotice(`保存 Secret 失败：${error.message}`, true);
        } finally {
          save.disabled = false;
        }
      });

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "删除 .env 值";
      remove.disabled = !secret.stored_in_env;
      remove.addEventListener("click", async () => {
        if (!confirm(`从 .env 删除 ${secret.name}？系统环境变量不会被修改。`)) return;
        remove.disabled = true;
        try {
          await jsonRequest(`/v1/settings/secrets/${encodeURIComponent(secret.name)}`, {method: "DELETE"});
          await loadSettings();
          showNotice(`${secret.name} 的 .env 值已删除。`);
        } catch (error) {
          showNotice(`删除 Secret 失败：${error.message}`, true);
        }
      });
      actions.append(save, remove);
      row.append(meta, inputWrap, actions);
      secretList.appendChild(row);
    }
  }

  function collectValues() {
    const values = {};
    const current = state.snapshot?.values || {};
    document.querySelectorAll("[data-setting]").forEach(input => {
      if (input.disabled) return;
      const name = input.dataset.setting;
      let value;
      if (input.type === "checkbox") value = input.checked;
      else if (input.type === "number") value = input.value === "" ? null : Number(input.value);
      else value = input.value;

      const existing = current[name];
      const equivalentBlank = value === "" && (existing === "" || existing === null || existing === undefined);
      if (!equivalentBlank && JSON.stringify(value) !== JSON.stringify(existing)) values[name] = value;
    });
    return values;
  }

  async function loadRuntimeStatus() {
    try {
      const data = await jsonRequest("/v1/runtime-status");
      for (const [name, status] of Object.entries(data || {})) {
        const badge = document.getElementById(`runtime-${name}`);
        if (!badge) continue;
        badge.classList.remove("unknown", "ok", "bad");
        badge.classList.add(status.ok ? "ok" : "bad");
        badge.textContent = status.ok ? `${Math.round(status.total_ms || 0)}ms` : "offline";
        badge.title = status.error || `HTTP ${status.status_code ?? "-"}`;
      }
    } catch (error) {
      console.warn("runtime status failed", error);
    }
  }

  async function loadSettings() {
    clearNotice();
    const snapshot = await jsonRequest("/v1/settings");
    state.snapshot = snapshot;
    configPath.textContent = `${snapshot.config_path} · secrets: ${snapshot.env_path}`;
    renderSections(snapshot);
    renderSecrets(snapshot);
    const migration = snapshot.last_migration;
    if (migration?.changed) {
      showNotice(`已自动迁移旧 config 中的 Secret：${(migration.migrated || []).map(item => item.secret).join(", ")}。旧配置已脱敏，备份：${migration.backup}`);
    }
    loadRuntimeStatus();
  }

  document.getElementById("saveSettings")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const payload = await jsonRequest("/v1/settings", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({values: collectValues()}),
      });
      state.snapshot = payload.settings;
      renderSections(payload.settings);
      renderSecrets(payload.settings);
      const result = payload.result || {};
      const runtimeApply = result.runtime_apply || {};
      if (runtimeApply.applied === false) {
        showNotice(
          `配置已持久化，但运行态应用失败：${runtimeApply.error || "unknown runtime error"}。无需重新保存；修复 Runtime 后可重新加载/切换。`,
          true,
        );
      } else if (result.changed) {
        const restart = result.restart_required || [];
        if (restart.length) {
          showNotice(`配置已保存。以下字段需要重启对应 Runtime 才完全生效：${restart.join(", ")}。无需重启整个 stack。`);
        } else {
          showNotice("配置已保存并已热生效，无需重启整个 stack。");
        }
      } else {
        showNotice("配置没有变化。", false);
      }
    } catch (error) {
      showNotice(`保存配置失败：${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("reloadSettings")?.addEventListener("click", () => {
    loadSettings().catch(error => showNotice(`读取配置失败：${error.message}`, true));
  });

  document.getElementById("migrateSecrets")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const payload = await jsonRequest("/v1/settings/migrate", {method: "POST"});
      state.snapshot = payload.settings;
      renderSections(payload.settings);
      renderSecrets(payload.settings);
      const result = payload.result || {};
      showNotice(result.changed ? `迁移完成：${(result.migrated || []).map(item => item.secret).join(", ")}` : "没有需要迁移的旧 Secret。");
    } catch (error) {
      showNotice(`迁移失败：${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  });

  loadSettings().catch(error => showNotice(`Settings Center 初始化失败：${error.message}`, true));
})();
