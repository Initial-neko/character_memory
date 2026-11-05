import {CallController} from './call-controller.mjs';
import {ScreenCapture} from './screen-capture.mjs';
import {HostBridge} from './host-bridge.mjs';
import {createStore} from './ui/store.mjs';
import {css} from './ui/styles.mjs';
import {createRequest, Orchestrator} from './ui/orchestrator.mjs';
import {createTrigger} from './ui/trigger.mjs';
import {createCapsule} from './ui/capsule.mjs';
import {createPanel} from './ui/panel.mjs';
import {createStage} from './ui/stage.mjs';
import {createSettings} from './ui/settings.mjs';

/** Wires the host surfaces to one call controller, one store and one orchestrator. */
export function createComponents(React) {
  const h = React.createElement;
  const call = new CallController();
  const screen = new ScreenCapture({call, local: typeof globalThis.navigator?.mediaDevices?.getDisplayMedia === 'function' ? null : {url: 'http://127.0.0.1:50134'}});
  const store = createStore({
    menu: false,
    anchor: null,
    panel: null,
    config: null,
    watch: null,
    status: '',
    activity: 'idle',
    recording: false,
    recordingPending: false,
    transcribing: false,
    frame: null,
    preview: null,
    mode: 'voice',
    elapsed: 0,
    queued: 0,
  });
  const refs = {trigger: null, props: {}};
  const bridge = new HostBridge(() => refs.props);
  const request = createRequest();
  const orchestrator = new Orchestrator({store, request, call, screen, bridge});

  // Mirror the session state for the Live2D page (and anything else that wants it).
  let presenceTimer = null;
  let lastPresence = '';
  store.subscribe(() => {
    const snap = store.get();
    const payload = {activity: snap.activity, text: String(snap.status ?? '').slice(0, 200), mood: 0};
    const key = `${payload.activity}|${payload.text}`;
    if (key === lastPresence) return;
    lastPresence = key;
    clearTimeout(presenceTimer);
    presenceTimer = setTimeout(() => {
      fetch('/companion/api/state', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payload),
      }).catch(() => {});
    }, 150);
  });

  const actions = {
    begin: (mode) => orchestrator.begin(mode),
    end: () => orchestrator.end(),
    chooseScreen: () => orchestrator.chooseScreen(),
    captureFrame: () => orchestrator.captureFrame(),
    sendFrame: () => orchestrator.sendFrame(),
    stopScreen: () => orchestrator.stopScreen(),
    toggleWatch: () => orchestrator.toggleWatch(),
    loadConfig: () => orchestrator.loadConfig(),
    configure: (revision) => orchestrator.configure(revision),
    loadVoices: () => orchestrator.loadVoices(),
    previewVoice: (voice) => orchestrator.previewVoice(voice),
    mute: (muted) => orchestrator.muteInput(muted),
    reportError: (error) => store.set({status: error?.message ?? String(error)}),
    openLive2d: () => window.open('/companion/live2d/', '_blank', 'noopener'),
  };

  const Trigger = createTrigger({React, store, refs});
  const Capsule = createCapsule({React, store, call, actions});
  const Stage = createStage({React, store, call});
  const Panel = createPanel({React, store, call, actions, Stage});
  const Settings = createSettings({React, store, request, actions, call});

  function Menu() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    if (!state.menu) return null;
    const style = state.anchor
      ? {left: state.anchor.left, bottom: state.anchor.bottom}
      : {right: 30, bottom: 94};
    const item = (label, run) => h('button', {
      type: 'button',
      role: 'menuitem',
      onClick: () => { store.set({menu: false}); Promise.resolve().then(run).catch(actions.reportError); },
    }, label);
    return h('div', {className: 'dsh-companion-menu', role: 'menu', style},
      item('语音通话', () => actions.begin('voice')),
      item('视频 / 屏幕', () => actions.begin('screen')),
      item('设置', () => store.set({panel: 'settings'})));
  }

  function OverlaySettings() {
    return h('section', {className: 'dsh-companion-panel', role: 'dialog', 'aria-label': '陪伴设置'},
      h('header', null,
        h('h2', null, '陪伴设置'),
        h('button', {type: 'button', onClick: () => store.set({panel: null})}, '关闭')),
      h(Settings));
  }

  function Overlay(props) {
    const input = typeof props.useInput === 'function' ? props.useInput((value) => value) : props.input;
    refs.props = {...props, input};
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const snap = React.useSyncExternalStore(call.subscribe, call.getSnapshot);

    React.useEffect(() => { call.navigate(props.sessionId); }, [props.sessionId]);
    React.useEffect(() => {
      const onKey = (event) => {
        if (event.key !== 'Escape') return;
        store.set({menu: false});
        refs.trigger?.focus();
      };
      const onPointer = (event) => {
        if (!state.menu) return;
        if (event.target.closest?.('.dsh-companion-menu,.dsh-companion-trigger')) return;
        store.set({menu: false});
      };
      const onMove = () => { if (state.menu) store.set({menu: false}); };
      document.addEventListener('keydown', onKey);
      document.addEventListener('pointerdown', onPointer);
      window.addEventListener('resize', onMove);
      return () => {
        document.removeEventListener('keydown', onKey);
        document.removeEventListener('pointerdown', onPointer);
        window.removeEventListener('resize', onMove);
      };
    }, [state.menu]);

    const expanded = state.panel === 'call' && !snap.minimized && snap.phase !== 'IDLE';
    return h('div', {className: 'dsh-companion-root'},
      h('style', null, css),
      h(Menu),
      h(Capsule),
      state.panel === 'settings' && h(OverlaySettings),
      expanded && h(Panel));
  }

  return {
    Trigger,
    Overlay,
    Settings,
    dispose() {
      orchestrator.dispose();
      store.set({menu: false, panel: null, preview: null});
    },
  };
}
