/** Composer entry: one button, menu opens above it, never a second composer. */
export function createTrigger({React, store, refs}) {
  const h = React.createElement;
  const WIDTH = 200;

  return function Trigger() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);

    function toggle() {
      // Desktop (Electron) must never replace its own window with the stage.
      if (/Electron/i.test(globalThis.navigator?.userAgent ?? '')) {
        store.set({menu: !store.get().menu});
        return;
      }
      store.set({bigchat: true, menu: false});
    }

    return h('button', {
      ref: (element) => { refs.trigger = element; },
      type: 'button',
      className: 'dsh-companion-trigger',
      'aria-label': '陪伴',
      'aria-expanded': state.menu,
      title: '语音、屏幕和陪伴设置',
      onClick: toggle,
    }, '✿ 陪伴');
  };
}
