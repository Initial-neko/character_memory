/** Expanded call surface: same anchor side as the capsule, so it grows downward. */
export function createPanel({React, store, call, actions, Stage}) {
  const h = React.createElement;
  const button = (label, fn, options = {}) => h('button', {
    type: 'button',
    onClick: () => Promise.resolve().then(fn).catch(actions.reportError),
    ...options,
  }, label);

  return function Panel() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const snap = React.useSyncExternalStore(call.subscribe, call.getSnapshot);
    if (state.panel !== 'call' || snap.minimized || snap.phase === 'IDLE' || state.bigchat) return null;

    return h('section', {className: 'dsh-companion-panel', role: 'dialog', 'aria-label': '陪伴'},
      h('header', null,
        h('h2', null, state.mode === 'voice' ? '语音陪伴' : '屏幕陪伴'),
        button('收起', () => call.minimize())),
      h('p', null, snap.screen === 'ON' ? `正在看：${snap.screenLabel}` : '画面还没开'),
      state.mode === 'screen' && snap.screen === 'ON' && state.watch && h('p', {className: 'dsh-companion-note'},
        `自动看：每 ${Math.round(state.watch.intervalMs / 1000)} 秒，已看 ${state.watch.reads}/${state.watch.maxReads}`,
        button(state.watch.active ? '暂停自动看' : '继续自动看', () => actions.toggleWatch())),
      h('footer', null,
        button('选窗口', () => actions.chooseScreen(), {disabled: snap.phase !== 'ACTIVE' || snap.screen === 'ON'}),
        button('不共享了', () => actions.stopScreen(), {disabled: snap.screen !== 'ON'}),
        button('大屏聊天', () => store.set({bigchat: true}), {className: 'dsh-companion-primary'})),
      h('p', {className: 'dsh-companion-note'}, '收起只是缩小，结束才真的停；画面只在本地采样。'),
      h('p', {className: 'dsh-companion-status', role: 'status'}, state.status));
  };
}
