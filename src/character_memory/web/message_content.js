(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before message_content.js");

  CM.messageContentHtml = message => {
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
      ? `<div class="sticker-bubble"><img src="${CM.escapeHtml(sticker.url)}" alt="${CM.escapeHtml(sticker.label || "表情包")}" loading="lazy"><span class="sticker-fallback">表情</span></div>`
      : "";
    const imageHtml = image?.url
      ? `<div class="image-bubble"><img src="${CM.escapeHtml(image.url)}" alt="${CM.escapeHtml(image.label || "图片")}" loading="lazy"><div class="image-caption">${CM.escapeHtml(image.label || "图片")}</div></div>`
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
