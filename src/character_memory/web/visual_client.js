(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before visual_client.js");

  const baseDirectEventToMessage = CM.directEventToMessage;
  CM.directEventToMessage = raw => {
    const message = baseDirectEventToMessage(raw);
    const metadata = raw?.metadata || {};
    const mediaId = metadata.media_id || null;
    if (!mediaId || metadata.action !== "IMAGE") return message;
    message.media_id = mediaId;
    message.image_id = null;
    message.image = {
      id: mediaId,
      label: metadata.image_label || (metadata.generation_purpose === "SELFIE" ? "自拍" : "配图"),
      source: metadata.generated ? "GENERATED" : "MEDIA",
      url: `/v1/media/${encodeURIComponent(mediaId)}`,
    };
    return message;
  };

  CM.registerFeature("visualClient", {});
})();
