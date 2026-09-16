(() => {
  const CM = window.CM;
  if (!CM) return;

  const MEDIA_BASE_KEY = "character-memory:media-base-url";

  function isTailscaleServePage(locationLike = window.location) {
    const hostname = String(locationLike?.hostname || "").toLowerCase();
    return locationLike?.protocol === "https:" && hostname.endsWith(".ts.net");
  }

  function defaultMediaBase(locationLike = window.location) {
    if (isTailscaleServePage(locationLike)) {
      return `https://${locationLike.hostname}:8443`;
    }
    return "http://127.0.0.1:8001";
  }

  function mediaBase() {
    const explicit = String(localStorage.getItem(MEDIA_BASE_KEY) || "").trim();
    return (explicit || defaultMediaBase()).replace(/\/+$/, "");
  }

  const tailscalePage = isTailscaleServePage();

  // voice.js and dictation.js already honor this key. Seed it only for the
  // documented Tailscale Serve origin, preserving any explicit operator override.
  if (tailscalePage && !String(localStorage.getItem(MEDIA_BASE_KEY) || "").trim()) {
    localStorage.setItem(MEDIA_BASE_KEY, defaultMediaBase());
  }

  // Settings Center remains PC-local in V1. A remote Settings link would point
  // to the phone/tablet's own loopback interface, so never show it on Serve.
  if (tailscalePage) document.getElementById("settingsLink")?.classList.add("hidden");

  if (!navigator.mediaDevices?.getDisplayMedia) {
    document.getElementById("voiceScreenButton")?.classList.add("hidden");
  }

  CM.mobileAccess = {
    mediaBase,
    defaultMediaBase,
    isTailscaleServePage,
    mediaBaseKey: MEDIA_BASE_KEY,
  };
})();
