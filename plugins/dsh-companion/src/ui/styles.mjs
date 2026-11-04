/* Styles are injected once by the overlay; every selector is namespaced. */
export const css = `
.dsh-companion-root{font:14px/1.5 system-ui,sans-serif;color:var(--dsw-alias-label-primary,#36333f)}
.dsh-companion-root button{font:inherit;cursor:pointer;border:1px solid #dbd8e5;border-radius:10px;background:var(--dsw-alias-bg-layer-1,#fff);color:inherit;padding:7px 11px;transition:background .15s,border-color .15s}
.dsh-companion-root button:hover:not(:disabled){background:#f6f3ff}
.dsh-companion-root button:disabled{opacity:.45;cursor:default}
.dsh-companion-root .dsh-companion-primary{background:#6d7cf6;border-color:transparent;color:#fff}
.dsh-companion-root .dsh-companion-primary:hover:not(:disabled){background:#5c6bef}
.dsh-companion-trigger{border-radius:16px!important;padding:5px 10px!important}
.dsh-companion-menu{position:fixed;z-index:70;width:200px;padding:6px;display:grid;gap:2px;background:var(--dsw-alias-bg-layer-1,#fffcf8);border:1px solid #dfdbe7;border-radius:14px;box-shadow:0 10px 28px #2520301f}
.dsh-companion-menu button{text-align:left;border:0;border-radius:9px;padding:8px 10px}
.dsh-companion-capsule{position:fixed;top:72px;right:24px;z-index:70;display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:30px;background:var(--dsw-alias-bg-layer-1,#fbf9ff);border:1px solid #dfdbe7;box-shadow:0 8px 24px #25203018}
.dsh-companion-capsule strong{max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.dsh-companion-time{font-variant-numeric:tabular-nums;opacity:.6;min-width:34px;text-align:right}
.dsh-companion-dot{width:8px;height:8px;border-radius:50%;background:#a9a3b8;flex:none}
.dsh-companion-dot.listening{background:#4f9a72;animation:dsh-companion-pulse 1.6s infinite}
.dsh-companion-dot.thinking{background:#c9912f;animation:dsh-companion-blink .9s infinite}
.dsh-companion-dot.speaking{background:#6d7cf6;animation:dsh-companion-pulse 1.1s infinite}
.dsh-companion-activity{font-size:12px;color:#7b7490;min-width:26px}
@keyframes dsh-companion-pulse{0%{box-shadow:0 0 0 0 #6d7cf655}70%{box-shadow:0 0 0 6px #6d7cf600}100%{box-shadow:0 0 0 0 #6d7cf600}}
@keyframes dsh-companion-blink{50%{opacity:.35}}
.dsh-companion-panel{position:fixed;top:128px;right:22px;z-index:70;width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 160px);overflow:auto;padding:18px;background:var(--dsw-alias-bg-layer-1,#fffcf8);border:1px solid #dfdbe7;border-radius:16px;box-shadow:0 10px 30px #2520301f}
.dsh-companion-panel h2{font-size:17px;margin:0}
.dsh-companion-panel header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.dsh-companion-panel p{margin:8px 0}
.dsh-companion-root label{display:block;font-size:13px;margin:12px 0}
.dsh-companion-root input:not([type=checkbox]),.dsh-companion-root select{display:block;width:100%;box-sizing:border-box;margin-top:4px;border:1px solid #cec9da;background:var(--dsw-alias-bg-base,#fff);color:inherit;border-radius:8px;padding:8px;font:inherit}
.dsh-companion-root small{display:block;opacity:.65;font-size:12px;margin-top:3px}
.dsh-companion-root footer{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.dsh-companion-panel img{width:100%;max-height:240px;object-fit:contain;border-radius:10px;margin-top:8px}
.dsh-companion-group{margin:16px 0 2px;font-size:12px;color:#8b8598;letter-spacing:.04em}
.dsh-companion-group:first-of-type{margin-top:4px}
.dsh-companion-config{border:0;padding:0;margin:0}
.dsh-companion-status{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:12px;font-size:13px;color:#6b6579}
.dsh-companion-note{color:#8b8598;font-size:12px;margin-top:10px}
.dsh-companion-root .end{background:#b64d61;color:#fff;border-color:transparent}
.dsh-companion-root .end:hover:not(:disabled){background:#a54457}
.dsh-companion-stage{position:relative;height:300px;margin:4px 0 12px;border-radius:14px;overflow:hidden;background:radial-gradient(circle at 50% 30%,#17253c,#0b1220 70%);border:1px solid #26374f}
.dsh-companion-stage-host{position:absolute;inset:0}
.dsh-companion-stage-host canvas{display:block;width:100%;height:100%}
.dsh-companion-stage-badge{position:absolute;left:10px;top:10px;padding:3px 9px;border-radius:999px;border:1px solid #3b567a;background:#0f1b2dcc;color:#cfe0f5;font-size:12px;cursor:pointer}
.dsh-companion-stage-badge:hover{background:#16263d}
.dsh-companion-stage-note{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;margin:0;padding:16px;text-align:center;color:#9fb3cc;font-size:13px}
.dsh-companion-stage-fps{position:absolute;right:10px;bottom:8px;color:#5f7592;font-size:11px}
`;
