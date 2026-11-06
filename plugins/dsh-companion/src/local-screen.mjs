/** Renderer helpers for the loopback screen service (desktop fallback). */
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function healthy(url, fetchImpl) {
  try {
    const response = await fetchImpl(`${url}/health`, {cache: 'no-store', signal: AbortSignal.timeout(800)});
    return response.ok;
  } catch {
    return false;
  }
}

/** Ask the plugin host to start the local screen service, then wait for health. */
export async function ensureScreenServer({url, fetchImpl = globalThis.fetch, ensurePath = '/companion/api/screen/ensure'} = {}) {
  const base = String(url || 'http://127.0.0.1:50134').replace(/\/+$/, '');
  if (await healthy(base, fetchImpl)) return {url: base, started: false};
  const response = await fetchImpl(ensurePath, {method: 'POST'});
  if (!response.ok) throw Error(`截屏服务启动失败：HTTP ${response.status}`);
  const result = await response.json();
  if (result?.ok !== true) throw Error(result?.error || '截屏服务没有就绪');
  for (let i = 0; i < 10; i += 1) {
    if (await healthy(base, fetchImpl)) return {url: base, started: true};
    await sleep(200);
  }
  throw Error('截屏服务没有响应');
}

/** Grab one JPEG frame from the local service. */
export async function captureLocalScreen({url, maxEdge = 1280, quality = 0.8, fetchImpl = globalThis.fetch} = {}) {
  const base = String(url || 'http://127.0.0.1:50134').replace(/\/+$/, '');
  const query = `?maxEdge=${encodeURIComponent(maxEdge)}&quality=${encodeURIComponent(quality)}`;
  const response = await fetchImpl(`${base}/screen/frame${query}`, {cache: 'no-store'});
  if (!response.ok) throw Error(`截屏失败：HTTP ${response.status}`);
  const blob = await response.blob();
  if (!blob || blob.size === 0) throw Error('截屏返回空画面');
  return {
    blob,
    width: Number(response.headers.get('x-frame-width')) || 0,
    height: Number(response.headers.get('x-frame-height')) || 0,
    capturedAt: new Date().toISOString(),
    fingerprint: null,
  };
}
