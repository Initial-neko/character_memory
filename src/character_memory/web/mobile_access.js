(() => {
  const CM = window.CM;
  if (!CM) return;

  const MEDIA_BASE_KEY = "character-memory:media-base-url";
  const localHosts = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

  function isRemoteSecurePage(locationLike = window.location) {
    const hostname = String(locationLike?.hostname || "").toLowerCase();
    return locationLike?.protocol === "https:" && hostname && !localHosts.has(hostname);
  }

  function defaultMediaBase(locationLike = window.location) {
    if (isRemoteSecurePage(locationLike)) {
      return `https://${locationLike.hostname}:8443`;
    }
    return "http://127.0.0.1:8001";
  }

  function mediaBase() {
    const explicit = String(localStorage.getItem(MEDIA_BASE_KEY) || "").trim();
    return (explicit || defaultMediaBase()).replace(/\/+$/, "");
  }

  // voice.js and dictation.js already honor this key. Seed it only for a secure
  // remote origin, preserving any explicit operator override.
  if (isRemoteSecurePage() && !String(localStorage.getItem(MEDIA_BASE_KEY) || "").trim()) {
    localStorage.setItem(MEDIA_BASE_KEY, defaultMediaBase());
  }

  CM.mobileAccess = {
    mediaBase,
    defaultMediaBase,
    isRemoteSecurePage,
    mediaBaseKey: MEDIA_BASE_KEY,
  };
})();
