const TITLES = {
  PAUSED: '通话已暂停',
  ERROR: '连接失败',
  CONNECTING: '正在接通',
};

const ACTIVITY = {
  listening: '在听',
  thinking: '在想',
  speaking: '在说',
};

export function createCapsule({React, store, call, actions}) {
  const h = React.createElement;

  return function Capsule() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const snap = React.useSyncExternalStore(call.subscribe, call.getSnapshot);
    if (snap.phase === 'IDLE') return null;

    const title = TITLES[snap.phase] ?? (state.mode === 'voice' ? '语音陪伴' : '屏幕陪伴');
    const activity = snap.phase === 'ACTIVE' ? state.activity : 'idle';
    const time = `${Math.floor(state.elapsed / 60)}:${String(state.elapsed % 60).padStart(2, '0')}`;

    return h('div', {className: 'dsh-companion-capsule', role: 'region', 'aria-label': '通话胶囊'},
      h('i', {className: `dsh-companion-dot ${activity}`, 'aria-hidden': true}),
      h('strong', {title}, title),
      h('span', {className: 'dsh-companion-time'}, time),
      activity !== 'idle' && h('span', {className: 'dsh-companion-activity', role: 'status'}, ACTIVITY[activity] ?? ''),
      state.queued > 0 && h('span', {className: 'dsh-companion-queued', title: '还有话在排队'}, `+${state.queued}`),
      snap.screen === 'ON' && h('span', {title: '正在共享所选画面'}, '▣'),
      h('button', {
        type: 'button',
        title: '只关麦克风，不结束通话',
        onClick: () => actions.mute(!snap.inputMuted),
      }, snap.inputMuted ? '开麦' : '静音'),
      h('button', {type: 'button', className: 'end', title: '停止录音、播放和屏幕采集', onClick: () => actions.end()}, '结束'),
      h('button', {type: 'button', title: '展开通话面板', onClick: () => { call.expand(); store.set({panel: 'call'}); }}, '展开'),
    );
  };
}
