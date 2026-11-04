/** Composer entry: one button, menu opens above it, never a second composer. */
export function createTrigger({React, store, refs}) {
  const h = React.createElement;
  const WIDTH = 200;

  return function Trigger() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);

    function toggle() {
      if (state.menu) { store.set({menu: false}); return; }
      const rect = refs.trigger?.getBoundingClientRect();
      if (!rect) { store.set({menu: true, anchor: null}); return; }
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - WIDTH - 8));
      store.set({menu: true, anchor: {left, bottom: window.innerHeight - rect.top + 8}});
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
