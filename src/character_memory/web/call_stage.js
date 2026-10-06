/* Display state only. Voice owns the session; VisualCapture owns consent/capture. */
(() => {
  const CM = window.CM;
  if (!CM) return;
  const card = document.querySelector('.voice-call-card');
  const title = document.getElementById('voiceStageTitle');
  const subtitle = document.getElementById('voiceStageSubtitle');
  const history = document.getElementById('voiceHistoryButton');
  const state = {live2d:false, visual:null, historyOpen:false};
  function getState() {
    const mode = state.visual === 'DISPLAY' ? 'SCREEN_SHARE' : state.visual === 'CAMERA' ? 'VIDEO' : state.live2d ? 'LIVE2D' : 'AVATAR';
    return {...state, mode, characterOverlay:state.live2d && Boolean(state.visual)};
  }
  function render() {
    const value = getState();
    if (card) card.dataset.stageMode = value.mode;
    card?.classList.toggle('stage-visual', Boolean(state.visual));
    card?.classList.toggle('stage-immersive', value.mode !== 'AVATAR');
    card?.classList.toggle('stage-character-overlay', value.characterOverlay);
    card?.classList.toggle('stage-history-open', state.historyOpen);
    history?.setAttribute('aria-expanded', String(state.historyOpen));
    if (title) title.textContent = {AVATAR:'当前对话', LIVE2D:'Live2D 模式', VIDEO:'视频通话', SCREEN_SHARE:'屏幕共享中'}[value.mode];
  }
  CM.callStage = {
    getState,
    setLive2d(value) { state.live2d = Boolean(value); render(); },
    setVisual(value) { state.visual = value?.active ? value.source : null; render(); },
    setHistory(value) { state.historyOpen = Boolean(value); render(); },
    setSubtitle(role, text) { if (subtitle) subtitle.textContent = role === 'user' ? '你：' + text : text; },
    // Stable renderer boundary for future native clients and motion orchestration.
    setCharacterState(value) { CM.live2d?.setPhase?.(value?.phase || 'idle'); },
    playAction(name) { return CM.live2d?.startMotion?.(name) ?? Promise.resolve(false); },
    setExpression(name) { return CM.live2d?.setExpression?.(name) ?? Promise.resolve(false); },
  };
  history?.addEventListener('click', () => CM.callStage.setHistory(!state.historyOpen));
  document.getElementById('voiceAvatarButton')?.addEventListener('click', () => {
    if (CM.live2d?.isEnabled?.()) CM.live2d.toggle();
  });
  document.getElementById('voiceStageOptionsButton')?.addEventListener('click', () => card?.classList.toggle('stage-options-open'));
  render();
})();
