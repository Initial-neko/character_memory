import {createLive2dView} from '../live2d-view.mjs';

const ACTIVITY_STATE = {listening: 'wait', thinking: 'think', speaking: 'speak', idle: 'idle'};
const STATE_LABEL = {idle: '空闲', wait: '在听', think: '在想', speak: '在说', pat: '被摸头', tired: '累了'};

/** Live2D stage rendered inside the expanded call panel. */
export function createStage({React, store, call}) {
  const h = React.createElement;
  return function Stage() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const snap = React.useSyncExternalStore(call.subscribe, call.getSnapshot);
    const host = React.useRef(null);
    const view = React.useRef(null);
    const [ready, setReady] = React.useState(false);
    const [fps, setFps] = React.useState(0);
    const [error, setError] = React.useState('');

    React.useEffect(() => {
      let alive = true;
      createLive2dView({container: host.current, onStatus: () => {}})
        .then((instance) => {
          if (!alive) { instance.destroy(); return; }
          view.current = instance;
          setReady(true);
        })
        .catch((failure) => { if (alive) setError(failure.message || String(failure)); });
      return () => {
        alive = false;
        view.current?.destroy();
        view.current = null;
      };
    }, []);

    const activity = state.activity || 'idle';
    React.useEffect(() => {
      const instance = view.current;
      if (!instance) return;
      instance.setState(snap.phase === 'ACTIVE' ? (ACTIVITY_STATE[activity] || 'idle') : 'idle');
    }, [activity, snap.phase, ready]);

    React.useEffect(() => {
      if (!ready) return undefined;
      const timer = setInterval(() => setFps(view.current?.runtime?.fps || 0), 1000);
      return () => clearInterval(timer);
    }, [ready]);

    const pat = () => view.current?.setState('pat');
    return h('div', {className: 'dsh-companion-stage'},
      h('div', {ref: host, className: 'dsh-companion-stage-host'}),
      !ready && !error && h('p', {className: 'dsh-companion-stage-note'}, '正在把她叫出来…'),
      error && h('p', {className: 'dsh-companion-stage-note'}, `形象没加载出来：${error}`),
      h('button', {
        type: 'button',
        className: 'dsh-companion-stage-badge',
        onClick: pat,
        title: '摸摸头',
      }, STATE_LABEL[activity] || '陪着'),
      ready && h('span', {className: 'dsh-companion-stage-fps'}, `${fps} fps`));
  };
}
