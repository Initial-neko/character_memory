/** A small floating version of her, draggable, click to go back to the big screen. */
export function createPet({React, store, Stage}) {
  const h = React.createElement;
  return function Pet() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const host = React.useRef(null);
    const drag = React.useRef(null);

    if (!state.pet) return null;

    const open = () => store.set({pet: false, bigchat: true});
    const close = () => store.set({pet: false});

    const down = (event) => {
      const element = host.current;
      if (!element) return;
      const rect = element.getBoundingClientRect();
      drag.current = {startX: event.clientX, startY: event.clientY, left: rect.left, top: rect.top, moved: false};
      element.setPointerCapture?.(event.pointerId);
    };
    const move = (event) => {
      const current = drag.current;
      const element = host.current;
      if (!current || !element) return;
      const dx = event.clientX - current.startX;
      const dy = event.clientY - current.startY;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) current.moved = true;
      element.style.left = `${current.left + dx}px`;
      element.style.top = `${current.top + dy}px`;
      element.style.right = 'auto';
      element.style.bottom = 'auto';
    };
    const up = (event) => {
      const current = drag.current;
      drag.current = null;
      host.current?.releasePointerCapture?.(event.pointerId);
      if (current && !current.moved) open();
    };

    return h('div', {
      ref: host,
      className: 'dsh-companion-pet',
      title: '点我放大，拖我走',
      onPointerDown: down,
      onPointerMove: move,
      onPointerUp: up,
    },
      h(Stage),
      h('button', {
        type: 'button',
        className: 'dsh-companion-pet-close',
        onClick: (event) => { event.stopPropagation(); close(); },
        title: '收起来',
      }, '×'));
  };
}
