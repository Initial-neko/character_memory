(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before message_content.js");

  // Voice messages always carry canonical text. Unlike WeChat-style
  // transcription-on-demand, Character Memory shows that text immediately
  // below every voice bubble so listening is optional, not required for access.
  // app.js still owns the shared audio player and its click binding, but this
  // is the only definition of CM.voiceMessageHtml. Nothing may define a second
  // renderer for the same name: which one won would depend on script order
  // alone, so a duplicate in app.js would silently restore the
  // reveal-on-click transcript whenever the load order changed.
  CM.voiceMessageHtml = message => {
    if (message.action !== "VOICE_MESSAGE") return "";
    const status = message.voice_status || "pending";
    const durationMs = Number(message.voice_duration_ms || 0);
    const seconds = durationMs > 0 ? Math.max(1, Math.round(durationMs / 1000)) : 0;
    const width = Math.min(260, 92 + Math.min(seconds || 1, 34) * 5);
    const mediaUrl = message.voice_media_id
      ? `/v1/media/${encodeURIComponent(message.voice_media_id)}`
      : "";
    const transcript = `<div class="voice-transcript voice-transcript-always" data-voice-transcript>${CM.escapeHtml(message.content || "")}</div>`;

    if (status === "failed") {
      // The stored cause is a diagnostic, not something the character said. It
      // arrives as the English exception sentence (with the sidecar URL still
      // in it), and it used to be painted as a second grey pill the same width
      // as the bubble above it, so the provider's error looked like a second
      // message. It now lives behind a Chinese summary, in a line that is
      // visibly not a bubble.
      const why = String(message.voice_error || "").trim();
      return `<div class="voice-message voice-failed"><div class="voice-bubble voice-disabled">⚠ 语音生成失败</div>${why ? `<details class="voice-error"><summary>技术细节</summary><code>${CM.escapeHtml(why)}</code></details>` : ""}${transcript}</div>`;
    }
    if (status !== "ready" || !mediaUrl) {
      return `<div class="voice-message voice-pending"><div class="voice-bubble voice-disabled"><span class="voice-glyph">)))</span><span>语音生成中…</span></div>${transcript}</div>`;
    }
    return `<div class="voice-message" data-voice-message="${CM.escapeHtml(message.id)}">
      <button class="voice-bubble" type="button" data-voice-play data-audio-url="${CM.escapeHtml(mediaUrl)}" style="--voice-width:${width}px" aria-label="播放语音消息">
        <span class="voice-glyph" aria-hidden="true">)))</span><span class="voice-duration">${seconds || "?"}"</span>
      </button>
      ${transcript}
    </div>`;
  };

  // Group bubbles size images and stickers differently from direct chat, and
  // p0_11.css keeps that sizing on group-only classes. The caller has to say
  // which surface it is rendering for; without the hook those rules are dead
  // and a group image falls back to the direct-chat width.
  CM.messageContentHtml = (message, options) => {
    const isGroup = (options && options.variant) === "group";
    const stickerClass = isGroup ? ' class="group-message-sticker"' : "";
    const imageClass = isGroup ? ' class="group-message-image"' : "";
    const sticker = message.sticker || (
      message.sticker_id
        ? {
            id: message.sticker_id,
            label: message.sticker_label || "表情包",
            url: `/v1/stickers/${encodeURIComponent(message.sticker_id)}/asset`,
          }
        : null
    );
    const image = message.image || null;
    const isVoiceMessage = message.action === "VOICE_MESSAGE";
    const hideStoredResourceText =
      isVoiceMessage ||
      (message.action === "STICKER" && sticker) ||
      (message.action === "IMAGE" && image);
    const text = hideStoredResourceText ? "" : String(message.content || "").trim();
    const textHtml = text ? `<div class="bubble">${CM.escapeHtml(text)}</div>` : "";
    const stickerHtml = sticker?.url
      ? `<div class="sticker-bubble"><img${stickerClass} src="${CM.escapeHtml(sticker.url)}" alt="${CM.escapeHtml(sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>`
      : "";
    // The caption is a caption, not a header: an uploaded photo carries no
    // label at all (its file name is not a description of it), and a bubble
    // whose only text would be the filename it arrived under shows none.
    const imageCaption = String(image?.label || "").trim();
    const imageHtml = image?.url
      ? `<div class="image-bubble"><img${imageClass} src="${CM.escapeHtml(image.url)}" alt="${CM.escapeHtml(imageCaption || "图片")}" loading="lazy">${imageCaption ? `<div class="image-caption">${CM.escapeHtml(imageCaption)}</div>` : ""}</div>`
      : "";
    const voiceHtml = CM.voiceMessageHtml(message);
    return `${textHtml}${stickerHtml}${imageHtml}${voiceHtml}`;
  };

  CM.bindMessageContent = row => {
    row.querySelectorAll(".sticker-bubble img").forEach(img => {
      img.addEventListener(
        "error",
        () => img.closest(".sticker-bubble")?.classList.add("broken"),
        {once:true},
      );
    });
    CM.bindVoiceMessage(row);
  };
})();
