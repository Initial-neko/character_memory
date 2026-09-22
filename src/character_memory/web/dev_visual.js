(() => {
  const $ = id => document.getElementById(id);
  const state = { lastImage: null, lastPrompt: "", providers: [] };

  function pretty(value) { return JSON.stringify(value, null, 2); }

  async function responseError(response) {
    const text = await response.text();
    let payload;
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = {text}; }
    const detail = payload.detail || payload.text || `${response.status} ${response.statusText}`;
    return new Error(typeof detail === "string" ? detail : pretty(detail));
  }

  async function jsonFetch(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) throw await responseError(response);
    const text = await response.text();
    return text ? JSON.parse(text) : {};
  }

  function imageRequestPayload() {
    return {
      character_id: $("imageCharacter").value,
      provider: $("imageProvider").value,
      purpose: $("imagePurpose").value,
      visual_intent: $("imageIntent").value.trim(),
      use_avatar_reference: $("imageUseAvatar").checked,
    };
  }

  function renderPrompt(prompt) {
    state.lastPrompt = String(prompt || "").trim();
    $("imageRewriteResult").textContent = state.lastPrompt || "尚未润色 Prompt。";
    $("copyImagePrompt").hidden = !state.lastPrompt;
  }

  function renderProviderStatus(data) {
    state.providers = data.providers || [];
    $("imageProviderStatus").textContent = pretty({default:data.default, providers:state.providers});
    const select = $("imageProvider");
    const previous = select.value;
    select.replaceChildren(...state.providers.map(item => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.id} · ${item.model || "default"}${item.available ? "" : item.configured ? " · dependency unavailable" : " · no key"}`;
      option.disabled = !item.available;
      return option;
    }));
    const preferred = state.providers.find(item => item.id === (previous || data.default) && item.available)
      || state.providers.find(item => item.id === data.default && item.available)
      || state.providers.find(item => item.available);
    if (preferred) select.value = preferred.id;
  }

  async function refreshProviders() {
    $("imageProviderStatus").textContent = "正在读取 Character Runtime Provider 状态...";
    try {
      renderProviderStatus(await jsonFetch("/v1/dev/visual/providers"));
    } catch (error) {
      $("imageProviderStatus").textContent = `ERROR: ${error.message}`;
    }
  }

  async function refreshCharacters() {
    try {
      const data = await jsonFetch("/v1/dev/characters");
      const select = $("imageCharacter");
      const previous = select.value;
      const characters = data.characters || [];
      if (!characters.length) return;
      select.replaceChildren(...characters.map(item => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.name || item.id} · ${item.id}`;
        return option;
      }));
      if (characters.some(item => item.id === previous)) select.value = previous;
    } catch (error) {
      console.warn("Dev ImageGen character list unavailable", error);
    }
  }

  async function rewriteImagePrompt() {
    const button = $("rewriteImagePrompt");
    button.disabled = true;
    $("imageRewriteResult").textContent = "AI 正在润色绘图 Prompt...";
    try {
      const data = await jsonFetch("/v1/dev/imagegen/rewrite", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(imageRequestPayload()),
      });
      renderPrompt(data.prompt);
      $("imageGenLatency").textContent = `${data.duration_ms ?? "-"} ms · rewrite`;
    } catch (error) {
      $("imageRewriteResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function runImageGen() {
    const button = $("runImageGen");
    button.disabled = true;
    $("rewriteImagePrompt").disabled = true;
    $("useImageAsAvatar").hidden = true;
    $("imageGenPreview").hidden = true;
    $("imageGenPreview").removeAttribute("src");
    $("imageGenLatency").textContent = "真实生成中...";
    $("imageGenResult").textContent = "真实生成中：AI Rewrite → Provider → MediaStorage ...";
    state.lastImage = null;
    try {
      const data = await jsonFetch("/v1/dev/imagegen", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(imageRequestPayload()),
      });
      state.lastImage = data.image || null;
      renderPrompt(data.prompt);
      // provider / model / duration stay outside the collapsed raw block.
      $("imageGenLatency").textContent = `${data.duration_ms ?? "-"} ms · ${data.provider || "-"} / ${data.model || "-"}`;
      $("imageGenResult").textContent = pretty(data);
      const previewUrl = data.image?.url || data.image?.data_url;
      if (previewUrl) {
        const preview = $("imageGenPreview");
        preview.src = previewUrl;
        preview.hidden = false;
      }
      if (data.image?.media_id) $("useImageAsAvatar").hidden = false;
    } catch (error) {
      $("imageGenLatency").textContent = `ERROR: ${error.message}`;
      $("imageGenResult").textContent = `ERROR: ${error.message}`;
    } finally {
      button.disabled = false;
      $("rewriteImagePrompt").disabled = false;
      refreshProviders();
    }
  }

  async function copyImagePrompt() {
    if (!state.lastPrompt) return;
    const button = $("copyImagePrompt");
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(state.lastPrompt);
      button.textContent = "已复制";
    } catch (error) {
      button.textContent = "复制失败";
      console.warn("copy image prompt failed", error);
    } finally {
      setTimeout(() => { button.textContent = original; }, 1200);
    }
  }

  async function useAsAvatar() {
    if (!state.lastImage?.media_id) return;
    const button = $("useImageAsAvatar");
    button.disabled = true;
    const old = button.textContent;
    button.textContent = "正在设置...";
    try {
      const data = await jsonFetch("/v1/dev/avatar-from-media", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          character_id: $("imageCharacter").value,
          media_id: state.lastImage.media_id,
        }),
      });
      button.textContent = "头像已更新";
      $("imageGenResult").textContent += `\n\nAvatar update:\n${pretty(data)}`;
    } catch (error) {
      button.textContent = `设置失败`;
      $("imageGenResult").textContent += `\n\nAVATAR ERROR: ${error.message}`;
    } finally {
      setTimeout(() => { button.disabled = false; button.textContent = old; }, 1400);
    }
  }

  $("rewriteImagePrompt")?.addEventListener("click", rewriteImagePrompt);
  $("copyImagePrompt")?.addEventListener("click", copyImagePrompt);
  $("runImageGen")?.addEventListener("click", runImageGen);
  $("refreshImageProviders")?.addEventListener("click", refreshProviders);
  $("useImageAsAvatar")?.addEventListener("click", useAsAvatar);
  $("imagePurpose")?.addEventListener("change", () => {
    const purpose = $("imagePurpose").value;
    if (purpose === "SCENE") $("imageUseAvatar").checked = false;
    if (purpose === "SELFIE" || purpose === "AVATAR") $("imageUseAvatar").checked = true;
  });

  refreshCharacters();
  refreshProviders();
})();
