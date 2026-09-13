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
    document.querySelectorAll("[data-setting]").forEach(input => {
      const name = input.dataset.setting;
      if (input.type === "checkbox") values[name] = input.checked;
      else if (input.type === "number") values[name] = input.value === "" ? null : Number(input.value);
      else values[name] = input.value;
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
      if (result.changed) {
        showNotice(`配置已保存。Backup: ${result.backup || "首次创建，无旧文件"}\n需要重启 stack 才会由各 Runtime 重新加载。`);
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
